"""Periodic sync of Kalshi portfolio state for the dashboard.

Polls Kalshi's portfolio endpoint and translates positions into the
``PortfolioSnapshot`` shape the existing dashboard consumes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from bot.portfolio_state import PortfolioSnapshot, PortfolioState, PositionSnapshot

logger = logging.getLogger(__name__)


async def run_portfolio_sync(
    client,
    portfolio_state: PortfolioState,
    *,
    longshot_portfolio=None,
    balance_getter=None,
    poll_interval_seconds: float = 15.0,
) -> None:
    """Poll Kalshi portfolio periodically and publish snapshots to the dashboard."""
    last_cash: float | None = None
    while True:
        try:
            positions = await _fetch_positions(client)
            if balance_getter is not None:
                try:
                    last_cash = await balance_getter()
                except Exception:
                    logger.debug("balance fetch failed", exc_info=True)

            now_us = int(time.time() * 1_000_000)
            now_ts = time.time()
            snapshots = [_to_snapshot(p) for p in positions if p.get("position", 0) > 0]
            portfolio_state.update(
                updated_at_us=now_us,
                monitored_markets=len(positions),
                eligible_markets=len(snapshots),
                in_range_markets=len(snapshots),
                positions=snapshots,
                cash_balance=last_cash,
                last_market_refresh_ts=now_ts,
                last_position_sync_ts=now_ts,
                last_price_cycle_ts=now_ts,
                last_error="",
            )
        except Exception as exc:
            logger.exception("portfolio sync failed")
            try:
                snapshot = portfolio_state.snapshot()
                portfolio_state.update(
                    updated_at_us=int(time.time() * 1_000_000),
                    monitored_markets=snapshot.monitored_markets,
                    eligible_markets=snapshot.eligible_markets,
                    in_range_markets=snapshot.in_range_markets,
                    positions=list(snapshot.positions),
                    cash_balance=snapshot.cash_balance,
                    last_market_refresh_ts=snapshot.last_market_refresh_ts,
                    last_position_sync_ts=snapshot.last_position_sync_ts,
                    last_price_cycle_ts=snapshot.last_price_cycle_ts,
                    last_error=str(exc),
                )
            except Exception:
                pass
        await asyncio.sleep(poll_interval_seconds)


async def _fetch_positions(client) -> list[dict]:
    try:
        return await client.get_positions()
    except Exception:
        logger.exception("get_positions failed")
        return []


def _to_snapshot(pos: dict) -> PositionSnapshot:
    ticker = str(pos.get("ticker") or "")
    event_ticker = str(pos.get("event_ticker") or "")
    size = float(pos.get("position") or 0)
    # market_exposure is cost basis in cents for Kalshi; convert to dollars.
    exposure_cents = float(pos.get("market_exposure") or 0)
    exposure_dollars = exposure_cents / 100.0 if abs(exposure_cents) > 1 else exposure_cents
    avg_price = exposure_dollars / size if size > 0 else 0.0
    realized = float(pos.get("realized_pnl") or 0)
    return PositionSnapshot(
        slug=ticker,
        title=ticker,
        outcome="NO",
        asset=f"{ticker}:no",
        condition_id=event_ticker,
        size=size,
        avg_price=avg_price,
        initial_value=exposure_dollars,
        current_price=avg_price,  # mark-to-market requires per-market fetch; use cost basis for now
        current_value=exposure_dollars,
        pnl_usd=realized,
        pnl_pct=(realized / exposure_dollars) if exposure_dollars > 0 else 0.0,
        end_date="",
        eta_seconds=0.0,
        source="kalshi",
    )
