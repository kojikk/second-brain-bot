"""Durable-очередь: идемпотентность приёма, claim, crash-recovery, confirm-флоу."""
import config
import queue_db as q


def _fresh(tmp_path):
    config.QUEUE_DB_PATH = str(tmp_path / "queue.db")
    q.init_db()


def test_enqueue_idempotent(tmp_path):
    _fresh(tmp_path)
    id1, created1 = q.enqueue("k1", 10, 20, "привет")
    id2, created2 = q.enqueue("k1", 10, 20, "привет")
    assert created1 is True
    assert created2 is False
    assert id1 == id2
    assert q.counts().get("pending") == 1


def test_claim_marks_processing(tmp_path):
    _fresh(tmp_path)
    q.enqueue("k1", 1, 1, "x")
    item = q.claim_next()
    assert item["status"] == "processing"
    assert q.claim_next() is None  # больше нет pending


def test_done_and_error(tmp_path):
    _fresh(tmp_path)
    q.enqueue("k1", 1, 1, "x")
    q.enqueue("k2", 1, 1, "y")
    a = q.claim_next()
    b = q.claim_next()
    q.mark_done(a["id"])
    q.mark_error(b["id"], "boom")
    c = q.counts()
    assert c.get("done") == 1 and c.get("error") == 1


def test_requeue_increments_attempts(tmp_path):
    _fresh(tmp_path)
    q.enqueue("k1", 1, 1, "x")
    item = q.claim_next()
    n1 = q.requeue(item["id"], "offline")
    assert n1 == 1
    again = q.claim_next()  # снова pending → берётся
    n2 = q.requeue(again["id"], "offline")
    assert n2 == 2


def test_crash_recovery_resets_stale_processing(tmp_path):
    _fresh(tmp_path)
    config.STALE_PROCESSING_SEC = 0  # всё processing считается зависшим
    q.enqueue("k1", 1, 1, "x")
    q.claim_next()
    assert q.counts().get("processing") == 1
    recovered = q.recover_processing()
    assert recovered == 1
    assert q.counts().get("pending") == 1


def test_confirm_flow(tmp_path):
    _fresh(tmp_path)
    q.enqueue("k1", 1, 1, "удали заметку")
    item = q.claim_next()
    q.suspend_for_confirm(item["id"], '[{"role":"user"}]', '{"tool":"soft_delete"}')
    assert q.counts().get("awaiting_confirm") == 1
    # решение «да» → resume
    ok = q.set_decision(item["id"], "yes")
    assert ok is True
    # повторное нажатие уже не проходит
    assert q.set_decision(item["id"], "no") is False
    resumed = q.claim_resume()
    assert resumed is not None
    assert resumed["confirm_decision"] == "yes"
    assert resumed["status"] == "processing"

# --- диалоговая память ---

def test_dialog_log_and_recent(tmp_path):
    _fresh(tmp_path)
    q.log_dialog(1, "user", "люблю нуар")
    q.log_dialog(1, "assistant", "учту")
    q.log_dialog(2, "user", "чужой чат")
    hist = q.recent_dialog(1)
    assert [h["role"] for h in hist] == ["user", "assistant"]
    assert hist[0]["content"] == "люблю нуар"
    assert all("чужой" not in h["content"] for h in hist)


def test_dialog_trims_tail_and_caps_content(tmp_path):
    _fresh(tmp_path)
    for i in range(60):
        q.log_dialog(1, "user", f"сообщение {i}")
    q.log_dialog(1, "assistant", "х" * 5000)
    hist = q.recent_dialog(1, limit=100)
    assert len(hist) <= q._DIALOG_KEEP
    assert hist[-1]["content"] == "х" * q._DIALOG_CONTENT_CAP
    assert hist[0]["content"] != "сообщение 0"  # старьё подрезано
