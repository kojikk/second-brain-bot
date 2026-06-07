"""
Минимальный асинхронный клиент Vault MCP по Streamable HTTP (stateless).

Сервер stateless и принимает tools/list и tools/call без предварительного
initialize-хендшейка (проверено вживую). Поэтому полноценная MCP-SDK-сессия не
нужна — достаточно POST JSON-RPC и разбора ответа (plain JSON либо один SSE-кадр
`data: {...}`). Аутентификация — Bearer-заголовок.

Vault MCP всегда LAN/loopback (Desktop по LAN либо 127.0.0.1), поэтому HTTP-клиент
здесь НЕ использует egress-прокси (`trust_env=False`): иначе LAN-запрос ушёл бы
через singbox-прок-си (HTTP_PROXY в окружении), который LAN не маршрутизирует →
502. NO_PROXY с CIDR ненадёжен (httpx не матчит подсети), поэтому не полагаемся
на него. Прокси нужен ТОЛЬКО для apinet (его держит openai-клиент в agent.py).

Различаем два класса ошибок:
  * MCPUnavailable — транспорт/сеть/5xx → элемент очереди РЕТРАИТСЯ позже
    (например, Desktop спит). Воркер не теряет сообщение.
  * MCPError       — прикладная ошибка инструмента/протокола (4xx, isError,
    JSON-RPC error) → возвращается агенту как текст результата.
"""
import json
import logging

import httpx

logger = logging.getLogger(__name__)


class MCPError(Exception):
    """Прикладная ошибка MCP (инструмент отклонил, протокол, 401/4xx)."""


class MCPUnavailable(MCPError):
    """Транспортная недоступность (сеть/5xx) — ретраебельно."""


def _parse_rpc(body: str, content_type: str) -> dict:
    """Разобрать ответ как plain JSON или как один SSE-кадр (event/data)."""
    if "text/event-stream" in content_type or body.lstrip().startswith("event:"):
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise MCPError("в SSE-ответе нет кадра data:")
    return json.loads(body)


def _content_text(result: dict) -> str:
    """Склеить текстовые части content[] из результата tools/call."""
    parts = result.get("content", []) or []
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")


class MCPClient:
    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self._url = url
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # И JSON, и SSE — сервер отвечает SSE-кадром.
            "Accept": "application/json, text/event-stream",
        }
        # .../mcp → .../healthz
        self._health_url = url.rsplit("/", 1)[0] + "/healthz"
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _rpc(self, method: str, params: dict) -> dict:
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
                resp = await client.post(self._url, headers=self._headers, json=payload)
        except httpx.HTTPError as e:
            raise MCPUnavailable(f"транспорт MCP недоступен: {e}") from e

        if resp.status_code == 401:
            raise MCPError("MCP 401: неверный или отсутствующий MCP_TOKEN")
        if resp.status_code >= 500:
            raise MCPUnavailable(f"MCP HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise MCPError(f"MCP HTTP {resp.status_code}: {resp.text[:200]}")

        data = _parse_rpc(resp.text, resp.headers.get("content-type", ""))
        if "error" in data and data["error"]:
            raise MCPError(data["error"].get("message", "ошибка JSON-RPC"))
        return data.get("result", {}) or {}

    async def list_tools(self) -> list[dict]:
        """Список инструментов (имя/описание/схема) — для системного промпта."""
        res = await self._rpc("tools/list", {})
        return res.get("tools", []) or []

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Вызвать инструмент. Текст результата возвращается строкой.

        isError=true превращается в MCPError (прикладная, не транспортная)."""
        res = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        text = _content_text(res)
        if res.get("isError"):
            raise MCPError(text or "инструмент вернул ошибку")
        return text

    async def healthy(self) -> bool:
        """GET /healthz без авторизации; False при любой сетевой ошибке."""
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                resp = await client.get(self._health_url)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
