"""
Telegram I/O второго-мозг-бота.

Ответственность тонкая: owner-allowlist, быстрый ack, ПЕРСИСТ в durable-очередь
(до обработки — чтобы ничего не потерялось), и доставка решений confirm-кнопок.
Весь «разум» — в worker.py + agent.py + brain (agent.md). Бот не лезет в вольт
напрямую, не хранит знания и не реализует правила раскладки.

Исключение из «тонкости»: голосовые. Их сначала надо распознать в текст
(transcribe.py), и только распознанный текст идёт в durable-очередь — схема
очереди текстовая, бинарь в неё не кладётся.
"""
import asyncio
import logging
import uuid

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

import config
import queue_db as q
import transcribe
import usage_tracker
from brain import Brain
from mcp_client import MCPClient
from tg_render import to_telegram_html
from worker import run_worker

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Пользователи, запросившие Sonnet для следующего сообщения.
_sonnet_next: set[int] = set()

BOT_COMMANDS = [
    BotCommand("status", "📊 Состояние очереди"),
    BotCommand("usage", "💰 Расход токенов API"),
    BotCommand("sonnet", "✨ Следующее сообщение через Sonnet"),
    BotCommand("lint", "🧹 Аудит и обновление вольта (Sonnet)"),
    BotCommand("help", "❔ Справка"),
]


def _allowed(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return bool(config.TELEGRAM_ALLOWED_USER_ID) and uid == config.TELEGRAM_ALLOWED_USER_ID


def auth(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _allowed(update):
            if update.message:
                await update.message.reply_text("🚫 Нет доступа.")
            return
        return await func(update, ctx)
    return wrapper


# ─── Команды ──────────────────────────────────────────────────────────────────

@auth
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧠 <b>Второй мозг</b>\n"
        "<i>Пиши поток мыслей или шли голосовое — сохраню и разложу по вольту.</i>\n"
        f"{'─' * 12}\n"
        "▸ ничего не теряется (durable-очередь)\n"
        "▸ голосовые распознаю в текст\n"
        "▸ структурные правки — с подтверждением\n\n"
        "/status · /usage · /sonnet · /help",
        parse_mode=ParseMode.HTML,
    )


@auth
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❔ <b>Как пользоваться</b>\n"
        f"{'─' * 12}\n"
        "Пиши текстом или шли голосовое — я кладу сообщение в надёжную очередь\n"
        "и разбираю его агентом поверх твоего вольта (через Vault MCP).\n\n"
        "<b>Команды</b>\n"
        "/status — что в очереди\n"
        "/usage — расход токенов и стоимость\n"
        "/sonnet [текст] — умная модель для следующего сообщения\n"
        "/lint — принудительный аудит и обновление вольта (Sonnet)",
        parse_mode=ParseMode.HTML,
    )


@auth
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = q.counts()
    mcp: MCPClient = ctx.application.bot_data["mcp"]
    healthy = await mcp.healthy()
    lines = ["📊 **Очередь**", "---"]
    if c:
        for st in ("pending", "processing", "awaiting_confirm", "resume", "done", "error"):
            if st in c:
                lines.append(f"▸ {st}: {c[st]}")
    else:
        lines.append("▸ пусто")
    lines.append(f"\n◆ Vault MCP: {'✓ доступен' if healthy else '✗ недоступен (Desktop офлайн)'}")
    await update.message.reply_text(
        to_telegram_html("\n".join(lines)), parse_mode=ParseMode.HTML,
    )


@auth
async def cmd_usage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        to_telegram_html(usage_tracker.format_usage_report()),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


@auth
async def cmd_sonnet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args_text = " ".join(ctx.args) if ctx.args else ""
    if args_text:
        await _ingest(update, args_text, force_sonnet=True)
    else:
        _sonnet_next.add(update.effective_user.id)
        await update.message.reply_text("✨ Следующее сообщение обработаю через Sonnet.")


# ─── Приём сообщений (ingest → durable-очередь) ───────────────────────────────

LINT_PROMPT = (
    "Прогони аудит вольта и наведи порядок. Шаги: вызови инструмент lint; "
    "разбери отчёт (орфаны, битые [[ссылки]], устаревшие сущности, "
    "необработанное сырьё в _raw/, открытые противоречия); затем безопасно "
    "исправь — разложи накопившееся сырьё, обнови _index.md и _hot.md, почини "
    "ссылки. Деструктивные и структурные операции — только через подтверждение. "
    "В конце дай краткий отчёт: что нашёл и что сделал."
)


@auth
async def cmd_lint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Принудительный аудит и обновление вольта (всегда через Sonnet)."""
    await _ingest(update, LINT_PROMPT, force_sonnet=True)


async def _ingest(update: Update, text: str, force_sonnet: bool) -> None:
    """Персист в очередь ДО обработки + быстрый ack. Источник правды — очередь."""
    key = f"tg:{update.effective_chat.id}:{update.message.message_id}"
    _, created = q.enqueue(
        key, update.effective_chat.id, update.effective_user.id, text, force_sonnet,
    )
    if created:
        ack = "✨ Принял (Sonnet), думаю…" if force_sonnet else "📥 Принял, разбираю…"
    else:
        ack = "↻ Это сообщение уже в работе."
    await update.message.reply_text(ack)


@auth
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return
    uid = update.effective_user.id
    force_sonnet = uid in _sonnet_next
    _sonnet_next.discard(uid)
    await _ingest(update, text, force_sonnet=force_sonnet)


@auth
async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Голосовое/аудио → транскрипция → durable-очередь как обычный текст.

    Распознаём ДО постановки в очередь (схема очереди текстовая, бинарь не
    кладётся). При ошибке распознавания ничего не теряется молча — честно
    сообщаем; пользователь может переслать текстом.
    """
    msg = update.message
    media = msg.voice or msg.audio
    if not media:
        return
    await msg.reply_text("🎤 Распознаю голосовое…")
    try:
        tg_file = await ctx.bot.get_file(media.file_id)
        audio = bytes(await tg_file.download_as_bytearray())
        ext = (media.mime_type or "audio/ogg").split("/")[-1].split(";")[0] or "ogg"
        text = await transcribe.transcribe(audio, filename=f"voice.{ext}")
    except Exception as e:
        logger.exception("транскрипция голосового упала")
        await msg.reply_text(f"❌ Не смог распознать голосовое: {e}")
        return
    if not text:
        await msg.reply_text("🤷 Не разобрал речь в голосовом — попробуй текстом.")
        return
    await msg.reply_text(f"🎤 Распознал: «{text}»")
    uid = update.effective_user.id
    force_sonnet = uid in _sonnet_next
    _sonnet_next.discard(uid)
    await _ingest(update, text, force_sonnet=force_sonnet)


# ─── Колбэки кнопок подтверждения ─────────────────────────────────────────────

@auth
async def handle_attachment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Вложения (кроме голосовых) пока не обрабатываются в контексте модели (p4).

    Бинарные оригиналы (PDF/картинки) в вольт не пушатся: add_raw — только
    текст, а оригиналы кладутся в _attachments/ через ФС на Desktop."""
    await update.message.reply_text(
        "📎 Пока я работаю с текстом и голосовыми.\n"
        "Пришли мысль или заметку текстом (или голосовым) — разложу по вольту.\n"
        "Файл-оригинал (PDF, картинку) положи в _attachments/ вольта через Desktop — "
        "приём файлов в обработку появится позже."
    )


async def on_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if not _allowed(update):
        return
    data = cq.data or ""
    try:
        action, sid = data.split(":", 1)
        item_id = int(sid)
    except ValueError:
        return
    decision = "yes" if action == "cy" else "no"
    ok = q.set_decision(item_id, decision)
    label = "✅ Подтверждено — выполняю…" if decision == "yes" else "✖️ Отменено."
    if not ok:
        label = "↻ Уже обработано."
    try:
        await cq.edit_message_reply_markup(reply_markup=None)
        await cq.edit_message_text(cq.message.text_html + f"\n\n<i>{label}</i>",
                                   parse_mode=ParseMode.HTML)
    except Exception:
        await cq.message.reply_text(label)


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    usage_tracker.init_db()
    q.init_db()
    mcp = MCPClient(config.MCP_URL, config.MCP_TOKEN, config.MCP_TIMEOUT_SEC)
    brain = Brain(mcp)
    app.bot_data["mcp"] = mcp
    app.bot_data["brain"] = brain
    await app.bot.set_my_commands(BOT_COMMANDS)
    # Воркер живёт в том же event-loop, что и поллинг PTB.
    app.bot_data["worker_task"] = asyncio.create_task(run_worker(app.bot, mcp, brain))
    logger.info("worker запущен")


async def _post_shutdown(app: Application) -> None:
    task = app.bot_data.get("worker_task")
    if task:
        task.cancel()


def run() -> None:
    config.validate()
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("sonnet", cmd_sonnet))
    app.add_handler(CommandHandler("lint", cmd_lint))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^c[yn]:\d+$"))
    # Голосовые/аудио — ДО обработчика вложений (иначе уйдут в «работаю с текстом»).
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(
        filters.ATTACHMENT & ~filters.VOICE & ~filters.AUDIO & ~filters.COMMAND,
        handle_attachment,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Второй-мозг-бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run()
