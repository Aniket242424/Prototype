"""
End-to-end paper trade test — Phase G.2.2.

Places a 1-share AAPL MKT BUY on the IBKR paper account, waits for fill,
prints the resulting position. This is the "is paper trading actually
working" smoke test.

Prerequisites:
- IB Gateway running, logged in to Paper Trading (DUQ522790)
- Read-Only API UNCHECKED in Gateway settings
- API socket 4002, localhost trusted

Note on market data: Free Trial paper account often returns nan for
live prices, but MKT orders still simulate-fill against IBKR's
delayed/EOD price stream. Submitting works even when ticker data is
empty.

Run:
    py -3.14 scripts/ibkr_paper_trade_test.py
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

from trading_agent.brokers.ibkr import IBKRClient  # noqa: E402
from trading_agent.brokers.ibkr.orders import (  # noqa: E402
    open_positions,
    place_order,
    wait_for_fill,
)


SYMBOL = "AAPL"
QUANTITY = 1
SIDE = "BUY"


async def main() -> None:
    print("=" * 60)
    print("IBKR end-to-end paper trade test")
    print(f"Order: {SIDE} {QUANTITY} {SYMBOL} MKT (paper)")
    print("=" * 60)

    client = IBKRClient()
    await client.connect()
    print(f"connected to {client.config.host}:{client.config.port}")
    print(f"account: {client.ib.managedAccounts()[0]}")
    print()

    print("--- 1. Place order ---")
    placed = await place_order(
        client,
        symbol=SYMBOL,
        side=SIDE,
        quantity=QUANTITY,
        order_type="MKT",
    )
    print(f"order_id={placed.order_id}")
    print(f"status={placed.status}")
    print()

    print("--- 2. Wait for fill (max 30s) ---")
    placed = await wait_for_fill(placed, timeout_sec=30.0)
    print(f"final status:    {placed.status}")
    print(f"filled qty:      {placed.filled_qty}")
    print(f"avg fill price:  ${placed.avg_fill_price:.2f}")
    if placed.is_filled:
        print("✓ FILLED")
    elif placed.is_cancelled:
        print("✗ CANCELLED")
    else:
        print(f"~ pending (status={placed.status})")
    print()

    print("--- 3. Open positions ---")
    positions = await open_positions(client)
    if not positions:
        print("  (no open positions)")
    else:
        for p in positions:
            print(
                f"  {p['symbol']:<8s}  {p['secType']:<6s}  "
                f"qty={p['position']:>6.0f}  avg_cost=${p['avg_cost']:.2f}"
            )
    print()

    await client.disconnect()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
