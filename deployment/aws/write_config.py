"""Create a private, isolated mock configuration without printing secrets."""

import json
import os
import re
import secrets
import sys
from pathlib import Path


def create_config(secret_file: Path, destination: Path) -> None:
    credentials = json.loads(secret_file.read_text(encoding="utf-8"))
    token = credentials["token"]
    chat_id = credentials["chat_id"]
    if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Invalid Telegram bot token format")
    if not isinstance(chat_id, str) or not re.fullmatch(r"-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{4,}", chat_id):
        raise ValueError("Invalid Telegram chat ID format")
    settings = {
        "DHAN_CLIENT_ID": "0000000000",
        "DHAN_ACCESS_TOKEN": "mock-token",
        "APP_SESSION_SECRET": secrets.token_urlsafe(48),
        "APP_ORDER_PIN": f"{secrets.randbelow(100_000_000):08d}",
        "APP_DRY_RUN": "true",
        "APP_FAST_ORDERS": "false",
        "APP_HOST": "127.0.0.1",
        "APP_PORT": "8000",
        "LOG_DIR": "/var/lib/trading/logs",
        "LOG_RETENTION_DAYS": "7",
        "LOG_LEVEL": "INFO",
        "TELEGRAM_ENABLED": "true",
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_CHAT_ID": chat_id,
        "TELEGRAM_HEARTBEAT_SECONDS": "900",
    }
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        for key, value in settings.items():
            file.write(f"{key}={value}\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: write_config.py SECRET_JSON DESTINATION_ENV")
    create_config(Path(sys.argv[1]), Path(sys.argv[2]))
