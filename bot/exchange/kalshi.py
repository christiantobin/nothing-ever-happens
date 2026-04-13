"""Kalshi exchange client backed by the official ``kalshi_python_async`` SDK.

Implements ``bot.exchange.base.ExchangeClient`` and adds a few passthrough helpers
used by the dashboard (``get_balance``, ``get_positions``, etc.). The SDK handles
request signing, retries, and schema migrations — we just translate between
Kalshi's typed models and this repo's ``bot.models`` dataclasses.

See docs/kalshi-api-reference.md for endpoint paths and the flagged fee
coefficient.

Token ID convention used across this codebase: ``"{market_ticker}:{side}"``
where side is ``"yes"`` or ``"no"``.
"""
from __future__ import annotations

import enum
import logging
import math
from typing import Any

from kalshi_python_async import (
    ApiException,
    Configuration,
    EventsApi,
    ExchangeApi,
    KalshiClient,
    MarketApi,
    OrdersApi,
    PortfolioApi,
)
from kalshi_python_async.auth import KalshiAuth  # SDK omits this from its __init__

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
    KalshiEnv.DEMO: "https://demo-api.kalshi.co/trade-api/v2",
    KalshiEnv.PROD: "https://api.elections.kalshi.com/trade-api/v2",
}


def _parse_token_id(token_id: str) -> tuple[str, str]:
    market_ticker, _, side = token_id.rpartition(":")
    if side not in ("yes", "no") or not market_ticker:
        raise ValueError(f"Invalid Kalshi token_id: {token_id}")
    return market_ticker, side


def _to_dollars(value: Any) -> float:
    """Kalshi exposes prices either as integer cents or FixedPointDollars strings."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Cents when > 1 (e.g. 88), dollars when <= 1.0.
        return float(value) / 100.0 if value > 1 else float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Get attribute from SDK model or dict-like response."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class KalshiExchangeClient:
    """Async Kalshi trade API client backed by the official SDK.

    Call ``close()`` before disposal.
    """

    def __init__(
        self,
        access_key_id: str,
        private_key_path: str,
        env: KalshiEnv = KalshiEnv.DEMO,
        allow_trading: bool = False,
    ):
        self._env = env
        self._allow_trading = allow_trading

        config = Configuration(host=_BASE_URLS[env])
        self._client = KalshiClient(config)
        # Work around SDK 3.2.0 bugs: set_kalshi_auth() has a NameError
        # (KalshiAuth not imported) and passes the path through as if it were
        # PEM content. Read the PEM ourselves and construct KalshiAuth.
        with open(private_key_path, "r") as f:
            pem = f.read()
        self._client.kalshi_auth = KalshiAuth(access_key_id, pem)

        self._markets = MarketApi(self._client)
        self._events = EventsApi(self._client)
        self._orders = OrdersApi(self._client)
        self._portfolio = PortfolioApi(self._client)
        self._exchange = ExchangeApi(self._client)

    async def close(self) -> None:
        await self._client.close()

    # ---- ExchangeClient Protocol ------------------------------------------

    def bootstrap_live_trading(self, token_id: str) -> None:
        # Kalshi has no proxy-wallet approval flow. No-op.
        return None

    async def get_mid_price(self, token_id: str) -> float:
        market_ticker, side = _parse_token_id(token_id)
        resp = await self._markets.get_market(market_ticker)
        m = _attr(resp, "market")
        if m is None:
            return 0.0
        if side == "no":
            bid = _to_dollars(_attr(m, "no_bid_dollars") or _attr(m, "no_bid"))
            ask = _to_dollars(_attr(m, "no_ask_dollars") or _attr(m, "no_ask"))
        else:
            bid = _to_dollars(_attr(m, "yes_bid_dollars") or _attr(m, "yes_bid"))
            ask = _to_dollars(_attr(m, "yes_ask_dollars") or _attr(m, "yes_ask"))
        if bid <= 0 and ask <= 0:
            return 0.0
        if bid <= 0:
            return ask
        if ask <= 0:
            return bid
        return (bid + ask) / 2.0

    async def get_market_rules(self, token_id: str) -> MarketRules | None:
        market_ticker, _ = _parse_token_id(token_id)
        try:
            resp = await self._markets.get_market(market_ticker)
        except ApiException:
            return None
        m = _attr(resp, "market")
        if m is None or _attr(m, "status") != "active":
            return None
        tick_size = _to_dollars(_attr(m, "tick_size") or 1)
        return MarketRules(tick_size=tick_size or 0.01, min_order_size=1.0)

    async def get_open_orders(self, token_id: str) -> list[OpenOrder]:
        market_ticker, side = _parse_token_id(token_id)
        try:
            resp = await self._orders.get_orders(ticker=market_ticker, status="resting")
        except ApiException:
            return []
        orders = _attr(resp, "orders", []) or []
        out: list[OpenOrder] = []
        for o in orders:
            if _attr(o, "side") != side:
                continue
            action = _attr(o, "action", "buy")
            price_cents = (
                _attr(o, "no_price") if side == "no" else _attr(o, "yes_price")
            )
            out.append(
                OpenOrder(
                    order_id=_attr(o, "order_id") or _attr(o, "id"),
                    token_id=token_id,
                    side=Side.BUY if action == "buy" else Side.SELL,
                    price=(price_cents or 0) / 100.0,
                    size_matched=float(_attr(o, "filled_volume", 0) or 0),
                    original_size=float(_attr(o, "count", 0) or 0),
                    status=_attr(o, "status"),
                )
            )
        return out

    async def get_order(self, order_id: str) -> OpenOrder | None:
        try:
            resp = await self._orders.get_order(order_id)
        except ApiException as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise
        o = _attr(resp, "order")
        if o is None:
            return None
        side = _attr(o, "side")
        action = _attr(o, "action", "buy")
        price_cents = (
            _attr(o, "no_price") if side == "no" else _attr(o, "yes_price")
        )
        ticker = _attr(o, "ticker") or _attr(o, "market_ticker") or ""
        return OpenOrder(
            order_id=_attr(o, "order_id") or _attr(o, "id"),
            token_id=f"{ticker}:{side}",
            side=Side.BUY if action == "buy" else Side.SELL,
            price=(price_cents or 0) / 100.0,
            size_matched=float(_attr(o, "filled_volume", 0) or 0),
            original_size=float(_attr(o, "count", 0) or 0),
            status=_attr(o, "status"),
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
        resp = await self._orders.create_order(**body)
        o = _attr(resp, "order")
        return OrderResult(
            order_id=_attr(o, "order_id") or _attr(o, "id") or "",
            status=_attr(o, "status", ""),
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
        resp = await self._orders.create_order(**body)
        o = _attr(resp, "order")
        return OrderResult(
            order_id=_attr(o, "order_id") or _attr(o, "id") or "",
            status=_attr(o, "status", ""),
            raw=o,
        )

    async def get_trades(
        self, token_id: str, after_timestamp: int | None = None
    ) -> list[Trade]:
        market_ticker, side = _parse_token_id(token_id)
        try:
            resp = await self._portfolio.get_fills(
                ticker=market_ticker,
                min_ts=after_timestamp,
            )
        except ApiException:
            return []
        fills = _attr(resp, "fills", []) or []
        out: list[Trade] = []
        for f in fills:
            if _attr(f, "side") != side:
                continue
            action = _attr(f, "action", "buy")
            price_cents = (
                _attr(f, "no_price") if side == "no" else _attr(f, "yes_price")
            )
            out.append(
                Trade(
                    trade_id=_attr(f, "trade_id") or _attr(f, "id") or "",
                    order_id=_attr(f, "order_id", "") or "",
                    token_id=token_id,
                    side=Side.BUY if action == "buy" else Side.SELL,
                    price=(price_cents or 0) / 100.0,
                    size=float(_attr(f, "count", 0) or 0),
                    fee=float(_attr(f, "fee_cents", 0) or 0) / 100.0,
                    timestamp=_attr(f, "created_time_unix")
                    or _attr(f, "created_time"),
                )
            )
        return out

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
            await self._orders.cancel_order(order_id)
            return True
        except ApiException:
            logger.exception("cancel_order failed", extra={"order_id": order_id})
            return False

    async def cancel_all(self) -> bool:
        try:
            resp = await self._orders.get_orders(status="resting")
        except ApiException:
            return False
        orders = _attr(resp, "orders", []) or []
        ok = True
        for o in orders:
            oid = _attr(o, "order_id") or _attr(o, "id")
            if oid:
                ok = await self.cancel_order(oid) and ok
        return ok

    # ---- Dashboard passthroughs -------------------------------------------

    async def get_balance(self) -> float:
        """Current cash balance in dollars."""
        try:
            resp = await self._portfolio.get_balance()
        except ApiException:
            return 0.0
        # SDK returns balance in cents.
        raw = _attr(resp, "balance", 0) or 0
        return float(raw) / 100.0 if raw > 1 else float(raw)

    async def get_positions(self) -> list[dict]:
        """Current open market positions. Returns a list of dicts for dashboard use."""
        try:
            resp = await self._portfolio.get_positions()
        except ApiException:
            return []
        positions = _attr(resp, "market_positions", []) or []
        out: list[dict] = []
        for p in positions:
            out.append(
                {
                    "ticker": _attr(p, "ticker", ""),
                    "event_ticker": _attr(p, "event_ticker", ""),
                    "position": int(_attr(p, "position", 0) or 0),
                    "market_exposure": float(_attr(p, "market_exposure", 0) or 0),
                    "realized_pnl": float(_attr(p, "realized_pnl", 0) or 0),
                    "total_traded": float(_attr(p, "total_traded", 0) or 0),
                    "resting_orders_count": int(
                        _attr(p, "resting_orders_count", 0) or 0
                    ),
                }
            )
        return out

    async def get_exchange_status(self) -> dict:
        try:
            resp = await self._exchange.get_exchange_status()
        except ApiException:
            return {"trading_active": False, "exchange_active": False}
        return {
            "trading_active": bool(_attr(resp, "trading_active", False)),
            "exchange_active": bool(_attr(resp, "exchange_active", False)),
        }

    # ---- Event discovery (used by kalshi_markets.py) ----------------------

    async def list_events(
        self,
        *,
        status: str = "open",
        with_nested_markets: bool = True,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict:
        """Fetch open events with nested markets.

        Uses the SDK's auth-configured REST transport but bypasses the pydantic
        response models — the SDK rejects responses with null values in fields
        its schema marks non-null, which Kalshi's demo returns for many events.
        We only need a few fields per market, so a dict response is sufficient.
        """
        import json

        params: list[tuple[str, str]] = [
            ("status", status),
            ("with_nested_markets", "true" if with_nested_markets else "false"),
            ("limit", str(limit)),
        ]
        if cursor:
            params.append(("cursor", cursor))
        query = "&".join(f"{k}={v}" for k, v in params)

        base = self._client.configuration.host.rstrip("/")
        url = f"{base}/events?{query}"
        # Signature is computed over the path WITHOUT query params.
        sign_path = f"{_path_from_host(base)}/events"
        headers = self._client.kalshi_auth.create_auth_headers("GET", sign_path)
        headers["accept"] = "application/json"

        response = await self._client.rest_client.request(
            "GET", url, headers=headers, _request_timeout=30
        )
        raw = await response.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not raw:
            return {"events": [], "cursor": None}
        return json.loads(raw)

    # ---- Fee estimation ---------------------------------------------------

    def estimate_fee(self, price: float, contracts: int) -> float:
        """Kalshi trading fee estimate.

        Formula: ``ceil(0.07 * price * (1 - price) * contracts * 100) / 100``.
        Verify coefficient against live Kalshi fees before production (see
        docs/kalshi-api-reference.md).
        """
        raw_cents = 0.07 * price * (1.0 - price) * contracts * 100.0
        return math.ceil(round(raw_cents, 6)) / 100.0


def _path_from_host(host: str) -> str:
    """Extract the path portion of a base URL (e.g., ``/trade-api/v2``)."""
    from urllib.parse import urlparse

    return urlparse(host).path.rstrip("/")
