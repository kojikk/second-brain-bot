"""Агентский луп: извлечение tool-call, idempotency-инъекция, confirm-пауза."""
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
        return "Готово, разложил."
    monkeypatch.setattr(agent, "_complete", fake_complete)

    out = await agent.run_loop(_FakeMCP(), [{"role": "user", "content": "hi"}],
                               "m", "k", "capture")
    assert isinstance(out, Final)
    assert "Готово" in out.text


@pytest.mark.asyncio
async def test_loop_pauses_on_structural_confirm(monkeypatch):
    replies = iter([
        '```tool\n{"tool": "soft_delete", "args": {"path": "x.md", "confirm": true}}\n```',
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
        return "Удалил, перенёс в .trash."
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
        return "Окей, отменил."
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
async def test_loop_graceful_summary_on_exhaustion(monkeypatch):
    """Упор в лимит шагов → не пустая отписка, а итоговый текст от модели."""
    monkeypatch.setattr(agent, "MAX_STEPS", 3)

    async def fake_complete(messages, model, request_type):
        # Финальная суммаризация (после исчерпания) — без tool-блока.
        if any("Достигнут лимит шагов" in str(m.get("content", "")) for m in messages):
            return "Разложил часть сырья, осталось починить ссылки."
        # Иначе бесконечно зовём инструмент, чтобы выработать бюджет шагов.
        return '```tool\n{"tool": "search", "args": {"query": "x"}}\n```'
    monkeypatch.setattr(agent, "_complete", fake_complete)

    out = await agent.run_loop(_FakeMCP(), [{"role": "user", "content": "наведи порядок"}],
                               "m", "k", "capture")
    assert isinstance(out, Final)
    assert "осталось починить ссылки" in out.text
