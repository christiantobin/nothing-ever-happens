# Kalshi API Reference Notes

**Fetched via context7 MCP on 2026-04-13.** Source: `docs.kalshi.com`.

This document is the source of truth for endpoint paths, request/response shapes, auth format, and fee calculation used by `bot/exchange/kalshi.py`. If Kalshi changes anything, update this doc first, then the code.

## Environments

| Env | Base URL |
|---|---|
| Demo | `https://demo-api.kalshi.co` |
| Production | `https://api.elections.kalshi.com` |

All API paths are prefixed with `/trade-api/v2`.

## Authentication

All authenticated requests require three headers:

| Header | Value |
|---|---|
| `KALSHI-ACCESS-KEY` | Your API key ID (from Kalshi dashboard) |
| `KALSHI-ACCESS-TIMESTAMP` | Current time in milliseconds, as a decimal string |
| `KALSHI-ACCESS-SIGNATURE` | Base64-encoded RSA-PSS signature (see below) |

### Signature

1. Strip query parameters from the path before signing. Everything from `?` onward is excluded.
2. Compose the message string: `{timestamp_ms}{method_upper}{path_without_query}`
3. UTF-8 encode the message.
4. Sign with RSA-PSS using:
   - MGF: `MGF1(SHA-256)`
   - Salt length: `PSS.DIGEST_LENGTH` (32 bytes for SHA-256)
   - Hash: `SHA-256`
5. Base64-encode the signature bytes.

Reference Python code from Kalshi docs (confirmed current):

```python
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
import base64

def sign_request(private_key, timestamp_ms, method, path):
    path_without_query = path.split('?')[0]
    message = f"{timestamp_ms}{method}{path_without_query}".encode('utf-8')
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode('utf-8')
```

## Endpoints We Use

All paths below are relative to `{base_url}/trade-api/v2`.

### `GET /exchange/status`

Health-check endpoint. Used in startup sanity check.

### `GET /events`

List events. Query params:
- `status` — `open` filters to active events
- `with_nested_markets=true` — include each event's child markets inline
- `limit`, `cursor` — pagination

Response shape (relevant fields only):

```json
{
  "events": [
    {
      "event_ticker": "KXPRES28",
      "title": "2028 U.S. Presidential Election",
      "markets": [
        { "ticker": "KXPRES28-HARRIS", "status": "active", "close_time": "...", ... }
      ]
    }
  ],
  "cursor": "..."
}
```

### `GET /markets/{ticker}`

Get market details. Relevant response fields: `status`, `close_time`, `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`, `notional_value_dollars`.

Note: prices may be exposed as either `yes_price`/`no_price` in cents (integers 1–99) or `*_dollars` as decimal strings. Accept both.

### `GET /markets/{ticker}/orderbook`

Get orderbook for a market.

**Important:** Kalshi returns **yes bids and no bids only** — no asks. In binary markets, a YES bid at price `X` is equivalent to a NO ask at price `(100 - X)`. Each entry is `[price_cents, size]`.

```json
{
  "orderbook": {
    "yes": [[12, 100], [10, 50], ...],
    "no":  [[85, 80], [80, 40], ...]
  }
}
```

To derive asks from this:
- `yes_ask_cents = 100 - no_best_bid_cents`
- `no_ask_cents  = 100 - yes_best_bid_cents`

### `POST /portfolio/orders`

Place an order.

Request body:

| Field | Type | Notes |
|---|---|---|
| `ticker` | string | Market ticker (required) |
| `side` | `"yes"` \| `"no"` | (required) |
| `action` | `"buy"` \| `"sell"` | (required) |
| `count` | integer | Contract quantity |
| `yes_price` | int 1–99 | Cents. Use for YES-side limits. |
| `no_price` | int 1–99 | Cents. Use for NO-side limits. |
| `type` | `"limit"` \| `"market"` | (required) |
| `time_in_force` | `"fill_or_kill"` \| `"good_till_canceled"` \| `"immediate_or_cancel"` | IOC = `"immediate_or_cancel"` |
| `client_order_id` | string (UUID) | Recommended for idempotency |

Response (success 201):

```json
{
  "order": {
    "order_id": "ord_abc123",
    "ticker": "...",
    "side": "no",
    "action": "buy",
    "count": 10,
    "status": "resting",
    "created_ts": 1678886400000,
    "filled_volume": 0,
    "is_live": true,
    ...
  }
}
```

**Field naming note:** the docs schema lists `id`, but the example response uses `order_id`. Based on observed Kalshi API behavior, use `order_id`. If a production response differs, update this doc.

### `GET /portfolio/orders`

List orders. Query params: `ticker`, `status` (`resting`, `canceled`, `executed`), `min_ts`, `max_ts`.

### `GET /portfolio/orders/{order_id}`

Get a single order. 404 if not found.

### `DELETE /portfolio/orders/{order_id}`

Cancel an order. No bulk-cancel endpoint; iterate for `cancel_all`.

### `GET /portfolio/fills`

List executed fills. Query params: `ticker`, `min_ts`, `max_ts`. Response has `fills` array with `trade_id`, `ticker`, `side`, `yes_price` / `no_price`, `count`, `created_time_unix`.

## Fees

**Formula:** The general historical Kalshi fee formula has been:

```
fee_per_trade = ceil(0.07 × price × (1 - price) × contracts × 100) / 100
```

Where `price` is in dollars (0.00–1.00). This produces a fee in dollars, rounded up to the nearest cent.

**VERIFY BEFORE LIVE TRADING.** The context7 results retrieved on 2026-04-13 covered fee *rounding mechanics* (round fee up to $0.0001, floor balance change to cent) but did not include the current coefficient. Before flipping to production, confirm the coefficient from:
1. Kalshi's fees page: `docs.kalshi.com/getting_started/fees`
2. The user's Kalshi account dashboard (fee schedule shown there)
3. A real demo-env trade (compare estimated to actual)

The code path: `KalshiExchangeClient.estimate_fee(price, contracts)`. Update the coefficient in one place if it differs.

**Rounding:** Kalshi rounds fees up to $0.0001 per trade internally and applies balance adjustments floored to the nearest cent, with a rounding-fee accumulator. For bot-side estimation, ceiling to the nearest cent is sufficient — we log estimated vs actual fees in `trade_ledger` and can refine if drift appears.

## Rate Limits

Not returned in this context7 pass. From prior Kalshi documentation (verify current values):
- Authenticated: several requests per second per key
- Public market data: less restrictive

For this bot's 30s scan cadence, rate limits are not a concern. Handle 429 responses by respecting `Retry-After`.

## Subpenny Pricing

Kalshi supports subpenny prices for some markets. Our longshot-fade strategy only places orders at whole-cent prices (ask from orderbook, which comes as integer cents from the API). No subpenny logic needed in this bot.

## Multivariate Event Collections

Separate endpoint (`/multivariate_event_collections/...`) for combinatoric markets. **Out of scope** for longshot fade phase 1 — the target is standard multi-outcome events exposed via `GET /events?with_nested_markets=true`.

## Open Questions (verify during implementation)

1. **Fee coefficient**: confirm `0.07` is current (see Fees section above).
2. **Order ID field name**: `order_id` vs `id` — use `order_id` per examples; update if a real response differs.
3. **Events endpoint query params**: context7 confirmed `status=open` and `with_nested_markets=true` work together; if not, fetch events list first then enumerate markets per event.
4. **Rate limits**: pull current values from Kalshi's rate-limits page when you have account access.
5. **Close time format**: ISO 8601 string with `Z` suffix observed; confirm timezone handling in real responses.
