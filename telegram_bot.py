"""
Telegram I/O второго-мозг-бота.

Ответственность тонкая: owner-allowlist, быстрый ack, ПЕРСИСТ в durable-очередь
(до обработки — чтобы ничего не потерялось), и доставка решений confirm-кнопок.
Весь «разум» — в worker.py + agent.py + brain (agent.md). Бот не лезет в вольт
напрямую, не хранит знания и не реализует правила раскладки.

Исключения из «тонкости»:
  * голосовые — сначала распознаются в текст (transcribe.py), в очередь идёт
    уже текст (схема очереди текстовая, бинарь в неё не кладётся);
  * /graph — механический срез графа (graph_export) без участия агента:
    бот снимает снапшот и отдаёт кнопку Mini App (graph_app.py).
"""
import asyncio
import logging

from telegram import (
    Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)

import config
import graph_app
import queue_db as q
import transcribe
import usage_tracker
from brain import Brain
from mcp_client import MCPClient, MCPError
from tg_render import to_telegram_html
from worker import run_worker

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Пользователи, запросившие thinking-режим для следующего сообщения.
_think_next: set[int] = set()

BOT_COMMANDS = [
    BotCommand("graph", "Интерактивный граф вольта"),
    BotCommand("think", "Глубокий режим для следующего сообщения"),
    BotCommand("status", "Очередь и связь с вольтом"),
    BotCommand("usage", "Расход токенов"),
    BotCommand("lint", "Аудит и уборка вольта"),
    BotCommand("help", "Как это работает"),
]

_STATUS_RU = {
    "pending": "в ожидании",
    "processing": "в работе",
    "awaiting_confirm": "ждут подтверждения",
    "resume": "возобновляются",
    "done": "готово",
    "error": "с ошибкой",
}


def _allowed(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return bool(config.TELEGRAM_ALLOWED_USER_ID) and uid == config.TELEGRAM_ALLOWED_USER_ID


def auth(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _allowed(update):
            if update.message:
                await update.message.reply_text("Нет доступа.")
            return
        return await func(update, ctx)
    return wrapper


# ─── Команды ──────────────────────────────────────────────────────────────────

@auth
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Второй мозг</b>\n"
        "Пиши мысль текстом или голосом — сохраню в вольт и разложу по местам.\n\n"
        "<blockquote>Каждое сообщение попадает в надёжную очередь и обрабатывается "
        "агентом поверх Obsidian-вольта: ничего не теряется, структурные правки — "
        "только с твоего подтверждения.</blockquote>\n\n"
        "/graph — живой граф знаний\n"
        "/think — глубокий режим для сложного вопроса\n"
        "/help — подробнее",
        parse_mode=ParseMode.HTML,
    )


@auth
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Как это работает</b>\n"
        "Сообщение (текст или распознанное голосовое) попадает в durable-очередь. "
        "Агент сверяется с вольтом (граф, поиск, горячий контекст), раскладывает "
        "знание и отвечает. Сложные запросы автоматически идут через расширенное "
        "мышление.\n\n"
        "<b>Команды</b>\n"
        "/graph — интерактивный граф вольта: связи, новые и изменённые узлы\n"
        "/think — следующее сообщение через глубокий режим\n"
        "/think текст — обработать текст в глубоком режиме сразу\n"
        "/lint — аудит вольта и уборка\n"
        "/status — очередь и доступность вольта\n"
        "/usage — расход токенов и стоимость\n\n"
        "<b>Подтверждения</b>\n"
        "Перемещения, повышения и удаления агент применяет только после твоего "
        "«Подтвердить» — до этого только план.",
        parse_mode=ParseMode.HTML,
    )


@auth
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    c = q.counts()
    mcp: MCPClient = ctx.application.bot_data["mcp"]
    healthy = await mcp.healthy()
    lines = ["<b>Очередь</b>"]
    shown = [(st, c[st]) for st in
             ("pending", "processing", "awaiting_confirm", "resume", "done", "error")
             if c.get(st)]
    if shown:
        lines += [f"{_STATUS_RU[st]} — {n}" for st, n in shown]
    else:
        lines.append("пусто")
    lines.append("")
    lines.append(f"Вольт: {'на связи' if healthy else 'недоступен (Desktop офлайн)'}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@auth
async def cmd_usage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        to_telegram_html(usage_tracker.format_usage_report()),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


@auth
async def cmd_think(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args_text = " ".join(ctx.args) if ctx.args else ""
    if args_text:
        await _ingest(update, args_text, force_thinking=True)
    else:
        _think_next.add(update.effective_user.id)
        await update.message.reply_text(
            "Следующее сообщение обработаю в глубоком режиме."
        )


@auth
async def cmd_graph(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Срез графа + кнопка Mini App. Механика, без участия агента."""
    if not config.GRAPH_PUBLIC_URL:
        await update.message.reply_text(
            "Просмотрщик графа не настроен: задай GRAPH_PUBLIC_URL."
        )
        return
    mcp: MCPClient = ctx.application.bot_data["mcp"]
    msg = await update.message.reply_text("Снимаю срез графа…")
    try:
        snap = await graph_app.take_snapshot(mcp)
    except (MCPError, ValueError) as e:
        logger.warning("graph snapshot failed: %s", e)
        await msg.edit_text("Вольт сейчас недоступен — попробуй, когда Desktop проснётся.")
        return

    stats = snap["graph"]["stats"]
    diff = snap["diff"]
    lines = [
        "<b>Граф вольта</b>",
        f"{stats['nodes']} узлов · {stats['edges']} связей",
    ]
    fresh = []
    if diff["new_nodes"]:
        fresh.append(f"новых узлов: {len(diff['new_nodes'])}")
    if diff["touched_nodes"]:
        fresh.append(f"изменённых: {len(diff['touched_nodes'])}")
    if diff["new_edges"]:
        fresh.append(f"новых связей: {len(diff['new_edges'])}")
    lines.append("с прошлого среза: " + (", ".join(fresh) if fresh else "без изменений"))

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Открыть граф", web_app=WebAppInfo(url=config.GRAPH_PUBLIC_URL)),
    ]])
    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb)


# ─── Приём сообщений (ingest → durable-очередь) ───────────────────────────────

LINT_PROMPT = (
    "Прогони аудит вольта и наведи порядок. Шаги: вызови инструмент lint; "
    "разбери отчёт (орфаны, битые [[ссылки]], устаревшие сущности, "
    "необработанное сырьё в _raw/, открытые противоречия, непокрытые "
    "semantic-рёбрами entity-страницы); затем безопасно исправь — разложи "
    "накопившееся сырьё, обнови _index.md и _hot.md, почини ссылки, докинь "
    "недостающие связи через graph_upsert. Деструктивные и структурные операции "
    "— только через подтверждение. В конце дай краткий отчёт: что нашёл и что сделал."
)


@auth
async def cmd_lint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Принудительный аудит и обновление вольта (всегда в глубоком режиме)."""
    await _ingest(update, LINT_PROMPT, force_thinking=True)


async def _ingest(update: Update, text: str, force_thinking: bool) -> None:
    """Персист в очередь ДО обработки + быстрый ack. Источник правды — очередь."""
    key = f"tg:{update.effective_chat.id}:{update.message.message_id}"
    _, created = q.enqueue(
        key, update.effective_chat.id, update.effective_user.id, text, force_thinking,
    )
    if created:
        ack = "Принял — думаю глубоко…" if force_thinking else "Принял, разбираю…"
    else:
        ack = "Это сообщение уже в работе."
    await update.message.reply_text(ack)


@auth
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return
    uid = update.effective_user.id
    force_thinking = uid in _think_next
    _think_next.discard(uid)
    await _ingest(update, text, force_thinking=force_thinking)


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
    await msg.reply_text("Распознаю голосовое…")
    try:
        tg_file = await ctx.bot.get_file(media.file_id)
        audio = bytes(await tg_file.download_as_bytearray())
        ext = (media.mime_type or "audio/ogg").split("/")[-1].split(";")[0] or "ogg"
        text = await transcribe.transcribe(audio, filename=f"voice.{ext}")
    except Exception as e:
        logger.exception("транскрипция голосового упала")
        await msg.reply_text(f"Не смог распознать голосовое: {e}")
        return
    if not text:
        await msg.reply_text("Не разобрал речь в голосовом — попробуй текстом.")
        return
    await msg.reply_text(f"Распознал: «{text}»")
    uid = update.effective_user.id
    force_thinking = uid in _think_next
    _think_next.discard(uid)
    await _ingest(update, text, force_thinking=force_thinking)


@auth
async def handle_attachment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Вложения (кроме голосовых) пока не обрабатываются в контексте модели (p4).

    Бинарные оригиналы (PDF/картинки) в вольт не пушатся: add_raw — только
    текст, а оригиналы кладутся в _attachments/ через ФС на Desktop."""
    await update.message.reply_text(
        "Пока работаю с текстом и голосовыми.\n"
        "Пришли мысль текстом или голосом — разложу по вольту. Файл-оригинал "
        "(PDF, картинку) положи в _attachments/ вольта через Desktop."
    )


# ─── Колбэки кнопок подтверждения ─────────────────────────────────────────────

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
    label = "Подтверждено, выполняю…" if decision == "yes" else "Отменено."
    if not ok:
        label = "Уже обработано."
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
    # HTTP-сервер Mini App «Граф» — тоже в этом loop'е.
    app.bot_data["graph_runner"] = await graph_app.start(mcp)
    logger.info("worker и graph mini app запущены")


async def _post_shutdown(app: Application) -> None:
    task = app.bot_data.get("worker_task")
    if task:
        task.cancel()
    runner = app.bot_data.get("graph_runner")
    if runner:
        await runner.cleanup()


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
    app.add_handler(CommandHandler("think", cmd_think))
    app.add_handler(CommandHandler("sonnet", cmd_think))  # legacy-алиас
    app.add_handler(CommandHandler("graph", cmd_graph))
    app.add_handler(CommandHandler("lint", cmd_lint))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^c[yn]:\d+$"))
    # Голосовые/аудио — ДО обработчика вложений (иначе уйдут в «работаю с текстом»).
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(
        filters.ATTACHMENT & ~filters.VOICE & ~filters.AUDIO & ~filters.COMMAND,
        handle_attachment,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("второй-мозг-бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run()
