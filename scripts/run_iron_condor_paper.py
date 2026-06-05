"""
Delta India BTC 0DTE Iron Condor — paper/live runner (Phase H.2).

Mirrors the validated backtest (scripts/backtest_iron_condor_btc.py):
  SELL short call  ~+1.5% OTM
  SELL short put   ~-1.5% OTM
  BUY  long call   ~+4.0% OTM   (caps upside, defines risk)
  BUY  long put    ~-4.0% OTM   (caps downside, defines risk)
held to the next daily settlement (12:00 UTC = 17:30 IST).

WHY TWO SUBCOMMANDS (not a long-lived loop):
  Delta daily options auto-settle at a fixed 12:00 UTC. A 30-day unattended
  test on a restart-prone EC2 box is far more robust as two stateless cron
  invocations than one process that must survive 30 days:

    enter   — select strikes, (paper) price or (live) place the 4 legs,
              persist the open condor to data/iron_condor_state.json
    settle  — read the persisted condor, compute realized P&L at the
              settlement price, append to data/iron_condor_trades.csv,
              clear state, send the Telegram daily summary

  Recommended cron (UTC), run settle THEN enter once per day just after
  12:00 UTC settlement:

    5 12 * * *  cd ~/Trading_Agent && python3 scripts/run_iron_condor_paper.py settle
    6 12 * * *  cd ~/Trading_Agent && python3 scripts/run_iron_condor_paper.py enter

SAFETY GATES (both must be true to send REAL orders):
  - DELTA_PAPER must be 'false'      (defaults to true / paper)
  - LIVE_TRADING must be 'true'      (the project-wide live gate)
  Otherwise every "order" is simulated from live bid/ask and only logged.

CONFIG (.env):
  DELTA_API_KEY / DELTA_API_SECRET        — already set
  DELTA_PAPER=true|false                  — paper (default) vs live
  LIVE_TRADING=true|false                 — project-wide live gate
  DELTA_IC_LOTS=10                        — lots per leg (1 lot = 0.001 BTC)
  DELTA_IC_MAX_RISK_USD=25                — abort if max-loss exceeds this
  DELTA_IC_UNDERLYING=BTC                 — BTC or ETH
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   — daily summary delivery

Run:
  python3 scripts/run_iron_condor_paper.py enter
  python3 scripts/run_iron_condor_paper.py settle
  python3 scripts/run_iron_condor_paper.py status    # show open position, no action
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO_ROOT / ".env")

from trading_agent.brokers.delta.client import DeltaClient  # noqa: E402
from trading_agent.brokers.delta.fees import (  # noqa: E402
    entry_fees_usd,
    settlement_fees_usd,
)

# Telegram alerter is optional — never let its absence break the runner.
try:
    from trading_agent.monitoring.telegram_alerter import alert as _tg_alert
except Exception:  # pragma: no cover
    _tg_alert = None

try:
    from trading_agent.core.logging import get_logger
    log = get_logger(__name__)
except Exception:  # pragma: no cover - allow standalone runs
    # structlog-compatible shim: structured calls pass kwargs (log.info("evt", k=v)).
    # A raw stdlib logger would raise on those, so use an adapter that folds
    # kwargs into the message and never raises.
    class _ShimLogger:
        def _emit(self, level: str, event: str, **kw) -> None:
            extras = " ".join(f"{k}={v}" for k, v in kw.items())
            print(f"[{level}] {event} {extras}".rstrip())

        def info(self, event="", **kw):
            self._emit("info", event, **kw)

        def warning(self, event="", **kw):
            self._emit("warning", event, **kw)

        def error(self, event="", **kw):
            self._emit("error", event, **kw)

        def debug(self, event="", **kw):
            self._emit("debug", event, **kw)

    log = _ShimLogger()


# ============================================================
# Config (env-driven)
# ============================================================
UNDERLYING = os.getenv("DELTA_IC_UNDERLYING", "BTC").upper()
PERP_SYMBOL = f"{UNDERLYING}USD"          # spot reference, e.g. BTCUSD

# Strategy variant — lets us paper-test more than one structure side by side.
# "standard" = ±1.5% short / ±4% wings; "narrow" = ±1% short / ±3% wings.
# Strikes are env-overridable so a cron can run a second variant in parallel.
VARIANT = os.getenv("DELTA_IC_VARIANT", "standard").lower()
SHORT_PCT = float(os.getenv("DELTA_IC_SHORT_PCT", "0.015"))
LONG_PCT = float(os.getenv("DELTA_IC_LONG_PCT", "0.040"))
LOTS = int(os.getenv("DELTA_IC_LOTS", "10"))
MAX_RISK_USD = float(os.getenv("DELTA_IC_MAX_RISK_USD", "25"))
CONTRACT_VALUE_DEFAULT = 0.001            # BTC per lot (read live per product)

# Paper capital + FX (for ₹ display). Delta settles in USDT, so we track USD
# natively and convert to INR for the dashboard.
PAPER_CAPITAL_INR = float(os.getenv("DELTA_IC_CAPITAL_INR", "200000"))
FX_INR_USD = float(os.getenv("DELTA_IC_FX_INR_USD", "84"))
PAPER_CAPITAL_USD = PAPER_CAPITAL_INR / FX_INR_USD

# Safety gates
PAPER = os.getenv("DELTA_PAPER", "true").lower() != "false"
LIVE_GATE = os.getenv("LIVE_TRADING", "false").lower() == "true"
IS_LIVE = (not PAPER) and LIVE_GATE

# "standard" keeps the original file names (preserves the running track's
# history); any other variant gets its own namespaced files.
_SUFFIX = "" if VARIANT == "standard" else f"_{VARIANT}"
STATE_FILE = Path(f"data/iron_condor_state{_SUFFIX}.json")
TRADES_CSV = Path(f"data/iron_condor_trades{_SUFFIX}.csv")
CSV_FIELDS = [
    "entry_ts", "settle_ts", "underlying", "expiry", "mode",
    "spot_entry", "spot_settle", "lots", "contract_value",
    "short_call_k", "short_put_k", "long_call_k", "long_put_k",
    "net_credit_usd", "max_loss_usd", "payoff_usd",
    "gross_pnl_usd", "entry_fees_usd", "settle_fees_usd", "net_pnl_usd",
    "pnl_usd",                       # kept = net_pnl_usd, for backward-compat
    "outcome",
]


# ============================================================
# Helpers
# ============================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def fnum(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


@dataclass
class Leg:
    role: str            # 'short_call' | 'short_put' | 'long_call' | 'long_put'
    side: str            # 'sell' | 'buy'
    option_type: str     # 'call' | 'put'
    product_id: int
    symbol: str
    strike: float
    price_per_btc: float    # entry premium per 1 BTC (bid for sells, ask for buys)
    contract_value: float   # BTC per lot
    tick_size: float = 0.1  # min price increment for limit orders
    order_id: int | None = None
    fill_price: float | None = None


@dataclass
class CondorState:
    entry_ts: str
    underlying: str
    expiry: str               # DDMMYY
    settlement_time: str      # ISO Z from product
    mode: str                 # 'paper' | 'live'
    spot_entry: float
    lots: int
    legs: list[dict]
    net_credit_usd: float
    max_loss_usd: float
    entry_fees_usd: float = 0.0


# ============================================================
# Strike selection
# ============================================================

def pick_strike(strikes: list[float], target: float, prefer: str) -> float:
    """
    prefer='above' -> smallest strike >= target
    prefer='below' -> largest strike <= target
    Falls back to nearest if no strike on the requested side.
    """
    above = sorted(s for s in strikes if s >= target)
    below = sorted((s for s in strikes if s <= target), reverse=True)
    if prefer == "above":
        return above[0] if above else below[0]
    return below[0] if below else above[0]


def build_legs(chain: list[dict], spot: float) -> dict[str, dict]:
    """
    Choose the 4 strikes for the condor from the live chain.
    Returns {role: product_dict} for the 4 legs, or raises ValueError.
    """
    calls = {p["strike"]: p for p in chain if p["option_type"] == "call"}
    puts = {p["strike"]: p for p in chain if p["option_type"] == "put"}
    if not calls or not puts:
        raise ValueError("Chain has no calls or no puts after filtering.")

    call_strikes = list(calls)
    put_strikes = list(puts)

    sc = pick_strike(call_strikes, spot * (1 + SHORT_PCT), "above")
    lc = pick_strike(call_strikes, spot * (1 + LONG_PCT), "above")
    sp = pick_strike(put_strikes, spot * (1 - SHORT_PCT), "below")
    lp = pick_strike(put_strikes, spot * (1 - LONG_PCT), "below")

    # Enforce a valid condor geometry: long wings strictly outside shorts.
    if lc <= sc:
        wider = sorted(s for s in call_strikes if s > sc)
        if not wider:
            raise ValueError(f"No long-call strike above short call {sc}")
        lc = wider[0]
    if lp >= sp:
        wider = sorted((s for s in put_strikes if s < sp), reverse=True)
        if not wider:
            raise ValueError(f"No long-put strike below short put {sp}")
        lp = wider[0]

    # Final geometry sanity: all four strikes distinct, both wings positive,
    # shorts straddle spot. A zero-width wing would make max-loss meaningless.
    if len({sc, sp, lc, lp}) != 4:
        raise ValueError(f"Degenerate condor strikes sc={sc} sp={sp} lc={lc} lp={lp}")
    if (lc - sc) <= 0 or (sp - lp) <= 0:
        raise ValueError(f"Non-positive wing width: call_wing={lc-sc} put_wing={sp-lp}")
    if not (sp < spot < sc):
        raise ValueError(f"Shorts do not straddle spot {spot}: sp={sp} sc={sc}")

    return {
        "short_call": calls[sc],
        "long_call": calls[lc],
        "short_put": puts[sp],
        "long_put": puts[lp],
    }


async def quote_leg(client: DeltaClient, role: str, product: dict) -> Leg:
    """Build a Leg with the entry premium taken conservatively from the order book."""
    side = "sell" if role.startswith("short") else "buy"
    option_type = product["option_type"]
    ticker: dict = {}
    try:
        ticker = await client.get_option_ticker(product["symbol"]) or {}
    except Exception as e:
        log.warning("delta.option_ticker_failed", symbol=product["symbol"], error=str(e))

    # 'quotes' may be present-but-None; coerce to {} before nested access.
    quotes = ticker.get("quotes") or {}
    bid = fnum(ticker.get("best_bid") or ticker.get("bid") or quotes.get("best_bid"))
    ask = fnum(ticker.get("best_ask") or ticker.get("ask") or quotes.get("best_ask"))
    mark = fnum(ticker.get("mark_price") or product.get("mark_price"))

    # Conservative fill assumption: sells fill at bid, buys fill at ask.
    # Fall back to mark if a side of the book is empty.
    if side == "sell":
        price = bid or mark
    else:
        price = ask or mark

    return Leg(
        role=role,
        side=side,
        option_type=option_type,
        product_id=int(product["id"]),
        symbol=product["symbol"],
        strike=float(product["strike"]),
        price_per_btc=fnum(price),
        contract_value=fnum(product.get("contract_value"), CONTRACT_VALUE_DEFAULT),
        tick_size=fnum(product.get("tick_size"), 0.1),
    )


# ============================================================
# Economics
# ============================================================

def compute_economics(legs: dict[str, Leg], lots: int) -> tuple[float, float]:
    """
    Returns (net_credit_usd, max_loss_usd) for the whole position.

    net_credit_per_btc = (short_call + short_put) - (long_call + long_put)
    max_loss_per_btc   = wing_width - net_credit_per_btc   (symmetric condor:
                         call wing = put wing by construction; use the LARGER
                         wing to be conservative if they differ)
    Multiply by contract_value (BTC/lot) and lots for USD.
    """
    cv = fnum(legs["short_call"].contract_value, CONTRACT_VALUE_DEFAULT) or CONTRACT_VALUE_DEFAULT
    credit_per_btc = (
        legs["short_call"].price_per_btc + legs["short_put"].price_per_btc
        - legs["long_call"].price_per_btc - legs["long_put"].price_per_btc
    )
    call_wing = legs["long_call"].strike - legs["short_call"].strike
    put_wing = legs["short_put"].strike - legs["long_put"].strike
    wing = max(call_wing, put_wing)
    max_loss_per_btc = wing - credit_per_btc

    net_credit_usd = credit_per_btc * cv * lots
    max_loss_usd = max_loss_per_btc * cv * lots
    return net_credit_usd, max_loss_usd


def settle_payoff_usd(state: CondorState, spot_settle: float) -> float:
    """
    Realized P&L at expiry given the settlement spot.
    P&L = net_credit + net_payoff, where for each leg at expiry:
      short call: -max(0, S-K)   (we sold)
      short put:  -max(0, K-S)
      long  call: +max(0, S-K)   (we bought)
      long  put:  +max(0, K-S)
    Scaled by contract_value * lots.
    """
    legs = {l["role"]: l for l in state.legs}
    cv = fnum(legs["short_call"].get("contract_value"), CONTRACT_VALUE_DEFAULT) or CONTRACT_VALUE_DEFAULT
    S = spot_settle

    def call_intrinsic(k: float) -> float:
        return max(0.0, S - k)

    def put_intrinsic(k: float) -> float:
        return max(0.0, k - S)

    payoff_per_btc = (
        -call_intrinsic(legs["short_call"]["strike"])
        - put_intrinsic(legs["short_put"]["strike"])
        + call_intrinsic(legs["long_call"]["strike"])
        + put_intrinsic(legs["long_put"]["strike"])
    )
    payoff_usd = payoff_per_btc * cv * state.lots
    return payoff_usd


# ============================================================
# State persistence
# ============================================================

def save_state(state: CondorState) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: never leave a half-written state file if we crash mid-write.
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    os.replace(tmp, STATE_FILE)


def load_state() -> CondorState | None:
    if not STATE_FILE.exists():
        return None
    try:
        raw = json.loads(STATE_FILE.read_text())
        return CondorState(**raw)
    except Exception as e:
        # A corrupt or stale-schema file must NOT wedge the cron forever.
        # Quarantine it so the next cycle can proceed, and surface the problem.
        log.error("ic.state_corrupt", error=str(e))
        try:
            STATE_FILE.replace(STATE_FILE.with_suffix(".corrupt"))
        except Exception:
            pass
        return None


def clear_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def append_trade(row: dict) -> None:
    TRADES_CSV.parent.mkdir(parents=True, exist_ok=True)
    # If an existing file has a DIFFERENT (older) header, migrate it: re-read
    # old rows and rewrite the whole file under the current schema so columns
    # never misalign. Only happens once when the schema changes.
    if TRADES_CSV.exists():
        existing = list(csv.DictReader(open(TRADES_CSV, newline="", encoding="utf-8")))
        existing_header = existing[0].keys() if existing else None
        header_matches = existing_header is not None and set(existing_header) == set(CSV_FIELDS)
        if not header_matches:
            with open(TRADES_CSV, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                w.writeheader()
                for old in existing:
                    w.writerow(old)            # missing new cols -> blank
                w.writerow(row)
            return
    write_header = not TRADES_CSV.exists()
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)


# ============================================================
# Telegram
# ============================================================

async def tg(kind: str, message: str) -> None:
    if _tg_alert is None:
        return
    try:
        await _tg_alert(kind, message)
    except Exception as e:  # never let telegram break the run
        log.warning("telegram.failed", error=str(e))


# ============================================================
# Expiry selection
# ============================================================

def parse_ddmmyy(s: str) -> datetime:
    return datetime(2000 + int(s[4:6]), int(s[2:4]), int(s[0:2]), 12, 0, tzinfo=timezone.utc)


MIN_TIME_TO_SETTLE_HRS = float(os.getenv("DELTA_IC_MIN_TTE_HRS", "2"))


async def choose_expiry(client: DeltaClient) -> str:
    """
    Pick the nearest expiry that is at least MIN_TIME_TO_SETTLE_HRS away, so we
    never sell a contract that is about to settle the same minute we enter.
    """
    from datetime import timedelta
    expiries = await client.list_expiries(UNDERLYING)
    if not expiries:
        raise ValueError(f"No expiries found for {UNDERLYING}")
    cutoff = now_utc() + timedelta(hours=MIN_TIME_TO_SETTLE_HRS)
    future = [e for e in expiries if parse_ddmmyy(e) > cutoff]
    if not future:
        raise ValueError(
            f"No expiry at least {MIN_TIME_TO_SETTLE_HRS}h out "
            f"(nearest candidates: {expiries[:3]})"
        )
    return future[0]


# ============================================================
# ENTER
# ============================================================

async def cmd_enter() -> None:
    if load_state() is not None:
        msg = "Iron Condor: an open position already exists — run 'settle' first. Skipping enter."
        print(msg)
        await tg("info", f"<b>IC skip</b>: {msg}")
        return

    async with DeltaClient.from_env() as client:
        # All setup steps that can legitimately fail (no chain, bad geometry,
        # illiquid strikes) should skip the day cleanly with an alert — never
        # crash the cron.
        try:
            spot = await client.get_spot_price(PERP_SYMBOL)
            expiry = await choose_expiry(client)
            chain = await client.get_option_chain(UNDERLYING, expiry)
            if not chain:
                raise ValueError(f"Empty chain for {UNDERLYING} {expiry}")
            legs_products = build_legs(chain, spot)
            legs: dict[str, Leg] = {}
            for role, product in legs_products.items():
                legs[role] = await quote_leg(client, role, product)
        except Exception as e:
            msg = f"Iron Condor enter SKIPPED: {e}"
            print(msg)
            log.warning("ic.enter_skipped", error=str(e))
            await tg("info", f"<b>IC skip</b>: {msg}")
            return

        # Every leg must have a real, positive premium — otherwise economics and
        # the risk gate are meaningless and could be silently bypassed.
        bad = [r for r, l in legs.items() if l.price_per_btc <= 0]
        if bad:
            msg = (f"Iron Condor ABORT: missing/zero premium on legs {bad} "
                   f"(illiquid or no quote). Skipping.")
            print(msg)
            await tg("error", f"<b>IC abort</b>: {msg}")
            return

        net_credit_usd, max_loss_usd = compute_economics(legs, LOTS)
        fees_in = entry_fees_usd([asdict(l) for l in legs.values()], spot, LOTS)
        net_credit_after_fees = net_credit_usd - fees_in

        # Risk gate (max loss grows by the brokerage we cannot recover)
        if (max_loss_usd + fees_in) > MAX_RISK_USD:
            msg = (f"Iron Condor ABORT: max-loss ${max_loss_usd:.2f} exceeds "
                   f"DELTA_IC_MAX_RISK_USD ${MAX_RISK_USD:.2f}. "
                   f"Reduce DELTA_IC_LOTS (now {LOTS}).")
            print(msg)
            await tg("error", f"<b>IC abort</b>: {msg}")
            return
        if net_credit_after_fees <= 0:
            msg = (f"Iron Condor ABORT: credit ${net_credit_usd:.2f} does not cover "
                   f"entry brokerage ${fees_in:.2f} (net ${net_credit_after_fees:.2f}).")
            print(msg)
            await tg("error", f"<b>IC abort</b>: {msg}")
            return

        mode = "live" if IS_LIVE else "paper"
        settlement_time = legs_products["short_call"].get("settlement_time", "")

        # Place orders (LIVE) or simulate (PAPER)
        if IS_LIVE:
            ok = await place_live_legs(client, legs)
            if not ok:
                # Unwind + alert already handled inside place_live_legs.
                print("Iron Condor LIVE entry aborted — not all legs filled. No state saved.")
                return

        state = CondorState(
            entry_ts=iso(now_utc()),
            underlying=UNDERLYING,
            expiry=expiry,
            settlement_time=settlement_time,
            mode=mode,
            spot_entry=spot,
            lots=LOTS,
            legs=[asdict(l) for l in legs.values()],
            net_credit_usd=net_credit_usd,
            max_loss_usd=max_loss_usd,
            entry_fees_usd=fees_in,
        )
        save_state(state)

        _print_entry(state, legs)
        await tg("trade_entry", _entry_telegram(state, legs))


def _marketable_limit(leg: Leg) -> float:
    """
    A limit that crosses the spread so it actually fills, snapped to tick size.
    BUY  -> round the ask UP a couple ticks (pay a bit more to ensure fill).
    SELL -> round the bid DOWN a couple ticks (accept a bit less to ensure fill).
    Never returns <= 0.
    """
    tick = leg.tick_size if leg.tick_size > 0 else 0.1
    pad = 2 * tick
    if leg.side == "buy":
        raw = leg.price_per_btc + pad
        px = round(round(raw / tick) * tick, 6)
    else:
        raw = max(leg.price_per_btc - pad, tick)
        px = round(round(raw / tick) * tick, 6)
    return max(px, tick)


def _filled_ok(result: dict, want_lots: int) -> bool:
    """True only if the order fully filled all requested lots."""
    state = str(result.get("state", "")).lower()
    size = fnum(result.get("size"), want_lots)
    unfilled = fnum(result.get("unfilled_size"), size)
    filled = size - unfilled
    return state in ("closed", "filled") and filled >= want_lots


async def _flatten_leg(client: DeltaClient, leg: Leg) -> None:
    """Reduce-only close of a leg that DID fill, used to unwind a broken entry."""
    opposite = "buy" if leg.side == "sell" else "sell"
    try:
        await client.place_order(
            product_id=leg.product_id,
            side=opposite,
            size=LOTS,
            order_type="market_order",
            time_in_force="ioc",
            reduce_only=True,
        )
        log.info("ic.leg_flattened", role=leg.role, symbol=leg.symbol)
    except Exception as e:
        log.error("ic.flatten_failed", role=leg.role, error=str(e))


async def place_live_legs(client: DeltaClient, legs: dict[str, Leg]) -> bool:
    """
    Place the 4 legs as REAL orders. Buy the protective WINGS FIRST so we are
    never momentarily short-naked, then sell the income legs.

    Verifies each fill. If ANY leg fails to fully fill, immediately unwinds the
    legs that did fill (reduce-only) and returns False — we refuse to ever hold
    an unbalanced / naked-short position. Returns True only when all 4 legs are
    confirmed filled.
    """
    ordered = ["long_call", "long_put", "short_call", "short_put"]
    filled: list[Leg] = []
    for role in ordered:
        leg = legs[role]
        limit_px = _marketable_limit(leg)
        if limit_px <= 0:
            log.error("ic.leg_bad_limit", role=role, limit=limit_px)
            await _unwind(client, filled)
            return False
        try:
            result = await client.place_order(
                product_id=leg.product_id,
                side=leg.side,
                size=LOTS,
                order_type="limit_order",
                limit_price=limit_px,
                time_in_force="ioc",
            )
        except Exception as e:
            log.error("ic.leg_place_failed", role=role, error=str(e))
            await _unwind(client, filled)
            return False

        leg.order_id = result.get("id")
        leg.fill_price = fnum(result.get("average_fill_price") or result.get("avg_fill_price"))
        ok = _filled_ok(result, LOTS)
        log.info("ic.leg_placed", role=role, order_id=leg.order_id,
                 state=result.get("state"), filled_ok=ok, fill=leg.fill_price)
        if not ok:
            log.error("ic.leg_not_filled", role=role, state=result.get("state"))
            await _unwind(client, filled)
            return False
        filled.append(leg)
    return True


async def _unwind(client: DeltaClient, filled: list[Leg]) -> None:
    """Flatten every leg that filled, in reverse (shorts first), on a broken entry."""
    if not filled:
        return
    log.warning("ic.unwinding", count=len(filled))
    for leg in reversed(filled):
        await _flatten_leg(client, leg)
    await tg("error", "<b>IC LIVE entry FAILED</b> — a leg did not fill; "
                      "filled legs were unwound (reduce-only). No position held.")


# ============================================================
# SETTLE
# ============================================================

async def cmd_settle() -> None:
    state = load_state()
    if state is None:
        print("Iron Condor: no open position to settle.")
        return

    # Only settle once the contract's settlement time has passed.
    settle_dt = None
    if state.settlement_time:
        try:
            settle_dt = datetime.fromisoformat(state.settlement_time.replace("Z", "+00:00"))
        except ValueError:
            settle_dt = parse_ddmmyy(state.expiry)
    else:
        settle_dt = parse_ddmmyy(state.expiry)

    if now_utc() < settle_dt:
        mins = (settle_dt - now_utc()).total_seconds() / 60.0
        print(f"Iron Condor: not yet expired ({mins:.0f} min to settlement). Skipping.")
        return

    async with DeltaClient.from_env() as client:
        # Settlement spot ~ index at 12:00 UTC. We run minutes after, so live
        # spot is a close proxy. Prefer the product's settlement_price if set.
        spot_settle = await _settlement_spot(client, state)

    payoff_usd = settle_payoff_usd(state, spot_settle)
    gross_pnl_usd = state.net_credit_usd + payoff_usd

    # Brokerage: entry fees (recompute if an older state lacks the field) +
    # settlement fees on any ITM leg (OTM legs are free on Delta).
    fees_in = fnum(getattr(state, "entry_fees_usd", 0.0))
    if fees_in <= 0:
        fees_in = entry_fees_usd(state.legs, state.spot_entry, state.lots)
    fees_settle = settlement_fees_usd(state.legs, spot_settle, state.lots)
    net_pnl_usd = gross_pnl_usd - fees_in - fees_settle

    # Outcome label (based on gross — where spot landed vs the shorts)
    inside = (
        _leg_strike(state, "short_put") < spot_settle < _leg_strike(state, "short_call")
    )
    if inside:
        outcome = "MAX_WIN"
    elif -gross_pnl_usd >= state.max_loss_usd * 0.95:
        outcome = "MAX_LOSS"
    else:
        outcome = "PARTIAL"

    row = {
        "entry_ts": state.entry_ts,
        "settle_ts": iso(now_utc()),
        "underlying": state.underlying,
        "expiry": state.expiry,
        "mode": state.mode,
        "spot_entry": round(state.spot_entry, 2),
        "spot_settle": round(spot_settle, 2),
        "lots": state.lots,
        "contract_value": state.legs[0].get("contract_value"),
        "short_call_k": _leg_strike(state, "short_call"),
        "short_put_k": _leg_strike(state, "short_put"),
        "long_call_k": _leg_strike(state, "long_call"),
        "long_put_k": _leg_strike(state, "long_put"),
        "net_credit_usd": round(state.net_credit_usd, 4),
        "max_loss_usd": round(state.max_loss_usd, 4),
        "payoff_usd": round(payoff_usd, 4),
        "gross_pnl_usd": round(gross_pnl_usd, 4),
        "entry_fees_usd": round(fees_in, 4),
        "settle_fees_usd": round(fees_settle, 4),
        "net_pnl_usd": round(net_pnl_usd, 4),
        "pnl_usd": round(net_pnl_usd, 4),   # backward-compat alias = net
        "outcome": outcome,
    }
    append_trade(row)
    clear_state()

    _print_settle(row)
    await tg("daily_summary", _settle_telegram(row))


async def _settlement_spot(client: DeltaClient, state: CondorState) -> float:
    """
    Official expiry settlement price; fall back to current spot if unavailable.

    The settled contract is NO LONGER state='live', so we look it up with
    get_product_by_symbol (no state filter) which is exactly why that helper
    exists. Only fall back to live spot if the official price truly isn't there.
    """
    sc_sym = next((l["symbol"] for l in state.legs if l["role"] == "short_call"), None)
    if sc_sym:
        try:
            prod = await client.get_product_by_symbol(sc_sym)
            if prod:
                sp = fnum(prod.get("settlement_price"))
                if sp > 0:
                    return sp
        except Exception as e:
            log.warning("ic.settlement_price_lookup_failed", error=str(e))
    # Fallback: current spot (we run within minutes of 12:00 UTC settlement).
    spot = await client.get_spot_price(PERP_SYMBOL)
    log.warning("ic.settlement_price_fallback_to_spot", symbol=sc_sym, spot=spot)
    return spot


def _leg_strike(state: CondorState, role: str) -> float:
    return next(l["strike"] for l in state.legs if l["role"] == role)


# ============================================================
# STATUS
# ============================================================

async def cmd_status() -> None:
    state = load_state()
    print("=" * 60)
    print(f"Iron Condor runner — mode: {'LIVE' if IS_LIVE else 'PAPER'}")
    print(f"  DELTA_PAPER={PAPER}  LIVE_TRADING={LIVE_GATE}")
    print(f"  underlying={UNDERLYING}  lots={LOTS}  max_risk=${MAX_RISK_USD}")
    print("=" * 60)
    if state is None:
        print("No open position.")
    else:
        print(f"OPEN condor entered {state.entry_ts}  expiry {state.expiry}")
        print(f"  spot@entry ${state.spot_entry:,.0f}")
        for l in state.legs:
            print(f"  {l['role']:11s} {l['side']:4s} {l['option_type']:4s} "
                  f"K={l['strike']:>8.0f}  {l['symbol']}  @ {l['price_per_btc']}")
        fees_in = fnum(getattr(state, "entry_fees_usd", 0.0))
        print(f"  net credit ${state.net_credit_usd:.2f}   entry fees ${fees_in:.2f}   "
              f"net-of-fees ${state.net_credit_usd - fees_in:.2f}   max loss ${state.max_loss_usd:.2f}")
    # quick connectivity check
    try:
        async with DeltaClient.from_env() as client:
            spot = await client.get_spot_price(PERP_SYMBOL)
            print(f"Live {PERP_SYMBOL} spot: ${spot:,.2f}  (API OK)")
    except Exception as e:
        print(f"API check FAILED: {e}")


# ============================================================
# Pretty-printers
# ============================================================

def _print_entry(state: CondorState, legs: dict[str, Leg]) -> None:
    print("=" * 64)
    print(f"IRON CONDOR ENTERED [{state.mode.upper()}]  {state.underlying} exp {state.expiry}")
    print(f"  spot ${state.spot_entry:,.0f}   lots {state.lots}")
    for role in ("short_call", "long_call", "short_put", "long_put"):
        l = legs[role]
        print(f"  {role:11s} {l.side:4s} K={l.strike:>8.0f}  @ ${l.price_per_btc:>8.2f}/btc  {l.symbol}")
    fees_in = fnum(getattr(state, "entry_fees_usd", 0.0))
    print(f"  GROSS CREDIT ${state.net_credit_usd:.2f}   ENTRY FEES ${fees_in:.2f}   "
          f"NET CREDIT ${state.net_credit_usd - fees_in:.2f}   MAX LOSS ${state.max_loss_usd:.2f}")
    print("=" * 64)


def _entry_telegram(state: CondorState, legs: dict[str, Leg]) -> str:
    tag = "🧪 PAPER" if state.mode == "paper" else "🔴 LIVE"
    lines = [
        f"<b>Iron Condor entered</b> [{VARIANT}] {tag}",
        f"{state.underlying} · exp {state.expiry} · spot ${state.spot_entry:,.0f} · {state.lots} lots",
        "",
        f"SELL call <code>{legs['short_call'].strike:.0f}</code>  /  SELL put <code>{legs['short_put'].strike:.0f}</code>",
        f"BUY  call <code>{legs['long_call'].strike:.0f}</code>  /  BUY  put <code>{legs['long_put'].strike:.0f}</code>",
        "",
        f"Credit <b>${state.net_credit_usd:.2f}</b> − fees ${fnum(getattr(state,'entry_fees_usd',0.0)):.2f} "
        f"= net <b>${state.net_credit_usd - fnum(getattr(state,'entry_fees_usd',0.0)):.2f}</b> · max loss ${state.max_loss_usd:.2f}",
        f"Win if {state.underlying} settles between "
        f"{legs['short_put'].strike:.0f} and {legs['short_call'].strike:.0f}.",
    ]
    return "\n".join(lines)


def _print_settle(row: dict) -> None:
    fees = fnum(row.get("entry_fees_usd")) + fnum(row.get("settle_fees_usd"))
    print("=" * 64)
    print(f"IRON CONDOR SETTLED [{row['mode'].upper()}]  {row['underlying']} exp {row['expiry']}")
    print(f"  spot entry ${row['spot_entry']:,.0f}  ->  settle ${row['spot_settle']:,.0f}")
    print(f"  gross P&L ${fnum(row.get('gross_pnl_usd')):+.2f}  fees ${fees:.2f}  "
          f"NET P&L ${row['net_pnl_usd']:+.2f}   outcome {row['outcome']}")
    print("=" * 64)


def _settle_telegram(row: dict) -> str:
    tag = "🧪 PAPER" if row["mode"] == "paper" else "🔴 LIVE"
    emoji = {"MAX_WIN": "✅", "PARTIAL": "🟡", "MAX_LOSS": "🔴"}.get(row["outcome"], "")
    net = fnum(row.get("net_pnl_usd"))
    fees = fnum(row.get("entry_fees_usd")) + fnum(row.get("settle_fees_usd"))
    sign = "+" if net >= 0 else ""
    net_inr = net * FX_INR_USD
    lines = [
        f"<b>Iron Condor settled</b> [{VARIANT}] {tag} {emoji}",
        f"{row['underlying']} · exp {row['expiry']}",
        f"spot ${row['spot_entry']:,.0f} → <b>${row['spot_settle']:,.0f}</b>",
        f"shorts {row['short_put_k']:.0f}–{row['short_call_k']:.0f}",
        "",
        f"Net P&L <b>{sign}${net:.2f}</b> (₹{net_inr:+,.0f})  ({row['outcome']})",
        f"gross ${fnum(row.get('gross_pnl_usd')):+.2f} − fees ${fees:.2f}",
    ]
    return "\n".join(lines)


# ============================================================
# Main
# ============================================================

async def _amain() -> None:
    parser = argparse.ArgumentParser(description="Delta BTC Iron Condor paper/live runner")
    parser.add_argument("command", choices=["enter", "settle", "status"],
                        help="enter = open condor, settle = close+report, status = show state")
    args = parser.parse_args()

    print(f"[{iso(now_utc())}] iron_condor[{VARIANT}] {args.command}  "
          f"strikes ±{SHORT_PCT*100:.2f}%/±{LONG_PCT*100:.1f}%  "
          f"mode={'LIVE' if IS_LIVE else 'PAPER'}  "
          f"(DELTA_PAPER={PAPER} LIVE_TRADING={LIVE_GATE})")

    if args.command == "enter":
        await cmd_enter()
    elif args.command == "settle":
        await cmd_settle()
    else:
        await cmd_status()


if __name__ == "__main__":
    asyncio.run(_amain())
