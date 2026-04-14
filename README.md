# Nothing Ever Happens — Kalshi Fork

US-accessible fork of [sterlingcrispin/nothing-ever-happens](https://github.com/sterlingcrispin/nothing-ever-happens) retargeted from Polymarket to [Kalshi](https://kalshi.com). Adds a sibling strategy, `longshot_fade`, that applies the "nothing ever happens" thesis per-outcome inside Kalshi multi-outcome events.

The original Polymarket runtime (`nothing_happens`) is preserved and still works; this fork adds a second runtime alongside it.

*FOR ENTERTAINMENT ONLY. PROVIDED AS IS, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED. USE AT YOUR OWN RISK. THE AUTHORS ARE NOT LIABLE FOR ANY CLAIMS, LOSSES, OR DAMAGES.*

![Dashboard screenshot](docs/dashboard.jpg)

- `bot/`: runtime, exchange clients, dashboard, recovery, and strategies (`nothing_happens`, `longshot_fade`)
- `scripts/`: operational helpers for deployed instances and local inspection
- `tests/`: focused unit and regression coverage
- `docs/superpowers/specs/`, `docs/superpowers/plans/`: design spec and implementation plan for the Kalshi port
- `docs/kalshi-api-reference.md`: grounded notes on Kalshi's auth, endpoints, and fees
- `docs/raspberry-pi-setup.md`: end-to-end deployment guide
- `docs/kalshi-demo-smoke-test.md`: demo→prod promotion checklist

## Runtimes

Select via the `STRATEGY` environment variable:

| `STRATEGY` | Venue | Strategy |
|---|---|---|
| `nothing_happens` (default) | Polymarket | Original — buys NO on standalone yes/no markets below a price cap |
| `longshot_fade` | Kalshi | Buys NO on individual outcomes inside multi-outcome events (≥ 3 arms) below a price cap |

Both runtimes share dashboard, DB, recovery, reconciliation, and risk-controls infrastructure.

## Why a Kalshi fork

Polymarket geofences US users. Kalshi is a CFTC-regulated US exchange settling in USD. For operators in the US, Kalshi is the practical venue.

## Safety Model

Real order transmission requires all three environment variables on either runtime:

- `BOT_MODE=live`
- `LIVE_TRADING_ENABLED=true`
- `DRY_RUN=false`

If any of those are missing, the bot uses `PaperExchangeClient` (Polymarket runtime) or refuses order placement (Kalshi runtime).

**Polymarket (`nothing_happens`) additionally requires:**

- `PRIVATE_KEY`
- `FUNDER_ADDRESS` for signature types `1` and `2`
- `DATABASE_URL`
- `POLYGON_RPC_URL` for proxy-wallet approvals and redemption

**Kalshi (`longshot_fade`) additionally requires:**

- `KALSHI_ACCESS_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` (RSA PEM on disk)
- `KALSHI_ENV=demo` or `prod`
- `DATABASE_URL` (SQLite or Postgres)

## Setup

```bash
pip install -r requirements.txt
cp config.example.json config.json
cp .env.example .env
```

`config.json` is intentionally local and ignored by git.

## Configuration

The runtime reads:

- `config.json` for non-secret runtime settings
- `.env` for secrets and runtime flags

Runtime configs live under `strategies.nothing_happens` (Polymarket) and `strategies.longshot_fade` (Kalshi). See [config.example.json](config.example.json) and [.env.example](.env.example).

You can point the runtime at a different config file with `CONFIG_PATH=/path/to/config.json`.

## Running Locally

```bash
STRATEGY=longshot_fade python -m bot.main    # Kalshi runtime
# or
python -m bot.main                            # defaults to nothing_happens (Polymarket)
```

The dashboard binds `$PORT` or `DASHBOARD_PORT` when one is set.

For a set-and-forget Kalshi deployment on a Raspberry Pi, see [docs/raspberry-pi-setup.md](docs/raspberry-pi-setup.md). Before flipping to prod, work through [docs/kalshi-demo-smoke-test.md](docs/kalshi-demo-smoke-test.md).

## Heroku Workflow

The shell helpers use either an explicit app name argument or `HEROKU_APP_NAME`.

```bash
export HEROKU_APP_NAME=<your-app>
./alive.sh
./logs.sh
./live_enabled.sh
./live_disabled.sh
./kill.sh
```

Generic deployment flow:

```bash
heroku config:set BOT_MODE=live DRY_RUN=false LIVE_TRADING_ENABLED=true -a "$HEROKU_APP_NAME"
heroku config:set PRIVATE_KEY=<key> FUNDER_ADDRESS=<addr> POLYGON_RPC_URL=<url> DATABASE_URL=<url> -a "$HEROKU_APP_NAME"
git push heroku <branch>:main
heroku ps:scale web=1 worker=0 -a "$HEROKU_APP_NAME"
```

Only run the `web` dyno. The `worker` entry exists only to fail fast if it is started accidentally.

## Tests

```bash
python -m pytest -q
```

## Included Scripts

| Script | Purpose |
| --- | --- |
| `scripts/db_stats.py` | Inspect live database table counts and recent activity |
| `scripts/export_db.py` | Export live tables from `DATABASE_URL` or a Heroku app |
| `scripts/wallet_history.py` | Pull positions, trades, and balances for the configured wallet |
| `scripts/parse_logs.py` | Convert Heroku JSON logs into readable terminal or HTML output |

## Repository Hygiene

Local config, ledgers, exports, reports, and deployment artifacts are ignored by default.
