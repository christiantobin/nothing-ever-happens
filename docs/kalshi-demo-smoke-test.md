# Kalshi Demo Smoke Test

Before flipping `KALSHI_ENV=prod` + `BOT_MODE=live` + `LIVE_TRADING_ENABLED=true` + `DRY_RUN=false`, run the bot against the Kalshi demo environment for at least 7 days and confirm all of the following.

## 1. Startup

- [ ] `sudo systemctl start nothing-ever-happens`
- [ ] `journalctl -u nothing-ever-happens -n 100` shows `longshot_fade_starting` with the expected config values
- [ ] `curl -fsS http://localhost:8080/health` returns 200
- [ ] `curl -fsS http://localhost:8080/status | jq` returns valid JSON

## 2. Discovery

- [ ] Logs show at least one scan cycle with > 0 discovered events
- [ ] At least one multi-outcome event appears in the Kalshi demo catalog

## 3. Entry (on demo funds)

- [ ] At least one candidate matched `price_cap` and an order was placed
- [ ] `sqlite3 bot.db "SELECT * FROM orders ORDER BY created_at DESC LIMIT 5"` shows rows with the correct `:no` token_id
- [ ] Logged `estimated_fee` matches the Kalshi demo fee schedule to within a cent

## 4. Recovery

- [ ] `sudo systemctl restart nothing-ever-happens`
- [ ] Open orders from before the restart are re-hydrated (check `/status` for `held_market_tickers`)
- [ ] No duplicate orders created for tickers already held

## 5. Resolution

- [ ] Wait for at least one demo market to resolve
- [ ] The corresponding position shows `net_qty=0` and a realized P&L in `positions` table
- [ ] P&L direction matches expectation: `+size` on non-resolution, `-cost` on resolution

## 6. Daily Summary

- [ ] Cron fires at 00:05 UTC
- [ ] Discord/Slack webhook receives a formatted message with non-zero counts
- [ ] Numbers in the message match the DB (`select` them and compare)

## 7. Pause

- [ ] Set `TRADING_PAUSED=1` in `.env`, `sudo systemctl restart nothing-ever-happens`
- [ ] Logs show `trading paused; skipping scan` on each scan cycle
- [ ] No new orders appear
- [ ] Clear `TRADING_PAUSED=`, restart — orders resume

## 8. Health flap

- [ ] Intentionally make the scan loop sleep (edit `scan_interval_seconds` to a huge value and restart)
- [ ] Verify `/health` returns 503 after `2 * scan_interval_seconds`
- [ ] Revert

## 9. Fee estimate vs actual

- [ ] On at least 3 executed demo fills, compare `estimated_fee` (logged at entry) against the `fee` column in the `fills` table
- [ ] Differences should be < $0.01 per contract; larger drift means `KalshiExchangeClient.estimate_fee` needs a coefficient update

Only after all boxes check, proceed to the flip to prod.
