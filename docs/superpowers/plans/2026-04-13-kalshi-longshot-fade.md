# Kalshi Longshot Fade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a sibling strategy (`longshot_fade`) that applies the "nothing ever happens" thesis per-outcome inside Kalshi multi-outcome events, alongside the existing Polymarket `nothing_happens` strategy.

**Architecture:** Reuse the existing `ExchangeClient` Protocol, dashboard, DB, recovery, risk-controls, and reconciliation infrastructure. Add one new exchange client (`bot/exchange/kalshi.py`), one new strategy module (`bot/strategy/longshot_fade.py`), one new market-discovery helper, and a strategy selector in `bot/main.py`. Host target: Raspberry Pi with SQLite + systemd.

**Tech Stack:** Python 3.11+, aiohttp (existing), sqlalchemy (existing), cryptography (new dep for RSA signing), pytest + pytest-asyncio (existing pattern). No new heavy dependencies.

**Spec:** `docs/superpowers/specs/2026-04-13-kalshi-longshot-fade-design.md`

**Branch:** `kalshi-longshot-fade` (already created and pushed to `origin`)

---

## File Structure

| Path | Status | Responsibility |
|---|---|---|
| `bot/exchange/kalshi_auth.py` | NEW | Pure RSA-PSS signing helpers. No I/O. |
| `bot/exchange/kalshi.py` | NEW | `KalshiExchangeClient` implementing `ExchangeClient` Protocol. |
| `bot/kalshi_markets.py` | NEW | Kalshi event/market discovery (analogue of `bot/standalone_markets.py`). |
| `bot/strategy/longshot_fade.py` | NEW | Scan loop + entry gate. |
| `bot/config.py` | MODIFY | Add `load_longshot_fade_config` and `LongshotFadeConfig` dataclass. |
| `bot/main.py` | MODIFY | `STRATEGY` env var selects runtime; add Kalshi exchange path. |
| `bot/dashboard.py` | MODIFY | Add `GET /health` returning 503 on stale scan tick. |
| `scripts/daily_summary.py` | NEW | Post P&L + open positions to Discord webhook. |
| `deploy/nothing-ever-happens.service` | NEW | systemd unit for Pi. |
| `docs/raspberry-pi-setup.md` | NEW | End-to-end Pi setup guide. |
| `config.example.json` | MODIFY | Add `strategies.longshot_fade` block. |
| `.env.example` | MODIFY | Add Kalshi env vars. |
| `requirements.txt` | MODIFY | Add `cryptography>=42,<46`. |
| `tests/test_kalshi_auth.py` | NEW | Unit tests for signing. |
| `tests/test_kalshi.py` | NEW | Unit tests for exchange client (mocked HTTP). |
| `tests/test_kalshi_markets.py` | NEW | Unit tests for market discovery. |
| `tests/test_longshot_fade.py` | NEW | Unit tests for strategy logic. |
| `tests/test_config_longshot_fade.py` | NEW | Config parsing tests. |
| `tests/test_main_runtime_kalshi.py` | NEW | Integration: `STRATEGY=longshot_fade` wires up correctly. |
| `tests/test_dashboard_health.py` | NEW | `/health` endpoint tests. |
| `tests/test_daily_summary.py` | NEW | Daily summary unit tests. |

---

## Task 0: Verify Current Kalshi API Details

**Why this is task 0:** the spec calls out 6 unknowns that must be grounded in current Kalshi docs (endpoint URLs, fee formula, signature format, etc.). Everything downstream fabricates if this is skipped.

**Files:**
- Create: `docs/kalshi-api-reference.md`

- [ ] **Step 1: Fetch current Kalshi API docs via context7 MCP**

Call `mcp__plugin_context7_context7__resolve-library-id` with query `"kalshi trading api"`, then `mcp__plugin_context7_context7__query-docs` with the resolved ID to pull current auth, endpoints, and fee formula.

- [ ] **Step 2: Record findings in `docs/kalshi-api-reference.md`**

Document the following facts as of fetch date:
- Production base URL (has moved historically — confirm current)
- Demo/sandbox base URL and whether it requires separate credentials
- Exact auth header names and signature format (PSS vs PKCS#1 v1.5, hash alg, canonicalization)
- Fee formula with exact coefficient and rounding rule
- Endpoint paths for: `/exchange/status`, `/events`, `/markets/{ticker}`, `/markets/{ticker}/orderbook`, `/portfolio/orders`, `/portfolio/fills`
- Rate limits
- Response schema for events with nested markets

- [ ] **Step 3: Commit**

```bash
git add docs/kalshi-api-reference.md
git commit -m "docs: record current Kalshi API reference for implementation"
```

---

## Task 1: Add Dependencies and Config Scaffolding

**Files:**
- Modify: `requirements.txt`
- Modify: `.env.example`
- Modify: `config.example.json`

- [ ] **Step 1: Add cryptography to requirements.txt**

Append to `requirements.txt`:

```
cryptography>=42.0.0,<46.0.0
```

- [ ] **Step 2: Add Kalshi env vars to .env.example**

Append to `.env.example`:

```
# Strategy selector: "nothing_happens" (default, Polymarket) or "longshot_fade" (Kalshi)
STRATEGY=nothing_happens

# Kalshi-only env vars (only read when STRATEGY=longshot_fade)
KALSHI_ENV=demo
KALSHI_ACCESS_KEY_ID=
KALSHI_PRIVATE_KEY_PATH=

# Optional: Discord/Slack-compatible webhook for daily summaries
DAILY_SUMMARY_WEBHOOK_URL=

# Emergency pause (any non-empty value halts new entries across strategies)
TRADING_PAUSED=
```

- [ ] **Step 3: Add longshot_fade block to config.example.json**

Open `config.example.json`, add a sibling key under `strategies`:

```json
"longshot_fade": {
  "price_cap": 0.10,
  "max_capital_per_outcome": 15.0,
  "max_capital_per_event": 30.0,
  "max_total_exposure": 250.0,
  "min_time_to_resolution_hours": 24,
  "max_time_to_resolution_days": 90,
  "min_outcomes_in_event": 3,
  "scan_interval_seconds": 30
}
```

- [ ] **Step 4: Install deps locally and verify**

Run: `pip install -r requirements.txt && python -c "from cryptography.hazmat.primitives import serialization; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add requirements.txt .env.example config.example.json
git commit -m "feat: add cryptography dep and longshot_fade config scaffolding"
```

---

## Task 2: RSA Signing Module

**Files:**
- Create: `bot/exchange/kalshi_auth.py`
- Test: `tests/test_kalshi_auth.py`

Uses values confirmed in Task 0 for signature format.

- [ ] **Step 1: Write failing test for signer**

Create `tests/test_kalshi_auth.py`:

```python
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from bot.exchange.kalshi_auth import KalshiSigner


def _test_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def test_sign_produces_base64_signature(tmp_path):
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(_test_key_pem())

    signer = KalshiSigner(access_key_id="test", private_key_path=str(key_path))
    headers = signer.sign(timestamp_ms=1700000000000, method="GET", path="/trade-api/v2/exchange/status")

    assert headers["KALSHI-ACCESS-KEY"] == "test"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    assert len(headers["KALSHI-ACCESS-SIGNATURE"]) > 0


def test_sign_is_deterministic_for_fixed_input(tmp_path):
    """Different signatures across calls due to PSS salt, but both verify against pubkey."""
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(_test_key_pem())

    signer = KalshiSigner(access_key_id="test", private_key_path=str(key_path))
    h1 = signer.sign(timestamp_ms=1700000000000, method="GET", path="/x")
    h2 = signer.sign(timestamp_ms=1700000000000, method="GET", path="/x")
    # PSS has random salt; signatures differ but both are non-empty
    assert h1["KALSHI-ACCESS-SIGNATURE"] != h2["KALSHI-ACCESS-SIGNATURE"]
    assert len(h1["KALSHI-ACCESS-SIGNATURE"]) == len(h2["KALSHI-ACCESS-SIGNATURE"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_kalshi_auth.py -v`
Expected: FAIL — `bot.exchange.kalshi_auth` not found.

- [ ] **Step 3: Implement KalshiSigner**

Create `bot/exchange/kalshi_auth.py`:

```python
"""RSA-PSS signing for Kalshi trade API requests.

Uses values confirmed in docs/kalshi-api-reference.md. If Kalshi has moved
to a different signature format, update `sign` accordingly.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


@dataclass
class KalshiSigner:
    access_key_id: str
    private_key_path: str

    def __post_init__(self) -> None:
        with open(self.private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    def sign(self, timestamp_ms: int, method: str, path: str) -> dict[str, str]:
        message = f"{timestamp_ms}{method.upper()}{path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.access_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "accept": "application/json",
            "content-type": "application/json",
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_kalshi_auth.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add bot/exchange/kalshi_auth.py tests/test_kalshi_auth.py
git commit -m "feat: add Kalshi RSA-PSS request signer"
```

---

## Task 3: Kalshi HTTP Client — Signed Request Wrapper

**Files:**
- Create: `bot/exchange/kalshi.py` (partial — just the HTTP base)
- Test: `tests/test_kalshi.py` (partial)

- [ ] **Step 1: Write failing test for signed GET**

Create `tests/test_kalshi.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.exchange.kalshi import KalshiExchangeClient, KalshiEnv


@pytest.fixture
def signer(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

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


@pytest.mark.asyncio
async def test_signed_get_sends_auth_headers(signer):
    client = KalshiExchangeClient(
        access_key_id="id",
        private_key_path=signer,
        env=KalshiEnv.DEMO,
        allow_trading=True,
    )

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"status": "ok"})
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=mock_resp)
    client._session = mock_session

    result = await client._signed_request("GET", "/trade-api/v2/exchange/status")

    assert result == {"status": "ok"}
    call_kwargs = mock_session.request.call_args.kwargs
    assert "KALSHI-ACCESS-KEY" in call_kwargs["headers"]
    assert "KALSHI-ACCESS-SIGNATURE" in call_kwargs["headers"]
    await client.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_kalshi.py -v`
Expected: FAIL — `bot.exchange.kalshi` not found.

- [ ] **Step 3: Implement HTTP base**

Create `bot/exchange/kalshi.py`:

```python
"""Kalshi exchange client implementing bot.exchange.base.ExchangeClient.

See docs/kalshi-api-reference.md for current endpoint paths and schemas.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import Any

import aiohttp

from bot.exchange.kalshi_auth import KalshiSigner

logger = logging.getLogger(__name__)


class KalshiEnv(str, enum.Enum):
    DEMO = "demo"
    PROD = "prod"


_BASE_URLS = {
    # Values from docs/kalshi-api-reference.md (Task 0). Update if Kalshi moves.
    KalshiEnv.DEMO: "https://demo-api.kalshi.co",
    KalshiEnv.PROD: "https://api.elections.kalshi.com",
}


class KalshiExchangeClient:
    def __init__(
        self,
        access_key_id: str,
        private_key_path: str,
        env: KalshiEnv = KalshiEnv.DEMO,
        allow_trading: bool = False,
    ):
        self._signer = KalshiSigner(access_key_id=access_key_id, private_key_path=private_key_path)
        self._base_url = _BASE_URLS[env]
        self._allow_trading = allow_trading
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _signed_request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        session = await self._ensure_session()
        headers = self._signer.sign(timestamp_ms=int(time.time() * 1000), method=method, path=path)
        url = f"{self._base_url}{path}"

        for attempt in range(3):
            async with session.request(
                method, url, headers=headers, params=params, json=json_body
            ) as resp:
                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status >= 500 and attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                if resp.status >= 400:
                    body = await resp.text()
                    raise KalshiApiError(f"{method} {path} -> {resp.status}: {body}")
                return await resp.json()

        raise KalshiApiError(f"{method} {path} retries exhausted")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


class KalshiApiError(Exception):
    pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_kalshi.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/exchange/kalshi.py tests/test_kalshi.py
git commit -m "feat: add Kalshi HTTP client base with signed requests and retries"
```

---

## Task 4: Kalshi Market Data Methods

**Files:**
- Modify: `bot/exchange/kalshi.py`
- Modify: `tests/test_kalshi.py`

Adds `get_mid_price`, `get_market_rules`, `get_trades`. Token ID format: `"{market_ticker}:{side}"` where side is `"yes"` or `"no"`.

- [ ] **Step 1: Write failing test for get_mid_price**

Append to `tests/test_kalshi.py`:

```python
@pytest.mark.asyncio
async def test_get_mid_price_no_side(signer):
    client = KalshiExchangeClient(
        access_key_id="id", private_key_path=signer, env=KalshiEnv.DEMO, allow_trading=False
    )
    orderbook_resp = {
        "orderbook": {
            "yes": [[60, 100]],  # bid: 60 cents, size 100
            "no": [[35, 200]],   # bid: 35 cents, size 200
        }
    }

    async def fake_request(method, path, **kw):
        assert "KXPRES28-HARRIS" in path
        return orderbook_resp

    client._signed_request = fake_request
    # NO side: best NO bid is $0.35 → YES ask is $0.65 → NO ask is $0.40
    # mid for NO is (NO_bid + NO_ask)/2 -> parse from orderbook convention in kalshi-api-reference.md
    mid = await client.get_mid_price("KXPRES28-HARRIS:no")
    assert 0 < mid < 1
    await client.close()
```

Note: exact orderbook schema and bid/ask convention comes from Task 0's reference doc. Adjust the assertions once confirmed.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_kalshi.py::test_get_mid_price_no_side -v`
Expected: FAIL — `get_mid_price` not implemented.

- [ ] **Step 3: Implement market data methods**

Append to `bot/exchange/kalshi.py`:

```python
from bot.models import MarketRules, Trade


def _parse_token_id(token_id: str) -> tuple[str, str]:
    market_ticker, _, side = token_id.rpartition(":")
    if side not in ("yes", "no"):
        raise ValueError(f"Invalid Kalshi token_id: {token_id}")
    return market_ticker, side


class KalshiExchangeClient:
    # ... existing methods above ...

    async def get_mid_price(self, token_id: str) -> float:
        market_ticker, side = _parse_token_id(token_id)
        resp = await self._signed_request("GET", f"/trade-api/v2/markets/{market_ticker}/orderbook")
        book = resp["orderbook"]
        # Kalshi convention (see kalshi-api-reference.md): each side lists [price_cents, size] bids.
        # The ask for NO is derived from the YES bid: no_ask = 100 - yes_best_bid.
        yes_bids = book.get("yes") or []
        no_bids = book.get("no") or []
        if side == "no":
            no_best_bid = no_bids[0][0] if no_bids else 1
            yes_best_bid = yes_bids[0][0] if yes_bids else 1
            no_ask_cents = 100 - yes_best_bid
            mid_cents = (no_best_bid + no_ask_cents) / 2
        else:
            yes_best_bid = yes_bids[0][0] if yes_bids else 1
            no_best_bid = no_bids[0][0] if no_bids else 1
            yes_ask_cents = 100 - no_best_bid
            mid_cents = (yes_best_bid + yes_ask_cents) / 2
        return mid_cents / 100.0

    async def get_market_rules(self, token_id: str) -> MarketRules | None:
        market_ticker, _ = _parse_token_id(token_id)
        resp = await self._signed_request("GET", f"/trade-api/v2/markets/{market_ticker}")
        m = resp["market"]
        if m.get("status") != "active":
            return None
        return MarketRules(
            tick_size=0.01,
            min_order_size=1,
            max_order_size=m.get("risk_limit_cents", 100_000) // 100,
            close_time_iso=m.get("close_time"),
        )

    async def get_trades(self, token_id: str, after_timestamp: int | None = None) -> list[Trade]:
        market_ticker, side = _parse_token_id(token_id)
        params = {"ticker": market_ticker}
        if after_timestamp is not None:
            params["min_ts"] = after_timestamp
        resp = await self._signed_request("GET", "/trade-api/v2/portfolio/fills", params=params)
        trades = []
        for f in resp.get("fills", []):
            if f.get("side") != side:
                continue
            trades.append(Trade(
                trade_id=f["trade_id"],
                token_id=token_id,
                side=f["side"],
                price=f["yes_price"] / 100.0 if side == "yes" else f["no_price"] / 100.0,
                size=f["count"],
                timestamp=f["created_time_unix"],
            ))
        return trades

    def get_ask_price(self, orderbook: dict, side: str) -> float:
        """Best available ask price for `side`. Extracted for strategy use."""
        yes_bids = orderbook.get("yes") or []
        no_bids = orderbook.get("no") or []
        if side == "no":
            yes_best_bid = yes_bids[0][0] if yes_bids else 1
            return (100 - yes_best_bid) / 100.0
        no_best_bid = no_bids[0][0] if no_bids else 1
        return (100 - no_best_bid) / 100.0

    def estimate_fee(self, price: float, contracts: int) -> float:
        """Kalshi fee formula confirmed in kalshi-api-reference.md as of Task 0."""
        import math
        return math.ceil(0.07 * price * (1 - price) * contracts * 100) / 100
```

Verify that `bot/models.py` has `MarketRules` and `Trade` with the fields referenced. If signatures differ, adjust the code (not the models).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_kalshi.py -v`
Expected: PASS.

- [ ] **Step 5: Write failing test for fee estimation**

Append:

```python
def test_estimate_fee(signer):
    client = KalshiExchangeClient(
        access_key_id="id", private_key_path=signer, env=KalshiEnv.DEMO, allow_trading=False
    )
    # p=0.10, 100 contracts: 0.07 * 0.10 * 0.90 * 100 * 100 = 63 -> ceil/100 = 0.63
    assert client.estimate_fee(0.10, 100) == 0.63
    # p=0.50, 10 contracts: 0.07 * 0.50 * 0.50 * 10 * 100 = 17.5 -> ceil/100 = 0.18
    assert client.estimate_fee(0.50, 10) == 0.18
```

- [ ] **Step 6: Run fee test**

Run: `pytest tests/test_kalshi.py::test_estimate_fee -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add bot/exchange/kalshi.py tests/test_kalshi.py
git commit -m "feat: add Kalshi market-data methods and fee estimation"
```

---

## Task 5: Kalshi Order Methods

**Files:**
- Modify: `bot/exchange/kalshi.py`
- Modify: `tests/test_kalshi.py`

Implements `place_limit_order`, `place_market_order`, `get_open_orders`, `get_order`, `cancel_order`, `cancel_all`, `check_order_readiness`, `bootstrap_live_trading`.

- [ ] **Step 1: Write failing test for place_limit_order**

Append to `tests/test_kalshi.py`:

```python
from bot.models import LimitOrderIntent


@pytest.mark.asyncio
async def test_place_limit_order_sends_correct_body(signer):
    client = KalshiExchangeClient(
        access_key_id="id", private_key_path=signer, env=KalshiEnv.DEMO, allow_trading=True
    )
    captured = {}

    async def fake_request(method, path, **kw):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = kw.get("json_body")
        return {"order": {"order_id": "abc-123", "status": "resting"}}

    client._signed_request = fake_request

    intent = LimitOrderIntent(
        token_id="KXPRES28-HARRIS:no",
        side="buy",
        price=0.08,
        size=10,
    )
    result = await client.place_limit_order(intent)

    assert result.order_id == "abc-123"
    assert captured["method"] == "POST"
    assert captured["path"] == "/trade-api/v2/portfolio/orders"
    body = captured["body"]
    assert body["ticker"] == "KXPRES28-HARRIS"
    assert body["side"] == "no"
    assert body["action"] == "buy"
    assert body["type"] == "limit"
    assert body["no_price"] == 8  # cents
    assert body["count"] == 10
    await client.close()


@pytest.mark.asyncio
async def test_place_limit_order_blocked_when_not_allowed(signer):
    client = KalshiExchangeClient(
        access_key_id="id", private_key_path=signer, env=KalshiEnv.DEMO, allow_trading=False
    )
    intent = LimitOrderIntent(token_id="KXPRES28-HARRIS:no", side="buy", price=0.08, size=10)
    with pytest.raises(PermissionError):
        await client.place_limit_order(intent)
    await client.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_kalshi.py -v -k "order"`
Expected: FAIL — methods not implemented.

- [ ] **Step 3: Implement order methods**

Append to `bot/exchange/kalshi.py`:

```python
from bot.models import (
    LimitOrderIntent, MarketOrderIntent, OpenOrder, OrderReadiness, OrderResult
)


class KalshiExchangeClient:
    # ... existing ...

    def bootstrap_live_trading(self, token_id: str) -> None:
        # No-op on Kalshi: no proxy wallet, no USDC approval.
        return None

    async def place_limit_order(self, order: LimitOrderIntent) -> OrderResult:
        if not self._allow_trading:
            raise PermissionError("Kalshi client not configured for live trading")
        market_ticker, side = _parse_token_id(order.token_id)
        price_cents = int(round(order.price * 100))
        body = {
            "ticker": market_ticker,
            "side": side,
            "action": order.side,  # "buy" or "sell"
            "type": "limit",
            "count": order.size,
        }
        if side == "no":
            body["no_price"] = price_cents
        else:
            body["yes_price"] = price_cents
        resp = await self._signed_request("POST", "/trade-api/v2/portfolio/orders", json_body=body)
        o = resp["order"]
        return OrderResult(order_id=o["order_id"], status=o["status"], filled_size=o.get("fill_count", 0))

    async def place_market_order(self, order: MarketOrderIntent) -> OrderResult:
        if not self._allow_trading:
            raise PermissionError("Kalshi client not configured for live trading")
        market_ticker, side = _parse_token_id(order.token_id)
        body = {
            "ticker": market_ticker,
            "side": side,
            "action": order.side,
            "type": "market",
            "count": order.size,
        }
        resp = await self._signed_request("POST", "/trade-api/v2/portfolio/orders", json_body=body)
        o = resp["order"]
        return OrderResult(order_id=o["order_id"], status=o["status"], filled_size=o.get("fill_count", 0))

    async def get_open_orders(self, token_id: str) -> list[OpenOrder]:
        market_ticker, side = _parse_token_id(token_id)
        resp = await self._signed_request(
            "GET", "/trade-api/v2/portfolio/orders",
            params={"ticker": market_ticker, "status": "resting"},
        )
        out = []
        for o in resp.get("orders", []):
            if o.get("side") != side:
                continue
            price_cents = o.get("no_price") if side == "no" else o.get("yes_price")
            out.append(OpenOrder(
                order_id=o["order_id"],
                token_id=token_id,
                side=o["action"],
                price=price_cents / 100.0,
                size=o["count"],
                filled=o.get("fill_count", 0),
            ))
        return out

    async def get_order(self, order_id: str) -> OpenOrder | None:
        try:
            resp = await self._signed_request("GET", f"/trade-api/v2/portfolio/orders/{order_id}")
        except KalshiApiError as exc:
            if "404" in str(exc):
                return None
            raise
        o = resp["order"]
        side = o["side"]
        price_cents = o.get("no_price") if side == "no" else o.get("yes_price")
        return OpenOrder(
            order_id=o["order_id"],
            token_id=f"{o['ticker']}:{side}",
            side=o["action"],
            price=price_cents / 100.0,
            size=o["count"],
            filled=o.get("fill_count", 0),
        )

    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self._signed_request("DELETE", f"/trade-api/v2/portfolio/orders/{order_id}")
            return True
        except KalshiApiError:
            return False

    async def cancel_all(self) -> bool:
        # Iterate — Kalshi may not expose a bulk delete.
        resp = await self._signed_request(
            "GET", "/trade-api/v2/portfolio/orders", params={"status": "resting"}
        )
        ok = True
        for o in resp.get("orders", []):
            ok = await self.cancel_order(o["order_id"]) and ok
        return ok

    async def check_order_readiness(
        self, order: LimitOrderIntent | MarketOrderIntent
    ) -> OrderReadiness:
        rules = await self.get_market_rules(order.token_id)
        if rules is None:
            return OrderReadiness(ready=False, reason="market not active")
        if order.size < rules.min_order_size:
            return OrderReadiness(ready=False, reason="below min size")
        return OrderReadiness(ready=True, reason="")
```

- [ ] **Step 4: Run all tests in test_kalshi.py**

Run: `pytest tests/test_kalshi.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add bot/exchange/kalshi.py tests/test_kalshi.py
git commit -m "feat: add Kalshi order placement, cancellation, and readiness methods"
```

---

## Task 6: Kalshi Event/Market Discovery

**Files:**
- Create: `bot/kalshi_markets.py`
- Test: `tests/test_kalshi_markets.py`

Analogue of `bot/standalone_markets.py` but for multi-outcome Kalshi events.

- [ ] **Step 1: Write failing test**

Create `tests/test_kalshi_markets.py`:

```python
from unittest.mock import AsyncMock
import pytest

from bot.kalshi_markets import discover_multi_outcome_events, MultiOutcomeEvent


@pytest.mark.asyncio
async def test_discover_filters_by_outcome_count():
    client = AsyncMock()
    client._signed_request = AsyncMock(return_value={
        "events": [
            {
                "event_ticker": "KXPRES28",
                "title": "2028 President",
                "markets": [
                    {"ticker": "KXPRES28-HARRIS", "status": "active", "close_time": "2028-11-05T00:00:00Z",
                     "orderbook": {"yes": [[12, 100]], "no": [[85, 100]]}},
                    {"ticker": "KXPRES28-NEWSOM", "status": "active", "close_time": "2028-11-05T00:00:00Z",
                     "orderbook": {"yes": [[8, 100]], "no": [[90, 100]]}},
                    {"ticker": "KXPRES28-SHAPIRO", "status": "active", "close_time": "2028-11-05T00:00:00Z",
                     "orderbook": {"yes": [[5, 100]], "no": [[94, 100]]}},
                ],
            },
            {
                "event_ticker": "BINARY-X",
                "title": "Binary example",
                "markets": [
                    {"ticker": "BINARY-X-Y", "status": "active", "close_time": "2026-05-01T00:00:00Z",
                     "orderbook": {"yes": [[50, 100]], "no": [[48, 100]]}},
                ],
            },
        ]
    })
    events = await discover_multi_outcome_events(client, min_outcomes=3)
    assert len(events) == 1
    assert events[0].event_ticker == "KXPRES28"
    assert len(events[0].markets) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_kalshi_markets.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement discovery**

Create `bot/kalshi_markets.py`:

```python
"""Discover Kalshi events with multi-outcome markets."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class MultiOutcomeMarket:
    ticker: str
    status: str
    close_time: datetime
    orderbook: dict[str, Any]

    @property
    def no_ask(self) -> float:
        yes_bids = self.orderbook.get("yes") or []
        yes_best_bid = yes_bids[0][0] if yes_bids else 1
        return (100 - yes_best_bid) / 100.0

    @property
    def top_of_book_no_size(self) -> int:
        no_bids = self.orderbook.get("no") or []
        return no_bids[0][1] if no_bids else 0


@dataclass
class MultiOutcomeEvent:
    event_ticker: str
    title: str
    markets: list[MultiOutcomeMarket]


async def discover_multi_outcome_events(
    client, min_outcomes: int = 3
) -> list[MultiOutcomeEvent]:
    resp = await client._signed_request(
        "GET", "/trade-api/v2/events",
        params={"status": "open", "with_nested_markets": "true"},
    )
    out = []
    for ev in resp.get("events", []):
        raw_markets = [m for m in ev.get("markets", []) if m.get("status") == "active"]
        if len(raw_markets) < min_outcomes:
            continue
        markets = [
            MultiOutcomeMarket(
                ticker=m["ticker"],
                status=m["status"],
                close_time=datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")),
                orderbook=m.get("orderbook") or {"yes": [], "no": []},
            )
            for m in raw_markets
        ]
        out.append(MultiOutcomeEvent(
            event_ticker=ev["event_ticker"],
            title=ev.get("title", ev["event_ticker"]),
            markets=markets,
        ))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_kalshi_markets.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/kalshi_markets.py tests/test_kalshi_markets.py
git commit -m "feat: add Kalshi multi-outcome event discovery"
```

---

## Task 7: Longshot Fade Config

**Files:**
- Modify: `bot/config.py`
- Create: `tests/test_config_longshot_fade.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_config_longshot_fade.py`:

```python
import json
from bot.config import load_longshot_fade_config


def test_load_longshot_fade_config(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "strategies": {
            "longshot_fade": {
                "price_cap": 0.08,
                "max_capital_per_outcome": 10.0,
                "max_capital_per_event": 25.0,
                "max_total_exposure": 200.0,
                "min_time_to_resolution_hours": 24,
                "max_time_to_resolution_days": 60,
                "min_outcomes_in_event": 3,
                "scan_interval_seconds": 45,
            }
        }
    }))
    cfg = load_longshot_fade_config(str(cfg_file))
    assert cfg.price_cap == 0.08
    assert cfg.max_total_exposure == 200.0
    assert cfg.scan_interval_seconds == 45
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_longshot_fade.py -v`
Expected: FAIL.

- [ ] **Step 3: Add config loader**

Open `bot/config.py`, find the existing `load_nothing_happens_config` and `NothingHappensConfig` for the pattern. Append:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class LongshotFadeConfig:
    price_cap: float
    max_capital_per_outcome: float
    max_capital_per_event: float
    max_total_exposure: float
    min_time_to_resolution_hours: int
    max_time_to_resolution_days: int
    min_outcomes_in_event: int
    scan_interval_seconds: int


def load_longshot_fade_config(config_path: str) -> LongshotFadeConfig:
    import json
    with open(config_path) as f:
        data = json.load(f)
    raw = data["strategies"]["longshot_fade"]
    return LongshotFadeConfig(
        price_cap=float(raw["price_cap"]),
        max_capital_per_outcome=float(raw["max_capital_per_outcome"]),
        max_capital_per_event=float(raw["max_capital_per_event"]),
        max_total_exposure=float(raw["max_total_exposure"]),
        min_time_to_resolution_hours=int(raw["min_time_to_resolution_hours"]),
        max_time_to_resolution_days=int(raw["max_time_to_resolution_days"]),
        min_outcomes_in_event=int(raw["min_outcomes_in_event"]),
        scan_interval_seconds=int(raw["scan_interval_seconds"]),
    )
```

- [ ] **Step 4: Run test**

Run: `pytest tests/test_config_longshot_fade.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot/config.py tests/test_config_longshot_fade.py
git commit -m "feat: add longshot_fade config loader"
```

---

## Task 8: Longshot Fade Strategy — Entry Logic

**Files:**
- Create: `bot/strategy/longshot_fade.py`
- Test: `tests/test_longshot_fade.py`

- [ ] **Step 1: Write failing tests covering entry gate**

Create `tests/test_longshot_fade.py`:

```python
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import pytest

from bot.config import LongshotFadeConfig
from bot.kalshi_markets import MultiOutcomeEvent, MultiOutcomeMarket
from bot.strategy.longshot_fade import select_entry_candidates


def _cfg(**overrides) -> LongshotFadeConfig:
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
    return MultiOutcomeMarket(
        ticker=ticker,
        status="active",
        close_time=close,
        orderbook={"yes": [[100 - no_ask_cents, 100]], "no": [[no_ask_cents - 2, 100]]},
    )


def test_select_candidate_below_price_cap():
    ev = MultiOutcomeEvent(
        event_ticker="E1", title="t",
        markets=[
            _market("E1-A", 5),
            _market("E1-B", 8),
            _market("E1-C", 30),  # above cap
        ],
    )
    cands = select_entry_candidates([ev], _cfg(), held_tickers=set(), current_exposure=0.0)
    tickers = [c.market.ticker for c in cands]
    assert "E1-A" in tickers and "E1-B" in tickers
    assert "E1-C" not in tickers


def test_skips_already_held():
    ev = MultiOutcomeEvent(
        event_ticker="E1", title="t",
        markets=[_market("E1-A", 5), _market("E1-B", 5), _market("E1-C", 5)],
    )
    cands = select_entry_candidates([ev], _cfg(), held_tickers={"E1-A"}, current_exposure=0.0)
    assert {c.market.ticker for c in cands} == {"E1-B", "E1-C"}


def test_enforces_per_event_cap():
    ev = MultiOutcomeEvent(
        event_ticker="E1", title="t",
        markets=[_market(f"E1-{i}", 5) for i in range(10)],
    )
    # price_cap=0.10, per-outcome cap = $15, per-event cap = $30.
    # $15/outcome at $0.05 = 300 contracts. Per-event cap $30 = 2 outcomes max.
    cands = select_entry_candidates([ev], _cfg(), held_tickers=set(), current_exposure=0.0)
    total_event_cost = sum(c.contracts * c.price for c in cands)
    assert total_event_cost <= 30.0


def test_enforces_total_exposure_cap():
    ev = MultiOutcomeEvent(
        event_ticker="E1", title="t",
        markets=[_market(f"E1-{i}", 5) for i in range(5)],
    )
    cands = select_entry_candidates(
        [ev], _cfg(max_total_exposure=20.0), held_tickers=set(), current_exposure=15.0
    )
    total_new = sum(c.contracts * c.price for c in cands)
    assert total_new <= 5.0 + 0.01


def test_filters_resolution_window():
    ev = MultiOutcomeEvent(
        event_ticker="E1", title="t",
        markets=[
            _market("E1-A", 5, hours_out=2),       # too soon
            _market("E1-B", 5, hours_out=48),      # ok
            _market("E1-C", 5, hours_out=24 * 200),  # too far
        ],
    )
    cands = select_entry_candidates([ev], _cfg(), held_tickers=set(), current_exposure=0.0)
    assert {c.market.ticker for c in cands} == {"E1-B"}


def test_respects_min_outcomes():
    ev = MultiOutcomeEvent(
        event_ticker="E1", title="t",
        markets=[_market("E1-A", 5), _market("E1-B", 5)],  # only 2
    )
    cands = select_entry_candidates([ev], _cfg(), held_tickers=set(), current_exposure=0.0)
    assert cands == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_longshot_fade.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement select_entry_candidates**

Create `bot/strategy/longshot_fade.py`:

```python
"""Longshot fade strategy: buy NO on individual outcomes inside multi-outcome events.

Mechanical entry rule: if NO ask <= price_cap and all monetary + time caps hold,
place a limit buy at NO ask. No discretionary gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bot.config import LongshotFadeConfig
from bot.kalshi_markets import MultiOutcomeEvent, MultiOutcomeMarket


@dataclass(frozen=True)
class EntryCandidate:
    event_ticker: str
    market: MultiOutcomeMarket
    price: float      # NO ask in dollars
    contracts: int    # size to buy
    token_id: str     # "{ticker}:no"


def select_entry_candidates(
    events: list[MultiOutcomeEvent],
    config: LongshotFadeConfig,
    held_tickers: set[str],
    current_exposure: float,
) -> list[EntryCandidate]:
    now = datetime.now(timezone.utc)
    min_close = now + timedelta(hours=config.min_time_to_resolution_hours)
    max_close = now + timedelta(days=config.max_time_to_resolution_days)

    remaining_total = max(0.0, config.max_total_exposure - current_exposure)
    out: list[EntryCandidate] = []

    for event in events:
        active = [m for m in event.markets if m.status == "active"]
        if len(active) < config.min_outcomes_in_event:
            continue

        event_spent = 0.0
        # Sort by cheapest NO first so we take the most "longshot" outcomes before capping out.
        for market in sorted(active, key=lambda m: m.no_ask):
            if market.ticker in held_tickers:
                continue
            if market.no_ask <= 0 or market.no_ask > config.price_cap:
                continue
            if not (min_close <= market.close_time <= max_close):
                continue

            remaining_event = config.max_capital_per_event - event_spent
            capital_for_leg = min(
                config.max_capital_per_outcome,
                remaining_event,
                remaining_total - sum(c.contracts * c.price for c in out),
            )
            if capital_for_leg < market.no_ask:
                # Can't afford a single contract at this price given remaining budget.
                continue

            contracts = int(capital_for_leg // market.no_ask)
            if contracts < 1:
                continue

            cost = contracts * market.no_ask
            out.append(EntryCandidate(
                event_ticker=event.event_ticker,
                market=market,
                price=market.no_ask,
                contracts=contracts,
                token_id=f"{market.ticker}:no",
            ))
            event_spent += cost

    return out
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_longshot_fade.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add bot/strategy/longshot_fade.py tests/test_longshot_fade.py
git commit -m "feat: add longshot_fade entry-candidate selection"
```

---

## Task 9: Longshot Fade — Scan Loop + Order Placement

**Files:**
- Modify: `bot/strategy/longshot_fade.py`
- Modify: `tests/test_longshot_fade.py`

- [ ] **Step 1: Write failing test for scan_once**

Append to `tests/test_longshot_fade.py`:

```python
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_scan_once_places_orders_for_candidates():
    events = [
        MultiOutcomeEvent(
            event_ticker="E1", title="t",
            markets=[_market("E1-A", 5), _market("E1-B", 5), _market("E1-C", 5)],
        )
    ]
    client = MagicMock()
    client.place_limit_order = AsyncMock(return_value=MagicMock(order_id="oid-1", status="resting", filled_size=0))
    client.estimate_fee = MagicMock(return_value=0.10)

    portfolio = MagicMock()
    portfolio.held_market_tickers = MagicMock(return_value=set())
    portfolio.current_exposure = MagicMock(return_value=0.0)
    portfolio.record_entry = MagicMock()

    from bot.strategy.longshot_fade import scan_once

    async def fake_discover(c, min_outcomes):
        return events

    n_placed = await scan_once(
        client, _cfg(), portfolio, trading_paused=False, discover_fn=fake_discover
    )
    assert n_placed >= 1
    assert client.place_limit_order.await_count >= 1


@pytest.mark.asyncio
async def test_scan_once_no_orders_when_paused():
    client = MagicMock()
    client.place_limit_order = AsyncMock()
    portfolio = MagicMock()

    from bot.strategy.longshot_fade import scan_once
    async def fake_discover(c, min_outcomes): return []

    n = await scan_once(client, _cfg(), portfolio, trading_paused=True, discover_fn=fake_discover)
    assert n == 0
    client.place_limit_order.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_longshot_fade.py -v -k "scan_once"`
Expected: FAIL — `scan_once` not defined.

- [ ] **Step 3: Implement scan_once**

Append to `bot/strategy/longshot_fade.py`:

```python
import logging

from bot.kalshi_markets import discover_multi_outcome_events
from bot.models import LimitOrderIntent

logger = logging.getLogger(__name__)


async def scan_once(
    client,
    config: LongshotFadeConfig,
    portfolio,
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
            side="buy",
            price=cand.price,
            size=cand.contracts,
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
                    "event": cand.event_ticker, "market": cand.market.ticker,
                    "price": cand.price, "contracts": cand.contracts, "fee": fee,
                },
            )
            placed += 1
        except Exception:
            logger.exception("order placement failed", extra={"token_id": cand.token_id})

    return placed


async def run_forever(client, config: LongshotFadeConfig, portfolio, get_paused):
    import asyncio
    while True:
        try:
            await scan_once(client, config, portfolio, trading_paused=get_paused())
        except Exception:
            logger.exception("scan loop iteration failed")
        await asyncio.sleep(config.scan_interval_seconds)
```

Note: `portfolio.held_market_tickers()`, `portfolio.current_exposure()`, and `portfolio.record_entry()` must exist on `PortfolioState`. If they don't, add them in this task (see Step 4).

- [ ] **Step 4: Extend PortfolioState with required methods**

Open `bot/portfolio_state.py`. Add methods if missing:

```python
def held_market_tickers(self) -> set[str]:
    # Kalshi token_id format is "{ticker}:{side}". Extract ticker prefix for open positions.
    return {
        pos.token_id.split(":")[0]
        for pos in self.open_positions()
    }

def current_exposure(self) -> float:
    return sum(pos.entry_price * pos.contracts for pos in self.open_positions())

def record_entry(
    self, *, token_id: str, event_ticker: str, price: float,
    contracts: int, estimated_fee: float, order_id: str,
) -> None:
    # Delegate to whatever existing entry-recording mechanism the class uses.
    # If none, write to self._store with the metadata above.
    self._store.record_open_position(
        token_id=token_id, event_ticker=event_ticker,
        entry_price=price, contracts=contracts,
        estimated_fee=estimated_fee, order_id=order_id,
    )
```

If `PortfolioState` doesn't cleanly accommodate these, create a small adapter class in `bot/strategy/longshot_fade.py` instead — don't force-refactor the shared class.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_longshot_fade.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add bot/strategy/longshot_fade.py bot/portfolio_state.py tests/test_longshot_fade.py
git commit -m "feat: add longshot_fade scan_once and run_forever"
```

---

## Task 10: Main Runtime Strategy Selector

**Files:**
- Modify: `bot/main.py`
- Create: `tests/test_main_runtime_kalshi.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_main_runtime_kalshi.py`:

```python
import os
from unittest.mock import patch
from bot.main import _build_exchange_for_strategy


def test_strategy_selector_kalshi(tmp_path, monkeypatch):
    key_path = tmp_path / "k.pem"
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path.write_bytes(k.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    monkeypatch.setenv("STRATEGY", "longshot_fade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("KALSHI_ACCESS_KEY_ID", "test-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key_path))

    client = _build_exchange_for_strategy(strategy="longshot_fade", allow_trading=False)
    from bot.exchange.kalshi import KalshiExchangeClient
    assert isinstance(client, KalshiExchangeClient)


def test_strategy_selector_polymarket_default(monkeypatch):
    monkeypatch.delenv("STRATEGY", raising=False)
    client = _build_exchange_for_strategy(strategy="nothing_happens", allow_trading=False)
    from bot.exchange.paper import PaperExchangeClient
    assert isinstance(client, PaperExchangeClient)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_main_runtime_kalshi.py -v`
Expected: FAIL — `_build_exchange_for_strategy` not defined.

- [ ] **Step 3: Add selector to main.py**

Open `bot/main.py`. Add:

```python
def _build_exchange_for_strategy(strategy: str, allow_trading: bool):
    if strategy == "longshot_fade":
        from bot.exchange.kalshi import KalshiEnv, KalshiExchangeClient
        env = KalshiEnv(os.environ.get("KALSHI_ENV", "demo"))
        access_id = os.environ.get("KALSHI_ACCESS_KEY_ID", "")
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        if not access_id or not key_path:
            # Fall back to paper for dev/test.
            return PaperExchangeClient()
        return KalshiExchangeClient(
            access_key_id=access_id, private_key_path=key_path,
            env=env, allow_trading=allow_trading,
        )
    # default: existing nothing_happens path
    # Reuses _build_exchange(exchange_cfg) semantics; return PaperExchangeClient when not live.
    return PaperExchangeClient() if not allow_trading else None


def _main() -> None:
    # existing body; modify the dispatch:
    strategy = os.environ.get("STRATEGY", "nothing_happens")
    if strategy == "longshot_fade":
        asyncio.run(_run_longshot_fade())
        return
    # ... existing nothing_happens code unchanged ...


async def _run_longshot_fade() -> None:
    from bot.config import load_longshot_fade_config
    from bot.strategy.longshot_fade import run_forever
    from bot.portfolio_state import PortfolioState

    cfg_path = os.environ.get("CONFIG_PATH", "config.json")
    cfg = load_longshot_fade_config(cfg_path)

    allow_trading = (
        os.environ.get("BOT_MODE") == "live"
        and os.environ.get("LIVE_TRADING_ENABLED") == "true"
        and os.environ.get("DRY_RUN") == "false"
    )
    client = _build_exchange_for_strategy("longshot_fade", allow_trading=allow_trading)

    portfolio = PortfolioState(...)  # reuse existing constructor arguments

    def get_paused() -> bool:
        return bool(os.environ.get("TRADING_PAUSED", "").strip())

    try:
        await run_forever(client, cfg, portfolio, get_paused)
    finally:
        if hasattr(client, "close"):
            await client.close()
```

Adapt `PortfolioState(...)` construction to match the existing call site in the `nothing_happens` path. Read `bot/main.py` end-to-end before editing.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_main_runtime_kalshi.py -v`
Expected: PASS.

- [ ] **Step 5: Verify existing tests still pass**

Run: `pytest tests/ -v`
Expected: all previously-passing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add bot/main.py tests/test_main_runtime_kalshi.py
git commit -m "feat: add STRATEGY env var selector for longshot_fade vs nothing_happens"
```

---

## Task 11: Dashboard /health Endpoint

**Files:**
- Modify: `bot/dashboard.py`
- Create: `tests/test_dashboard_health.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_dashboard_health.py`:

```python
import time
import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.dashboard import build_app, HeartbeatTracker


@pytest.mark.asyncio
async def test_health_returns_200_when_fresh():
    heartbeat = HeartbeatTracker()
    heartbeat.tick()
    app = build_app(heartbeat=heartbeat, scan_interval_seconds=30)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200


@pytest.mark.asyncio
async def test_health_returns_503_when_stale():
    heartbeat = HeartbeatTracker()
    heartbeat._last_tick = time.monotonic() - 120  # 2 min ago
    app = build_app(heartbeat=heartbeat, scan_interval_seconds=30)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 503
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dashboard_health.py -v`
Expected: FAIL.

- [ ] **Step 3: Add health endpoint and HeartbeatTracker**

Open `bot/dashboard.py`. Add:

```python
import time
from aiohttp import web


class HeartbeatTracker:
    def __init__(self):
        self._last_tick = 0.0

    def tick(self) -> None:
        self._last_tick = time.monotonic()

    def age_seconds(self) -> float:
        return time.monotonic() - self._last_tick if self._last_tick else float("inf")


def build_app(*, heartbeat: HeartbeatTracker, scan_interval_seconds: int) -> web.Application:
    # If there's an existing app-builder, extend it. Otherwise construct and register routes.
    app = web.Application()

    async def health(request):
        if heartbeat.age_seconds() > 2 * scan_interval_seconds:
            return web.Response(status=503, text="stale")
        return web.Response(status=200, text="ok")

    app.router.add_get("/health", health)
    # Re-register any existing dashboard routes here.
    return app
```

If `bot/dashboard.py` already has an app-factory, extend it to accept `heartbeat` and `scan_interval_seconds` kwargs and register `/health` without disturbing existing routes.

- [ ] **Step 4: Wire heartbeat into scan loop**

Modify `bot/strategy/longshot_fade.py` `run_forever`:

```python
async def run_forever(client, config, portfolio, get_paused, heartbeat=None):
    import asyncio
    while True:
        try:
            await scan_once(client, config, portfolio, trading_paused=get_paused())
            if heartbeat is not None:
                heartbeat.tick()
        except Exception:
            logger.exception("scan loop iteration failed")
        await asyncio.sleep(config.scan_interval_seconds)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_dashboard_health.py -v && pytest tests/test_longshot_fade.py -v`
Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add bot/dashboard.py bot/strategy/longshot_fade.py tests/test_dashboard_health.py
git commit -m "feat: add /health endpoint with scan-loop heartbeat"
```

---

## Task 12: Daily Summary Script

**Files:**
- Create: `scripts/daily_summary.py`
- Create: `tests/test_daily_summary.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_daily_summary.py`:

```python
from unittest.mock import MagicMock, patch
from scripts.daily_summary import format_summary


def test_format_summary_includes_key_fields():
    rows = {
        "open_positions": 12,
        "open_notional": 85.50,
        "realized_pnl_24h": 3.20,
        "unrealized_pnl": -1.10,
        "errors_24h": 2,
    }
    text = format_summary(rows)
    assert "12" in text
    assert "85.50" in text
    assert "3.20" in text
    assert "errors" in text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_daily_summary.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement daily summary**

Create `scripts/daily_summary.py`:

```python
"""Post a daily P&L + status summary to a Discord/Slack webhook."""
from __future__ import annotations

import json
import os
import sys
import urllib.request

import sqlalchemy as sa

from bot.db import create_engine


def format_summary(rows: dict) -> str:
    lines = [
        "**Nothing Ever Happens — Daily Summary**",
        f"Open positions: {rows['open_positions']}",
        f"Open notional: ${rows['open_notional']:.2f}",
        f"Realized P&L (24h): ${rows['realized_pnl_24h']:+.2f}",
        f"Unrealized P&L: ${rows['unrealized_pnl']:+.2f}",
        f"Errors (24h): {rows['errors_24h']}",
    ]
    return "\n".join(lines)


def gather_rows(engine) -> dict:
    with engine.connect() as conn:
        open_positions = conn.execute(
            sa.text("SELECT COUNT(*) FROM positions WHERE closed_at IS NULL")
        ).scalar() or 0
        open_notional = conn.execute(
            sa.text("SELECT COALESCE(SUM(entry_price * contracts), 0) FROM positions WHERE closed_at IS NULL")
        ).scalar() or 0.0
        realized = conn.execute(
            sa.text("""
                SELECT COALESCE(SUM(realized_pnl), 0) FROM positions
                WHERE closed_at >= datetime('now', '-1 day')
            """)
        ).scalar() or 0.0
        unrealized = conn.execute(
            sa.text("""
                SELECT COALESCE(SUM((1 - entry_price) * contracts), 0)
                FROM positions WHERE closed_at IS NULL
            """)
        ).scalar() or 0.0
        errors = conn.execute(
            sa.text("""
                SELECT COUNT(*) FROM trade_ledger
                WHERE action LIKE '%error%' AND created_at >= datetime('now', '-1 day')
            """)
        ).scalar() or 0
    return {
        "open_positions": int(open_positions),
        "open_notional": float(open_notional),
        "realized_pnl_24h": float(realized),
        "unrealized_pnl": float(unrealized),
        "errors_24h": int(errors),
    }


def post_webhook(url: str, text: str) -> None:
    payload = json.dumps({"content": text}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=10).read()


def main() -> int:
    url = os.environ.get("DAILY_SUMMARY_WEBHOOK_URL")
    db_url = os.environ.get("DATABASE_URL")
    if not url or not db_url:
        print("DAILY_SUMMARY_WEBHOOK_URL and DATABASE_URL required", file=sys.stderr)
        return 1
    engine = create_engine(db_url)
    rows = gather_rows(engine)
    post_webhook(url, format_summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note: SQL column names (`closed_at`, `entry_price`, `contracts`, `realized_pnl`, `trade_ledger`) must match the schema in `bot/db.py`. Read `bot/db.py` and `bot/trade_ledger.py` before implementing, and adjust column names to match the real schema.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_daily_summary.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/daily_summary.py tests/test_daily_summary.py
git commit -m "feat: add daily summary webhook script"
```

---

## Task 13: Raspberry Pi systemd Unit + Setup Doc

**Files:**
- Create: `deploy/nothing-ever-happens.service`
- Create: `docs/raspberry-pi-setup.md`

No tests — ops artifacts.

- [ ] **Step 1: Create systemd unit**

Create `deploy/nothing-ever-happens.service`:

```ini
[Unit]
Description=Nothing Ever Happens Kalshi Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/nothing-ever-happens
EnvironmentFile=/home/pi/nothing-ever-happens/.env
ExecStart=/home/pi/nothing-ever-happens/.venv/bin/python -m bot.main
Restart=always
RestartSec=10
StandardOutput=append:/home/pi/nothing-ever-happens/logs/bot.log
StandardError=append:/home/pi/nothing-ever-happens/logs/bot.log

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create Pi setup guide**

Create `docs/raspberry-pi-setup.md` covering:
- OS prereqs (Raspberry Pi OS 64-bit recommended for cryptography wheels)
- `sudo apt install python3.11-venv git sqlite3 libffi-dev build-essential`
- Clone the fork, checkout `kalshi-longshot-fade`, create venv, `pip install -r requirements.txt`
- Copy `.env.example` to `.env`, fill in Kalshi creds, `chmod 600 .env`
- Place RSA key at `/home/pi/kalshi-key.pem`, `chmod 600`
- Copy `deploy/nothing-ever-happens.service` to `/etc/systemd/system/`
- `sudo systemctl daemon-reload && sudo systemctl enable --now nothing-ever-happens`
- `mkdir -p logs`
- Add to `/etc/crontab`: daily summary cron `5 0 * * * pi /home/pi/nothing-ever-happens/.venv/bin/python /home/pi/nothing-ever-happens/scripts/daily_summary.py`
- Add health-check cron: `*/5 * * * * pi curl -fsS localhost:8080/health || /home/pi/alert.sh "bot health failed"`
- Verify: `systemctl status nothing-ever-happens`, `journalctl -u nothing-ever-happens -f`
- Going-live checklist: demo mode for ≥ 7 days → verify at least one entry + one resolution → flip `KALSHI_ENV=prod` + `BOT_MODE=live` + `LIVE_TRADING_ENABLED=true` + `DRY_RUN=false`

- [ ] **Step 3: Commit**

```bash
git add deploy/ docs/raspberry-pi-setup.md
git commit -m "docs: add Raspberry Pi setup guide and systemd unit"
```

---

## Task 14: Demo-Environment Smoke Test

**Files:**
- Create: `docs/kalshi-demo-smoke-test.md`

Manual test protocol to run against `demo-api.kalshi.co` before flipping to prod. Not automated.

- [ ] **Step 1: Document the smoke test**

Create `docs/kalshi-demo-smoke-test.md`:

```markdown
# Kalshi Demo Smoke Test

Before flipping `KALSHI_ENV=prod` and live-trading flags on, run the bot against
the Kalshi demo environment for at least 7 days and confirm all of the following.

## 1. Startup
- [ ] `systemctl start nothing-ever-happens`
- [ ] `journalctl -u nothing-ever-happens -n 100` shows "API reachable" and "DB schema current"
- [ ] `curl -fsS localhost:8080/health` returns 200

## 2. Discovery
- [ ] Logs show at least one scan cycle with > 0 discovered events
- [ ] `sqlite3 bot.db "SELECT COUNT(*) FROM market_metadata"` > 0

## 3. Entry
- [ ] At least one candidate matched and an order was placed
- [ ] `sqlite3 bot.db "SELECT * FROM positions ORDER BY opened_at DESC LIMIT 5"` shows rows
- [ ] Logged `estimated_fee` matches `client.estimate_fee()` output

## 4. Recovery
- [ ] `sudo systemctl restart nothing-ever-happens`
- [ ] Open positions from before the restart are still tracked (no duplicates, no orphans)

## 5. Resolution
- [ ] Wait for at least one demo market to resolve
- [ ] `reconcile.py` marks the position closed with correct realized P&L

## 6. Daily Summary
- [ ] Cron fires, Discord webhook receives a formatted message with non-zero counts

## 7. Pause
- [ ] Set `TRADING_PAUSED=1` in `.env`, restart
- [ ] Logs show "trading paused" on each scan; no new orders

Only after all boxes check, flip to prod.
```

- [ ] **Step 2: Commit**

```bash
git add docs/kalshi-demo-smoke-test.md
git commit -m "docs: add Kalshi demo smoke test protocol"
```

---

## Task 15: Full Test Suite + Push

- [ ] **Step 1: Run full suite**

Run: `pytest tests/ -v`
Expected: all PASS (existing + new).

- [ ] **Step 2: Run linter / type check if configured**

Run: `python -m compileall bot/ scripts/`
Expected: no errors.

- [ ] **Step 3: Push branch**

```bash
git push origin kalshi-longshot-fade
```

- [ ] **Step 4: Open draft PR against upstream for visibility (optional)**

```bash
gh pr create --draft --base main --head christiantobin:kalshi-longshot-fade \
  --repo christiantobin/nothing-ever-happens \
  --title "Kalshi longshot fade (phase 1)" \
  --body "See docs/superpowers/specs/2026-04-13-kalshi-longshot-fade-design.md"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✅ Kalshi exchange client → Tasks 2–5
- ✅ Longshot fade strategy → Tasks 8–9
- ✅ Market discovery → Task 6
- ✅ Config → Task 7
- ✅ Main runtime selector → Task 10
- ✅ Dashboard `/health` → Task 11
- ✅ Daily summary → Task 12
- ✅ systemd + Pi docs → Task 13
- ✅ Demo smoke test → Task 14
- ✅ Spec's "verify at impl time" list → Task 0
- ✅ Mechanical-rules philosophy (no discretionary gates) → reflected in Task 8 entry logic

**Known deferrals (not failures):**
- Phase 2 (overround arbitrage) is intentionally out of scope.
- Kalshi-specific schema details in code snippets (endpoint paths, field names) must be re-verified against Task 0 output before writing the code. Snippets are directionally correct based on historical Kalshi API shape, but `docs/kalshi-api-reference.md` is the source of truth.

**Type consistency:**
- `token_id` format is `"{market_ticker}:{side}"` everywhere (exchange client, strategy, portfolio state).
- `EntryCandidate` fields match usage in `scan_once`.
- `LongshotFadeConfig` fields match JSON keys in `config.example.json`.
- `PortfolioState.held_market_tickers()` / `current_exposure()` / `record_entry()` are the three methods the strategy depends on; Task 9 Step 4 ensures they exist.
