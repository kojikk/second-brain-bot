"""
Учёт расхода токенов и стоимости запросов к LLM (локальный SQLite).

Числа берутся либо из ответа провайдера (USAGE_SOURCE=provider), либо
оцениваются по длине текста (USAGE_SOURCE=estimate) — последнее надёжнее на
apinet.cloud, который присылает недостоверный usage.

Здесь только запись/аналитика; сам вызов LLM делает agent.py (AsyncOpenAI).
"""
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta

from config import (
    PRICING, USAGE_SOURCE, CHARS_PER_TOKEN,
    MONTHLY_BUDGET_USD, USAGE_DB_PATH,
)

logger = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(USAGE_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(USAGE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                TEXT NOT NULL,
                model             TEXT NOT NULL,
                request_type      TEXT,
                prompt_tokens     INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                total_tokens      INTEGER NOT NULL,
                cost_usd          REAL NOT NULL,
                source            TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts)")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def _cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price = PRICING.get(model)
    if not price:
        price = max(PRICING.values(), key=lambda p: p["output"]) if PRICING else {"input": 0, "output": 0}
    return prompt_tokens / 1_000_000 * price["input"] + completion_tokens / 1_000_000 * price["output"]


def record(model: str, request_type: str, prompt_text: str, completion_text: str,
           provider_usage: tuple[int, int] | None = None) -> dict:
    """Записать один вызов LLM. Возвращает {prompt_tokens, completion_tokens, cost_usd}."""
    if USAGE_SOURCE == "provider" and provider_usage:
        pt, ct = provider_usage
        source = "provider"
    else:
        pt = estimate_tokens(prompt_text)
        ct = estimate_tokens(completion_text)
        source = "estimate"

    cost = _cost_usd(model, pt, ct)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "request_type": request_type or "agent",
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": pt + ct,
        "cost_usd": cost,
        "source": source,
    }
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO usage
                   (ts, model, request_type, prompt_tokens, completion_tokens,
                    total_tokens, cost_usd, source)
                   VALUES (:ts, :model, :request_type, :prompt_tokens,
                           :completion_tokens, :total_tokens, :cost_usd, :source)""",
                row,
            )
    except Exception as e:
        logger.warning(f"usage record failed: {e}")
    return {"prompt_tokens": pt, "completion_tokens": ct, "cost_usd": cost}


def record_completion(resp, model: str, request_type: str, prompt_text: str) -> None:
    """Снять расход с ответа AsyncOpenAI и записать (не роняет вызывающего)."""
    try:
        completion_text = (resp.choices[0].message.content or "") if resp.choices else ""
        provider_usage = None
        u = getattr(resp, "usage", None)
        if u is not None:
            provider_usage = (
                getattr(u, "prompt_tokens", 0) or 0,
                getattr(u, "completion_tokens", 0) or 0,
            )
        record(model, request_type, prompt_text, completion_text, provider_usage)
    except Exception as e:
        logger.warning(f"usage bookkeeping failed: {e}")


# ─── Аналитика ────────────────────────────────────────────────────────────────

def _agg(where: str = "", params: tuple = ()) -> dict:
    sql = (
        "SELECT COUNT(*) AS n, "
        "COALESCE(SUM(prompt_tokens),0) AS pt, "
        "COALESCE(SUM(completion_tokens),0) AS ct, "
        "COALESCE(SUM(total_tokens),0) AS tt, "
        "COALESCE(SUM(cost_usd),0) AS cost "
        "FROM usage"
    )
    if where:
        sql += f" WHERE {where}"
    with _connect() as conn:
        r = conn.execute(sql, params).fetchone()
    return {"n": r["n"], "pt": r["pt"], "ct": r["ct"], "tt": r["tt"], "cost": r["cost"]}


def _since_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def last_request() -> dict | None:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM usage ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def stats_period(hours: int) -> dict:
    return _agg("ts >= ?", (_since_iso(hours),))


def month_spend() -> float:
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return _agg("ts >= ?", (start.isoformat(),))["cost"]


def _fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def format_usage_report() -> str:
    """Markdown-отчёт для Telegram (рендерится через tg_render)."""
    init_db()
    last = last_request()
    day = stats_period(24)
    week = stats_period(24 * 7)
    spent = month_spend()
    budget = MONTHLY_BUDGET_USD
    pct = (spent / budget * 100) if budget else 0

    lines = ["**Расход API**"]

    if last:
        model_short = last["model"].split("/")[-1]
        lines.append(
            f"последний запрос — {model_short} · {last['request_type']} · "
            f"{_fmt_int(last['total_tokens'])} ток · ${last['cost_usd']:.4f}"
        )
    else:
        lines.append("последний запрос — *нет данных*")

    lines.append(f"сутки — {day['n']} запр · {_fmt_int(day['tt'])} ток · ${day['cost']:.2f}")
    lines.append(f"неделя — {week['n']} запр · {_fmt_int(week['tt'])} ток · ${week['cost']:.2f}")
    lines.append(f"\n**Бюджет месяца:** ${spent:.2f} / ${budget:.2f} ({pct:.0f}%)")
    if USAGE_SOURCE == "estimate":
        lines.append("\n*Токены — локальная оценка (apinet присылает недостоверный usage).*")
    return "\n".join(lines)
