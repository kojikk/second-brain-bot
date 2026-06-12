"""Агентский луп: извлечение tool-call, idempotency-инъекция, confirm-пауза, thinking."""
import json

import pytest

import agent
from agent import Confirm, Final


def test_extract_tool_call_with_newlines():
    text = 'думаю...\n```tool\n{"tool": "search", "args": {"query": "x"}}\n```'
    tc = agent._extract_tool_call(text)
    assert tc["tool"] == "search"
    assert tc["args"]["query"] == "x"


def test_extract_none_when_no_block():
    assert agent._extract_tool_call("просто финальный ответ") is None


def test_inject_idempotency_only_for_write_tools():
    args = agent._inject_idempotency("create_note", {"path": "a.md"}, "tg:1:2")
    assert args["idempotency_key"].startswith("tg:1:2:")
    # read-инструменты не трогаем
    same = agent._inject_idempotency("search", {"query": "x"}, "tg:1:2")
    assert "idempotency_key" not in same


def test_inject_idempotency_deterministic():
    a = agent._inject_idempotency("create_note", {"path": "a.md"}, "k")
    b = agent._inject_idempotency("create_note", {"path": "a.md"}, "k")
    assert a["idempotency_key"] == b["idempotency_key"]


def test_inject_respects_existing_key():
    args = agent._inject_idempotency("create_note", {"path": "a.md", "idempotency_key": "mine"}, "k")
    assert args["idempotency_key"] == "mine"


def test_tools_description_marks_required():
    tools = [{
        "name": "create_note",
        "description": "make note",
        "inputSchema": {"properties": {"path": {}, "content": {}}, "required": ["path"]},
    }]
    desc = agent._tools_description(tools)
    assert "path*" in desc and "content?" in desc


# --- _extract_thinking ---

def test_extract_thinking_none():
    thinking, clean = agent._extract_thinking("просто текст без блоков")
    assert thinking == ""
    assert clean == "просто текст без блоков"


def test_extract_thinking_present():
    raw = "<thinking>я думаю над этим</thinking>\nФинальный ответ."
    thinking, clean = agent._extract_thinking(raw)
    assert thinking == "я думаю над этим"
    assert clean == "Финальный ответ."


def test_extract_thinking_multiblock():
    raw = "<thinking>часть 1</thinking>\n```tool\n{}\n```\n<thinking>часть 2</thinking>\nОтвет."
    thinking, clean = agent._extract_thinking(raw)
    assert "часть 1" in thinking and "часть 2" in thinking
    assert "<thinking>" not in clean


def test_extract_thinking_multiline():
    raw = "<thinking>\nстрока 1\nстрока 2\n</thinking>\nОтвет"
    thinking, clean = agent._extract_thinking(raw)
    assert "строка 1" in thinking
    assert clean == "Ответ"


# --- decide_model: Sonnet 4.6 база, thinking для тяжёлого ---

def test_decide_model_default_is_base():
    assert agent.decide_model("запиши мысль про кофе") == agent.CLAUDE_MODEL


def test_decide_model_triggers_thinking():
    assert agent.decide_model("проанализируй мой план") == agent.CLAUDE_THINKING_MODEL


def test_decide_model_force_thinking():
    assert agent.decide_model("привет", force_thinking=True) == agent.CLAUDE_THINKING_MODEL


def test_graph_upsert_gets_idempotency():
    args = agent._inject_idempotency("graph_upsert", {"edges": []}, "tg:1:2")
    assert args["idempotency_key"].startswith("tg:1:2:")


@pytest.mark.asyncio
async def test_build_seed_hides_graph_export():
    tools = [
        {"name": "search", "description": "", "inputSchema": {}},
        {"name": "graph_export", "description": "", "inputSchema": {}},
    ]
    seed = await agent.build_seed("мозг", "2026-06-12", tools, "привет")
    assert "graph_export" not in seed[0]["content"]
    assert "search" in seed[0]["content"]


@pytest.mark.asyncio
async def test_complete_reads_reasoning_content(monkeypatch):
    """apinet кладёт extended thinking в message.reasoning_content — не теряем его."""
    from types import SimpleNamespace

    fake_resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="Париж.", reasoning_content="думаю про столицы\n"))])

    class _FakeCompletions:
        async def create(self, **kw):
            return fake_resp

    monkeypatch.setattr(agent, "_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions())))
    monkeypatch.setattr(agent.usage_tracker, "record_completion",
                        lambda *a, **kw: None)

    thinking, clean = await agent._complete(
        [{"role": "user", "content": "столица Франции?"}],
        "claude-sonnet-4-6-thinking", "capture")
    assert thinking == "думаю про столицы"
    assert clean == "Париж."


class _FakeMCP:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if args.get("confirm") is False:
            return "DRY-RUN (soft_delete). Nothing changed."
        if args.get("confirm") is True:
            return "moved to .trash"
        return "ok"


@pytest.mark.asyncio
async def test_loop_final_answer(monkeypatch):
    async def fake_complete(messages, model, request_type):
        return "", "Готово, разложил."
    monkeypatch.setattr(agent, "_complete", fake_complete)

    out = await agent.run_loop(_FakeMCP(), [{"role": "user", "content": "hi"}],
                               "m", "k", "capture")
    assert isinstance(out, Final)
    assert "Готово" in out.text
    assert out.thinking == ""


@pytest.mark.asyncio
async def test_loop_thinking_forwarded_to_final(monkeypatch):
    """thinking из финального шага пробрасывается в Final.thinking."""
    async def fake_complete(messages, model, request_type):
        return "рассуждение модели", "Финальный ответ пользователю."
    monkeypatch.setattr(agent, "_complete", fake_complete)

    out = await agent.run_loop(_FakeMCP(), [{"role": "user", "content": "hi"}],
                               "m", "k", "capture")
    assert isinstance(out, Final)
    assert out.thinking == "рассуждение модели"
    assert "Финальный ответ" in out.text


@pytest.mark.asyncio
async def test_loop_thinking_discarded_for_tool_steps(monkeypatch):
    """thinking промежуточных шагов не попадает в финальный Final."""
    step = [0]

    async def fake_complete(messages, model, request_type):
        step[0] += 1
        if step[0] == 1:
            # Промежуточный шаг: есть thinking + tool-блок
            return "промежуточное размышление", '```tool\n{"tool": "search", "args": {"query": "x"}}\n```'
        # Финальный шаг: другое thinking + текст
        return "финальное размышление", "Вот результат."
    monkeypatch.setattr(agent, "_complete", fake_complete)

    out = await agent.run_loop(_FakeMCP(), [{"role": "user", "content": "найди x"}],
                               "m", "k", "capture")
    assert isinstance(out, Final)
    assert out.thinking == "финальное размышление"
    assert "промежуточное" not in out.thinking


@pytest.mark.asyncio
async def test_loop_pauses_on_structural_confirm(monkeypatch):
    replies = iter([
        ("", '```tool\n{"tool": "soft_delete", "args": {"path": "x.md", "confirm": true}}\n```'),
    ])

    async def fake_complete(messages, model, request_type):
        return next(replies)
    monkeypatch.setattr(agent, "_complete", fake_complete)

    mcp = _FakeMCP()
    out = await agent.run_loop(mcp, [{"role": "user", "content": "удали x"}],
                               "m", "k", "capture")
    assert isinstance(out, Confirm)
    assert "DRY-RUN" in out.plan_text
    assert out.pending_tool["tool"] == "soft_delete"
    # дешёвая защита: применения (confirm=true) на паузе ещё НЕ было
    assert all(a.get("confirm") is not True for _, a in mcp.calls)


@pytest.mark.asyncio
async def test_resume_applies_when_approved(monkeypatch):
    async def fake_complete(messages, model, request_type):
        return "", "Удалил, перенёс в .trash."
    monkeypatch.setattr(agent, "_complete", fake_complete)

    mcp = _FakeMCP()
    out = await agent.resume_after_confirm(
        mcp, [{"role": "user", "content": "удали x"}],
        {"tool": "soft_delete", "args": {"path": "x.md", "confirm": True}},
        approved=True, model="m", item_key="k", request_type="capture",
    )
    assert isinstance(out, Final)
    assert any(a.get("confirm") is True for _, a in mcp.calls)


@pytest.mark.asyncio
async def test_resume_skips_when_declined(monkeypatch):
    async def fake_complete(messages, model, request_type):
        return "", "Окей, отменил."
    monkeypatch.setattr(agent, "_complete", fake_complete)

    mcp = _FakeMCP()
    out = await agent.resume_after_confirm(
        mcp, [{"role": "user", "content": "удали x"}],
        {"tool": "soft_delete", "args": {"path": "x.md", "confirm": True}},
        approved=False, model="m", item_key="k", request_type="capture",
    )
    assert isinstance(out, Final)
    # при отказе структурный инструмент НЕ вызывался вовсе
    assert mcp.calls == []


@pytest.mark.asyncio
async def test_loop_nudges_when_no_vault_consult(monkeypatch):
    """Финал без единого инструмента → один пуш «сверься с вольтом» → инструмент → финал."""
    step = [0]

    async def fake_complete(messages, model, request_type):
        step[0] += 1
        if step[0] == 1:
            return "", "Отвечаю из головы, не глядя в вольт."
        if step[0] == 2:
            # после пуша модель одумалась и пошла в вольт
            assert any(str(m.get("content", "")).startswith("[vault-check]")
                       for m in messages)
            return "", '```tool\n{"tool": "graph_query", "args": {"question": "x"}}\n```'
        return "", "Теперь отвечаю по вольту."
    monkeypatch.setattr(agent, "_complete", fake_complete)

    mcp = _FakeMCP()
    out = await agent.run_loop(mcp, [{"role": "user", "content": "что у меня по X?"}],
                               "m", "k", "capture")
    assert isinstance(out, Final)
    assert "по вольту" in out.text
    assert ("graph_query", {"question": "x"}) in [(n, a) for n, a in mcp.calls]


@pytest.mark.asyncio
async def test_loop_nudges_only_once(monkeypatch):
    """Если модель упорно финалит без инструментов — второй финал отдаём как есть."""
    async def fake_complete(messages, model, request_type):
        return "", "Упорно отвечаю без вольта."
    monkeypatch.setattr(agent, "_complete", fake_complete)

    out = await agent.run_loop(_FakeMCP(), [{"role": "user", "content": "hi"}],
                               "m", "k", "capture")
    assert isinstance(out, Final)
    assert "Упорно" in out.text


@pytest.mark.asyncio
async def test_loop_graceful_summary_on_exhaustion(monkeypatch):
    """Упор в лимит шагов → не пустая отписка, а итоговый текст от модели."""
    monkeypatch.setattr(agent, "MAX_STEPS", 3)

    async def fake_complete(messages, model, request_type):
        # Финальная суммаризация (после исчерпания) — без tool-блока.
        if any("Достигнут лимит шагов" in str(m.get("content", "")) for m in messages):
            return "", "Разложил часть сырья, осталось починить ссылки."
        # Иначе бесконечно зовём инструмент, чтобы выработать бюджет шагов.
        return "", '```tool\n{"tool": "search", "args": {"query": "x"}}\n```'
    monkeypatch.setattr(agent, "_complete", fake_complete)

    out = await agent.run_loop(_FakeMCP(), [{"role": "user", "content": "наведи порядок"}],
                               "m", "k", "capture")
    assert isinstance(out, Final)
    assert "осталось починить ссылки" in out.text
