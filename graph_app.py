"""
Mini App «Граф» — встроенный HTTP-сервер интерактивного просмотрщика вольта.

Бот по /graph снимает снапшот графа (graph_export из Vault MCP), а этот сервер
отдаёт Telegram Mini App: статический viewer (webapp/index.html) + JSON-API.

Снапшоты: data/graph/current.json + previous.json. Ротация только при реальном
изменении графа — повторное открытие не затирает «прошлое состояние», и viewer
честно подсвечивает новые/затронутые узлы относительно последнего изменения.

Безопасность: это публичный HTTPS-эндпоинт (Caddy на kojikk-server), поэтому
все API-запросы аутентифицируются initData Telegram Mini App (HMAC с токеном
бота, по доке Telegram) + проверкой, что user.id == владелец. Без подписи
наружу уходит только статический HTML без данных.
"""
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import parse_qsl

from aiohttp import web

import config
from mcp_client import MCPClient, MCPError, MCPUnavailable

logger = logging.getLogger(__name__)

_WEBAPP_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "webapp", "index.html")
_CURRENT = "current.json"
_PREVIOUS = "previous.json"

# Превью файла в просмотрщике — обрезаем, чтобы не таскать мегабайты в webview.
_PREVIEW_MAX_CHARS = 4000


# ─── Снапшоты ─────────────────────────────────────────────────────────────────

def _snap_path(name: str) -> str:
    return os.path.join(config.GRAPH_DIR, name)


def _load_snapshot(name: str) -> dict | None:
    try:
        with open(_snap_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _graph_fingerprint(data: dict) -> str:
    """Хэш содержимого графа БЕЗ поля generated — для «реально ли изменился»."""
    body = json.dumps(
        {"nodes": data.get("nodes", []), "edges": data.get("edges", [])},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def _edge_key(e: dict) -> str:
    return f"{e.get('src')}|{e.get('tgt')}|{e.get('relation')}|{e.get('layer')}"


def _diff(current: dict, previous: dict | None) -> dict:
    """Что изменилось с прошлого снапшота: новые узлы/рёбра и затронутые узлы."""
    if not previous:
        return {"new_nodes": [], "new_edges": [], "touched_nodes": [], "baseline": None}
    prev_ids = {n["id"] for n in previous.get("nodes", [])}
    prev_gen = previous.get("generated", "")
    new_nodes = [n["id"] for n in current.get("nodes", []) if n["id"] not in prev_ids]
    new_set = set(new_nodes)
    touched = [
        n["id"] for n in current.get("nodes", [])
        if n["id"] not in new_set and n.get("mtime", "") > prev_gen
    ]
    prev_edges = {_edge_key(e) for e in previous.get("edges", [])}
    new_edges = [
        {"src": e["src"], "tgt": e["tgt"]}
        for e in current.get("edges", []) if _edge_key(e) not in prev_edges
    ]
    return {
        "new_nodes": new_nodes,
        "new_edges": new_edges,
        "touched_nodes": touched,
        "baseline": prev_gen,
    }


async def take_snapshot(mcp: MCPClient) -> dict:
    """Снять свежий снапшот графа и отротировать previous при реальном изменении.

    Возвращает {"graph": <данные>, "diff": <изменения>} — то, что ест viewer.
    Может бросить MCPUnavailable/MCPError — вызывающий решает, что показать.
    """
    raw = await mcp.call_tool("graph_export", {})
    data = json.loads(raw)

    os.makedirs(config.GRAPH_DIR, exist_ok=True)
    current = _load_snapshot(_CURRENT)
    if current and _graph_fingerprint(current) != _graph_fingerprint(data):
        # Граф изменился — прежний current становится базой для подсветки.
        os.replace(_snap_path(_CURRENT), _snap_path(_PREVIOUS))
    with open(_snap_path(_CURRENT), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    return {"graph": data, "diff": _diff(data, _load_snapshot(_PREVIOUS))}


# ─── Аутентификация Mini App (initData) ───────────────────────────────────────

def validate_init_data(init_data: str, bot_token: str) -> int | None:
    """Проверить подпись initData по доке Telegram. Возвращает user.id или None.

    secret = HMAC_SHA256(key="WebAppData", msg=bot_token);
    hash   = HMAC_SHA256(secret, data_check_string) — все пары кроме hash,
    отсортированные по ключу, как k=v через \n. Плюс свежесть auth_date.
    """
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        their_hash = pairs.pop("hash", "")
        if not their_hash:
            return None
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        ours = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(ours, their_hash):
            return None
        auth_age = time.time() - int(pairs.get("auth_date", "0"))
        if auth_age > config.GRAPH_AUTH_MAX_AGE:
            return None
        return int(json.loads(pairs.get("user", "{}")).get("id", 0)) or None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _authorized(payload: dict) -> bool:
    uid = validate_init_data(str(payload.get("initData", "")), config.TELEGRAM_BOT_TOKEN)
    return uid is not None and uid == config.TELEGRAM_ALLOWED_USER_ID


# ─── HTTP-хэндлеры ────────────────────────────────────────────────────────────

async def _index(_request: web.Request) -> web.Response:
    try:
        with open(_WEBAPP_INDEX, encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return web.Response(status=500, text="viewer is not bundled")
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def _api_graph(request: web.Request) -> web.Response:
    """Свежий граф + diff. При офлайне Desktop — последний снапшот с stale=true."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "bad request"}, status=400)
    if not _authorized(payload):
        return web.json_response({"error": "unauthorized"}, status=401)

    mcp: MCPClient = request.app["mcp"]
    try:
        snap = await take_snapshot(mcp)
        return web.json_response({**snap, "stale": False})
    except (MCPUnavailable, MCPError, json.JSONDecodeError) as e:
        current = _load_snapshot(_CURRENT)
        if current is None:
            return web.json_response(
                {"error": "вольт недоступен и снапшота ещё нет"}, status=503)
        logger.warning("graph api: live export failed (%s), отдаю снапшот", e)
        return web.json_response({
            "graph": current,
            "diff": _diff(current, _load_snapshot(_PREVIOUS)),
            "stale": True,
        })


async def _api_preview(request: web.Request) -> web.Response:
    """Превью файла узла. Путь принимается ТОЛЬКО из текущего снапшота."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "bad request"}, status=400)
    if not _authorized(payload):
        return web.json_response({"error": "unauthorized"}, status=401)

    node_id = str(payload.get("node", ""))
    current = _load_snapshot(_CURRENT) or {"nodes": []}
    node = next((n for n in current["nodes"] if n["id"] == node_id), None)
    if node is None or node.get("kind") not in ("note", "org"):
        return web.json_response({"error": "узел не найден"}, status=404)
    path = node_id if node["kind"] == "note" else f"{node_id}/_home.md"

    mcp: MCPClient = request.app["mcp"]
    try:
        text = await mcp.call_tool("read_file", {"path": path})
    except (MCPUnavailable, MCPError) as e:
        return web.json_response({"error": f"вольт недоступен: {e}"}, status=503)

    # Снимаем untrusted-обёртку Vault MCP — viewer показывает контент как текст.
    from brain import _strip_untrusted
    text = _strip_untrusted(text)
    truncated = len(text) > _PREVIEW_MAX_CHARS
    return web.json_response({
        "path": path,
        "content": text[:_PREVIEW_MAX_CHARS],
        "truncated": truncated,
    })


# ─── Жизненный цикл ───────────────────────────────────────────────────────────

async def start(mcp: MCPClient) -> web.AppRunner:
    """Поднять сервер Mini App в текущем event-loop. Возвращает runner для stop."""
    app = web.Application()
    app["mcp"] = mcp
    app.add_routes([
        web.get("/", _index),
        web.post("/api/graph", _api_graph),
        web.post("/api/preview", _api_preview),
        # Вендорные библиотеки viewer'а (force-graph, marked, dompurify) —
        # отдаём сами, чтобы webview не зависел от доступности CDN из РФ.
        web.static("/vendor", os.path.join(os.path.dirname(_WEBAPP_INDEX), "vendor")),
    ])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.GRAPH_PORT)
    await site.start()
    logger.info("graph mini app слушает :%d", config.GRAPH_PORT)
    return runner
