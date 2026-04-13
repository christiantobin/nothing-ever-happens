"""Discover Kalshi events with multi-outcome markets.

Analogous to bot/standalone_markets.py but shaped for Kalshi's event→market
hierarchy. A multi-outcome event is an event with at least ``min_outcomes``
active markets.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MultiOutcomeMarket:
    ticker: str
    status: str
    close_time: datetime
    orderbook: dict[str, Any]

    @property
    def no_ask(self) -> float:
        """Best NO ask derived from YES bids. Returns 1.0 if the YES side is empty."""
        yes_bids = self.orderbook.get("yes") or []
        yes_best_bid_cents = yes_bids[0][0] if yes_bids else 0
        return (100 - yes_best_bid_cents) / 100.0

    @property
    def top_of_book_no_size(self) -> int:
        no_bids = self.orderbook.get("no") or []
        return int(no_bids[0][1]) if no_bids else 0


@dataclass(frozen=True)
class MultiOutcomeEvent:
    event_ticker: str
    title: str
    markets: tuple[MultiOutcomeMarket, ...]


def _parse_close_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def discover_multi_outcome_events(
    client,
    min_outcomes: int = 3,
) -> list[MultiOutcomeEvent]:
    """Fetch open Kalshi events with at least ``min_outcomes`` active markets.

    Uses the client's ``_signed_request`` to hit ``GET /events?with_nested_markets=true``.
    If an event has nested markets without orderbooks, a second request per market
    fetches the orderbook. Events without enough active markets are skipped.
    """
    resp = await client._signed_request(
        "GET",
        "/events",
        params={"status": "open", "with_nested_markets": "true"},
    )
    events: list[MultiOutcomeEvent] = []

    for ev in resp.get("events", []):
        raw_markets = [m for m in ev.get("markets", []) if m.get("status") == "active"]
        if len(raw_markets) < min_outcomes:
            continue

        markets: list[MultiOutcomeMarket] = []
        for m in raw_markets:
            close_time = _parse_close_time(m.get("close_time"))
            if close_time is None:
                continue
            orderbook = m.get("orderbook")
            if not orderbook:
                orderbook = await _fetch_orderbook(client, m["ticker"])
            markets.append(
                MultiOutcomeMarket(
                    ticker=m["ticker"],
                    status=m["status"],
                    close_time=close_time,
                    orderbook=orderbook or {"yes": [], "no": []},
                )
            )

        if len(markets) < min_outcomes:
            continue

        events.append(
            MultiOutcomeEvent(
                event_ticker=ev["event_ticker"],
                title=ev.get("title", ev["event_ticker"]),
                markets=tuple(markets),
            )
        )

    return events


async def _fetch_orderbook(client, market_ticker: str) -> dict[str, Any]:
    try:
        resp = await client._signed_request(
            "GET", f"/markets/{market_ticker}/orderbook"
        )
    except Exception:
        return {"yes": [], "no": []}
    return resp.get("orderbook") or {"yes": [], "no": []}
