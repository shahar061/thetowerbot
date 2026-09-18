"""Account rate and age come from persisted, account-bound Stats evidence."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import db
from fleet.account_metrics import account_metrics, game_started_date


def test_game_started_requires_a_valid_day_precision_date() -> None:
    assert game_started_date("August 29 2026") == "2026-08-29"
    assert game_started_date("August 32 2026") is None
    assert game_started_date("29/08/2026") is None


def test_metrics_use_game_date_and_saved_recent_rate(tmp_path: Path) -> None:
    db.bind_account(tmp_path / "tower_bot.db", "ACCOUNT-A")
    (tmp_path / "reroll-lifetime.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "lifetime_coins": 1000,
        "observed_at": 10, "baseline_run_id": 0,
        "game_started": "2026-08-29", "recent_coins_per_hour": 720,
    }))
    result = account_metrics(tmp_path, "ACCOUNT-A", today=date(2026, 9, 18))
    assert result["account_age_days"] == 20
    assert result["recent_cps"] == .2
    assert result["lifetime_coins"] == 1000
    assert account_metrics(tmp_path, "ACCOUNT-B")["account_age_days"] is None


def test_unreadable_or_future_creation_date_stays_unknown(tmp_path: Path) -> None:
    db.bind_account(tmp_path / "tower_bot.db", "ACCOUNT-A")
    path = tmp_path / "reroll-lifetime.json"
    path.write_text(json.dumps({"account_id": "ACCOUNT-A", "lifetime_coins": 5,
                                "observed_at": 10, "game_started": "2026-09-19"}))
    result = account_metrics(tmp_path, "ACCOUNT-A", today=date(2026, 9, 18))
    assert result["account_age_days"] is None
    assert result["recent_cps"] is None
