"""Longshot fade strategy for Kalshi multi-outcome events.

Applies the "nothing ever happens" thesis per-outcome: buy NO on any individual
outcome where the ask is at or below ``price_cap``, subject to hard monetary
caps. Entirely mechanical — no runtime judgment gates.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bot.config import LongshotFadeConfig
from bot.kalshi_markets import (
    MultiOutcomeEvent,
    MultiOutcomeMarket,
    discover_multi_outcome_events,
)
from bot.models import LimitOrderIntent, Side

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntryCandidate:
    event_ticker: str
    market: MultiOutcomeMarket
    price: float
    contracts: int
    token_id: str


def select_entry_candidates(
    events: list[MultiOutcomeEvent],
    config: LongshotFadeConfig,
    held_tickers: set[str],
    current_exposure: float,
) -> list[EntryCandidate]:
    """Mechanically pick entry candidates.

    Rules (in order):
    - Event must have at least ``min_outcomes_in_event`` active markets.
    - Outcome's NO ask must be > 0 and <= ``price_cap``.
    - Outcome's close time must fall within the configured resolution window.
    - Per-outcome, per-event, and total-exposure caps all hold.
    - Cheapest (most longshot) outcomes taken first when per-event cap binds.
    """
    now = datetime.now(timezone.utc)
    min_close = now + timedelta(hours=config.min_time_to_resolution_hours)
    max_close = now + timedelta(days=config.max_time_to_resolution_days)

    remaining_total = max(0.0, config.max_total_exposure - current_exposure)
    candidates: list[EntryCandidate] = []

    for event in events:
        active = [m for m in event.markets if m.status == "active"]
        if len(active) < config.min_outcomes_in_event:
            continue

        event_spent = 0.0
        for market in sorted(active, key=lambda m: m.no_ask):
            if market.ticker in held_tickers:
                continue
            if market.no_ask <= 0 or market.no_ask > config.price_cap:
                continue
            if not (min_close <= market.close_time <= max_close):
                continue

            already_committed = sum(c.contracts * c.price for c in candidates)
            capital_for_leg = min(
                config.max_capital_per_outcome,
                config.max_capital_per_event - event_spent,
                remaining_total - already_committed,
            )
            if capital_for_leg < market.no_ask:
                continue

            contracts = int(capital_for_leg // market.no_ask)
            if contracts < 1:
                continue

            candidates.append(
                EntryCandidate(
                    event_ticker=event.event_ticker,
                    market=market,
                    price=market.no_ask,
                    contracts=contracts,
                    token_id=f"{market.ticker}:no",
                )
            )
            event_spent += contracts * market.no_ask

    return candidates


async def scan_once(
    client,
    config: LongshotFadeConfig,
    portfolio,
    *,
    trading_paused: bool,
    discover_fn=discover_multi_outcome_events,
) -> int:
    """Run one scan cycle. Returns number of orders placed."""
    if trading_paused:
        logger.info("trading paused; skipping scan")
        return 0

    events = await discover_fn(client, min_outcomes=config.min_outcomes_in_event)
    held = portfolio.held_market_tickers()
    exposure = portfolio.current_exposure()

    candidates = select_entry_candidates(events, config, held, exposure)
    placed = 0

    for cand in candidates:
        intent = LimitOrderIntent(
            token_id=cand.token_id,
            side=Side.BUY,
            price=cand.price,
            size=float(cand.contracts),
        )
        try:
            fee = client.estimate_fee(cand.price, cand.contracts)
            result = await client.place_limit_order(intent)
            portfolio.record_entry(
                token_id=cand.token_id,
                event_ticker=cand.event_ticker,
                price=cand.price,
                contracts=cand.contracts,
                estimated_fee=fee,
                order_id=result.order_id,
            )
            logger.info(
                "placed longshot fade order",
                extra={
                    "event": cand.event_ticker,
                    "market": cand.market.ticker,
                    "price": cand.price,
                    "contracts": cand.contracts,
                    "fee": fee,
                },
            )
            placed += 1
        except Exception:
            logger.exception(
                "order placement failed", extra={"token_id": cand.token_id}
            )

    return placed


async def run_forever(
    client,
    config: LongshotFadeConfig,
    portfolio,
    get_paused,
    *,
    heartbeat=None,
    discover_fn=discover_multi_outcome_events,
) -> None:
    while True:
        try:
            await scan_once(
                client,
                config,
                portfolio,
                trading_paused=get_paused(),
                discover_fn=discover_fn,
            )
            if heartbeat is not None:
                heartbeat.tick()
        except Exception:
            logger.exception("scan loop iteration failed")
        await asyncio.sleep(config.scan_interval_seconds)
