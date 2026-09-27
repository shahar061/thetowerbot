"""Read only metrics that a bound worker has actually recorded."""

from __future__ import annotations

from contextlib import closing
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.request import urlopen

import db as bot_db
import upgrades
from fleet.account_metrics import account_metrics
from fleet.build_route_eval import EVIDENCE_MAX_AGE_SECONDS
from runtime_records import RuntimeRecords, RuntimeRecordsError


W20 = 20


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _observed_time(value: object, now: float) -> float | None:
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= now - value <= EVIDENCE_MAX_AGE_SECONDS):
        return None
    return float(value)


def _mission_view(worker_root: Path, *, account_id: str, lease_id: str,
                  attempt_id: str, generation: str, now: float) -> dict[str, Any]:
    unknown = {"state": "unknown", "reason": "Mission evidence unavailable",
               "last_claim_at": None}
    row = _read_record(worker_root / "mission-notification-state.json")
    if row is None or not isinstance(row.get("scope"), dict):
        return unknown
    expected = {"account_id": account_id, "lease_id": lease_id,
                "attempt_id": attempt_id, "generation": generation}
    if any(row['scope'].get(key) != value for key, value in expected.items()):
        return unknown
    # A supplied epoch is never ignored. Legacy sidecars can be shown only
    # before this worker has an authoritative fact-scope producer.
    active = None
    try:
        from evidence_scope import FactScope
        with closing(sqlite3.connect(f"file:{worker_root / 'tower_bot.db'}?mode=ro", uri=True, timeout=.1)) as conn:
            raw = conn.execute('SELECT detail FROM fact_scope WHERE id=1').fetchone()
            active = FactScope(**json.loads(raw[0])) if raw else None
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        pass
    epoch = row['scope'].get('fact_epoch')
    if active is not None:
        if (active.account_id, active.lease_id, active.generation) != (account_id, lease_id, generation):
            return unknown
        if type(epoch) is not int or epoch != active.epoch:
            return unknown
    elif 'fact_epoch' in row['scope']:
        return unknown
    if _observed_time(row.get("observed_at"), now) is None:
        return {**unknown, "reason": "Mission observation is stale or invalid"}
    state, reason, claimed = row.get("state"), row.get("reason"), row.get("last_claim_at")
    if (state not in {"unknown", "pending", "claiming", "clear"}
            or reason is not None and not isinstance(reason, str)
            or claimed is not None and (type(claimed) not in (int, float)
                                        or not math.isfinite(claimed) or not 0 <= claimed <= now)):
        return unknown
    return {"state": state, "reason": reason, "last_claim_at": claimed}


def _matches_live_scope(start: Mapping[str, Any] | None, heartbeat: Mapping[str, Any] | None,
                        process_status: Mapping[str, Any], *, worker: str,
                        account_id: str, lease_id: str, attempt_id: str) -> bool:
    if (start is None or heartbeat is None or start.get("attempt_verified") is not True
            or process_status.get("state") != "running"
            or type(process_status.get("pid")) is not int
            or type(start.get("pid")) is not int
            or type(heartbeat.get("pid")) is not int
            or not isinstance(start.get("generation"), str) or not start["generation"]
            or not isinstance(start.get("boot_id"), str) or not start["boot_id"]):
        return False
    expected = {"worker_id": worker, "lease_id": lease_id, "attempt_id": attempt_id,
                "generation": start["generation"], "boot_id": start["boot_id"],
                "pid": process_status["pid"]}
    return (all(start.get(key) == value for key, value in expected.items())
            and all(heartbeat.get(key) == value for key, value in expected.items())
            and heartbeat.get("account_id") == account_id
            and process_status.get("attempt_id") == attempt_id)


def _monitor_health(monitor: Mapping[str, Any] | None, start: Mapping[str, Any] | None,
                    process_status: Mapping[str, Any], *, worker: str, account_id: str,
                    lease_id: str, attempt_id: str, now: float,
                    recent_progress: bool) -> tuple[str, str | None] | None:
    if (monitor is None or start is None
            or _observed_time(monitor.get("observed_at_utc"), now) is None):
        return None
    expected = {"worker_id": worker, "account_id": account_id,
                "lease_id": lease_id, "attempt_id": attempt_id,
                "generation": start["generation"], "boot_id": start["boot_id"],
                "pid": process_status["pid"]}
    if (type(monitor.get("pid")) is not int
            or not all(monitor.get(key) == value for key, value in expected.items())):
        return None
    state = {
        "healthy": "progressing" if recent_progress else "unknown",
        "expected_wait": "waiting", "paused": "waiting", "restarted": "recovering",
        "wait_expired": "attention", "blocked_scan": "attention",
        "observation_required": "attention", "capability_starved": "attention",
        "identity_conflict": "attention", "quarantined": "attention",
    }.get(monitor.get("state"))
    if state is None:
        return None
    reason = monitor.get("reason")
    return state, reason if isinstance(reason, str) and reason else None


def _overview(worker_root: Path, *, account_id: str, lease_id: str,
              attempt_id: str | None, process_status: Mapping[str, Any],
              monitor_status: Mapping[str, Any] | None,
              live_status: Mapping[str, Any] | None, live_account_verified: bool,
              db_path: Path,
              now: float) -> dict[str, Any] | None:
    """Summarize account-bound evidence without promoting historical facts to live."""
    try:
        records = RuntimeRecords(worker_root / "runtime-records.json").read()
    except RuntimeRecordsError:
        records = None
    start = records.get("start") if records else None
    worker = worker_root.name
    start_matches = (isinstance(start, dict) and start.get("worker_id") == worker
                     and start.get("lease_id") == lease_id
                     and start.get("attempt_verified") is True
                     and isinstance(start.get("attempt_id"), str)
                     and bool(start["attempt_id"])
                     and isinstance(start.get("generation"), str) and bool(start["generation"])
                     and isinstance(start.get("boot_id"), str) and bool(start["boot_id"])
                     and type(start.get("pid")) is int and start["pid"] > 0)
    if attempt_id is None and start_matches:
        attempt_id = start["attempt_id"]
    if not isinstance(attempt_id, str) or not attempt_id:
        return None
    start_matches = start_matches and start.get("attempt_id") == attempt_id
    source = {"revision": start.get("revision") if start_matches else None,
              "hash": start.get("source_hash") if start_matches else None}
    completed = None
    open_run = None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=.1) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT id, tier, wave, coins, ended_at FROM runs WHERE ended_at IS NOT NULL "
                "ORDER BY id DESC LIMIT 1").fetchone()
            if row is not None:
                completed = {"id": row["id"], "tier": row["tier"], "wave": row["wave"],
                             "coins": row["coins"], "ended_at": row["ended_at"]}
            row = connection.execute(
                "SELECT id FROM runs WHERE ended_at IS NULL "
                "ORDER BY id DESC LIMIT 1").fetchone()
            if row is not None:
                open_run = dict(row)
    except (OSError, sqlite3.Error):
        pass
    health_state = "stopped" if process_status.get("state") in {"stopped", "paused"} else "unknown"
    reason = "Worker is stopped" if health_state == "stopped" else "Process evidence unavailable"
    last_scan = None
    last_progress = None
    current = None
    live_scope_verified = False
    heartbeat = _read_record(worker_root / "worker-heartbeat.json")
    if process_status.get("state") == "running":
        identity_matches = (live_account_verified and _matches_live_scope(
            start if start_matches else None, heartbeat, process_status,
            worker=worker, account_id=account_id, lease_id=lease_id, attempt_id=attempt_id))
        if not live_account_verified:
            reason = "Live account selection is unavailable or does not match registration"
        elif not identity_matches:
            reason = "Heartbeat process or account scope does not match the live process"
        else:
            last_progress = _observed_time(heartbeat.get("last_semantic_progress_utc"), now)
            scan = _observed_time(heartbeat.get("last_completed_scan_utc"), now)
            if scan is None or heartbeat.get("observation_required") is True:
                reason = "Completed scan is missing, stale, or needs re-observation"
            else:
                live_scope_verified = True
                last_scan = scan
                wait_reason = heartbeat.get("wait_reason")
                health_state = ("waiting" if isinstance(wait_reason, str) and wait_reason else
                                "progressing" if last_progress is not None else "unknown")
                reason = (wait_reason if health_state == "waiting" else
                          "No recent semantic progress" if health_state == "unknown" else None)
                run_observation = heartbeat.get("run_observation")
                if (isinstance(live_status, Mapping) and live_status.get("screen") == "IN_RUN"
                        and isinstance(live_status.get("run"), dict) and open_run is not None
                        and type(live_status["run"].get("id")) is int
                        and type(heartbeat.get("run_id")) is int
                        and live_status["run"]["id"] == open_run["id"]
                        and heartbeat["run_id"] == open_run["id"]
                        and isinstance(run_observation, dict)
                        and type(run_observation.get("run_id")) is int
                        and run_observation["run_id"] == open_run["id"]
                        and type(heartbeat.get("clock_epoch")) is int
                        and type(run_observation.get("clock_epoch")) is int
                        and run_observation["clock_epoch"] == heartbeat["clock_epoch"]
                        and heartbeat.get("observation_required") is False
                        and (run_at := _observed_time(run_observation.get("observed_at_utc"), now))
                        is not None and run_at >= start["started_at"]
                        and scan >= run_at):
                    wave = run_observation.get("wave")
                    speed = run_observation.get("game_speed")
                    current = {"id": open_run["id"], "tier": None,
                               "wave": wave if type(wave) is int and wave > 0 else None,
                               "speed": speed if type(speed) in (int, float)
                               and math.isfinite(speed) and speed > 0 else None,
                               "coins": None, "observed_at": run_at}
        if (live_account_verified and start_matches
                and type(process_status.get("pid")) is int
                and process_status["pid"] == start["pid"]
                and process_status.get("attempt_id") == attempt_id):
            scoped_monitor = _monitor_health(
                monitor_status, start, process_status, worker=worker,
                account_id=account_id, lease_id=lease_id, attempt_id=attempt_id,
                now=now, recent_progress=last_progress is not None)
            if scoped_monitor is not None:
                health_state, monitor_reason = scoped_monitor
                reason = (monitor_reason or reason or
                          (str(monitor_status.get("state")) if health_state != "progressing" else None))
    strategy = None
    try:
        from fleet.build_route_store import BuildRouteStore
        assignment = BuildRouteStore(worker_root.parent.parent).read().assignments.get(worker)
        if assignment is not None and assignment.account_id == account_id:
            strategy = {"id": assignment.strategy_id, "name": assignment.strategy_name,
                        "version": assignment.strategy_version}
    except (OSError, ValueError):
        pass
    currency = {key: None for key in ("coins_lower", "coins_upper", "reserved",
                                      "available_lower", "gems")}
    if live_scope_verified:
        from currencies import currency_overview
        currency = currency_overview(db_path, account_id=account_id, lease_id=lease_id,
                                     generation=start['generation'], now=now)
    missions = (_mission_view(worker_root, account_id=account_id, lease_id=lease_id,
                              attempt_id=attempt_id, generation=start["generation"], now=now)
                if live_scope_verified else
                {"state": "unknown", "reason": "Mission evidence unavailable", "last_claim_at": None})
    recovery = None
    if live_scope_verified:
        from recovery_status import recovery_overview
        # Scoped to this exact live attempt/generation; its own observation time.
        recovery = recovery_overview(worker_root, account_id=account_id, lease_id=lease_id,
                                     attempt_id=attempt_id, generation=start["generation"],
                                     now=now)
    blockers = [reason] if reason else []
    unknown_count = sum((health_state == "unknown", currency["coins_lower"] is None,
                         currency["gems"] is None, missions["state"] == "unknown",
                         process_status.get("state") == "running" and current is None))
    return {"account_id": account_id, "lease_id": lease_id, "attempt_id": attempt_id,
            "observed_at": now,
            "health": {"state": health_state, "reason": reason,
                       "last_completed_scan_at": last_scan, "last_progress_at": last_progress,
                       "incidents_open": None},
            "current_run": current, "last_completed_run": completed,
            "currency": currency, "missions": missions, "strategy": strategy,
            "source": source, "recovery": recovery,
            "unknown_count": unknown_count,
            "blockers": blockers}


def play_to_t1w20(rows: Iterable[tuple[float, float, int | None, int | None]]
                  ) -> tuple[float | None, float]:
    """Play seconds up to and including the first run ending at T1 W20+.

    `rows` are ended runs `(started_at, ended_at, tier, wave)` in id order.
    Play on every tier counts; only a Tier 1 run stops the clock. Returns
    `(seconds_to_w20, seconds_played)`, the first None until W20.
    """
    played = 0.
    for started_at, ended_at, tier, wave in rows:
        played += max(0., ended_at - started_at)
        if tier == 1 and wave is not None and wave >= W20:
            return played, played
    return None, played


def recent_workshop_purchases(db: sqlite3.Connection, limit: int = 5) -> list[dict[str, Any]]:
    """The latest verified Workshop buys, with what they cost and why.

    Purchases record their reason from the route decision. A weighted draw
    confirmed before that also left a ROUTE_DECISION audit row naming it.
    """
    rows = db.execute(
        "SELECT id, ts, item, category, delta, price, detail FROM ledger "
        "WHERE kind='WORKSHOP_BUY' AND dry_run=0 "
        "AND json_extract(detail, '$.verdict') IN ('bought','free') "
        "ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
    draws: dict[int, float] = {}
    for (detail,) in db.execute(
            "SELECT detail FROM ledger WHERE kind='ROUTE_DECISION' "
            "AND json_extract(detail, '$.purchase_event_id') IN (%s)" % ",".join("?" * len(rows)),
            [row["id"] for row in rows]):
        try:
            audit = json.loads(detail)
            draws[audit["purchase_event_id"]] = audit["eligible_odds"][audit["selected_upgrade_id"]]
        except (ValueError, TypeError, KeyError):
            continue
    recent = []
    for row in rows:
        try:
            reason = json.loads(row["detail"] or "{}").get("reason")
        except (ValueError, AttributeError):
            reason = None
        if reason is None and row["id"] in draws:
            reason = f"Random draw ({draws[row['id']]:.0%})"
        recent.append({"at": row["ts"], "item": row["item"], "category": row["category"],
                       "cost": -row["delta"] if row["delta"] is not None else row["price"],
                       "reason": reason})
    return recent


def read_play(db_path: Path) -> tuple[float | None, float] | None:
    """`play_to_t1w20` over a worker database, or None if it cannot be read."""
    if not Path(db_path).is_file():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=.1) as db:
            rows = db.execute(
                "SELECT started_at, ended_at, tier, wave FROM runs "
                "WHERE ended_at IS NOT NULL ORDER BY id").fetchall()
    except sqlite3.Error:
        return None
    return play_to_t1w20(rows)


def observed_metrics(worker_root: Path, *, account_key: str, account_id: str,
                     web_port: int, running: bool,
                     fetch: Callable[..., Any] = urlopen,
                     process_status: Mapping[str, Any] | None = None,
                     lease_id: str | None = None,
                     attempt_id: str | None = None,
                     monitor_status: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"milestone": "Reach T1 W60"}
    db_path = Path(worker_root) / "tower_bot.db"
    account_bound = db_path.is_file() and bot_db.bound_account(db_path) == account_id
    if account_bound:
        from fleet.build_route_runtime import BuildRouteRuntime
        from fleet.build_route_store import RouteUnavailable
        route_runtime = BuildRouteRuntime(Path(worker_root).parent.parent,
                                          Path(worker_root).name, account_id)
        try:
            route_runtime.current()
            applied = route_runtime.applied_revision()
            if applied is not None:
                result["route_revision_applied"] = applied
                try:
                    battle = json.loads((Path(worker_root) / "build-route-battle.json").read_text())
                    if (battle.get("account_id") == account_id
                            and battle.get("revision") == applied
                            and isinstance(battle.get("evidence_at"), (int, float))
                            and 0 <= time.time() - battle["evidence_at"] <= 10):
                        result["battle_evaluation"] = battle
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
                try:
                    resources = json.loads((Path(worker_root) / "build-route-resources.json").read_text())
                    if (resources.get("account_id") == account_id
                            and resources.get("revision") == applied
                            and isinstance(resources.get("observed_at"), (int, float))
                            and 0 <= time.time() - resources["observed_at"] <= 600):
                        result["resource_evaluation"] = resources
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
        except RouteUnavailable as exc:
            result["route_error"] = exc.reason
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=.1) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT ended_at, tier, wave, coins FROM runs WHERE ended_at IS NOT NULL "
                    "ORDER BY id DESC LIMIT 3").fetchall()
                best = db.execute(
                    "SELECT MAX(wave) FROM runs WHERE tier=1 AND ended_at IS NOT NULL").fetchone()[0]
                result["recent_workshop_purchases"] = recent_workshop_purchases(db)
                bought = db.execute(
                    "SELECT COUNT(*) FROM ledger WHERE kind='WORKSHOP_BUY' AND dry_run=0 "
                    "AND json_extract(detail, '$.verdict') IN ('bought','free')"
                ).fetchone()[0]
                confirmed_unlocks = {row[0] for row in db.execute(
                    "SELECT DISTINCT item FROM ledger WHERE kind='WORKSHOP_BUY' AND dry_run=0 "
                    "AND json_extract(detail, '$.verdict') IN ('bought','free')")}
                observed_unlocks = {(row[0], row[1]) for row in db.execute(
                    "SELECT DISTINCT item, category FROM ledger WHERE kind='BUY_SKIPPED' "
                    "AND reason='already_unlocked' "
                    "AND json_extract(detail, '$.detail')='the rows it grants are on the tab'")}
                bought += sum(1 for item, category in observed_unlocks
                              if item not in confirmed_unlocks
                              and (upgrade := upgrades.resolve(item, category)) is not None
                              and upgrade.unlock)
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
        play = read_play(db_path)
        if play is not None:
            if play[0] is not None:
                result["play_seconds_to_t1w20"] = play[0]
            else:
                result["play_seconds_so_far"] = play[1]
        account = account_metrics(Path(worker_root), account_id)
        if account["lifetime_coins"] is not None:
            result["lifetime_coins"] = account["lifetime_coins"]
            result["lifetime_coins_incomplete"] = account["lifetime_coins_incomplete"]
            result["game_started"] = account["game_started"]
            result["account_age_days"] = account["account_age_days"]
            result["recent_cps"] = account["recent_cps"]
    live_status: Mapping[str, Any] | None = None
    live_account_verified = False
    if running:
        try:
            base = f"http://127.0.0.1:{web_port}"
            with fetch(base + "/api/accounts?local_only=true", timeout=.2) as response:
                catalog = json.load(response)
            if (catalog.get("active") != account_key or not any(
                    item.get("key") == account_key and item.get("account_id") == account_id
                    and item.get("running") is True for item in catalog.get("accounts", []))):
                raise ValueError("live account does not match registration")
            live_account_verified = True
            with fetch(base + "/api/status", timeout=.2) as response:
                status = json.load(response)
            live_status = status if isinstance(status, dict) else None
            screen = status.get("screen")
            if screen in {"IN_RUN", "MAIN_MENU", "GAME_OVER", "UNKNOWN"}:
                result["game_screen"] = screen
            result["battle_cash"] = status.get("wallet")
            if status.get("stalled"):
                result["error"] = f"stalled ({status['stalled']}), paused"
            run = status.get("run")
            if isinstance(run, dict):
                result["run_duration_seconds"] = run.get("elapsed")
                if (screen == "IN_RUN" and isinstance(run.get("id"), int)
                        and not isinstance(run["id"], bool) and run["id"] > 0):
                    result["current_run_id"] = run["id"]
        except (OSError, ValueError, TypeError, KeyError):
            pass
    plan_path = Path(worker_root) / "reroll-plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if (account_bound and plan.get("account_id") == account_id
                and isinstance(plan.get("observed_at"), (int, float))):
            result["reroll_plan"] = plan
    except (OSError, ValueError, TypeError):
        pass
    if (account_bound and isinstance(lease_id, str) and lease_id
            and isinstance(process_status, Mapping)):
        overview = _overview(Path(worker_root), account_id=account_id,
                             lease_id=lease_id, attempt_id=attempt_id,
                             process_status=process_status, monitor_status=monitor_status,
                             live_status=live_status,
                             live_account_verified=live_account_verified,
                             db_path=db_path, now=time.time())
        if overview is not None:
            result["overview"] = overview
    return result
