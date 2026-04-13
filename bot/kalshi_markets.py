"""Discover Kalshi events with multi-outcome markets.

Analogue of bot/standalone_markets.py shaped for Kalshi's event→market
hierarchy. A multi-outcome event is an event with at least ``min_outcomes``
active markets.

Uses inline pricing fields on each nested market (``no_ask_dollars``,
``no_bid_size_fp``) — Kalshi's ``/events?with_nested_markets=true`` does not
embed a full orderbook, only the best-of-book values, which is all the
longshot-fade strategy needs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MultiOutcomeMarket:
    ticker: str
    status: str
    close_time: datetime
    no_ask: float
    no_bid_size: int
    # Kept for backward compatibility with any external caller; always empty
    # for events-endpoint markets since the full orderbook is not inlined.
    orderbook: dict[str, Any]


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


def _parse_dollars(value: Any) -> float | None:
    """Parse Kalshi ``*_dollars`` fields which may be strings or numbers."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_size(value: Any) -> int:
    if value is None:
        return 0
    try:
        # Kalshi's fixed-point size fields (``*_size_fp``) are decimal strings in
        # "contract units × 10^-?" form; for integer contracts they round to ints.
        return int(float(value))
    except (TypeError, ValueError):
        return 0


async def discover_multi_outcome_events(
    client,
    min_outcomes: int = 3,
    *,
    max_pages: int = 5,
) -> list[MultiOutcomeEvent]:
    """Fetch open Kalshi events with at least ``min_outcomes`` active markets.

    Paginates up to ``max_pages`` (Kalshi defaults to 100 events per page;
    500 is plenty of demo surface for a scan). Events without enough active
    markets are skipped. Uses inline market fields — no per-market follow-up
    fetches.
    """
    events: list[MultiOutcomeEvent] = []
    cursor: str | None = None

    for _ in range(max_pages):
        params: dict[str, Any] = {"status": "open", "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        resp = await client._signed_request("GET", "/events", params=params)

        for ev in resp.get("events", []):
            parsed = _parse_event(ev, min_outcomes=min_outcomes)
            if parsed is not None:
                events.append(parsed)

        cursor = resp.get("cursor") or None
        if not cursor:
            break

    return events


def _parse_event(
    ev: dict[str, Any],
    *,
    min_outcomes: int,
) -> MultiOutcomeEvent | None:
    raw_markets = [m for m in ev.get("markets", []) if m.get("status") == "active"]
    if len(raw_markets) < min_outcomes:
        return None

    markets: list[MultiOutcomeMarket] = []
    for m in raw_markets:
        close_time = _parse_close_time(m.get("close_time"))
        if close_time is None:
            continue
        no_ask = _parse_dollars(m.get("no_ask_dollars"))
        if no_ask is None:
            continue
        markets.append(
            MultiOutcomeMarket(
                ticker=m["ticker"],
                status=m["status"],
                close_time=close_time,
                no_ask=no_ask,
                no_bid_size=_parse_size(m.get("no_bid_size_fp")),
                orderbook={"yes": [], "no": []},
            )
        )

    if len(markets) < min_outcomes:
        return None

    return MultiOutcomeEvent(
        event_ticker=ev["event_ticker"],
        title=ev.get("title", ev["event_ticker"]),
        markets=tuple(markets),
    )
