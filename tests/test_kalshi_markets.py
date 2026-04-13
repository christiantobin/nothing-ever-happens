from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.kalshi_markets import discover_multi_outcome_events


def _make_market(ticker, *, no_ask="0.08", no_bid_size="100", close_time="2030-01-01T00:00:00Z", status="active"):
    return {
        "ticker": ticker,
        "status": status,
        "close_time": close_time,
        "no_ask_dollars": no_ask,
        "no_bid_size_fp": no_bid_size,
    }


@pytest.mark.asyncio
async def test_discover_filters_by_outcome_count():
    client = MagicMock()
    client._signed_request = AsyncMock(
        return_value={
            "events": [
                {
                    "event_ticker": "KXPRES28",
                    "title": "2028 President",
                    "markets": [
                        _make_market("KXPRES28-HARRIS", no_ask="0.88"),
                        _make_market("KXPRES28-NEWSOM", no_ask="0.92"),
                        _make_market("KXPRES28-SHAPIRO", no_ask="0.95"),
                    ],
                },
                {
                    "event_ticker": "BINARY-X",
                    "title": "Binary example",
                    "markets": [_make_market("BINARY-X-Y", no_ask="0.52")],
                },
            ],
            "cursor": "",
        }
    )
    events = await discover_multi_outcome_events(client, min_outcomes=3)
    assert len(events) == 1
    assert events[0].event_ticker == "KXPRES28"
    assert len(events[0].markets) == 3
    assert events[0].markets[0].no_ask == pytest.approx(0.88)


@pytest.mark.asyncio
async def test_discover_skips_inactive_markets():
    client = MagicMock()
    client._signed_request = AsyncMock(
        return_value={
            "events": [
                {
                    "event_ticker": "E",
                    "title": "t",
                    "markets": [
                        _make_market("E-A", status="active"),
                        _make_market("E-B", status="closed"),
                        _make_market("E-C", status="active"),
                    ],
                }
            ],
            "cursor": "",
        }
    )
    events = await discover_multi_outcome_events(client, min_outcomes=3)
    assert events == []


@pytest.mark.asyncio
async def test_discover_paginates_to_cursor_exhaustion():
    client = MagicMock()

    call_count = {"n": 0}
    pages = [
        {
            "events": [
                {
                    "event_ticker": "E1",
                    "title": "t",
                    "markets": [
                        _make_market("E1-A"),
                        _make_market("E1-B"),
                        _make_market("E1-C"),
                    ],
                }
            ],
            "cursor": "next-1",
        },
        {
            "events": [
                {
                    "event_ticker": "E2",
                    "title": "t",
                    "markets": [
                        _make_market("E2-A"),
                        _make_market("E2-B"),
                        _make_market("E2-C"),
                    ],
                }
            ],
            "cursor": "",
        },
    ]

    async def fake(method, path, **kw):
        idx = call_count["n"]
        call_count["n"] += 1
        return pages[idx]

    client._signed_request = fake
    events = await discover_multi_outcome_events(client, min_outcomes=3)
    assert {e.event_ticker for e in events} == {"E1", "E2"}
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_discover_skips_markets_missing_price_or_close_time():
    client = MagicMock()
    client._signed_request = AsyncMock(
        return_value={
            "events": [
                {
                    "event_ticker": "E",
                    "title": "t",
                    "markets": [
                        _make_market("E-A", no_ask=None),
                        _make_market("E-B", close_time=None),
                        _make_market("E-C"),
                        _make_market("E-D"),
                        _make_market("E-E"),
                    ],
                }
            ],
            "cursor": "",
        }
    )
    events = await discover_multi_outcome_events(client, min_outcomes=3)
    assert len(events) == 1
    assert {m.ticker for m in events[0].markets} == {"E-C", "E-D", "E-E"}
