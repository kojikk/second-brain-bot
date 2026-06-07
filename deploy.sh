#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "=== Building image ==="
docker compose build

echo ""
echo "=== Running tests (against freshly built image) ==="
docker compose run --rm --no-deps \
  -e TELEGRAM_ALLOWED_USER_ID=1 \
  -e MCP_URL=http://127.0.0.1:8788/mcp \
  -e MCP_TOKEN_FILE= -e TELEGRAM_BOT_TOKEN_FILE= -e CLAUDE_API_KEY_FILE= \
  second-brain-bot pytest tests/ -v

echo ""
echo "=== Tests passed. Deploying ==="
docker compose up -d --force-recreate

echo ""
echo "=== Done. Logs: ==="
sleep 4
docker logs second-brain-bot --tail 20
