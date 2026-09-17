"""Read only metrics that a bound worker has actually recorded."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen

import db as bot_db
from fleet.reroll_lifetime import read_lifetime


def observed_metrics(worker_root: Path, *, account_key: str, account_id: str,
                     web_port: int, running: bool,
                     fetch: Callable[..., Any] = urlopen) -> dict[str, Any]:
    result: dict[str, Any] = {"milestone": "Reach T1 W60"}
    db_path = Path(worker_root) / "tower_bot.db"
    account_bound = db_path.is_file() and bot_db.bound_account(db_path) == account_id
    if account_bound:
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=.1) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT ended_at, tier, wave, coins FROM runs WHERE ended_at IS NOT NULL "
                    "ORDER BY id DESC LIMIT 3").fetchall()
                best = db.execute(
                    "SELECT MAX(wave) FROM runs WHERE tier=1 AND ended_at IS NOT NULL").fetchone()[0]
                bought = db.execute(
                    "SELECT COUNT(*) FROM ledger WHERE kind='WORKSHOP_BUY' AND dry_run=0 "
                    "AND json_extract(detail, '$.verdict') IN ('bought','free')"
                ).fetchone()[0]
            result["workshop_upgrades_bought"] = bought
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
        lifetime = read_lifetime(Path(worker_root), account_id)
        if lifetime is not None:
            result["lifetime_coins"] = lifetime["lifetime_coins"]
            result["lifetime_coins_incomplete"] = lifetime["coins_incomplete"]
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
            screen = status.get("screen")
            if screen in {"IN_RUN", "MAIN_MENU", "GAME_OVER", "UNKNOWN"}:
                result["game_screen"] = screen
            result["battle_cash"] = status.get("wallet")
            run = status.get("run")
            if isinstance(run, dict):
                result["run_duration_seconds"] = run.get("elapsed")
                if (screen == "IN_RUN" and isinstance(run.get("id"), int)
                        and not isinstance(run["id"], bool) and run["id"] > 0):
                    result["current_run_id"] = run["id"]
            result["observed_at"] = time.time()
        except (OSError, ValueError, TypeError, KeyError):
            pass
    plan_path = Path(worker_root) / "reroll-plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if (account_bound and plan.get("account_id") == account_id
                and isinstance(plan.get("observed_at"), (int, float))
                and time.time() - plan["observed_at"] <= 120):
            result["reroll_plan"] = plan
    except (OSError, ValueError, TypeError):
        pass
    return result
