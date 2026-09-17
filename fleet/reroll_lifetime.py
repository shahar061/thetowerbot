"""Account-bound lifetime coin baseline plus verified completed run payouts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import db


def read_lifetime(root: Path, account_id: str) -> dict[str, Any] | None:
    root = Path(root)
    database = root / "tower_bot.db"
    if not database.is_file() or db.bound_account(database) != account_id:
        return None
    try:
        record = json.loads((root / "reroll-lifetime.json").read_text())
        baseline = record.get("lifetime_coins")
        run_id = record.get("baseline_run_id", 0)
        if (record.get("account_id") != account_id or not isinstance(baseline, int)
                or isinstance(baseline, bool) or baseline < 0
                or not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 0
                or not isinstance(record.get("observed_at"), (int, float))):
            return None
        with db.reader(database) as connection:
            missing_id = connection.execute(
                "SELECT MIN(id) FROM runs WHERE id>? AND ended_at IS NOT NULL AND coins IS NULL",
                (run_id,),
            ).fetchone()[0]
            count, extra = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(coins),0) FROM runs "
                "WHERE id>? AND ended_at IS NOT NULL AND (? IS NULL OR id<?)",
                (run_id, missing_id, missing_id),
            ).fetchone()
        return {**record, "lifetime_coins": baseline + extra,
                "later_runs": count, "coins_incomplete": missing_id is not None}
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return None
