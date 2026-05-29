"""
IBKR Gateway smoke test — Phase G.2.1.

Connects to a locally-running IB Gateway (default 127.0.0.1:4002 paper),
fetches account summary, and pulls a delayed-data snapshot for AAPL.

Prerequisites:
- IB Gateway installed + logged into Paper Trading
- API enabled on socket 4002, localhost trusted
- Read-Only API may be ON or OFF (this test doesn't place orders)

Run:
    py -3.14 scripts/ibkr_smoke_test.py
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
from trading_agent.brokers.ibkr.instruments import by_symbol  # noqa: E402


async def main() -> None:
    print("=" * 60)
    print("IBKR Gateway smoke test")
    print("=" * 60)

    client = IBKRClient()
    cfg = client.config
    print(f"Target: {cfg.host}:{cfg.port}  client_id={cfg.client_id}  readonly={cfg.readonly}")
    print()

    print("--- 1. Connect ---")
    await client.connect()
    print(f"  connected={client.connected}")
    print()

    print("--- 2. Account summary ---")
    try:
        summary = await client.account_summary()
        print(f"  account: {summary.get('account')}")
        # Pick a few interesting fields to print
        values = summary.get("values", {})
        for tag in ("NetLiquidation", "TotalCashValue", "BuyingPower", "AvailableFunds"):
            if tag in values:
                v = values[tag]
                print(f"  {tag}: {v['value']} {v['currency']}")
    except Exception as e:
        print(f"  ERROR: {e}")
    print()

    print("--- 3. Qualify + snapshot AAPL ---")
    try:
        spec = by_symbol("AAPL")
        qualified = await client.qualify_contract(spec.contract)
        print(f"  qualified contract: conId={qualified.conId} exchange={qualified.exchange}")
        # Use delayed snapshot — works without market data subscription
        client.ib.reqMarketDataType(3)  # 3 = DELAYED
        ticker = client.ib.reqMktData(qualified, "", snapshot=False, regulatorySnapshot=False)
        # Give IB a moment to fill in the ticker
        for _ in range(10):
            await asyncio.sleep(0.3)
            if ticker.last or ticker.bid or ticker.ask or ticker.close:
                break
        print(f"  AAPL bid={ticker.bid}  ask={ticker.ask}  last={ticker.last}  close={ticker.close}")
        client.ib.cancelMktData(qualified)
    except Exception as e:
        print(f"  ERROR: {e}")
    print()

    print("--- 4. Disconnect ---")
    await client.disconnect()
    print(f"  connected={client.connected}")
    print()
    print("Smoke test complete.")


if __name__ == "__main__":
    asyncio.run(main())
