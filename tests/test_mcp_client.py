"""Парсинг ответов MCP и классификация ошибок (без реальной сети)."""
import json

import httpx
import pytest

import mcp_client
from mcp_client import MCPClient, MCPError, MCPUnavailable

# Реальный класс фиксируем ДО любого monkeypatch, иначе фабрика, вызывая
# httpx.AsyncClient, попадёт сама в себя → бесконечная рекурсия.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _sse(obj: dict) -> str:
    return f"event: message\ndata: {json.dumps(obj)}\n\n"


def test_parse_sse_frame():
    body = _sse({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    out = mcp_client._parse_rpc(body, "text/event-stream")
    assert out["result"]["tools"] == []


def test_parse_plain_json():
    out = mcp_client._parse_rpc('{"result": {"ok": 1}}', "application/json")
    assert out["result"]["ok"] == 1


def test_content_text_joins_text_parts():
    res = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    assert mcp_client._content_text(res) == "a\nb"


def _client_with(handler, monkeypatch) -> MCPClient:
    c = MCPClient("http://x/mcp", "t" * 40)
    transport = httpx.MockTransport(handler)

    def _factory(*a, **k):
        k.pop("timeout", None)
        return _REAL_ASYNC_CLIENT(transport=transport)

    # monkeypatch автоматически восстановит атрибут после теста.
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", _factory)
    return c


@pytest.mark.asyncio
async def test_call_tool_returns_text(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = _sse({"jsonrpc": "2.0", "id": 1,
                     "result": {"content": [{"type": "text", "text": "created x.md"}]}})
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    c = _client_with(handler, monkeypatch)
    out = await c.call_tool("create_note", {"path": "x.md"})
    assert "created x.md" in out


@pytest.mark.asyncio
async def test_5xx_is_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    c = _client_with(handler, monkeypatch)
    with pytest.raises(MCPUnavailable):
        await c.call_tool("read_hot", {})


@pytest.mark.asyncio
async def test_401_is_error_not_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    c = _client_with(handler, monkeypatch)
    with pytest.raises(MCPError) as ei:
        await c.call_tool("read_hot", {})
    assert not isinstance(ei.value, MCPUnavailable)


@pytest.mark.asyncio
async def test_tool_iserror_raises(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = _sse({"jsonrpc": "2.0", "id": 1,
                     "result": {"isError": True, "content": [{"type": "text", "text": "ALREADY_EXISTS"}]}})
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    c = _client_with(handler, monkeypatch)
    with pytest.raises(MCPError):
        await c.call_tool("create_note", {"path": "x.md"})
