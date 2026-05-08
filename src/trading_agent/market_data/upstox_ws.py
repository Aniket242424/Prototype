"""
Upstox v3 Market Data WebSocket client.

Behavior:
- Calls REST `/v3/feed/market-data-feed/authorize` to get a one-time WS URL.
- Connects via standard WebSocket (auth is in URL).
- Sends a JSON `sub` message with the requested mode and instrument keys.
- Receives binary protobuf frames (FeedResponse), decodes them, and yields Tick objects.
- Reconnects with exponential backoff on disconnect; rebuilds subscriptions on reconnect.

Modes supported in Phase 1.1:
- "ltpc"   — last-trade + close (smallest, fastest; sufficient for the 5 underlyings)

Future modes (Phase 1.2):
- "full"   — depth + Greeks + IV + OI (needed for Phase 2 options intel)
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

import websockets

from trading_agent.core.exceptions import MarketDataError
from trading_agent.core.logging import get_logger
from trading_agent.market_data.dtos import FeedFrame, Tick
from trading_agent.market_data.protos import upstox_v3_pb2 as pb
from trading_agent.market_data.upstox_rest import UpstoxRestClient

log = get_logger(__name__)

Mode = Literal["ltpc", "full", "first_level_with_greeks"]


@dataclass
class _WsConfig:
    instrument_keys: tuple[str, ...]
    mode: Mode
    reconnect_initial_sec: float = 1.0
    reconnect_max_sec: float = 60.0
    ping_interval_sec: float = 20.0
    ping_timeout_sec: float = 20.0


def _ts_ms_to_dt(ms: int) -> datetime:
    """Upstox timestamps are epoch-ms in UTC; convert to aware UTC datetime."""
    if ms <= 0:
        return datetime.now(tz=timezone.utc)
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _decode_feed_response(payload: bytes) -> FeedFrame:
    """Decode one binary FeedResponse frame to typed Tick objects."""
    msg = pb.FeedResponse()
    msg.ParseFromString(payload)

    type_name = pb.FeedResponse.Type.Name(msg.type)
    current_ts = _ts_ms_to_dt(msg.currentTs)

    ticks: list[Tick] = []
    for instrument_key, feed in msg.feeds.items():
        which = feed.WhichOneof("FeedUnion")
        if which == "ltpc":
            ltpc = feed.ltpc
            if ltpc.ltp == 0 and ltpc.cp == 0:
                continue  # empty — skip
            ticks.append(Tick(
                instrument_key=instrument_key,
                ts=_ts_ms_to_dt(ltpc.ltt) if ltpc.ltt > 0 else current_ts,
                ltp=Decimal(str(ltpc.ltp)),
                cp=Decimal(str(ltpc.cp)) if ltpc.cp else None,
            ))
        elif which == "firstLevelWithGreeks":
            f = feed.firstLevelWithGreeks
            ticks.append(Tick(
                instrument_key=instrument_key,
                ts=_ts_ms_to_dt(f.ltpc.ltt) if f.ltpc.ltt > 0 else current_ts,
                ltp=Decimal(str(f.ltpc.ltp)),
                cp=Decimal(str(f.ltpc.cp)) if f.ltpc.cp else None,
                bid=Decimal(str(f.firstLevelQuote.bidP)) if f.firstLevelQuote.bidP else None,
                ask=Decimal(str(f.firstLevelQuote.askP)) if f.firstLevelQuote.askP else None,
                bid_qty=int(f.firstLevelQuote.bidQ) if f.firstLevelQuote.bidQ else None,
                ask_qty=int(f.firstLevelQuote.askQ) if f.firstLevelQuote.askQ else None,
            ))
        elif which == "fullFeed":
            ff = feed.fullFeed
            inner = ff.WhichOneof("FullFeedUnion")
            if inner == "marketFF":
                mf = ff.marketFF
                bid = ask = None
                bid_qty = ask_qty = None
                if mf.marketLevel.bidAskQuote:
                    q0 = mf.marketLevel.bidAskQuote[0]
                    bid = Decimal(str(q0.bidP)) if q0.bidP else None
                    ask = Decimal(str(q0.askP)) if q0.askP else None
                    bid_qty = int(q0.bidQ) if q0.bidQ else None
                    ask_qty = int(q0.askQ) if q0.askQ else None
                ticks.append(Tick(
                    instrument_key=instrument_key,
                    ts=_ts_ms_to_dt(mf.ltpc.ltt) if mf.ltpc.ltt > 0 else current_ts,
                    ltp=Decimal(str(mf.ltpc.ltp)),
                    cp=Decimal(str(mf.ltpc.cp)) if mf.ltpc.cp else None,
                    bid=bid, ask=ask, bid_qty=bid_qty, ask_qty=ask_qty,
                    volume=int(mf.vtt) if mf.vtt else None,
                    oi=int(mf.oi) if mf.oi else None,
                ))
            elif inner == "indexFF":
                idx = ff.indexFF
                ticks.append(Tick(
                    instrument_key=instrument_key,
                    ts=_ts_ms_to_dt(idx.ltpc.ltt) if idx.ltpc.ltt > 0 else current_ts,
                    ltp=Decimal(str(idx.ltpc.ltp)),
                    cp=Decimal(str(idx.ltpc.cp)) if idx.ltpc.cp else None,
                ))
    return FeedFrame(feed_type=type_name, current_ts=current_ts, ticks=ticks)


class UpstoxWebSocketClient:
    """
    Long-lived WebSocket consumer. Yields decoded FeedFrame objects.

    Usage:
        client = UpstoxWebSocketClient(rest, instrument_keys=[...], mode="ltpc")
        async for frame in client.frames():
            for tick in frame.ticks:
                ...
    """

    def __init__(
        self,
        rest: UpstoxRestClient,
        instrument_keys: Sequence[str],
        mode: Mode = "ltpc",
        ping_interval_sec: float = 20.0,
    ):
        self._rest = rest
        self._cfg = _WsConfig(
            instrument_keys=tuple(instrument_keys),
            mode=mode,
            ping_interval_sec=ping_interval_sec,
        )
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def frames(self) -> AsyncIterator[FeedFrame]:
        backoff = self._cfg.reconnect_initial_sec
        while not self._stop.is_set():
            try:
                authz = await self._rest.authorize_ws()
                log.info("ws.connecting", url_prefix=authz.redirect_uri[:40])
                async with websockets.connect(
                    authz.redirect_uri,
                    ping_interval=self._cfg.ping_interval_sec,
                    ping_timeout=self._cfg.ping_timeout_sec,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    await self._subscribe(ws)
                    backoff = self._cfg.reconnect_initial_sec  # reset on successful connect
                    async for frame in self._read_frames(ws):
                        yield frame
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._stop.is_set():
                    break
                log.warning("ws.disconnected", error=str(e), reconnect_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._cfg.reconnect_max_sec)

    async def _subscribe(self, ws) -> None:
        sub = {
            "guid": str(uuid.uuid4()),
            "method": "sub",
            "data": {
                "mode": self._cfg.mode,
                "instrumentKeys": list(self._cfg.instrument_keys),
            },
        }
        await ws.send(json.dumps(sub).encode("utf-8"))
        log.info("ws.subscribed", mode=self._cfg.mode, count=len(self._cfg.instrument_keys))

    async def _read_frames(self, ws) -> AsyncIterator[FeedFrame]:
        async for raw in ws:
            if self._stop.is_set():
                break
            if isinstance(raw, str):
                # Upstox occasionally sends JSON status messages — log and skip.
                log.debug("ws.text_frame", body=raw[:200])
                continue
            try:
                frame = _decode_feed_response(raw)
            except Exception as e:
                log.error("ws.decode_failed", error=str(e), bytes=len(raw))
                raise MarketDataError(f"Failed to decode WS frame: {e}") from e
            yield frame
