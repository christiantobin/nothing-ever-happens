"""Post a daily P&L + status summary to a Discord/Slack-compatible webhook.

Reads state from the SQLite/Postgres DB at ``DATABASE_URL`` and posts a one-line
summary to ``DAILY_SUMMARY_WEBHOOK_URL``. Intended to run from cron on the Pi.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from bot.db import fills_table, orders_table, positions_table, trade_events_table


def format_summary(rows: dict) -> str:
    lines = [
        "**Nothing Ever Happens — Daily Summary**",
        f"Open positions: {rows['open_positions']}",
        f"Open notional: ${rows['open_notional']:.2f}",
        f"Realized P&L (24h): ${rows['realized_pnl_24h']:+.2f}",
        f"Total realized P&L: ${rows['total_realized_pnl']:+.2f}",
        f"Fills (24h): {rows['fills_24h']}",
        f"Errors (24h): {rows['errors_24h']}",
    ]
    return "\n".join(lines)


def gather_rows(engine) -> dict:
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    day_ago_ts = day_ago.timestamp()

    with engine.connect() as conn:
        open_positions = conn.execute(
            sa.select(sa.func.count())
            .select_from(positions_table)
            .where(positions_table.c.net_qty > 0)
        ).scalar() or 0

        open_notional = conn.execute(
            sa.select(sa.func.coalesce(sa.func.sum(
                positions_table.c.net_qty * positions_table.c.avg_entry
            ), 0))
            .where(positions_table.c.net_qty > 0)
        ).scalar() or 0.0

        total_realized = conn.execute(
            sa.select(sa.func.coalesce(sa.func.sum(positions_table.c.realized_pnl), 0))
        ).scalar() or 0.0

        realized_24h = conn.execute(
            sa.select(sa.func.coalesce(sa.func.sum(positions_table.c.realized_pnl), 0))
            .where(positions_table.c.last_updated >= day_ago)
        ).scalar() or 0.0

        fills_24h = conn.execute(
            sa.select(sa.func.count())
            .select_from(fills_table)
            .where(fills_table.c.filled_at >= day_ago)
        ).scalar() or 0

        errors_24h = conn.execute(
            sa.select(sa.func.count())
            .select_from(trade_events_table)
            .where(trade_events_table.c.ts >= day_ago_ts)
            .where(trade_events_table.c.error.isnot(None))
        ).scalar() or 0

    return {
        "open_positions": int(open_positions),
        "open_notional": float(open_notional),
        "total_realized_pnl": float(total_realized),
        "realized_pnl_24h": float(realized_24h),
        "fills_24h": int(fills_24h),
        "errors_24h": int(errors_24h),
    }


def post_webhook(url: str, text: str) -> None:
    payload = json.dumps({"content": text}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def main() -> int:
    url = os.environ.get("DAILY_SUMMARY_WEBHOOK_URL", "").strip()
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("DAILY_SUMMARY_WEBHOOK_URL not set", file=sys.stderr)
        return 1
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    from bot.db import create_engine

    engine = create_engine(db_url)
    rows = gather_rows(engine)
    post_webhook(url, format_summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
