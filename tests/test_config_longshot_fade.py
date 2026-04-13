import json
import pytest

from bot.config import load_longshot_fade_config


def _write_cfg(tmp_path, overrides=None):
    base = {
        "price_cap": 0.08,
        "max_capital_per_outcome": 10.0,
        "max_capital_per_event": 25.0,
        "max_total_exposure": 200.0,
        "min_time_to_resolution_hours": 24,
        "max_time_to_resolution_days": 60,
        "min_outcomes_in_event": 3,
        "scan_interval_seconds": 45,
    }
    if overrides:
        base.update(overrides)
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"strategies": {"longshot_fade": base}}))
    return str(p)


def test_load_longshot_fade_config(tmp_path):
    cfg = load_longshot_fade_config(_write_cfg(tmp_path))
    assert cfg.price_cap == 0.08
    assert cfg.max_total_exposure == 200.0
    assert cfg.scan_interval_seconds == 45


def test_rejects_price_cap_out_of_range(tmp_path):
    with pytest.raises(ValueError, match="price_cap"):
        load_longshot_fade_config(_write_cfg(tmp_path, {"price_cap": 1.5}))


def test_rejects_per_event_below_per_outcome(tmp_path):
    with pytest.raises(ValueError, match="max_capital_per_event"):
        load_longshot_fade_config(
            _write_cfg(tmp_path, {"max_capital_per_outcome": 30.0, "max_capital_per_event": 20.0})
        )


def test_rejects_total_below_per_event(tmp_path):
    with pytest.raises(ValueError, match="max_total_exposure"):
        load_longshot_fade_config(_write_cfg(tmp_path, {"max_total_exposure": 10.0}))


def test_rejects_bad_scan_interval(tmp_path):
    with pytest.raises(ValueError, match="scan_interval_seconds"):
        load_longshot_fade_config(_write_cfg(tmp_path, {"scan_interval_seconds": 1}))


def test_rejects_single_outcome(tmp_path):
    with pytest.raises(ValueError, match="min_outcomes_in_event"):
        load_longshot_fade_config(_write_cfg(tmp_path, {"min_outcomes_in_event": 1}))
