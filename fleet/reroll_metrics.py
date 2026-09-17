"""Read only metrics that a bound worker has actually recorded."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen


def observed_metrics(worker_root: Path, *, account_key: str, account_id: str,
                     web_port: int, running: bool,
                     fetch: Callable[..., Any] = urlopen) -> dict[str, Any]:
    result: dict[str, Any] = {"milestone": "Reach T1 W60"}
    db_path = Path(worker_root) / "tower_bot.db"
    if db_path.is_file():
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=.1) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT ended_at, tier, wave, coins FROM runs WHERE ended_at IS NOT NULL "
                    "ORDER BY id DESC LIMIT 3").fetchall()
                best = db.execute(
                    "SELECT MAX(wave) FROM runs WHERE tier=1 AND ended_at IS NOT NULL").fetchone()[0]
            result["best_tier_1_wave"] = best
            result["milestone"] = (
                "T1 W60 reached · earn stones" if best is not None and best >= 60
                else "Reach T1 W60")
            result["recent_runs"] = [
                f"T{row['tier'] if row['tier'] is not None else '?'} "
                f"W{row['wave'] if row['wave'] is not None else '?'} · "
                f"{row['coins'] if row['coins'] is not None else '?'} coins"
                for row in rows]
            if rows:
                result["tier"] = rows[0]["tier"]
                result["wave"] = rows[0]["wave"]
                result["run_coins"] = rows[0]["coins"]
                result["observed_at"] = rows[0]["ended_at"]
        except (OSError, sqlite3.Error):
            pass
    if running:
        try:
            base = f"http://127.0.0.1:{web_port}"
            with fetch(base + "/api/accounts?local_only=true", timeout=.2) as response:
                catalog = json.load(response)
            if (catalog.get("active") != account_key or not any(
                    item.get("key") == account_key and item.get("account_id") == account_id
                    and item.get("running") is True for item in catalog.get("accounts", []))):
                return result
            with fetch(base + "/api/status", timeout=.2) as response:
                status = json.load(response)
            result["wallet_coins"] = status.get("wallet")
            run = status.get("run")
            if isinstance(run, dict):
                result["run_duration_seconds"] = run.get("elapsed")
            result["observed_at"] = time.time()
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return result
