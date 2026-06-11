"""Mini App «Граф»: аутентификация initData, diff снапшотов, ротация."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

import config
import graph_app
from graph_app import _diff, take_snapshot, validate_init_data

BOT_TOKEN = "123456:TEST-token"


def _make_init_data(user_id: int = 1, auth_age_sec: int = 10,
                    token: str = BOT_TOKEN, tamper: bool = False) -> str:
    pairs = {
        "auth_date": str(int(time.time()) - auth_age_sec),
        "query_id": "AAE",
        "user": json.dumps({"id": user_id, "first_name": "K"}),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if tamper:
        h = ("0" if h[0] != "0" else "1") + h[1:]
    return urlencode({**pairs, "hash": h})


# --- validate_init_data ---

def test_init_data_valid():
    assert validate_init_data(_make_init_data(user_id=42), BOT_TOKEN) == 42


def test_init_data_bad_hash():
    assert validate_init_data(_make_init_data(tamper=True), BOT_TOKEN) is None


def test_init_data_wrong_token():
    data = _make_init_data(token="999:OTHER")
    assert validate_init_data(data, BOT_TOKEN) is None


def test_init_data_expired(monkeypatch):
    monkeypatch.setattr(config, "GRAPH_AUTH_MAX_AGE", 60)
    data = _make_init_data(auth_age_sec=3600)
    assert validate_init_data(data, BOT_TOKEN) is None


def test_init_data_garbage():
    assert validate_init_data("", BOT_TOKEN) is None
    assert validate_init_data("hash=zzz&user=not-json", BOT_TOKEN) is None


# --- diff ---

def _node(nid: str, mtime: str = "") -> dict:
    n = {"id": nid, "kind": "note", "label": nid, "entity": False, "degree": 1}
    if mtime:
        n["mtime"] = mtime
    return n


def test_diff_no_baseline():
    d = _diff({"nodes": [_node("a.md")], "edges": []}, None)
    assert d["new_nodes"] == [] and d["baseline"] is None


def test_diff_new_touched_and_edges():
    previous = {
        "generated": "2026-06-10T00:00:00Z",
        "nodes": [_node("a.md", "2026-06-01T00:00:00Z"), _node("b.md", "2026-06-01T00:00:00Z")],
        "edges": [{"src": "a.md", "tgt": "b.md", "relation": "ссылается", "layer": "derived"}],
    }
    current = {
        "generated": "2026-06-12T00:00:00Z",
        "nodes": [
            _node("a.md", "2026-06-11T00:00:00Z"),   # изменён после prev.generated
            _node("b.md", "2026-06-01T00:00:00Z"),   # не тронут
            _node("c.md", "2026-06-12T00:00:00Z"),   # новый
        ],
        "edges": [
            {"src": "a.md", "tgt": "b.md", "relation": "ссылается", "layer": "derived"},
            {"src": "a.md", "tgt": "c.md", "relation": "использует", "layer": "semantic"},
        ],
    }
    d = _diff(current, previous)
    assert d["new_nodes"] == ["c.md"]
    assert d["touched_nodes"] == ["a.md"]
    assert d["new_edges"] == [{"src": "a.md", "tgt": "c.md"}]
    assert d["baseline"] == "2026-06-10T00:00:00Z"


# --- take_snapshot: ротация previous только при реальном изменении ---

class _FakeMCP:
    def __init__(self, payloads: list[dict]):
        self._payloads = list(payloads)

    async def call_tool(self, name: str, args: dict) -> str:
        assert name == "graph_export"
        return json.dumps(self._payloads.pop(0), ensure_ascii=False)


def _export(nodes: list[dict], generated: str) -> dict:
    return {"generated": generated, "stats": {"nodes": len(nodes), "edges": 0},
            "nodes": nodes, "edges": []}


@pytest.mark.asyncio
async def test_snapshot_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GRAPH_DIR", str(tmp_path))
    v1 = _export([_node("a.md")], "2026-06-10T00:00:00Z")
    v1_again = _export([_node("a.md")], "2026-06-10T01:00:00Z")   # тот же граф, новый generated
    v2 = _export([_node("a.md"), _node("b.md", "2026-06-12T00:00:00Z")], "2026-06-12T00:00:00Z")

    mcp = _FakeMCP([v1, v1_again, v2])

    s1 = await take_snapshot(mcp)
    assert s1["diff"]["baseline"] is None            # первого previous ещё нет

    s2 = await take_snapshot(mcp)
    assert s2["diff"]["baseline"] is None            # граф не менялся — ротации не было

    s3 = await take_snapshot(mcp)
    assert s3["diff"]["new_nodes"] == ["b.md"]       # ротация: prev = v1*, новый узел виден
