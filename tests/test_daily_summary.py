from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from bot.db import (
    create_engine,
    fills_table,
    metadata,
    orders_table,
    positions_table,
    trade_events_table,
)
from scripts.daily_summary import format_summary, gather_rows


def test_format_summary_includes_fields():
    rows = {
        "open_positions": 12,
        "open_notional": 85.5,
        "total_realized_pnl": 10.25,
        "realized_pnl_24h": 3.2,
        "fills_24h": 4,
        "errors_24h": 2,
    }
    text = format_summary(rows)
    assert "12" in text
    assert "85.50" in text
    assert "+3.20" in text
    assert "Errors (24h): 2" in text


def test_gather_rows_empty_db(tmp_path):
    db_path = tmp_path / "empty.db"
    engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(engine)

    rows = gather_rows(engine)
    assert rows == {
        "open_positions": 0,
        "open_notional": 0.0,
        "total_realized_pnl": 0.0,
        "realized_pnl_24h": 0.0,
        "fills_24h": 0,
        "errors_24h": 0,
    }


def test_gather_rows_with_data(tmp_path):
    db_path = tmp_path / "data.db"
    engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(engine)

    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            positions_table.insert().values(
                token_id="E1-A:no",
                net_qty=10,
                avg_entry=0.05,
                realized_pnl=1.25,
                last_updated=now,
            )
        )
        conn.execute(
            positions_table.insert().values(
                token_id="E1-B:no",
                net_qty=20,
                avg_entry=0.08,
                realized_pnl=0.0,
                last_updated=now,
            )
        )
        conn.execute(
            positions_table.insert().values(
                token_id="CLOSED:no",
                net_qty=0,
                avg_entry=0.0,
                realized_pnl=2.5,
                last_updated=now,
            )
        )
        conn.execute(
            fills_table.insert().values(
                fill_id="f1",
                order_id="o1",
                token_id="E1-A:no",
                side="BUY",
                price=0.05,
                size=10,
                fee=0.02,
                filled_at=now,
            )
        )

    rows = gather_rows(engine)
    assert rows["open_positions"] == 2
    # 10 * 0.05 + 20 * 0.08 = 2.10
    assert rows["open_notional"] == pytest.approx(2.10)
    # 1.25 + 0.0 + 2.5 = 3.75
    assert rows["total_realized_pnl"] == pytest.approx(3.75)
    assert rows["fills_24h"] == 1
