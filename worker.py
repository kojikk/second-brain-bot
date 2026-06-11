"""
Асинхронный воркер — сердце надёжности.

Тянет элементы durable-очереди и прогоняет агентский луп. Живёт в том же
event-loop, что и Telegram-бот (PTB), поэтому может слать сообщения напрямую.

Гарантии:
  * at-least-once: элемент помечается done только после успешного финала;
  * толерантность к офлайну Desktop: MCPUnavailable → requeue с экспоненциальным
    бэкоффом; пользователю один раз сообщаем «сохранено, разложу позже»;
  * human-in-the-loop: Confirm-исход → элемент в awaiting_confirm + план с
    кнопками в Telegram; продолжение — при статусе resume.
"""
import asyncio
import html as html_module
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

import agent
import queue_db as q
from agent import Confirm, Final
from brain import Brain
from config import (
    MCP_RETRY_BASE_SEC, MCP_RETRY_MAX_SEC, TIMEZONE, WORKER_POLL_SEC,
)
from mcp_client import MCPClient, MCPUnavailable
from tg_render import to_telegram_html

logger = logging.getLogger(__name__)

_THINKING_MAX_LEN = 3000


def _today() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()


def _backoff(attempts: int) -> float:
    return min(MCP_RETRY_BASE_SEC * (2 ** max(0, attempts - 1)), MCP_RETRY_MAX_SEC)


def _user_error_message(err: str) -> str:
    """Перевести исключение в дружелюбный текст для пользователя.

    Частый кейс — apinet вернул 402 «Insufficient USD balance»: это не баг, а
    пустой баланс. Не пугаем сырым JSON, а подсказываем пополнить.
    """
    low = err.lower()
    if "insufficient" in low and "balance" in low or "402" in err:
        return ("На балансе apinet закончились средства — пополни, "
                "и я продолжу обработку.")
    return f"Не получилось обработать: {err}"


def _build_message(thinking: str, md: str) -> str:
    """Собрать HTML-сообщение: expandable blockquote с thinking (если есть) + ответ."""
    answer_html = to_telegram_html(md)
    if not thinking:
        return answer_html
    if len(thinking) > _THINKING_MAX_LEN:
        thinking = thinking[:_THINKING_MAX_LEN] + "\n…[обрезано]"
    thinking_html = f"<blockquote expandable><i>Размышление</i>\n\n{html_module.escape(thinking)}</blockquote>\n\n"
    return thinking_html + answer_html


async def _send(bot, chat_id: int, md: str, thinking: str = "") -> None:
    """Отправить ответ агента как Telegram-HTML (с фолбэком в plain).

    Если передан thinking — вставляет expandable blockquote перед ответом
    в том же сообщении.
    """
    html = _build_message(thinking, md)
    try:
        await bot.send_message(
            chat_id, html,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("send HTML failed, plain fallback: %s", e)
        try:
            await bot.send_message(chat_id, md, disable_web_page_preview=True)
        except Exception as e2:
            logger.error("send failed entirely: %s", e2)


def _confirm_kb(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Подтвердить", callback_data=f"cy:{item_id}"),
        InlineKeyboardButton("Отмена", callback_data=f"cn:{item_id}"),
    ]])


async def _handle_outcome(bot, item, outcome) -> None:
    chat_id = item["chat_id"]
    if isinstance(outcome, Final):
        await _send(bot, chat_id, outcome.text, thinking=outcome.thinking)
        q.mark_done(item["id"])
    elif isinstance(outcome, Confirm):
        q.suspend_for_confirm(
            item["id"],
            json.dumps(outcome.messages, ensure_ascii=False),
            json.dumps(outcome.pending_tool, ensure_ascii=False),
        )
        plan = to_telegram_html(
            f"**Нужно подтверждение**\n\n{outcome.plan_text}"
        )
        await bot.send_message(
            chat_id, plan, parse_mode=ParseMode.HTML,
            reply_markup=_confirm_kb(item["id"]), disable_web_page_preview=True,
        )


async def _process(bot, mcp: MCPClient, brain: Brain, item, is_resume: bool) -> None:
    model = agent.decide_model(item["text"], bool(item["force_sonnet"]))
    item_key = item["idempotency_key"]

    if is_resume:
        messages = json.loads(item["messages"])
        pending = json.loads(item["pending_tool"])
        approved = item["confirm_decision"] == "yes"
        outcome = await agent.resume_after_confirm(
            mcp, messages, pending, approved, model, item_key, "capture",
        )
    else:
        brain_text = await brain.get()           # может бросить MCPUnavailable
        tools = await mcp.list_tools()           # ← до любых записей: безопасно реквью
        seed = await agent.build_seed(brain_text, _today(), tools, item["text"])
        outcome = await agent.run_loop(mcp, seed, model, item_key, "capture")

    await _handle_outcome(bot, item, outcome)


async def run_worker(bot, mcp: MCPClient, brain: Brain) -> None:
    """Бесконечный цикл обработки очереди."""
    recovered = q.recover_processing()
    if recovered:
        logger.info("crash-recovery: возобновлено %d зависших элементов", recovered)

    while True:
        item = q.claim_resume()
        is_resume = item is not None
        if item is None:
            item = q.claim_next()
        if item is None:
            await asyncio.sleep(WORKER_POLL_SEC)
            continue

        try:
            await _process(bot, mcp, brain, item, is_resume)
        except MCPUnavailable as e:
            attempts = q.requeue(item["id"], str(e))
            logger.warning("MCP недоступен (попытка %d): %s", attempts, e)
            if not item["offline_notified"]:
                await _send(
                    bot, item["chat_id"],
                    "Сохранил. Разложу, когда вернётся доступ к вольту "
                    "(Desktop сейчас недоступен).",
                )
                q.mark_offline_notified(item["id"])
            await asyncio.sleep(_backoff(attempts))
        except Exception as e:
            logger.exception("обработка элемента %s упала", item["id"])
            q.mark_error(item["id"], str(e))
            await _send(bot, item["chat_id"], _user_error_message(str(e)))
