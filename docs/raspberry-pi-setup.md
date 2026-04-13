# Raspberry Pi Setup Guide

End-to-end setup for running the `longshot_fade` Kalshi strategy on a Raspberry Pi as a set-and-forget systemd service.

## Prerequisites

- Raspberry Pi 4 or newer, running Raspberry Pi OS 64-bit (Bookworm or later recommended — prebuilt `cryptography` wheels are available for ARM64)
- Pi connected to the internet with a stable network (wired preferred)
- A funded Kalshi account with an RSA API key pair generated in the Kalshi dashboard

## 1. System Packages

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv git sqlite3 libffi-dev build-essential
```

## 2. Clone the Fork

```bash
cd /home/pi
git clone https://github.com/christiantobin/nothing-ever-happens.git
cd nothing-ever-happens
git checkout kalshi-longshot-fade
```

## 3. Virtualenv and Dependencies

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Configuration

```bash
cp config.example.json config.json
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

```
STRATEGY=longshot_fade
KALSHI_ENV=demo                  # Flip to "prod" only after demo validation
KALSHI_ACCESS_KEY_ID=<paste from Kalshi dashboard>
KALSHI_PRIVATE_KEY_PATH=/home/pi/kalshi-key.pem
DATABASE_URL=sqlite:////home/pi/nothing-ever-happens/bot.db
DASHBOARD_PORT=8080
DAILY_SUMMARY_WEBHOOK_URL=       # Optional Discord/Slack webhook
TRADING_PAUSED=

# Live-gate (all three required to transmit real orders in prod)
BOT_MODE=paper                   # Set to "live" when ready
LIVE_TRADING_ENABLED=false       # Set to "true" when ready
DRY_RUN=true                     # Set to "false" when ready
```

## 5. Install the RSA Private Key

```bash
# Copy your kalshi-key.pem onto the Pi, e.g. via scp or a USB drive
chmod 600 /home/pi/kalshi-key.pem
```

## 6. systemd Service

```bash
sudo cp deploy/nothing-ever-happens.service /etc/systemd/system/
sudo systemctl daemon-reload
mkdir -p /home/pi/nothing-ever-happens/logs
sudo systemctl enable --now nothing-ever-happens
```

Verify:

```bash
systemctl status nothing-ever-happens
journalctl -u nothing-ever-happens -f    # live tail
```

## 7. Daily Summary Cron

```bash
crontab -e
```

Add:

```
5 0 * * * cd /home/pi/nothing-ever-happens && /home/pi/nothing-ever-happens/.venv/bin/python -m dotenv run -- /home/pi/nothing-ever-happens/.venv/bin/python scripts/daily_summary.py >> /home/pi/nothing-ever-happens/logs/daily_summary.log 2>&1
```

Or the simpler form that sources `.env` directly:

```
5 0 * * * set -a && . /home/pi/nothing-ever-happens/.env && set +a && /home/pi/nothing-ever-happens/.venv/bin/python /home/pi/nothing-ever-happens/scripts/daily_summary.py >> /home/pi/nothing-ever-happens/logs/daily_summary.log 2>&1
```

## 8. Health Check Cron

```
*/5 * * * * curl -fsS http://localhost:8080/health >/dev/null || echo "$(date -Iseconds) bot /health failed" >> /home/pi/nothing-ever-happens/logs/health.log
```

Point this at a webhook or pager instead of a log file if you want push alerts.

## 9. SQLite Backup Cron

```
0 3 * * * sqlite3 /home/pi/nothing-ever-happens/bot.db ".backup /home/pi/nothing-ever-happens/bot.db.bak"
0 4 * * 0 find /home/pi/nothing-ever-happens -name "bot.db.bak" -mtime +14 -delete
```

## 10. NTP Sanity

Kalshi rejects signed requests with skewed timestamps. Confirm NTP is active:

```bash
timedatectl status | grep -E "synchronized|NTP"
```

If not, `sudo systemctl enable --now systemd-timesyncd`.

## Going Live Checklist

**Do not flip the live-gate flags until all boxes are checked.** See `docs/kalshi-demo-smoke-test.md`.

Short form:

1. Run in `KALSHI_ENV=demo` with `BOT_MODE=paper` for ≥ 7 days
2. Confirm at least one demo entry and one demo resolution through the full lifecycle
3. Verify `/health` has never flapped
4. Verify daily summary webhook has fired and looks right
5. Set `KALSHI_ENV=prod`, `BOT_MODE=live`, `LIVE_TRADING_ENABLED=true`, `DRY_RUN=false`
6. `sudo systemctl restart nothing-ever-happens`
7. Watch logs for 30 minutes to confirm the switchover is clean
