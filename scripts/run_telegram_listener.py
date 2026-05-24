"""
Standalone runner for the Telegram listener.

Usage:
    py -3.14 scripts/run_telegram_listener.py

What it does:
    Long-polls Telegram for messages addressed to the bot whose token is in
    TELEGRAM_BOT_TOKEN. Honors commands from TELEGRAM_CHAT_ID only.

    On Ctrl+C, exits cleanly.

Production tip: run via Docker (`docker compose up -d telegram_listener`)
so it auto-restarts on crashes and survives reboots.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_agent.core.logging import configure_logging  # noqa: E402
from trading_agent.monitoring.telegram_listener import listen_forever  # noqa: E402


def main() -> None:
    configure_logging()
    try:
        asyncio.run(listen_forever())
    except KeyboardInterrupt:
        print("\nShutting down listener.")


if __name__ == "__main__":
    main()
