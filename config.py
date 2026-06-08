"""
Конфигурация и секреты второго-мозг-бота.

Источник правды — переменные окружения (.env вне репо / Docker secrets).
Секреты НИКОГДА не хардкодятся и не логируются. validate() — fail-closed
при старте: без обязательных значений процесс не поднимается.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _read_secret(value: str | None, file_env: str) -> str | None:
    """Предпочесть Docker-secret (файл), иначе env-литерал."""
    path = os.getenv(file_env)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return value


# ─── LLM (Claude через apinet, OpenAI-совместимый) ────────────────────────────
CLAUDE_API_KEY      = _read_secret(os.getenv("CLAUDE_API_KEY"), "CLAUDE_API_KEY_FILE")
CLAUDE_BASE_URL     = os.getenv("CLAUDE_BASE_URL", "https://apinet.cloud/v1")
CLAUDE_MODEL        = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_SONNET_MODEL = os.getenv("CLAUDE_SONNET_MODEL", "claude-sonnet-4-6")

# Транскрипция голосовых: apinet НЕ имеет whisper-канала, аудио распознаёт
# мультимодальная модель на эндпоинте /audio/transcriptions тем же ключом/egress'ом.
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gemini-2.5-flash")

# ─── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN       = _read_secret(os.getenv("TELEGRAM_BOT_TOKEN"), "TELEGRAM_BOT_TOKEN_FILE")
TELEGRAM_ALLOWED_USER_ID = int(os.getenv("TELEGRAM_ALLOWED_USER_ID", "0"))

# ─── Vault MCP (HTTP, Bearer) ─────────────────────────────────────────────────
MCP_URL    = os.getenv("MCP_URL", "http://192.168.1.50:8788/mcp")
MCP_TOKEN  = _read_secret(os.getenv("MCP_TOKEN"), "MCP_TOKEN_FILE")
MCP_TIMEOUT_SEC = float(os.getenv("MCP_TIMEOUT_SEC", "30"))

# Имя файла-«мозга» в вольте + TTL кэша.
AGENT_MD_PATH = os.getenv("AGENT_MD_PATH", "_system/agent.md")
AGENT_MD_TTL  = int(os.getenv("AGENT_MD_TTL", "900"))  # сек

# Структурные/удаляющие инструменты — двухфазные (dry-run → confirm).
STRUCTURAL_TOOLS = {"move", "promote", "soft_delete"}

# ─── Надёжность / очередь ─────────────────────────────────────────────────────
QUEUE_DB_PATH      = os.getenv("QUEUE_DB_PATH", "/app/data/queue.db")
MAX_STEPS          = int(os.getenv("MAX_STEPS", "20"))
WORKER_POLL_SEC    = float(os.getenv("WORKER_POLL_SEC", "1.0"))
MCP_RETRY_BASE_SEC = float(os.getenv("MCP_RETRY_BASE_SEC", "5"))
MCP_RETRY_MAX_SEC  = float(os.getenv("MCP_RETRY_MAX_SEC", "300"))
# Сколько секунд «processing» считается зависшим (crash-recovery возобновляет).
STALE_PROCESSING_SEC = int(os.getenv("STALE_PROCESSING_SEC", "120"))

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# ─── Учёт расхода токенов ─────────────────────────────────────────────────────
PRICING = {
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
}
USAGE_SOURCE       = os.getenv("USAGE_SOURCE", "estimate")
CHARS_PER_TOKEN    = float(os.getenv("CHARS_PER_TOKEN", "3.5"))
MONTHLY_BUDGET_USD = float(os.getenv("MONTHLY_BUDGET_USD", "50"))
USAGE_DB_PATH      = os.getenv("USAGE_DB_PATH", "/app/data/usage.db")


def validate() -> None:
    """Fail-closed: упасть на старте, если нет обязательной конфигурации."""
    missing = [
        name for name, val in (
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("CLAUDE_API_KEY", CLAUDE_API_KEY),
            ("MCP_TOKEN", MCP_TOKEN),
            ("MCP_URL", MCP_URL),
        ) if not val
    ]
    if not TELEGRAM_ALLOWED_USER_ID:
        missing.append("TELEGRAM_ALLOWED_USER_ID")
    if MCP_TOKEN and len(MCP_TOKEN) < 32:
        raise SystemExit("FATAL config: MCP_TOKEN короче 32 символов (fail-closed).")
    if missing:
        raise SystemExit(
            "FATAL config: не заданы обязательные переменные: " + ", ".join(missing)
        )
