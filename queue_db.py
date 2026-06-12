"""
Durable-очередь на SQLite — центральная гарантия надёжности бота.

Контракт:
  * capture сперва ПЕРСИСТИТСЯ (enqueue), потом обрабатывается → ничего не теряется;
  * idempotency_key уникален → повторная доставка из Telegram не плодит дубль;
  * crash-recovery: «processing», зависшие при рестарте, возвращаются в «pending»;
  * толерантность к офлайну Desktop: requeue с ростом attempts, воркер ретраит позже;
  * confirm-флоу: dry-run структурной операции висит как «awaiting_confirm» с
    сохранённым состоянием диалога, переживая рестарт.

Статусы: pending → processing → {done | error | awaiting_confirm}
         awaiting_confirm → (кнопка) → resume → {done | error | awaiting_confirm}

Все операции — короткие, выполняются из единственного потока event-loop'а
(хэндлеры Telegram и воркер живут в одном loop), поэтому соединение на вызов.
"""
import os
import sqlite3
import time
from datetime import datetime, timezone

import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.QUEUE_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(config.QUEUE_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key  TEXT UNIQUE NOT NULL,
                chat_id          INTEGER NOT NULL,
                user_id          INTEGER NOT NULL,
                text             TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'pending',
                force_sonnet     INTEGER NOT NULL DEFAULT 0,
                messages         TEXT,            -- JSON состояния диалога (для resume)
                pending_tool     TEXT,            -- JSON {name,args} ждущий confirm
                confirm_decision TEXT,            -- 'yes' | 'no'
                attempts         INTEGER NOT NULL DEFAULT 0,
                last_error       TEXT,
                offline_notified INTEGER NOT NULL DEFAULT 0,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, id)")
        # Короткая диалоговая память: последние реплики чата подмешиваются в
        # контекст агента (долгая память — это вольт, не эта таблица).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dialog (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                role       TEXT NOT NULL,      -- 'user' | 'assistant'
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dialog_chat ON dialog(chat_id, id)")


def enqueue(idempotency_key: str, chat_id: int, user_id: int, text: str,
            force_sonnet: bool = False) -> tuple[int, bool]:
    """Поставить сообщение в очередь. Возвращает (id, created).

    created=False, если ключ уже был (идемпотентность на уровне приёма)."""
    ts = _now()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO queue
               (idempotency_key, chat_id, user_id, text, status, force_sonnet,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (idempotency_key, chat_id, user_id, text, 1 if force_sonnet else 0, ts, ts),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT id FROM queue WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            return (row["id"], False)
        return (cur.lastrowid, True)


def recover_processing() -> int:
    """Crash-recovery: вернуть зависшие 'processing' обратно в 'pending'.

    'awaiting_confirm' и 'resume' не трогаем — их состояние валидно после рестарта."""
    cutoff = time.time() - config.STALE_PROCESSING_SEC
    with _connect() as conn:
        rows = conn.execute("SELECT id, updated_at FROM queue WHERE status='processing'").fetchall()
        recovered = 0
        for r in rows:
            try:
                age = datetime.fromisoformat(r["updated_at"]).timestamp()
            except ValueError:
                age = 0
            if age <= cutoff:
                conn.execute(
                    "UPDATE queue SET status='pending', updated_at=? WHERE id=?",
                    (_now(), r["id"]),
                )
                recovered += 1
        return recovered


def _get(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM queue WHERE id=?", (item_id,)).fetchone()


def claim_next() -> sqlite3.Row | None:
    """Взять старейший 'pending' и пометить 'processing' (атомарно).

    Возвращает строку уже в статусе 'processing' (после UPDATE)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM queue WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE queue SET status='processing', updated_at=? WHERE id=?",
            (_now(), row["id"]),
        )
        return _get(conn, row["id"])


def claim_resume() -> sqlite3.Row | None:
    """Взять старейший элемент со статусом 'resume' (подтверждённый пользователем).

    Возвращает строку уже в статусе 'processing' (после UPDATE)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM queue WHERE status='resume' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE queue SET status='processing', updated_at=? WHERE id=?",
            (_now(), row["id"]),
        )
        return _get(conn, row["id"])


def mark_done(item_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE queue SET status='done', updated_at=? WHERE id=?",
            (_now(), item_id),
        )


def mark_error(item_id: int, err: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE queue SET status='error', last_error=?, updated_at=? WHERE id=?",
            (err[:500], _now(), item_id),
        )


def requeue(item_id: int, err: str) -> int:
    """Вернуть элемент в 'pending' (Desktop офлайн и т.п.), attempts++.

    Возвращает новое значение attempts."""
    with _connect() as conn:
        conn.execute(
            """UPDATE queue
               SET status='pending', attempts=attempts+1, last_error=?, updated_at=?
               WHERE id=?""",
            (err[:500], _now(), item_id),
        )
        row = conn.execute("SELECT attempts FROM queue WHERE id=?", (item_id,)).fetchone()
        return row["attempts"] if row else 0


def mark_offline_notified(item_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE queue SET offline_notified=1, updated_at=? WHERE id=?",
            (_now(), item_id),
        )


def suspend_for_confirm(item_id: int, messages_json: str, pending_tool_json: str) -> None:
    """Сохранить состояние диалога и ждущую структурную операцию (awaiting_confirm)."""
    with _connect() as conn:
        conn.execute(
            """UPDATE queue
               SET status='awaiting_confirm', messages=?, pending_tool=?, updated_at=?
               WHERE id=?""",
            (messages_json, pending_tool_json, _now(), item_id),
        )


def set_decision(item_id: int, decision: str) -> bool:
    """Зафиксировать решение по confirm и перевести в 'resume'.

    Возвращает False, если элемент не в 'awaiting_confirm' (повторное нажатие)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT status FROM queue WHERE id=?", (item_id,)
        ).fetchone()
        if not row or row["status"] != "awaiting_confirm":
            return False
        conn.execute(
            "UPDATE queue SET status='resume', confirm_decision=?, updated_at=? WHERE id=?",
            (decision, _now(), item_id),
        )
        return True


# ─── Диалоговая память ────────────────────────────────────────────────────────

_DIALOG_KEEP = 40          # строк на чат (≈20 обменов)
_DIALOG_CONTENT_CAP = 1200  # символов на реплику при записи


def log_dialog(chat_id: int, role: str, content: str) -> None:
    """Записать реплику и подрезать хвост истории чата."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO dialog (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, content[:_DIALOG_CONTENT_CAP], _now()),
        )
        conn.execute(
            """DELETE FROM dialog WHERE chat_id=? AND id NOT IN
               (SELECT id FROM dialog WHERE chat_id=? ORDER BY id DESC LIMIT ?)""",
            (chat_id, chat_id, _DIALOG_KEEP),
        )


def recent_dialog(chat_id: int, limit: int = 6) -> list[dict]:
    """Последние реплики чата в хронологическом порядке (для seed агента)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM dialog WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def counts() -> dict:
    with _connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM queue GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
