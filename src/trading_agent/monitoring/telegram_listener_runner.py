"""
Module entrypoint for the Telegram listener.

Run with:
    python -m trading_agent.monitoring.telegram_listener_runner

Used by docker-compose so the listener auto-restarts on crashes.
"""
from __future__ import annotations

import asyncio

from trading_agent.core.logging import configure_logging
from trading_agent.monitoring.telegram_listener import listen_forever


def main() -> None:
    configure_logging()
    try:
        asyncio.run(listen_forever())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
