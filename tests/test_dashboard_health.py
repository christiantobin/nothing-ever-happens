import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.dashboard import HeartbeatTracker, build_longshot_fade_app


@pytest.mark.asyncio
async def test_health_returns_503_before_any_tick():
    hb = HeartbeatTracker()
    app = build_longshot_fade_app(heartbeat=hb, scan_interval_seconds=30)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 503


@pytest.mark.asyncio
async def test_health_returns_200_when_fresh():
    hb = HeartbeatTracker()
    hb.tick()
    app = build_longshot_fade_app(heartbeat=hb, scan_interval_seconds=30)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        assert await resp.text() == "ok"


@pytest.mark.asyncio
async def test_health_returns_503_when_stale():
    hb = HeartbeatTracker()
    hb._last_tick = time.monotonic() - 120
    app = build_longshot_fade_app(heartbeat=hb, scan_interval_seconds=30)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 503
        body = await resp.text()
        assert "stale" in body


@pytest.mark.asyncio
async def test_status_reports_portfolio():
    from bot.strategy.longshot_fade import LongshotFadePortfolio

    hb = HeartbeatTracker()
    hb.tick()
    portfolio = LongshotFadePortfolio(store=None)
    portfolio.record_entry(
        token_id="E1-A:no",
        event_ticker="E1",
        price=0.05,
        contracts=10,
        estimated_fee=0.02,
        order_id="x",
    )

    app = build_longshot_fade_app(
        heartbeat=hb, scan_interval_seconds=30, portfolio=portfolio
    )
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/status")
        assert resp.status == 200
        body = await resp.json()
        assert body["held_market_tickers"] == ["E1-A"]
        assert body["current_exposure"] == 0.5
