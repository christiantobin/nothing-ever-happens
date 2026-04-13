from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bot.exchange.kalshi import (
    KalshiApiError,
    KalshiEnv,
    KalshiExchangeClient,
    _ask_price_cents,
    _parse_token_id,
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


def _client(key_path, allow_trading=False):
    return KalshiExchangeClient(
        access_key_id="id",
        private_key_path=key_path,
        env=KalshiEnv.DEMO,
        allow_trading=allow_trading,
    )


# ---- Token id parsing -----------------------------------------------------


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


# ---- Orderbook parsing ----------------------------------------------------


def test_ask_price_derivation():
    book = {"yes": [[12, 100]], "no": [[85, 100]]}
    # NO ask = 100 - best YES bid = 100 - 12 = 88
    assert _ask_price_cents(book, "no") == 88
    # YES ask = 100 - best NO bid = 100 - 85 = 15
    assert _ask_price_cents(book, "yes") == 15


def test_ask_price_empty_book():
    assert _ask_price_cents({"yes": [], "no": []}, "no") is None


# ---- HTTP wrapper ---------------------------------------------------------


@pytest.mark.asyncio
async def test_signed_request_includes_auth_headers(key_path):
    client = _client(key_path)
    captured = {}

    class _FakeResp:
        status = 200

        async def json(self):
            return {"status": "ok"}

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    def _request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _FakeResp()

    session = MagicMock()
    session.closed = False
    session.request = _request
    session.close = AsyncMock()
    client._session = session

    result = await client._signed_request("GET", "/exchange/status")
    assert result == {"status": "ok"}
    assert captured["url"].endswith("/trade-api/v2/exchange/status")
    assert "KALSHI-ACCESS-KEY" in captured["headers"]
    assert "KALSHI-ACCESS-SIGNATURE" in captured["headers"]
    assert "KALSHI-ACCESS-TIMESTAMP" in captured["headers"]
    await client.close()


@pytest.mark.asyncio
async def test_signed_request_raises_on_4xx(key_path):
    client = _client(key_path)

    class _FakeResp:
        status = 400

        async def json(self):
            return {}

        async def text(self):
            return "bad"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    session = MagicMock()
    session.closed = False
    session.request = lambda *a, **k: _FakeResp()
    session.close = AsyncMock()
    client._session = session

    with pytest.raises(KalshiApiError):
        await client._signed_request("GET", "/x")
    await client.close()


# ---- Market data ----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mid_price_derives_ask(key_path):
    client = _client(key_path)

    async def fake(method, path, **kw):
        assert path == "/markets/KXPRES28-HARRIS/orderbook"
        return {"orderbook": {"yes": [[10, 100]], "no": [[88, 100]]}}

    client._signed_request = fake
    mid = await client.get_mid_price("KXPRES28-HARRIS:no")
    # no_bid=88, no_ask=100-10=90, mid=(88+90)/2=89
    assert mid == pytest.approx(0.89, abs=1e-6)
    await client.close()


@pytest.mark.asyncio
async def test_get_market_rules_inactive_returns_none(key_path):
    client = _client(key_path)

    async def fake(method, path, **kw):
        return {"market": {"status": "closed"}}

    client._signed_request = fake
    assert await client.get_market_rules("FOO:no") is None
    await client.close()


@pytest.mark.asyncio
async def test_get_market_rules_active(key_path):
    client = _client(key_path)

    async def fake(method, path, **kw):
        return {"market": {"status": "active"}}

    client._signed_request = fake
    rules = await client.get_market_rules("FOO:no")
    assert rules is not None
    assert rules.tick_size == 0.01
    assert rules.min_order_size == 1.0
    await client.close()


# ---- Fee estimation -------------------------------------------------------


def test_estimate_fee(key_path):
    client = _client(key_path)
    # p=0.10, 100 contracts: 0.07*0.10*0.90*100 = 0.63 exactly
    assert client.estimate_fee(0.10, 100) == pytest.approx(0.63)
    # p=0.50, 10 contracts: 0.07*0.25*10 = 0.175 → ceil to 0.18
    assert client.estimate_fee(0.50, 10) == pytest.approx(0.18)
    # p=0.05, 20 contracts: 0.07*0.05*0.95*20 = 0.0665 → ceil to 0.07
    assert client.estimate_fee(0.05, 20) == pytest.approx(0.07)


# ---- Order placement ------------------------------------------------------


@pytest.mark.asyncio
async def test_place_limit_order_no_side(key_path):
    client = _client(key_path, allow_trading=True)
    captured = {}

    async def fake(method, path, **kw):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = kw.get("json_body")
        return {"order": {"order_id": "abc", "status": "resting"}}

    client._signed_request = fake
    intent = LimitOrderIntent(
        token_id="KXPRES28-HARRIS:no", side=Side.BUY, price=0.08, size=10
    )
    result = await client.place_limit_order(intent)
    assert result.order_id == "abc"
    assert captured["method"] == "POST"
    assert captured["path"] == "/portfolio/orders"
    body = captured["body"]
    assert body["ticker"] == "KXPRES28-HARRIS"
    assert body["side"] == "no"
    assert body["action"] == "buy"
    assert body["type"] == "limit"
    assert body["no_price"] == 8
    assert body["count"] == 10
    await client.close()


@pytest.mark.asyncio
async def test_place_limit_order_blocked_when_not_allowed(key_path):
    client = _client(key_path, allow_trading=False)
    intent = LimitOrderIntent(
        token_id="KXPRES28-HARRIS:no", side=Side.BUY, price=0.08, size=10
    )
    with pytest.raises(PermissionError):
        await client.place_limit_order(intent)
    await client.close()


@pytest.mark.asyncio
async def test_place_market_order_ioc(key_path):
    client = _client(key_path, allow_trading=True)
    captured = {}

    async def fake(method, path, **kw):
        captured["body"] = kw.get("json_body")
        return {"order": {"order_id": "m1", "status": "executed"}}

    client._signed_request = fake
    intent = MarketOrderIntent(token_id="FOO:yes", side=Side.BUY, amount=5.0, reference_price=0.20)
    result = await client.place_market_order(intent)
    assert result.order_id == "m1"
    assert captured["body"]["type"] == "market"
    assert captured["body"]["time_in_force"] == "immediate_or_cancel"
    await client.close()


# ---- Cancellation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_order_returns_false_on_error(key_path):
    client = _client(key_path, allow_trading=True)

    async def fake(method, path, **kw):
        raise KalshiApiError("404")

    client._signed_request = fake
    assert await client.cancel_order("nope") is False
    await client.close()


@pytest.mark.asyncio
async def test_cancel_all_iterates(key_path):
    client = _client(key_path, allow_trading=True)
    calls = []

    async def fake(method, path, **kw):
        calls.append((method, path))
        if method == "GET":
            return {"orders": [{"order_id": "a"}, {"order_id": "b"}]}
        return None

    client._signed_request = fake
    assert await client.cancel_all() is True
    assert ("DELETE", "/portfolio/orders/a") in calls
    assert ("DELETE", "/portfolio/orders/b") in calls
    await client.close()


# ---- Readiness ------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_order_readiness_inactive(key_path):
    client = _client(key_path)

    async def fake(method, path, **kw):
        return {"market": {"status": "closed"}}

    client._signed_request = fake
    intent = LimitOrderIntent(token_id="FOO:no", side=Side.BUY, price=0.1, size=1)
    readiness = await client.check_order_readiness(intent)
    assert readiness.ready is False
    assert "not active" in readiness.reason
    await client.close()


@pytest.mark.asyncio
async def test_check_order_readiness_ok(key_path):
    client = _client(key_path)

    async def fake(method, path, **kw):
        return {"market": {"status": "active"}}

    client._signed_request = fake
    intent = LimitOrderIntent(token_id="FOO:no", side=Side.BUY, price=0.1, size=5)
    readiness = await client.check_order_readiness(intent)
    assert readiness.ready is True
    await client.close()


# ---- bootstrap is a no-op -------------------------------------------------


def test_bootstrap_live_trading_noop(key_path):
    client = _client(key_path)
    assert client.bootstrap_live_trading("FOO:no") is None
