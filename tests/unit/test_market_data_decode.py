"""
Tests for the protobuf -> Tick decoder.

We construct a known FeedResponse bytestring, decode it, and assert the result.
This catches any drift between our .proto and the live Upstox v3 schema.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from trading_agent.market_data.protos import upstox_v3_pb2 as pb
from trading_agent.market_data.upstox_ws import _decode_feed_response


def _build_ltpc_feed_response() -> bytes:
    fr = pb.FeedResponse()
    fr.type = pb.FeedResponse.Type.live_feed
    fr.currentTs = 1715202000000  # arbitrary epoch-ms

    feed = fr.feeds["NSE_INDEX|Nifty 50"]
    feed.requestMode = pb.Feed.RequestMode.LTPC
    feed.ltpc.ltp = 22500.55
    feed.ltpc.ltt = 1715202001000
    feed.ltpc.ltq = 1
    feed.ltpc.cp = 22480.10

    return fr.SerializeToString()


def test_decode_ltpc_frame_yields_one_tick():
    payload = _build_ltpc_feed_response()
    frame = _decode_feed_response(payload)

    assert frame.feed_type == "live_feed"
    assert len(frame.ticks) == 1

    tick = frame.ticks[0]
    assert tick.instrument_key == "NSE_INDEX|Nifty 50"
    assert tick.ltp == Decimal("22500.55")
    assert tick.cp == Decimal("22480.1")
    assert tick.bid is None
    assert tick.ask is None


def test_decode_empty_ltpc_skipped():
    """A feed with all-zero LTPC should be skipped (Upstox sends these on subscribe)."""
    fr = pb.FeedResponse()
    fr.type = pb.FeedResponse.Type.initial_feed
    fr.currentTs = 1715202000000
    feed = fr.feeds["NSE_INDEX|Nifty 50"]
    feed.requestMode = pb.Feed.RequestMode.LTPC
    # leave ltp/cp at default 0

    frame = _decode_feed_response(fr.SerializeToString())
    assert frame.feed_type == "initial_feed"
    assert frame.ticks == []


def test_decode_first_level_with_greeks():
    fr = pb.FeedResponse()
    fr.type = pb.FeedResponse.Type.live_feed
    fr.currentTs = 1715202000000

    feed = fr.feeds["NSE_FO|54321"]
    feed.requestMode = pb.Feed.RequestMode.FIRST_LEVEL_WITH_GREEKS
    f = feed.firstLevelWithGreeks
    f.ltpc.ltp = 125.50
    f.ltpc.ltt = 1715202001000
    f.firstLevelQuote.bidP = 125.20
    f.firstLevelQuote.askP = 125.80
    f.firstLevelQuote.bidQ = 250
    f.firstLevelQuote.askQ = 175

    frame = _decode_feed_response(fr.SerializeToString())
    assert len(frame.ticks) == 1
    t = frame.ticks[0]
    assert t.instrument_key == "NSE_FO|54321"
    assert t.ltp == Decimal("125.5")
    assert t.bid == Decimal("125.2")
    assert t.ask == Decimal("125.8")
    assert t.bid_qty == 250
    assert t.ask_qty == 175
