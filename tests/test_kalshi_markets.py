from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.kalshi_markets import discover_multi_outcome_events


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
                        {
                            "ticker": "KXPRES28-HARRIS",
                            "status": "active",
                            "close_time": "2028-11-05T00:00:00Z",
                            "orderbook": {"yes": [[12, 100]], "no": [[85, 100]]},
                        },
                        {
                            "ticker": "KXPRES28-NEWSOM",
                            "status": "active",
                            "close_time": "2028-11-05T00:00:00Z",
                            "orderbook": {"yes": [[8, 100]], "no": [[90, 100]]},
                        },
                        {
                            "ticker": "KXPRES28-SHAPIRO",
                            "status": "active",
                            "close_time": "2028-11-05T00:00:00Z",
                            "orderbook": {"yes": [[5, 100]], "no": [[94, 100]]},
                        },
                    ],
                },
                {
                    "event_ticker": "BINARY-X",
                    "title": "Binary example",
                    "markets": [
                        {
                            "ticker": "BINARY-X-Y",
                            "status": "active",
                            "close_time": "2026-05-01T00:00:00Z",
                            "orderbook": {"yes": [[50, 100]], "no": [[48, 100]]},
                        }
                    ],
                },
            ]
        }
    )
    events = await discover_multi_outcome_events(client, min_outcomes=3)
    assert len(events) == 1
    assert events[0].event_ticker == "KXPRES28"
    assert len(events[0].markets) == 3
    # No ask derived from YES bid: 100 - 12 = 88 cents = 0.88
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
                        {
                            "ticker": "E-A",
                            "status": "active",
                            "close_time": "2030-01-01T00:00:00Z",
                            "orderbook": {"yes": [[10, 1]], "no": [[89, 1]]},
                        },
                        {
                            "ticker": "E-B",
                            "status": "closed",
                            "close_time": "2030-01-01T00:00:00Z",
                            "orderbook": {},
                        },
                        {
                            "ticker": "E-C",
                            "status": "active",
                            "close_time": "2030-01-01T00:00:00Z",
                            "orderbook": {"yes": [[5, 1]], "no": [[94, 1]]},
                        },
                    ],
                }
            ]
        }
    )
    events = await discover_multi_outcome_events(client, min_outcomes=3)
    # Only 2 active, below min_outcomes=3
    assert events == []


@pytest.mark.asyncio
async def test_discover_fetches_missing_orderbook():
    client = MagicMock()

    calls: list[tuple[str, str]] = []

    async def fake_request(method, path, **kw):
        calls.append((method, path))
        if path == "/events":
            return {
                "events": [
                    {
                        "event_ticker": "E",
                        "title": "t",
                        "markets": [
                            {
                                "ticker": "E-A",
                                "status": "active",
                                "close_time": "2030-01-01T00:00:00Z",
                            },
                            {
                                "ticker": "E-B",
                                "status": "active",
                                "close_time": "2030-01-01T00:00:00Z",
                            },
                            {
                                "ticker": "E-C",
                                "status": "active",
                                "close_time": "2030-01-01T00:00:00Z",
                            },
                        ],
                    }
                ]
            }
        # orderbook fetch
        return {"orderbook": {"yes": [[5, 10]], "no": [[94, 10]]}}

    client._signed_request = fake_request
    events = await discover_multi_outcome_events(client, min_outcomes=3)
    assert len(events) == 1
    assert len(events[0].markets) == 3
    # 3 per-market orderbook calls
    orderbook_calls = [c for c in calls if "orderbook" in c[1]]
    assert len(orderbook_calls) == 3
