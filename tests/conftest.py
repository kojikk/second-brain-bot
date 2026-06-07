import os
import sys

# Конфиг читается при импорте; зададим безопасные значения для тестов ДО импортов.
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "1")
os.environ.setdefault("MCP_URL", "http://127.0.0.1:8788/mcp")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
