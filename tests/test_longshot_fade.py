from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.config import LongshotFadeConfig
from bot.kalshi_markets import MultiOutcomeEvent, MultiOutcomeMarket
from bot.strategy.longshot_fade import scan_once, select_entry_candidates


def _cfg(**overrides):
    defaults = dict(
        price_cap=0.10,
        max_capital_per_outcome=15.0,
        max_capital_per_event=30.0,
        max_total_exposure=250.0,
        min_time_to_resolution_hours=24,
        max_time_to_resolution_days=90,
        min_outcomes_in_event=3,
        scan_interval_seconds=30,
    )
    defaults.update(overrides)
    return LongshotFadeConfig(**defaults)


def _market(ticker: str, no_ask_cents: int, hours_out: int = 48) -> MultiOutcomeMarket:
    close = datetime.now(timezone.utc) + timedelta(hours=hours_out)
    # Kalshi orderbook uses YES bids to imply NO asks.
    yes_bid_cents = 100 - no_ask_cents
    return MultiOutcomeMarket(
        ticker=ticker,
        status="active",
        close_time=close,
        orderbook={"yes": [[yes_bid_cents, 100]], "no": [[no_ask_cents - 2, 100]]},
    )


def _event(ticker: str, markets):
    return MultiOutcomeEvent(event_ticker=ticker, title=ticker, markets=tuple(markets))


# ---- select_entry_candidates --------------------------------------------


def test_candidate_below_price_cap_taken():
    ev = _event("E1", [_market("E1-A", 5), _market("E1-B", 8), _market("E1-C", 30)])
    cands = select_entry_candidates([ev], _cfg(), held_tickers=set(), current_exposure=0.0)
    tickers = [c.market.ticker for c in cands]
    assert "E1-A" in tickers
    assert "E1-B" in tickers
    assert "E1-C" not in tickers


def test_skips_already_held():
    ev = _event("E1", [_market("E1-A", 5), _market("E1-B", 5), _market("E1-C", 5)])
    cands = select_entry_candidates([ev], _cfg(), held_tickers={"E1-A"}, current_exposure=0.0)
    assert {c.market.ticker for c in cands} == {"E1-B", "E1-C"}


def test_enforces_per_event_cap():
    ev = _event("E1", [_market(f"E1-{i}", 5) for i in range(10)])
    cands = select_entry_candidates([ev], _cfg(), held_tickers=set(), current_exposure=0.0)
    event_cost = sum(c.contracts * c.price for c in cands)
    assert event_cost <= 30.0 + 1e-9


def test_enforces_total_exposure_cap():
    ev = _event("E1", [_market(f"E1-{i}", 5) for i in range(5)])
    cands = select_entry_candidates(
        [ev], _cfg(max_total_exposure=20.0), held_tickers=set(), current_exposure=15.0
    )
    new_cost = sum(c.contracts * c.price for c in cands)
    assert new_cost <= 5.0 + 1e-9


def test_filters_resolution_window():
    ev = _event(
        "E1",
        [
            _market("E1-A", 5, hours_out=2),
            _market("E1-B", 5, hours_out=48),
            _market("E1-C", 5, hours_out=24 * 200),
        ],
    )
    cands = select_entry_candidates([ev], _cfg(), held_tickers=set(), current_exposure=0.0)
    assert {c.market.ticker for c in cands} == {"E1-B"}


def test_respects_min_outcomes():
    ev = _event("E1", [_market("E1-A", 5), _market("E1-B", 5)])
    cands = select_entry_candidates([ev], _cfg(), held_tickers=set(), current_exposure=0.0)
    assert cands == []


def test_takes_cheapest_first():
    ev = _event(
        "E1",
        [_market("E1-A", 9), _market("E1-B", 3), _market("E1-C", 7)],
    )
    cands = select_entry_candidates([ev], _cfg(), held_tickers=set(), current_exposure=0.0)
    # All under cap, but order should be by ask ascending.
    assert cands[0].market.ticker == "E1-B"


def test_token_id_format():
    ev = _event("E1", [_market("E1-A", 5), _market("E1-B", 5), _market("E1-C", 5)])
    cands = select_entry_candidates([ev], _cfg(), held_tickers=set(), current_exposure=0.0)
    assert cands[0].token_id.endswith(":no")


# ---- scan_once -----------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_once_places_orders():
    ev = _event("E1", [_market("E1-A", 5), _market("E1-B", 5), _market("E1-C", 5)])

    client = MagicMock()
    order_result = MagicMock(order_id="oid-1", status="resting")
    client.place_limit_order = AsyncMock(return_value=order_result)
    client.estimate_fee = MagicMock(return_value=0.10)

    portfolio = MagicMock()
    portfolio.held_market_tickers = MagicMock(return_value=set())
    portfolio.current_exposure = MagicMock(return_value=0.0)
    portfolio.record_entry = MagicMock()

    async def fake_discover(_client, min_outcomes):
        return [ev]

    n = await scan_once(
        client, _cfg(), portfolio, trading_paused=False, discover_fn=fake_discover
    )
    assert n >= 1
    assert client.place_limit_order.await_count >= 1
    assert portfolio.record_entry.call_count >= 1


@pytest.mark.asyncio
async def test_scan_once_no_orders_when_paused():
    client = MagicMock()
    client.place_limit_order = AsyncMock()
    portfolio = MagicMock()

    async def fake_discover(_client, min_outcomes):
        return []

    n = await scan_once(
        client, _cfg(), portfolio, trading_paused=True, discover_fn=fake_discover
    )
    assert n == 0
    client.place_limit_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_once_continues_after_order_error():
    ev = _event("E1", [_market("E1-A", 5), _market("E1-B", 5), _market("E1-C", 5)])

    client = MagicMock()
    client.estimate_fee = MagicMock(return_value=0.10)

    call_count = {"n": 0}

    async def flaky_place(intent):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("kaboom")
        return MagicMock(order_id=f"oid-{call_count['n']}", status="resting")

    client.place_limit_order = flaky_place

    portfolio = MagicMock()
    portfolio.held_market_tickers = MagicMock(return_value=set())
    portfolio.current_exposure = MagicMock(return_value=0.0)
    portfolio.record_entry = MagicMock()

    async def fake_discover(_client, min_outcomes):
        return [ev]

    n = await scan_once(
        client, _cfg(), portfolio, trading_paused=False, discover_fn=fake_discover
    )
    # First call errored, remaining candidates still attempted.
    assert call_count["n"] >= 2
    assert n == call_count["n"] - 1
