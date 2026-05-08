"""
Options Intelligence Engine.

Reads the latest `options_chain_snapshots` row per underlying, computes:
  - ATM strike + ATM CE/PE IV
  - IV rank (30-day) and IV percentile (30-day)
  - IV skew = put_iv - call_iv at ATM
  - PCR (OI and volume)
  - Max pain strike
  - OI buildup classification on ATM CE and PE (vs previous snapshot)
  - ATM bid/ask spread (bps of mid)
  - Total gamma exposure proxy

Persists nothing of its own (the snapshot is already in
options_chain_snapshots). Publishes the derived intel summary on
`intel:{underlying}` and caches it in Redis for the Opportunity engine.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

import orjson
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from trading_agent.core.logging import get_logger
from trading_agent.core.time_utils import now_ist
from trading_agent.infrastructure.models import OptionsChainSnapshotRow
from trading_agent.regime.dtos import OIBuildup, OptionsIntel

log = get_logger(__name__)

CHAN_INTEL = "intel:{underlying}"


# ---------------- Pure compute helpers ----------------

def _safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _atm_strike_index(strikes: list[dict], spot: float) -> int:
    """Index into strikes[] of the strike closest to spot."""
    if not strikes:
        return -1
    best_idx = 0
    best_diff = abs(float(strikes[0]["strike_price"]) - spot)
    for i in range(1, len(strikes)):
        d = abs(float(strikes[i]["strike_price"]) - spot)
        if d < best_diff:
            best_diff = d
            best_idx = i
    return best_idx


def _max_pain(strikes: list[dict]) -> float | None:
    """
    Max pain = strike at which total option WRITER payoff is minimized
    (i.e. the strike at which the most premium expires worthless).

    For each candidate expiry strike K:
        pain = sum over all CE strikes c of OI_c * max(0, K - c)
             + sum over all PE strikes p of OI_p * max(0, p - K)
    Min argmin → max pain.
    """
    if not strikes:
        return None
    candidates = []
    for s in strikes:
        K = _safe_float(s.get("strike_price"))
        if K is None:
            continue
        candidates.append(K)
    if not candidates:
        return None

    # Pre-extract for speed
    ce_data = []
    pe_data = []
    for s in strikes:
        K = _safe_float(s.get("strike_price"))
        if K is None:
            continue
        ce_oi = _safe_float((s.get("call_options") or {}).get("market_data", {}).get("oi")) or 0
        pe_oi = _safe_float((s.get("put_options") or {}).get("market_data", {}).get("oi")) or 0
        ce_data.append((K, ce_oi))
        pe_data.append((K, pe_oi))

    best_K = None
    best_pain = float("inf")
    for K in candidates:
        ce_pain = sum(oi * max(0, K - k) for k, oi in ce_data)
        pe_pain = sum(oi * max(0, k - K) for k, oi in pe_data)
        total = ce_pain + pe_pain
        if total < best_pain:
            best_pain = total
            best_K = K
    return best_K


def _spread_bps(market_data: dict) -> float | None:
    """Spread in basis points of mid. None if data missing."""
    bid = _safe_float(market_data.get("bid_price"))
    ask = _safe_float(market_data.get("ask_price"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return ((ask - bid) / mid) * 10000


def _classify_buildup(price_change_pct: float, oi_change_pct: float) -> OIBuildup:
    """
    Standard interpretation:
      price up  + OI up   → long buildup (fresh longs)
      price down + OI up  → short buildup (fresh shorts)
      price up  + OI down → short covering
      price down + OI down → long unwinding
      else                 → neutral
    """
    THRESHOLD = 0.5  # at least 0.5% move in either dimension
    if abs(price_change_pct) < THRESHOLD and abs(oi_change_pct) < THRESHOLD:
        label = "neutral"
    elif price_change_pct > 0 and oi_change_pct > 0:
        label = "long_buildup"
    elif price_change_pct < 0 and oi_change_pct > 0:
        label = "short_buildup"
    elif price_change_pct > 0 and oi_change_pct < 0:
        label = "short_covering"
    elif price_change_pct < 0 and oi_change_pct < 0:
        label = "long_unwinding"
    else:
        label = "neutral"
    return OIBuildup(label=label, price_change_pct=price_change_pct, oi_change_pct=oi_change_pct)


def _gamma_exposure_proxy(strikes: list[dict]) -> float | None:
    """
    Sum of |gamma| × (CE_OI + PE_OI) across strikes.
    Approximate dealer gamma exposure — high values can amplify intraday moves.
    """
    total = 0.0
    found = False
    for s in strikes:
        co = (s.get("call_options") or {})
        po = (s.get("put_options") or {})
        for side in (co, po):
            md = side.get("market_data") or {}
            grk = side.get("option_greeks") or {}
            gamma = _safe_float(grk.get("gamma"))
            oi = _safe_float(md.get("oi"))
            if gamma is None or oi is None:
                continue
            total += abs(gamma) * oi
            found = True
    return total if found else None


# ---------------- IV history helpers ----------------

async def _iv_history_30d(
    session_factory: async_sessionmaker, underlying: str
) -> list[float]:
    """Fetch the last ~30 days of ATM IV samples from past chain snapshots."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
    samples: list[float] = []
    async with session_factory() as session:
        rows = (await session.execute(
            select(OptionsChainSnapshotRow)
            .where(OptionsChainSnapshotRow.underlying == underlying)
            .where(OptionsChainSnapshotRow.ts >= cutoff)
            .order_by(OptionsChainSnapshotRow.ts.asc())
        )).scalars().all()
    for row in rows:
        chain = row.chain or {}
        strikes = chain.get("strikes") or []
        spot = _safe_float(chain.get("underlying_spot"))
        if spot is None or not strikes:
            continue
        idx = _atm_strike_index(strikes, spot)
        if idx < 0:
            continue
        s = strikes[idx]
        co_iv = _safe_float((s.get("call_options") or {}).get("option_greeks", {}).get("iv"))
        po_iv = _safe_float((s.get("put_options") or {}).get("option_greeks", {}).get("iv"))
        if co_iv and po_iv:
            samples.append((co_iv + po_iv) / 2)
        elif co_iv:
            samples.append(co_iv)
        elif po_iv:
            samples.append(po_iv)
    return samples


def _iv_rank_pct(history: list[float], current: float) -> tuple[float | None, float | None]:
    """Returns (iv_rank_30d, iv_percentile_30d) or (None, None) if not enough history."""
    if len(history) < 5 or current is None:
        return (None, None)
    lo = min(history)
    hi = max(history)
    rank = (current - lo) / (hi - lo) if hi > lo else None
    below = sum(1 for v in history if v < current)
    pct = below / len(history)
    return (rank, pct)


# ---------------- Engine ----------------

class OptionsIntelEngine:
    def __init__(self, session_factory: async_sessionmaker, redis: Redis):
        self._session_factory = session_factory
        self._redis = redis
        self._latest: dict[str, OptionsIntel] = {}
        self._previous_atm_oi: dict[str, dict[str, float]] = {}
        # Map underlying -> last ATM CE/PE OI + ATM premium for buildup detection.

    @property
    def latest(self) -> dict[str, OptionsIntel]:
        return self._latest

    async def evaluate(self, underlying: str) -> OptionsIntel | None:
        """Read latest chain snapshot for `underlying`, compute intel, persist+publish."""
        async with self._session_factory() as session:
            row = (await session.execute(
                select(OptionsChainSnapshotRow)
                .where(OptionsChainSnapshotRow.underlying == underlying)
                .order_by(OptionsChainSnapshotRow.ts.desc())
                .limit(1)
            )).scalar_one_or_none()
        if row is None:
            return None
        chain = row.chain or {}
        strikes = chain.get("strikes") or []
        spot = _safe_float(chain.get("underlying_spot"))
        expiry_iso = chain.get("expiry")
        if spot is None or not strikes or expiry_iso is None:
            return None

        idx = _atm_strike_index(strikes, spot)
        atm = strikes[idx]
        atm_strike = float(atm["strike_price"])
        co = atm.get("call_options") or {}
        po = atm.get("put_options") or {}
        co_md = co.get("market_data") or {}
        po_md = po.get("market_data") or {}
        co_grk = co.get("option_greeks") or {}
        po_grk = po.get("option_greeks") or {}

        atm_ce_iv = _safe_float(co_grk.get("iv"))
        atm_pe_iv = _safe_float(po_grk.get("iv"))
        iv_skew = (atm_pe_iv - atm_ce_iv) if (atm_ce_iv is not None and atm_pe_iv is not None) else None

        # IV rank/percentile from history
        history = await _iv_history_30d(self._session_factory, underlying)
        atm_iv_avg = None
        if atm_ce_iv is not None and atm_pe_iv is not None:
            atm_iv_avg = (atm_ce_iv + atm_pe_iv) / 2
        elif atm_ce_iv is not None:
            atm_iv_avg = atm_ce_iv
        elif atm_pe_iv is not None:
            atm_iv_avg = atm_pe_iv
        iv_rank, iv_pct = _iv_rank_pct(history, atm_iv_avg) if atm_iv_avg is not None else (None, None)

        # PCR
        total_call_oi = sum(
            int(_safe_float((s.get("call_options") or {}).get("market_data", {}).get("oi")) or 0)
            for s in strikes
        )
        total_put_oi = sum(
            int(_safe_float((s.get("put_options") or {}).get("market_data", {}).get("oi")) or 0)
            for s in strikes
        )
        total_call_vol = sum(
            int(_safe_float((s.get("call_options") or {}).get("market_data", {}).get("volume")) or 0)
            for s in strikes
        )
        total_put_vol = sum(
            int(_safe_float((s.get("put_options") or {}).get("market_data", {}).get("volume")) or 0)
            for s in strikes
        )
        pcr_oi = (total_put_oi / total_call_oi) if total_call_oi > 0 else None
        pcr_vol = (total_put_vol / total_call_vol) if total_call_vol > 0 else None

        # Max pain
        mp = _max_pain(strikes)

        # ATM buildup vs previous snapshot
        atm_ce_buildup = atm_pe_buildup = None
        prev = self._previous_atm_oi.get(underlying)
        if prev:
            ce_oi_now = _safe_float(co_md.get("oi")) or 0
            pe_oi_now = _safe_float(po_md.get("oi")) or 0
            ce_ltp_now = _safe_float(co_md.get("ltp")) or 0
            pe_ltp_now = _safe_float(po_md.get("ltp")) or 0
            if prev.get("ce_oi") and prev.get("ce_ltp"):
                price_chg = (ce_ltp_now - prev["ce_ltp"]) / prev["ce_ltp"] * 100 if prev["ce_ltp"] > 0 else 0
                oi_chg = (ce_oi_now - prev["ce_oi"]) / prev["ce_oi"] * 100 if prev["ce_oi"] > 0 else 0
                atm_ce_buildup = _classify_buildup(price_chg, oi_chg)
            if prev.get("pe_oi") and prev.get("pe_ltp"):
                price_chg = (pe_ltp_now - prev["pe_ltp"]) / prev["pe_ltp"] * 100 if prev["pe_ltp"] > 0 else 0
                oi_chg = (pe_oi_now - prev["pe_oi"]) / prev["pe_oi"] * 100 if prev["pe_oi"] > 0 else 0
                atm_pe_buildup = _classify_buildup(price_chg, oi_chg)

        self._previous_atm_oi[underlying] = {
            "ce_oi": _safe_float(co_md.get("oi")) or 0,
            "pe_oi": _safe_float(po_md.get("oi")) or 0,
            "ce_ltp": _safe_float(co_md.get("ltp")) or 0,
            "pe_ltp": _safe_float(po_md.get("ltp")) or 0,
        }

        # Spreads at ATM
        ce_spread = _spread_bps(co_md)
        pe_spread = _spread_bps(po_md)

        # Gamma proxy
        gamma_exp = _gamma_exposure_proxy(strikes)

        intel = OptionsIntel(
            underlying=underlying,
            ts=now_ist(),
            expiry=date.fromisoformat(expiry_iso),
            spot=spot,
            atm_strike=atm_strike,
            atm_call_iv=atm_ce_iv,
            atm_put_iv=atm_pe_iv,
            iv_rank_30d=iv_rank,
            iv_percentile_30d=iv_pct,
            iv_skew=iv_skew,
            total_call_oi=total_call_oi,
            total_put_oi=total_put_oi,
            pcr_oi=pcr_oi,
            pcr_volume=pcr_vol,
            max_pain_strike=mp,
            atm_call_buildup=atm_ce_buildup,
            atm_put_buildup=atm_pe_buildup,
            atm_call_spread_bps=ce_spread,
            atm_put_spread_bps=pe_spread,
            total_gamma_exposure=gamma_exp,
        )
        self._latest[underlying] = intel
        await self._publish(intel)
        return intel

    async def _publish(self, intel: OptionsIntel) -> None:
        channel = CHAN_INTEL.format(underlying=intel.underlying)
        payload = orjson.dumps({
            "underlying": intel.underlying,
            "ts": intel.ts.isoformat(),
            "spot": intel.spot,
            "atm_strike": intel.atm_strike,
            "atm_call_iv": intel.atm_call_iv,
            "atm_put_iv": intel.atm_put_iv,
            "iv_rank_30d": intel.iv_rank_30d,
            "iv_percentile_30d": intel.iv_percentile_30d,
            "iv_skew": intel.iv_skew,
            "pcr_oi": intel.pcr_oi,
            "pcr_volume": intel.pcr_volume,
            "max_pain_strike": intel.max_pain_strike,
            "atm_call_spread_bps": intel.atm_call_spread_bps,
            "atm_put_spread_bps": intel.atm_put_spread_bps,
            "total_gamma_exposure": intel.total_gamma_exposure,
            "atm_call_buildup": intel.atm_call_buildup.label if intel.atm_call_buildup else None,
            "atm_put_buildup": intel.atm_put_buildup.label if intel.atm_put_buildup else None,
        })
        await self._redis.publish(channel, payload)
        await self._redis.set(f"intel:{intel.underlying}:latest", payload, ex=120)
