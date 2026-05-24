"""
Module entrypoint for the pre-market briefing worker.

Run with:
    python -m trading_agent.premarket.runner

Used by docker-compose so the worker auto-restarts on crashes.
"""
from __future__ import annotations

import asyncio

from trading_agent.core.logging import configure_logging
from trading_agent.premarket.worker import briefing_loop


def main() -> None:
    configure_logging()
    try:
        asyncio.run(briefing_loop())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
