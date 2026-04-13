"""Tests for the SDK-backed KalshiExchangeClient.

We mock the SDK's api-class methods so tests don't require network or
real credentials. Response shapes are dicts by default; the real SDK returns
typed models but the client uses ``_attr`` to accept both.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bot.exchange.kalshi import (
    KalshiEnv,
    KalshiExchangeClient,
    _parse_token_id,
    _to_dollars,
)
from bot.models import LimitOrderIntent, MarketOrderIntent, Side


@pytest.fixture
def key_path(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "key.pem"
    p.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(p)


@pytest.fixture
def client(key_path):
    c = KalshiExchangeClient(
        access_key_id="id",
        private_key_path=key_path,
        env=KalshiEnv.DEMO,
        allow_trading=True,
    )
    # Replace each api with MagicMock so we can stub per test.
    c._markets = MagicMock()
    c._events = MagicMock()
    c._orders = MagicMock()
    c._portfolio = MagicMock()
    c._exchange = MagicMock()
    return c


@pytest.fixture
def unauth_client(key_path):
    c = KalshiExchangeClient(
        access_key_id="id",
        private_key_path=key_path,
        env=KalshiEnv.DEMO,
        allow_trading=False,
    )
    c._markets = MagicMock()
    c._events = MagicMock()
    c._orders = MagicMock()
    c._portfolio = MagicMock()
    c._exchange = MagicMock()
    return c


# ---- Token id parsing ----------------------------------------------------


def test_parse_token_id_splits_side():
    assert _parse_token_id("KXPRES28-HARRIS:no") == ("KXPRES28-HARRIS", "no")
    assert _parse_token_id("FOO:yes") == ("FOO", "yes")


def test_parse_token_id_rejects_bad_format():
    with pytest.raises(ValueError):
        _parse_token_id("no-colon")
    with pytest.raises(ValueError):
        _parse_token_id(":no")
    with pytest.raises(ValueError):
        _parse_token_id("FOO:maybe")


# ---- Dollar coercion -----------------------------------------------------


def test_to_dollars_handles_cents_and_dollars():
    assert _to_dollars(88) == pytest.approx(0.88)  # cents
    assert _to_dollars(0.12) == pytest.approx(0.12)  # already dollars
    assert _to_dollars("0.88") == pytest.approx(0.88)
    assert _to_dollars(None) == 0.0


# ---- Market data ---------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mid_price_no_side(client):
    client._markets.get_market = AsyncMock(
        return_value={"market": {"no_bid_dollars": 0.35, "no_ask_dollars": 0.40}}
    )
    mid = await client.get_mid_price("FOO:no")
    assert mid == pytest.approx(0.375)


@pytest.mark.asyncio
async def test_get_market_rules_active(client):
    client._markets.get_market = AsyncMock(
        return_value={"market": {"status": "active", "tick_size": 1}}
    )
    rules = await client.get_market_rules("FOO:no")
    assert rules is not None
    assert rules.min_order_size == 1.0


@pytest.mark.asyncio
async def test_get_market_rules_closed(client):
    client._markets.get_market = AsyncMock(
        return_value={"market": {"status": "closed"}}
    )
    assert await client.get_market_rules("FOO:no") is None


# ---- Order placement -----------------------------------------------------


@pytest.mark.asyncio
async def test_place_limit_order_sends_correct_body(client):
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return {"order": {"order_id": "abc", "status": "resting"}}

    client._orders.create_order = fake_create
    intent = LimitOrderIntent(
        token_id="KXPRES28-HARRIS:no", side=Side.BUY, price=0.08, size=10
    )
    result = await client.place_limit_order(intent)
    assert result.order_id == "abc"
    assert captured["ticker"] == "KXPRES28-HARRIS"
    assert captured["side"] == "no"
    assert captured["action"] == "buy"
    assert captured["type"] == "limit"
    assert captured["no_price"] == 8
    assert captured["count"] == 10


@pytest.mark.asyncio
async def test_place_limit_order_blocked_when_not_allowed(unauth_client):
    intent = LimitOrderIntent(
        token_id="FOO:no", side=Side.BUY, price=0.08, size=10
    )
    with pytest.raises(PermissionError):
        await unauth_client.place_limit_order(intent)


@pytest.mark.asyncio
async def test_place_market_order_ioc(client):
    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return {"order": {"order_id": "m1", "status": "executed"}}

    client._orders.create_order = fake_create
    intent = MarketOrderIntent(
        token_id="FOO:yes", side=Side.BUY, amount=5.0, reference_price=0.20
    )
    result = await client.place_market_order(intent)
    assert result.order_id == "m1"
    assert captured["type"] == "market"
    assert captured["time_in_force"] == "immediate_or_cancel"


# ---- Cancellation --------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_order_returns_true(client):
    client._orders.cancel_order = AsyncMock(return_value={})
    assert await client.cancel_order("oid") is True


@pytest.mark.asyncio
async def test_cancel_order_returns_false_on_exception(client):
    from kalshi_python_async import ApiException

    async def raise_exc(order_id):
        raise ApiException(status=404, reason="not found")

    client._orders.cancel_order = raise_exc
    assert await client.cancel_order("oid") is False


@pytest.mark.asyncio
async def test_cancel_all_iterates(client):
    client._orders.get_orders = AsyncMock(
        return_value={"orders": [{"order_id": "a"}, {"order_id": "b"}]}
    )
    cancelled = []

    async def fake_cancel(order_id):
        cancelled.append(order_id)

    client._orders.cancel_order = fake_cancel
    assert await client.cancel_all() is True
    assert cancelled == ["a", "b"]


# ---- Readiness -----------------------------------------------------------


@pytest.mark.asyncio
async def test_check_order_readiness_inactive(client):
    client._markets.get_market = AsyncMock(
        return_value={"market": {"status": "closed"}}
    )
    intent = LimitOrderIntent(token_id="FOO:no", side=Side.BUY, price=0.1, size=1)
    readiness = await client.check_order_readiness(intent)
    assert readiness.ready is False


@pytest.mark.asyncio
async def test_check_order_readiness_ok(client):
    client._markets.get_market = AsyncMock(
        return_value={"market": {"status": "active", "tick_size": 1}}
    )
    intent = LimitOrderIntent(token_id="FOO:no", side=Side.BUY, price=0.1, size=5)
    readiness = await client.check_order_readiness(intent)
    assert readiness.ready is True


# ---- bootstrap is a no-op ------------------------------------------------


def test_bootstrap_live_trading_noop(client):
    assert client.bootstrap_live_trading("FOO:no") is None


# ---- Fee estimation ------------------------------------------------------


def test_estimate_fee(client):
    # 0.07 * 0.10 * 0.90 * 100 = 0.63 exactly
    assert client.estimate_fee(0.10, 100) == pytest.approx(0.63)
    # 0.07 * 0.50 * 0.50 * 10 = 0.175 -> 0.18
    assert client.estimate_fee(0.50, 10) == pytest.approx(0.18)
    # 0.07 * 0.05 * 0.95 * 20 = 0.0665 -> 0.07
    assert client.estimate_fee(0.05, 20) == pytest.approx(0.07)


# ---- Passthrough helpers -------------------------------------------------


@pytest.mark.asyncio
async def test_get_balance_converts_cents_to_dollars(client):
    client._portfolio.get_balance = AsyncMock(return_value={"balance": 25000})
    assert await client.get_balance() == pytest.approx(250.00)


@pytest.mark.asyncio
async def test_get_positions_parses(client):
    client._portfolio.get_positions = AsyncMock(
        return_value={
            "market_positions": [
                {
                    "ticker": "T1",
                    "event_ticker": "E1",
                    "position": 10,
                    "market_exposure": 1.50,
                    "realized_pnl": 0.25,
                    "total_traded": 2.0,
                    "resting_orders_count": 0,
                }
            ]
        }
    )
    pos = await client.get_positions()
    assert len(pos) == 1
    assert pos[0]["ticker"] == "T1"
    assert pos[0]["position"] == 10


@pytest.mark.asyncio
async def test_get_exchange_status(client):
    client._exchange.get_exchange_status = AsyncMock(
        return_value={"trading_active": True, "exchange_active": True}
    )
    s = await client.get_exchange_status()
    assert s["trading_active"] is True


# ---- list_events passthrough ---------------------------------------------


@pytest.mark.asyncio
async def test_list_events_builds_url_and_signs(client):
    """list_events bypasses EventsApi (strict pydantic) and signs the path
    without query params."""
    captured: dict = {}

    class _FakeResp:
        async def read(self):
            return b'{"events": [{"event_ticker": "E1"}], "cursor": "c2"}'

    async def fake_request(method, url, headers=None, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp()

    client._client.rest_client.request = fake_request
    # Stub auth header creator so it doesn't need a real private key.
    client._client.kalshi_auth.create_auth_headers = (
        lambda method, path: {"KALSHI-ACCESS-KEY": "k", "signed_path": path}
    )

    resp = await client.list_events(cursor="c1", limit=50)
    assert resp["events"][0]["event_ticker"] == "E1"
    assert "status=open" in captured["url"]
    assert "with_nested_markets=true" in captured["url"]
    assert "cursor=c1" in captured["url"]
    assert "limit=50" in captured["url"]
    # Signature is over the path WITHOUT query params.
    assert captured["headers"]["signed_path"] == "/trade-api/v2/events"
