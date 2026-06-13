"""Mini App «Граф»: аутентификация initData, diff снапшотов, ротация."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

import config
import graph_app
from graph_app import (_build_code_graph, _diff, _ns_project, _snap_names,
                       take_snapshot, validate_init_data)

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


# --- код-граф: парсинг JSONL и неймспейсы ---

def _code_jsonl() -> str:
    return "\n".join(json.dumps(o, ensure_ascii=False) for o in [
        {"t": "meta", "project": "foo", "commit": "abc1234", "scanned": "2026-06-13"},
        {"t": "node", "id": "a.py", "kind": "module", "file": "a.py"},
        {"t": "node", "id": "a.py#f", "kind": "function", "file": "a.py", "line": 10},
        {"t": "node", "id": "b.py", "kind": "module", "file": "b.py"},
        {"t": "edge", "src": "a.py", "tgt": "a.py#f", "rel": "defines", "conf": "extracted"},
        {"t": "edge", "src": "a.py", "tgt": "b.py", "rel": "imports", "conf": "extracted"},
    ])


def test_build_code_graph():
    g = _build_code_graph(_code_jsonl(), "foo")
    assert g["namespace"] == "code:foo"
    assert g["meta"]["commit"] == "abc1234" and g["meta"]["scanned"] == "2026-06-13"
    assert g["stats"] == {"nodes": 3, "edges": 2}
    byid = {n["id"]: n for n in g["nodes"]}
    assert byid["a.py"]["kind"] == "code" and byid["a.py"]["codeKind"] == "module"
    assert byid["a.py#f"]["codeKind"] == "function" and byid["a.py#f"]["line"] == 10
    assert byid["a.py#f"]["label"] == "f"           # подпись = символ после '#'
    assert byid["a.py"]["degree"] == 2              # два инцидентных ребра
    # «сообщество» = файл: символ делит его с модулем, другой модуль — отдельное
    assert byid["a.py"]["community"] == byid["a.py#f"]["community"]
    assert byid["b.py"]["community"] != byid["a.py"]["community"]
    assert all(e["layer"] == "code" for e in g["edges"])


def test_build_code_graph_skips_garbage():
    g = _build_code_graph('шум\n{"t":"node","id":"x.py","kind":"module"}\n', "p")
    assert g["stats"]["nodes"] == 1


def test_ns_project_validation():
    assert _ns_project("kb") is None
    assert _ns_project("code:second-brain-bot") == "second-brain-bot"
    assert _ns_project("code:../etc/passwd") is None   # обход пути отклонён
    assert _ns_project("garbage") is None
    assert _snap_names("kb") == ("current.json", "previous.json")
    assert _snap_names("code:foo") == ("current.code-foo.json", "previous.code-foo.json")


class _FakeReadMCP:
    """MCP, отдающий read_file с untrusted-обёрткой Vault MCP."""

    def __init__(self, jsonl: str):
        self._jsonl = jsonl

    async def call_tool(self, name: str, args: dict) -> str:
        assert name == "read_file"
        assert args["path"] == "_system/graph/code/foo.jsonl"
        return ('<<<UNTRUSTED_VAULT_CONTENT source="x"\nbla\n---\n'
                + self._jsonl + "\nUNTRUSTED_VAULT_CONTENT>>>")


@pytest.mark.asyncio
async def test_snapshot_code_ns(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GRAPH_DIR", str(tmp_path))
    mcp = _FakeReadMCP(_code_jsonl())
    s = await take_snapshot(mcp, "code:foo")
    assert s["graph"]["namespace"] == "code:foo"
    assert s["graph"]["stats"]["nodes"] == 3
    assert (tmp_path / "current.code-foo.json").exists()
    assert s["diff"]["baseline"] is None             # своего previous ещё нет
    # повторный снимок без изменений — ротации previous не возникает
    s2 = await take_snapshot(mcp, "code:foo")
    assert s2["diff"]["baseline"] is None
