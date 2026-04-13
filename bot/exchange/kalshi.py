"""Kalshi exchange client implementing bot.exchange.base.ExchangeClient.

See docs/kalshi-api-reference.md for endpoint paths, auth, and fee schema.

Kalshi token_id convention used here: ``"{market_ticker}:{side}"`` where side is
``"yes"`` or ``"no"``. The ExchangeClient Protocol takes opaque token_ids.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import math
import time
from typing import Any

import aiohttp

from bot.exchange.kalshi_auth import KalshiSigner
from bot.models import (
    LimitOrderIntent,
    MarketOrderIntent,
    MarketRules,
    OpenOrder,
    OrderReadiness,
    OrderResult,
    Side,
    Trade,
)

logger = logging.getLogger(__name__)


class KalshiEnv(str, enum.Enum):
    DEMO = "demo"
    PROD = "prod"


_BASE_URLS = {
    KalshiEnv.DEMO: "https://demo-api.kalshi.co",
    KalshiEnv.PROD: "https://api.elections.kalshi.com",
}

_API_PREFIX = "/trade-api/v2"


class KalshiApiError(Exception):
    pass


def _parse_token_id(token_id: str) -> tuple[str, str]:
    market_ticker, _, side = token_id.rpartition(":")
    if side not in ("yes", "no") or not market_ticker:
        raise ValueError(f"Invalid Kalshi token_id: {token_id}")
    return market_ticker, side


def _ask_price_cents(orderbook: dict[str, Any], side: str) -> int | None:
    """Derive best ask for the given side from Kalshi bid-only orderbook.

    Kalshi returns yes_bids and no_bids only. A YES bid at X is equivalent to a
    NO ask at (100 - X), and vice versa.
    """
    if side == "no":
        yes_bids = orderbook.get("yes") or []
        if not yes_bids:
            return None
        return 100 - yes_bids[0][0]
    no_bids = orderbook.get("no") or []
    if not no_bids:
        return None
    return 100 - no_bids[0][0]


def _bid_price_cents(orderbook: dict[str, Any], side: str) -> int | None:
    bids = orderbook.get(side) or []
    if not bids:
        return None
    return bids[0][0]


class KalshiExchangeClient:
    """Async Kalshi trade API client.

    Construct with explicit credentials; the main runtime resolves env vars and
    passes them in. Call ``close()`` before disposal (no async context manager
    provided because the Protocol clients are not used that way elsewhere).
    """

    def __init__(
        self,
        access_key_id: str,
        private_key_path: str,
        env: KalshiEnv = KalshiEnv.DEMO,
        allow_trading: bool = False,
    ):
        self._signer = KalshiSigner(access_key_id=access_key_id, private_key_path=private_key_path)
        self._base_url = _BASE_URLS[env]
        self._env = env
        self._allow_trading = allow_trading
        self._session: aiohttp.ClientSession | None = None

    # ---- HTTP plumbing -----------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
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
        full_path = path if path.startswith(_API_PREFIX) else f"{_API_PREFIX}{path}"
        session = await self._ensure_session()
        url = f"{self._base_url}{full_path}"

        last_exc: Exception | None = None
        for attempt in range(3):
            headers = self._signer.sign(
                timestamp_ms=int(time.time() * 1000),
                method=method,
                path=full_path,
            )
            try:
                async with session.request(
                    method, url, headers=headers, params=params, json=json_body
                ) as resp:
                    if resp.status == 429:
                        retry_after = float(resp.headers.get("Retry-After", "1"))
                        await asyncio.sleep(retry_after)
                        continue
                    if 500 <= resp.status < 600 and attempt < 2:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        raise KalshiApiError(f"{method} {full_path} -> {resp.status}: {body}")
                    if resp.status == 204:
                        return None
                    return await resp.json()
            except aiohttp.ClientError as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                raise KalshiApiError(f"{method} {full_path} network error: {exc}") from exc

        raise KalshiApiError(f"{method} {full_path} retries exhausted: {last_exc}")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ---- ExchangeClient Protocol ------------------------------------------

    def bootstrap_live_trading(self, token_id: str) -> None:
        # Kalshi has no proxy-wallet approval flow. No-op.
        return None

    async def get_mid_price(self, token_id: str) -> float:
        market_ticker, side = _parse_token_id(token_id)
        resp = await self._signed_request("GET", f"/markets/{market_ticker}/orderbook")
        book = resp["orderbook"]
        bid = _bid_price_cents(book, side)
        ask = _ask_price_cents(book, side)
        if bid is None and ask is None:
            return 0.0
        if bid is None:
            return ask / 100.0
        if ask is None:
            return bid / 100.0
        return ((bid + ask) / 2.0) / 100.0

    async def get_market_rules(self, token_id: str) -> MarketRules | None:
        market_ticker, _ = _parse_token_id(token_id)
        try:
            resp = await self._signed_request("GET", f"/markets/{market_ticker}")
        except KalshiApiError:
            return None
        market = resp.get("market")
        if not market or market.get("status") != "active":
            return None
        return MarketRules(tick_size=0.01, min_order_size=1.0)

    async def get_open_orders(self, token_id: str) -> list[OpenOrder]:
        market_ticker, side = _parse_token_id(token_id)
        resp = await self._signed_request(
            "GET",
            "/portfolio/orders",
            params={"ticker": market_ticker, "status": "resting"},
        )
        out: list[OpenOrder] = []
        for o in resp.get("orders", []):
            if o.get("side") != side:
                continue
            price_cents = o.get("no_price") if side == "no" else o.get("yes_price")
            action = o.get("action", "buy")
            out.append(
                OpenOrder(
                    order_id=o.get("order_id") or o.get("id"),
                    token_id=token_id,
                    side=Side.BUY if action == "buy" else Side.SELL,
                    price=(price_cents or 0) / 100.0,
                    size_matched=float(o.get("filled_volume", 0) or 0),
                    original_size=float(o.get("count", 0) or 0),
                    status=o.get("status"),
                )
            )
        return out

    async def get_order(self, order_id: str) -> OpenOrder | None:
        try:
            resp = await self._signed_request("GET", f"/portfolio/orders/{order_id}")
        except KalshiApiError as exc:
            if " 404" in str(exc):
                return None
            raise
        o = resp.get("order")
        if not o:
            return None
        side = o["side"]
        price_cents = o.get("no_price") if side == "no" else o.get("yes_price")
        action = o.get("action", "buy")
        return OpenOrder(
            order_id=o.get("order_id") or o.get("id"),
            token_id=f"{o['ticker']}:{side}",
            side=Side.BUY if action == "buy" else Side.SELL,
            price=(price_cents or 0) / 100.0,
            size_matched=float(o.get("filled_volume", 0) or 0),
            original_size=float(o.get("count", 0) or 0),
            status=o.get("status"),
        )

    async def place_limit_order(self, order: LimitOrderIntent) -> OrderResult:
        if not self._allow_trading:
            raise PermissionError("Kalshi client not configured for live trading")
        market_ticker, side = _parse_token_id(order.token_id)
        price_cents = int(round(order.price * 100))
        body: dict[str, Any] = {
            "ticker": market_ticker,
            "side": side,
            "action": "buy" if order.side == Side.BUY else "sell",
            "type": "limit",
            "count": int(order.size),
            "time_in_force": "good_till_canceled",
        }
        if side == "no":
            body["no_price"] = price_cents
        else:
            body["yes_price"] = price_cents
        resp = await self._signed_request("POST", "/portfolio/orders", json_body=body)
        o = resp["order"]
        return OrderResult(
            order_id=o.get("order_id") or o.get("id"),
            status=o.get("status", ""),
            raw=o,
        )

    async def place_market_order(self, order: MarketOrderIntent) -> OrderResult:
        if not self._allow_trading:
            raise PermissionError("Kalshi client not configured for live trading")
        market_ticker, side = _parse_token_id(order.token_id)
        body: dict[str, Any] = {
            "ticker": market_ticker,
            "side": side,
            "action": "buy" if order.side == Side.BUY else "sell",
            "type": "market",
            "count": int(order.size),
            "time_in_force": "immediate_or_cancel",
        }
        resp = await self._signed_request("POST", "/portfolio/orders", json_body=body)
        o = resp["order"]
        return OrderResult(
            order_id=o.get("order_id") or o.get("id"),
            status=o.get("status", ""),
            raw=o,
        )

    async def get_trades(
        self, token_id: str, after_timestamp: int | None = None
    ) -> list[Trade]:
        market_ticker, side = _parse_token_id(token_id)
        params: dict[str, Any] = {"ticker": market_ticker}
        if after_timestamp is not None:
            params["min_ts"] = after_timestamp
        resp = await self._signed_request("GET", "/portfolio/fills", params=params)
        trades: list[Trade] = []
        for f in resp.get("fills", []):
            if f.get("side") != side:
                continue
            price_cents = f.get("no_price") if side == "no" else f.get("yes_price")
            action = f.get("action", "buy")
            trades.append(
                Trade(
                    trade_id=f.get("trade_id") or f.get("id"),
                    order_id=f.get("order_id", ""),
                    token_id=token_id,
                    side=Side.BUY if action == "buy" else Side.SELL,
                    price=(price_cents or 0) / 100.0,
                    size=float(f.get("count", 0) or 0),
                    fee=float(f.get("fee_cents", 0) or 0) / 100.0,
                    timestamp=f.get("created_time_unix") or f.get("created_time"),
                )
            )
        return trades

    async def check_order_readiness(
        self, order: LimitOrderIntent | MarketOrderIntent
    ) -> OrderReadiness:
        rules = await self.get_market_rules(order.token_id)
        if rules is None:
            return OrderReadiness(ready=False, reason="market not active")
        size = order.size if isinstance(order, LimitOrderIntent) else order.amount
        if size < rules.min_order_size:
            return OrderReadiness(ready=False, reason="below min size")
        return OrderReadiness(ready=True, reason="")

    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self._signed_request("DELETE", f"/portfolio/orders/{order_id}")
            return True
        except KalshiApiError:
            logger.exception("cancel_order failed", extra={"order_id": order_id})
            return False

    async def cancel_all(self) -> bool:
        try:
            resp = await self._signed_request(
                "GET", "/portfolio/orders", params={"status": "resting"}
            )
        except KalshiApiError:
            return False
        ok = True
        for o in resp.get("orders", []):
            oid = o.get("order_id") or o.get("id")
            if oid:
                ok = await self.cancel_order(oid) and ok
        return ok

    # ---- Fee estimation ----------------------------------------------------

    def estimate_fee(self, price: float, contracts: int) -> float:
        """Kalshi trading fee estimate.

        Formula: ``ceil(0.07 * price * (1 - price) * contracts * 100) / 100``.
        The coefficient is flagged for verification in docs/kalshi-api-reference.md;
        update here if Kalshi publishes a different value. ``price`` is in dollars
        (0.00–1.00), result is in dollars rounded up to the nearest cent.
        """
        raw_cents = 0.07 * price * (1.0 - price) * contracts * 100.0
        # Guard against floating-point noise (e.g. 0.63000000000001 -> 0.64).
        return math.ceil(round(raw_cents, 6)) / 100.0
