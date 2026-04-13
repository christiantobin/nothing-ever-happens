# Kalshi Longshot Fade — Design Spec

**Date:** 2026-04-13
**Author:** christiantobin
**Status:** Draft, pending user review

## Goal

Fork `sterlingcrispin/nothing-ever-happens` to `christiantobin/nothing-ever-happens` and add a sibling strategy that applies the "nothing ever happens" thesis to individual outcomes inside Kalshi multi-outcome events. Bankroll target: $500, set-and-forget on a Raspberry Pi.

Phase 1 (this spec): **longshot fade** on Kalshi.
Phase 2 (separate spec, later): **overround arbitrage** on Kalshi.

## Why Kalshi, not Polymarket

Operator is a US resident. Kalshi is a CFTC-regulated US exchange; Polymarket geofences US users and using it from the US has legal/ToS concerns. Kalshi settles in USD via ACH; no wallet, no gas.

## Strategy Thesis

The parent bot (`nothing_happens`) buys NO on standalone yes/no markets where retail overpays for YES on dramatic low-probability events. This spec extends the same thesis to multi-outcome events: inside a market with N ≥ 3 outcomes, each outcome's YES/NO is effectively a mini binary. Buy NO on any individual outcome priced below a configured cap. Each individual outcome in a many-way market is a longshot — the vast majority of specific predictions don't happen.

The strategy is independent per outcome: no hedging across outcomes, no atomic multi-leg execution. That is phase 2 (overround arbitrage), and is explicitly out of scope here.

## Architecture

### Decision: Option 1 (shared infrastructure, new strategy + exchange client)

- New `bot/exchange/kalshi.py` implementing the existing `ExchangeClient` Protocol from `bot/exchange/base.py`
- New `bot/strategy/longshot_fade.py` alongside `bot/strategy/nothing_happens.py`
- Reuse unchanged: `bot/dashboard.py`, `bot/db.py`, `bot/live_recovery.py`, `bot/risk_controls.py`, `bot/store.py`, `bot/reconcile.py`, `bot/trade_ledger.py`, `bot/portfolio_state.py`, `bot/logging_config.py`
- `bot/main.py` gains strategy selection via env var `STRATEGY` (default: `nothing_happens` for backward compat; set `STRATEGY=longshot_fade` for this sibling)

### Rationale

The `ExchangeClient` Protocol is keyed on `token_id` per method, which accommodates Kalshi's `market_ticker + side` addressing without any interface changes. The existing infrastructure (dashboard, DB, recovery) is genuinely reusable, and a single process that can run either strategy keeps ops simple for set-and-forget.

Option 2 (separate package tree) and Option 3 (rewrite `ExchangeClient` venue-agnostic) were considered and rejected. Option 2 duplicates ops surface for no payoff at $500 scale. Option 3 is unnecessary work that risks regressing the Polymarket path.

## Kalshi Exchange Client (`bot/exchange/kalshi.py`)

### Authentication

- RSA private-key signing. Per-request headers:
  - `KALSHI-ACCESS-KEY`: API key ID from Kalshi dashboard
  - `KALSHI-ACCESS-TIMESTAMP`: current UNIX millis
  - `KALSHI-ACCESS-SIGNATURE`: RSA-PSS signature over `timestamp + method + path`, base64-encoded
- Private key stored as PEM file path in `KALSHI_PRIVATE_KEY_PATH` env var, key ID in `KALSHI_ACCESS_KEY_ID`
- **Verify against current Kalshi docs at implementation time** via context7 MCP; flag any discrepancy before coding the signer

### Environments

| Env | Base URL | Use |
|---|---|---|
| Demo | `https://demo-api.kalshi.co/trade-api/v2` | Dev + paper trading |
| Prod | `https://api.elections.kalshi.com/trade-api/v2` | Live (verify current prod URL at impl time — Kalshi has moved domains) |

Demo environment is the Kalshi equivalent of `PaperExchangeClient`: real API plumbing, fake funds. Selected by `KALSHI_ENV={demo,prod}` env var.

### Identifier Mapping

Existing Protocol uses `token_id: str`. On Kalshi we use a composite:

```
token_id = f"{market_ticker}:{side}"   # e.g., "KXPRES28-DEM-HARRIS:no"
```

Parsed in the client; strategy and store layers treat it opaquely.

### Methods

Implements all 11 methods of `ExchangeClient`:

- `bootstrap_live_trading(token_id)` — no-op on Kalshi (no proxy-wallet approval dance like Polymarket)
- `get_mid_price(token_id)` — `/markets/{ticker}` orderbook, mid of best bid/ask for requested side
- `get_market_rules(token_id)` — returns `MarketRules` with Kalshi tick size ($0.01), min order size (1 contract), and resolution timestamp
- `get_open_orders(token_id)` — `/portfolio/orders?ticker=…&status=resting`
- `get_order(order_id)` — `/portfolio/orders/{id}`
- `place_limit_order(order)` — `/portfolio/orders` with `type=limit`
- `place_market_order(order)` — `/portfolio/orders` with `type=market` (IOC semantics)
- `get_trades(token_id, after_timestamp)` — `/portfolio/fills?ticker=…&min_ts=…`
- `check_order_readiness(order)` — verifies: market open, sufficient balance, order size within limits
- `cancel_order(order_id)` — `DELETE /portfolio/orders/{id}`
- `cancel_all()` — `DELETE /portfolio/orders` (or iterate if endpoint not available)

### Fee Estimation

Kalshi fee formula (verify at implementation time; it has changed historically):

```python
def estimate_fee(price: float, contracts: int) -> float:
    return math.ceil(0.07 * price * (1 - price) * contracts * 100) / 100
```

Exposed as `KalshiExchangeClient.estimate_fee(price, contracts)`. Strategy uses it in every entry-gating check.

### HTTP Layer

- Single `aiohttp.ClientSession` per client instance
- Retry with exponential backoff on 5xx and network errors (3 attempts, 0.5s → 2s)
- Rate limit handling: on 429, respect `Retry-After` header
- All request/response pairs logged in structured JSON via existing `logging_config.py`

## Strategy: Longshot Fade (`bot/strategy/longshot_fade.py`)

### Scan Loop

Runs every `SCAN_INTERVAL_SECONDS` (default 30):

1. Fetch open Kalshi events via `/events?status=open&with_nested_markets=true`
2. Filter to events with `len(markets) ≥ min_outcomes_in_event` (default 3)
3. For each outcome (market) inside each event:
   - Skip if already holding a position on this `market_ticker`
   - Skip if event's resolution time is outside `[min_time_to_resolution, max_time_to_resolution]`
   - Fetch NO ask for the outcome
   - If `no_ask ≤ price_cap`, compute desired contracts from `max_capital_per_outcome`
   - Apply all risk controls (per-event cap, global exposure cap, min liquidity at top of book)
   - If all gates pass: place single limit buy on NO at `no_ask`

### Entry Gate

Single rule: `no_ask ≤ price_cap` and monetary caps not exceeded. Estimated fee is computed and logged for every entry (for P&L attribution), but is not a gate — the price cap already encodes the edge assumption.

### Position Lifecycle

- Hold to Kalshi auto-settlement. NO pays $1 if the outcome does not resolve, $0 if it does
- No mid-life management, no stop-loss, no partial exit
- Existing `reconcile.py` marks positions resolved and realizes P&L when Kalshi settles the market
- Existing `live_recovery.py` handles restart-mid-trade: reconciles open orders + fills on boot

### Risk Controls (defaults for $500 bankroll)

All configurable via `config.json` under `strategies.longshot_fade`:

| Param | Default | Rationale |
|---|---|---|
| `price_cap` | `0.10` | Market implies ≥ 90% "nothing happens" |
| `max_capital_per_outcome` | `$15` | ~3% bankroll; many small bets |
| `max_capital_per_event` | `$30` | Don't stack correlated longshots in one event |
| `max_total_exposure` | `$250` | 50% bankroll ceiling |
| `min_time_to_resolution` | `24h` | Avoid same-day resolution with no "nothing happens" runway |
| `max_time_to_resolution` | `90 days` | Capital opportunity cost |
| `min_outcomes_in_event` | `3` | Binaries are out of scope |
| `scan_interval_seconds` | `30` | Arbs persist; no HFT needed |

**Philosophy:** Every rule above is a **hard mechanical cap or binary filter**. No runtime judgment, no "is this edge good enough" gates. If the price cap is met and the monetary caps aren't exceeded, the bot trades. Operator mindset stays consistent: commit to "nothing ever happens," let the numbers do the work. Fees are logged on every entry for after-the-fact P&L attribution, not gated on at entry time (price cap already bakes in the edge assumption).

Reuses existing `bot/risk_controls.py` for: daily loss cap, global kill switch, `TRADING_PAUSED` env-var pause.

### Failure Modes

| Scenario | Handling |
|---|---|
| Kalshi API down during scan | Skip cycle, retry next scan; alert after 3 consecutive failures |
| Order rejected (insufficient balance, market closed) | Log, skip; do not retry same market this cycle |
| Bot crashes mid-order | `live_recovery.py` reconciles order+fill state from Kalshi on restart |
| Clock skew | Startup check rejects trading if NTP offset > 5s; alert |
| Fee formula changed | Startup sanity check against a known market; refuse to trade if mismatch |
| Kalshi schema changed | JSON parsing errors; alert and halt |

## Database

SQLite via `DATABASE_URL=sqlite:///bot.db`. Existing schema in `bot/db.py` and `bot/trade_ledger.py` is reused unchanged. No Kalshi-specific tables needed — the existing `positions`, `orders`, `trades`, and `market_metadata` tables accept Kalshi tokens identically to Polymarket tokens.

Backup: daily cron `sqlite3 bot.db ".backup bot.db.bak"` on the Pi, rotated weekly.

## Configuration

### `config.json` additions

```json
{
  "strategies": {
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
  }
}
```

### `.env` additions

```
STRATEGY=longshot_fade
KALSHI_ENV=demo
KALSHI_ACCESS_KEY_ID=<from Kalshi dashboard>
KALSHI_PRIVATE_KEY_PATH=/home/pi/kalshi-key.pem
DATABASE_URL=sqlite:////home/pi/nothing-ever-happens/bot.db
DISCORD_WEBHOOK_URL=<optional, for daily summaries>
DASHBOARD_PORT=8080
TRADING_PAUSED=false
```

Live trading still requires the existing three-gate: `BOT_MODE=live`, `LIVE_TRADING_ENABLED=true`, `DRY_RUN=false`. Demo environment is selected by `KALSHI_ENV=demo` regardless of the live-gate values.

## Hosting: Raspberry Pi

### Process Supervision (`systemd`)

`/etc/systemd/system/nothing-ever-happens.service`:

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

### Watchdog

- `bot/dashboard.py` gains `GET /health` — returns 200 if last scan tick is within `2 * scan_interval_seconds`, else 503
- Cron every 5 min: `curl -fsS localhost:8080/health || notify-webhook "health check failed"`
- Daily cron at 00:00 UTC: `bot/scripts/daily_summary.py` posts P&L + open positions + error count to `DISCORD_WEBHOOK_URL`

### Startup Sanity Checks

Before the scan loop starts, `main.py` verifies:

1. Kalshi API reachable (`GET /exchange/status`)
2. DB schema current (sqlalchemy migration check)
3. `TRADING_PAUSED` is respected from the start

Any failure → log, post to webhook, halt (do not trade). No brittle sanity checks (e.g. "fee formula matches hardcoded value") — if Kalshi changes the formula, logged fees and P&L will surface the change faster than a synthetic pre-flight check would.

## Testing

### Unit tests (`tests/`)

- `tests/test_kalshi.py` — mirror `test_polymarket_clob.py`. Mocked HTTP covering: RSA signature correctness, price parsing, order placement happy-path, rate-limit handling, error responses (insufficient funds, market closed, invalid ticker)
- `tests/test_longshot_fade.py` — mirror `test_nothing_happens.py`. Covers: entry gating against all risk-control limits, price cap logic, event/outcome filtering, fee-inclusive edge calculation, time-to-resolution bounds
- `tests/test_main_runtime_kalshi.py` — `STRATEGY=longshot_fade` wires up `KalshiExchangeClient` + `longshot_fade` strategy; existing `STRATEGY=nothing_happens` path still wires up the Polymarket stack

### Integration tests

- Run full bot in demo mode against `demo-api.kalshi.co` for 1 week before flipping to prod
- Verify: at least 1 simulated entry, correct fee accounting, correct P&L recording on a resolved market, recovery after forced restart mid-trade

## Fork Logistics

1. `gh repo fork sterlingcrispin/nothing-ever-happens --clone=false` creates `christiantobin/nothing-ever-happens`
2. In the existing local checkout at `/home/cjtobin/vscode/nothing-ever-happens`:
   - Rename current `origin` → `upstream`: `git remote rename origin upstream`
   - Add fork as `origin`: `git remote add origin https://github.com/christiantobin/nothing-ever-happens.git`
3. Create branch `kalshi-longshot-fade`, push to `origin`
4. All work happens on that branch; `main` tracks `upstream/main` for future sync

## User Prerequisites (in parallel with implementation)

1. Sign up at kalshi.com, complete KYC (SSN required)
2. Fund with $500 via ACH
3. Generate RSA API key pair in Kalshi dashboard, download the PEM private key
4. Save PEM to Pi at `/home/pi/kalshi-key.pem`, `chmod 600`
5. Save key ID to Pi's `.env` as `KALSHI_ACCESS_KEY_ID`

None of these block implementation — the exchange client and strategy can be developed and unit-tested against mocked HTTP, and integration-tested against demo env (verify at implementation time whether Kalshi's demo environment requires KYC or provides a separate sandbox credential flow; if real KYC is required before any API access, steps 1–3 become a prerequisite for integration testing rather than only for live trading).

## Phase 2 (Out of Scope for This Spec)

Overround arbitrage (`bot/strategy/overround_arb.py`) will reuse the Kalshi exchange client and all shared infrastructure built in Phase 1. Separate spec will cover atomic N-leg execution, partial-fill liquidation, and the different capital model. Not built now; noted so that the Phase 1 Kalshi client is designed to accommodate it (specifically: order placement must support batched/concurrent `asyncio.gather` patterns, and fee estimation must be callable without side effects).

## Open Items to Verify at Implementation Time

These are known unknowns that should be resolved with live Kalshi docs (via context7 MCP) before writing the exchange client:

1. Exact current Kalshi API base URL for production (has moved domains historically)
2. Exact current fee formula and rate (has changed historically)
3. Whether market-data endpoints require auth in the current API version
4. Exact RSA signature format — PSS vs PKCS#1 v1.5, hash algorithm, canonicalization of path/query
5. Market-group vs event-group terminology in current API response schema
6. Rate limits (requests/min) for authenticated and anonymous endpoints

Each of these, if different from this spec's assumption, will be flagged and the spec updated before implementation proceeds.
