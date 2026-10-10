"""Reroll overview reports only verified observations."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import db as bot_db
from fleet.reroll_metrics import _observed_time, observed_metrics, play_to_t1w20, read_play
from fleet.build_route import RouteDocument
from fleet.build_route_runtime import BuildRouteRuntime
from fleet.build_route_store import BuildRouteStore
from fleet.identity import Attempt
from runtime_identity import BackendIdentity
from runtime_records import RuntimeRecords
from runtime_progress import ProgressRecorder


class Response(io.BytesIO):
    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_saved_runs_and_verified_live_wallet(tmp_path: Path) -> None:
    db = tmp_path / "tower_bot.db"
    bot_db.bind_account(db, "42")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO runs (id, started_at, ended_at, tier, wave, coins) "
                     "VALUES (1, 1, 2, 1, 75, 150)")
        for verdict in ("bought", "free", "unproven"):
            conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,dry_run,detail) "
                         "VALUES(1,'WORKSHOP_BUY','Damage','ATTACK','coins',0,?)",
                         (json.dumps({"verdict": verdict}),))
    def fetch(url: str, *, timeout: float) -> Response:
        assert timeout == .2
        if url.endswith("/api/accounts?local_only=true"):
            return Response(b'{"active":"worker:Air_2","accounts":[{"key":"worker:Air_2","account_id":"42","running":true}]}')
        return Response(b'{"screen":"IN_RUN","wallet":230,"run":{"id":9,"elapsed":31.5}}')

    result = observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                              web_port=10002, running=True, fetch=fetch)
    assert result["best_tier_1_wave"] == 75
    assert result["run_coins"] == 150
    assert result["battle_cash"] == 230
    assert result["run_duration_seconds"] == 31.5
    assert result["game_screen"] == "IN_RUN"
    assert result["current_run_id"] == 9
    assert result["workshop_upgrades_bought"] == 2


def test_overview_keeps_stopped_run_historical(tmp_path: Path) -> None:
    database = tmp_path / "tower_bot.db"
    bot_db.bind_account(database, "42")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO runs(id, started_at, ended_at, tier, wave, coins) "
                           "VALUES(7, 1, 2, 1, 200, 123)")
    result = observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                              web_port=0, running=False,
                              lease_id="lease-1", attempt_id="attempt-1",
                              process_status={"state": "stopped"})
    overview = result["overview"]
    assert overview["health"]["state"] == "stopped"
    assert overview["current_run"] is None
    assert overview["last_completed_run"] == {
        "id": 7, "tier": 1, "wave": 200, "coins": 123, "ended_at": 2}
    assert overview["currency"]["coins_lower"] is None
    assert overview["missions"]["state"] == "unknown"
    assert result["observed_at"] == 2


def test_current_overview_requires_matching_fresh_process_and_account(tmp_path: Path) -> None:
    worker_root = tmp_path / "Air_2"
    worker_root.mkdir()
    database = worker_root / "tower_bot.db"
    bot_db.bind_account(database, "42")
    now = time.time()
    with sqlite3.connect(database) as connection:
        bot_db.start_run(connection, 9, now - 10)
    attempt = Attempt("Air_2", "endpoint-1", "lease-1", "attempt-1", "generation-1", now - 10)
    RuntimeRecords(worker_root / "runtime-records.json").start(
        attempt, BackendIdentity("rev-1", "hash-1", now - 10), boot_id="boot-1", pid=111)
    progress = ProgressRecorder(worker_root / "worker-heartbeat.json", attempt=attempt,
                                account_id="42", boot_id="boot-1", pid=111)
    with progress.phase("capture", 20):
        progress.observe_capture()
    progress.observe_run(9, wave=31, game_speed=2.5, observed_at=time.time())
    progress.meaningful_progress("wave", "9:31")
    progress.complete_scan(9, "frame-9")
    heartbeat = progress.snapshot()

    selected = {"key": "worker:Air_2", "run_id": 9}
    push = {"account": "42", "mode": "push", "phase": "pushing", "every": 10,
            "farms_remaining": 0, "farm_tier": 1, "target_tier": 2, "blocker": None}

    def fetch(url: str, *, timeout: float) -> Response:
        if url.endswith("/api/accounts?local_only=true"):
            return Response(json.dumps({"active": selected["key"], "accounts": [
                {"key": "worker:Air_2", "account_id": "42", "running": True}]}).encode())
        return Response(json.dumps({"screen": "IN_RUN", "run": {"id": selected["run_id"]},
                                    "wallet": 230, "bot": {"push_runs": push}}).encode())

    def read(**kwargs: object) -> dict[str, Any]:
        return observed_metrics(worker_root, account_key="worker:Air_2", account_id="42",
                                web_port=10002, running=True, fetch=fetch,
                                lease_id="lease-1", attempt_id="attempt-1",
                                process_status={"state": "running", "pid": 111,
                                                "attempt_id": "attempt-1"}, **kwargs)

    current = read()["overview"]
    assert current["push_runs"] == push
    push["account"] = "old-account"
    assert read()["overview"]["push_runs"] is None
    push["account"] = "42"
    assert current["current_run"] == {"id": 9, "tier": None, "wave": 31,
                                      "speed": 2.5, "coins": None,
                                      "observed_at": heartbeat["run_observation"]["observed_at_utc"]}
    assert current["source"] == {"revision": "rev-1", "hash": "hash-1"}
    mission_path = worker_root / "mission-notification-state.json"
    mission = {"scope": {"account_id": "42", "lease_id": "lease-1",
                         "attempt_id": "attempt-1", "generation": "generation-1"},
               "observed_at": now - 2, "state": "pending", "reason": "Mission badge confirmed",
               "last_claim_at": now - 60}
    mission_path.write_text(json.dumps(mission))
    assert read()["overview"]["missions"] == {
        "state": "pending", "reason": "Mission badge confirmed", "last_claim_at": now - 60}
    assert read()["overview"]["recovery"] is None  # No producer yet: unknown.
    recovery = {"schema_version": 1, "scope": {
        "worker": "Air_2", "account_id": "42", "lease_id": "lease-1", "attempt_id": "attempt-1",
        "attempt_generation": "generation-1"}, "mode": "shadow", "phase": "provider",
        "calls_remaining": 1, "observed_at": now - 2, "api_key": "synthetic-secret"}
    (worker_root / "recovery-status.json").write_text(json.dumps(recovery))
    view = read()["overview"]["recovery"]
    assert view["mode"] == "shadow" and view["calls_remaining"] == 1
    assert view["observed_at"] == now - 2 and "synthetic-secret" not in json.dumps(view)
    selected["key"] = "worker:other"
    changed_account = read()["overview"]
    assert changed_account["push_runs"] is None
    assert changed_account["recovery"] is None
    assert changed_account["current_run"] is None
    assert changed_account["missions"]["state"] == "unknown"
    selected["key"] = "worker:Air_2"
    mission["scope"]["attempt_id"] = "previous-attempt"
    mission_path.write_text(json.dumps(mission))
    assert read()["overview"]["missions"]["state"] == "unknown"
    mission["scope"]["attempt_id"] = "attempt-1"
    mission["observed_at"] = now - 1000
    mission_path.write_text(json.dumps(mission))
    assert read()["overview"]["missions"]["state"] == "unknown"
    mission_path.unlink()
    monitor = {"worker_id": "Air_2", "account_id": "42", "lease_id": "lease-1",
               "attempt_id": "attempt-1", "generation": "generation-1", "boot_id": "boot-1",
               "pid": 111, "observed_at_utc": now - 1,
               "state": "blocked_scan", "reason": "Scan deadline elapsed"}
    attention = read(monitor_status=monitor)["overview"]
    assert attention["health"]["state"] == "attention"
    assert attention["health"]["reason"] == "Scan deadline elapsed"
    no_reason = read(monitor_status={**monitor, "state": "observation_required", "reason": None})["overview"]
    assert no_reason["health"]["state"] == "attention"
    assert no_reason["health"]["reason"] == "observation_required"
    assert read(monitor_status={"state": "identity_conflict", "reason": "wrong process"})["overview"]["health"]["state"] != "attention"
    assert read(monitor_status={**monitor, "attempt_id": "previous"})["overview"]["health"]["state"] == "progressing"
    assert read(monitor_status={**monitor, "observed_at_utc": now - 1000})["overview"]["health"]["state"] == "progressing"
    assert read(monitor_status={**monitor, "observed_at_utc": now + 1000})["overview"]["health"]["state"] == "progressing"
    selected["run_id"] = 10
    assert read()["overview"]["current_run"] is None
    selected["run_id"] = 9
    heartbeat["last_completed_scan_utc"] = now - 1000
    (worker_root / "worker-heartbeat.json").write_text(json.dumps(heartbeat))
    blocked = read(monitor_status=monitor)["overview"]
    assert blocked["current_run"] is None
    assert blocked["health"]["state"] == "attention"
    assert read(monitor_status={**monitor, "state": "restarted"})["overview"]["health"]["state"] == "recovering"
    heartbeat["last_completed_scan_utc"] = now - 2
    (worker_root / "worker-heartbeat.json").write_text(json.dumps(heartbeat))
    heartbeat["last_semantic_progress_utc"] = now - 1000
    (worker_root / "worker-heartbeat.json").write_text(json.dumps(heartbeat))
    no_progress = read()["overview"]
    assert no_progress["health"]["state"] == "unknown"
    assert no_progress["health"]["last_progress_at"] is None
    heartbeat["last_semantic_progress_utc"] = now - 3
    heartbeat["account_id"] = "other"
    (worker_root / "worker-heartbeat.json").write_text(json.dumps(heartbeat))
    mismatched = read()["overview"]
    assert mismatched["current_run"] is None
    assert mismatched["health"]["state"] == "unknown"
    assert "account" in mismatched["health"]["reason"]
    heartbeat["account_id"] = "42"
    heartbeat["last_completed_scan_utc"] = now - 1000
    (worker_root / "worker-heartbeat.json").write_text(json.dumps(heartbeat))
    stale = read()["overview"]
    assert stale["current_run"] is None
    assert stale["health"]["state"] == "unknown"
    baseline = progress.snapshot()
    for field, wrong in (("lease_id", "old-lease"), ("attempt_id", "old-attempt"),
                         ("generation", "old-generation"), ("boot_id", "old-boot"),
                         ("pid", 999), ("pid", True), ("run_id", True)):
        altered = {**baseline, field: wrong}
        (worker_root / "worker-heartbeat.json").write_text(json.dumps(altered))
        assert read()["overview"]["current_run"] is None, field
    for field, wrong in (("run_id", 10), ("observed_at_utc", now + 1000),
                         ("observed_at_utc", now - 1000),
                         ("observed_at_utc", now - 11), ("clock_epoch", True)):
        altered = {**baseline, "run_observation": {**baseline["run_observation"], field: wrong}}
        (worker_root / "worker-heartbeat.json").write_text(json.dumps(altered))
        assert read()["overview"]["current_run"] is None, field
    before_completion = {**baseline, "last_completed_scan_utc":
                         baseline["run_observation"]["observed_at_utc"] - .01}
    (worker_root / "worker-heartbeat.json").write_text(json.dumps(before_completion))
    assert read()["overview"]["current_run"] is None
    (worker_root / "worker-heartbeat.json").write_text(json.dumps(baseline))
    assert read()["overview"]["current_run"]["wave"] == 31
    conflicting = {**baseline, "boot_id": "old-boot"}
    (worker_root / "worker-heartbeat.json").write_text(json.dumps(conflicting))
    identity_alert = read(monitor_status={**monitor, "state": "identity_conflict",
                                          "reason": "heartbeat_generation_conflict"})["overview"]
    assert identity_alert["current_run"] is None
    assert identity_alert["health"]["state"] == "attention"
    (worker_root / "worker-heartbeat.json").write_text(json.dumps(baseline))
    wrong_process = observed_metrics(
        worker_root, account_key="worker:Air_2", account_id="42", web_port=10002,
        running=True, fetch=fetch, lease_id="lease-1", attempt_id="attempt-1",
        process_status={"state": "running", "pid": True, "attempt_id": "attempt-1"})
    assert wrong_process["overview"]["current_run"] is None


def test_freshness_boundary_and_invalid_runtime_record_fail_closed(tmp_path: Path) -> None:
    assert _observed_time(880., 1000.) == 880.
    assert _observed_time(879.999, 1000.) is None
    assert _observed_time(1000.001, 1000.) is None
    root = tmp_path / "workers" / "Air_2"
    root.mkdir(parents=True)
    bot_db.bind_account(root / "tower_bot.db", "42")
    now = time.time()
    attempt = Attempt("Air_2", "endpoint-1", "lease-1", "attempt-1", "gen-1", now - 10)
    record = root / "runtime-records.json"
    RuntimeRecords(record).start(attempt, BackendIdentity("rev-1", "hash-1", now - 10),
                                 boot_id="boot-1", pid=111)
    def read(attempt_value: object = "attempt-1") -> dict[str, Any]:
        return observed_metrics(root, account_key="worker:Air_2", account_id="42",
                                web_port=0, running=False, lease_id="lease-1",
                                attempt_id=attempt_value, process_status={"state": "stopped"})
    assert read()["overview"]["source"] == {"revision": "rev-1", "hash": "hash-1"}
    assert "overview" not in read(True)
    valid = json.loads(record.read_text())
    for field, wrong in (("source_hash", 123), ("pid", True), ("attempt_id", False)):
        corrupt = json.loads(json.dumps(valid))
        corrupt["start"][field] = wrong
        record.write_text(json.dumps(corrupt))
        assert read()["overview"]["source"] == {"revision": None, "hash": None}


def test_worker_overview_includes_persisted_age_and_cps(tmp_path: Path) -> None:
    bot_db.bind_account(tmp_path / "tower_bot.db", "42")
    (tmp_path / "reroll-lifetime.json").write_text(json.dumps({
        "account_id": "42", "lifetime_coins": 1000, "observed_at": 10,
        "game_started": "2026-08-29", "recent_coins_per_hour": 720,
    }))
    result = observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                              web_port=0, running=False)
    assert result["game_started"] == "2026-08-29"
    assert result["recent_cps"] == .2
    assert isinstance(result["account_age_days"], int)


def test_malformed_lifetime_record_does_not_break_worker_metrics(tmp_path: Path) -> None:
    bot_db.bind_account(tmp_path / "tower_bot.db", "42")
    (tmp_path / "reroll-lifetime.json").write_text("[]", encoding="utf-8")
    result = observed_metrics(tmp_path, account_key="worker:Air18", account_id="42",
                              web_port=0, running=False)
    assert "lifetime_coins" not in result


def test_visible_unlock_grants_count_once_without_claiming_a_price(tmp_path: Path) -> None:
    db = tmp_path / "tower_bot.db"
    bot_db.bind_account(db, "42")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO ledger(ts,kind,item,category,reason,detail) "
                     "VALUES(1,'BUY_SKIPPED','Unlock Cash Bonuses','UTILITY',"
                     "'already_unlocked',?)",
                     (json.dumps({"detail": "the rows it grants are on the tab"}),))
        conn.execute("INSERT INTO ledger(ts,kind,item,category,reason,detail) "
                     "VALUES(2,'BUY_SKIPPED','Unlock Cash Bonuses','UTILITY',"
                     "'already_unlocked',?)",
                     (json.dumps({"detail": "the rows it grants are on the tab"}),))
    result = observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                              web_port=0, running=False)
    assert result["workshop_upgrades_bought"] == 1


def test_unverified_live_account_does_not_supply_wallet(tmp_path: Path) -> None:
    def fetch(_url: str, *, timeout: float) -> Response:
        return Response(b'{"active":"worker:other","accounts":[]}')

    result = observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                              web_port=10002, running=True, fetch=fetch)
    assert "battle_cash" not in result
    assert "game_screen" not in result


def test_plan_requires_bound_account_but_survives_a_quiet_run(tmp_path: Path) -> None:
    bot_db.bind_account(tmp_path / "tower_bot.db", "42")
    path = tmp_path / "reroll-plan.json"
    path.write_text(json.dumps({"account_id": "other", "observed_at": time.time()}))
    read = lambda: observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                                    web_port=0, running=False)
    assert "reroll_plan" not in read()
    path.write_text(json.dumps({"account_id": "42", "observed_at": "soon"}))
    assert "reroll_plan" not in read()
    # The worker only republishes from the main menu, so a plan goes quiet for
    # the whole run. It stays visible; the UI labels its age instead.
    path.write_text(json.dumps({"account_id": "42", "observed_at": time.time() - 900}))
    assert read()["reroll_plan"]["account_id"] == "42"
    path.write_text(json.dumps({"account_id": "42", "observed_at": time.time(),
                                "item": "Damage", "state": "buy"}))
    assert read()["reroll_plan"]["item"] == "Damage"


def test_route_revision_and_errors_are_account_bound(tmp_path: Path) -> None:
    root = tmp_path / "workers" / "Air_38"
    root.mkdir(parents=True)
    bot_db.bind_account(root / "tower_bot.db", "42")
    route = BuildRouteStore(tmp_path).publish(RouteDocument.compatibility(), 0, "operator")
    BuildRouteRuntime(tmp_path, "Air_38", "42").acknowledge(route.revision, "42")
    read = lambda account_id: observed_metrics(root, account_key="worker:Air_38",
                                                account_id=account_id, web_port=0, running=False)
    assert read("42")["route_revision_applied"] == 1
    assert "route_revision_applied" not in read("different")
    (tmp_path / "build-route.json").write_text("{broken")
    assert "current route invalid" in read("42")["route_error"]


def test_lifetime_baseline_adds_only_later_recorded_runs(tmp_path: Path) -> None:
    db = tmp_path / "tower_bot.db"
    bot_db.bind_account(db, "42")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO runs(id,started_at,ended_at,tier,wave,coins) VALUES(1,1,2,1,10,50)")
        conn.execute("INSERT INTO runs(id,started_at,ended_at,tier,wave,coins) VALUES(2,3,4,1,12,25)")
    (tmp_path / "reroll-lifetime.json").write_text(json.dumps({
        "account_id": "42", "lifetime_coins": 1000, "observed_at": 2,
        "baseline_run_id": 1,
    }))
    result = observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                              web_port=0, running=False)
    assert result["lifetime_coins"] == 1025
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO runs(id,started_at,ended_at,tier,wave,coins) VALUES(3,5,6,1,14,NULL)")
    incomplete = observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                                  web_port=0, running=False)
    assert incomplete["lifetime_coins"] == 1025
    assert incomplete["lifetime_coins_incomplete"] is True


def test_play_time_stops_at_the_first_tier_1_wave_20_run() -> None:
    rows = [(0, 100, 1, 12), (200, 260, 2, 30), (300, 400, 1, 21), (500, 900, 1, 40)]
    assert play_to_t1w20(rows) == (260, 260)


def test_play_time_without_wave_20_is_the_total_so_far() -> None:
    assert play_to_t1w20([(0, 100, 1, 19), (100, 90, 1, 5), (200, 230, None, None)]) == (None, 130)


def test_read_play_reads_ended_runs_in_order(tmp_path: Path) -> None:
    db = tmp_path / "tower_bot.db"
    bot_db.bind_account(db, "42")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO runs (id, started_at, ended_at, tier, wave) VALUES (2, 50, 80, 1, 20)")
        conn.execute("INSERT INTO runs (id, started_at, ended_at, tier, wave) VALUES (1, 0, 40, 1, 9)")
        conn.execute("INSERT INTO runs (id, started_at, tier, wave) VALUES (3, 90, 1, 3)")
    assert read_play(db) == (70, 70)
    assert read_play(tmp_path / "missing.db") is None


def test_observed_metrics_report_play_time_to_wave_20(tmp_path: Path) -> None:
    db = tmp_path / "tower_bot.db"
    bot_db.bind_account(db, "42")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO runs (id, started_at, ended_at, tier, wave, coins) VALUES (1, 0, 600, 1, 14, 5)")
    result = observed_metrics(tmp_path, account_key="k", account_id="42", web_port=0, running=False)
    assert result["play_seconds_so_far"] == 600 and "play_seconds_to_t1w20" not in result
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO runs (id, started_at, ended_at, tier, wave, coins) VALUES (2, 700, 1000, 1, 22, 5)")
    result = observed_metrics(tmp_path, account_key="k", account_id="42", web_port=0, running=False)
    assert result["play_seconds_to_t1w20"] == 900 and "play_seconds_so_far" not in result


def test_recent_workshop_purchases_carry_cost_and_reason(tmp_path: Path) -> None:
    db = tmp_path / "tower_bot.db"
    bot_db.bind_account(db, "42")
    with sqlite3.connect(db) as conn:
        def buy(ts: float, item: str, spent: int, detail: dict) -> int:
            return conn.execute(
                "INSERT INTO ledger(ts,kind,item,category,currency,delta,dry_run,detail) "
                "VALUES(?,'WORKSHOP_BUY',?,'UTILITY','coins',?,0,?)",
                (ts, item, -spent, json.dumps(detail))).lastrowid
        buy(1, "Oldest", 1, {"verdict": "bought"})
        for ts in range(2, 6):
            buy(ts, f"Item {ts}", ts * 10, {"verdict": "bought", "reason": f"Rule {ts}"})
        buy(6, "Unproven", 9, {"verdict": "unproven"})
        drawn = buy(7, "Coins / Kill Bonus", 126, {"verdict": "bought"})
        conn.execute(
            "INSERT INTO ledger(ts,kind,item,category,currency,dry_run,reason,detail) "
            "VALUES(8,'ROUTE_DECISION','Coins / Kill Bonus','UTILITY','coins',0,'draw',?)",
            (json.dumps({"purchase_event_id": drawn,
                         "eligible_odds": {"coins_per_kill_bonus": .714, "cash_bonus": .286},
                         "selected_upgrade_id": "coins_per_kill_bonus"}),))

    recent = observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                              web_port=0, running=False)["recent_workshop_purchases"]

    assert [(row["item"], row["cost"], row["reason"]) for row in recent] == [
        ("Coins / Kill Bonus", 126, "Random draw (71%)"),
        ("Item 5", 50, "Rule 5"), ("Item 4", 40, "Rule 4"),
        ("Item 3", 30, "Rule 3"), ("Item 2", 20, "Rule 2")]
    assert recent[0]["at"] == 7


def test_scoped_currency_adapter_keeps_unknowns_age_and_commitments(tmp_path):
    from currencies import CurrencyRepository, currency_overview
    from evidence_scope import FactScope, BalanceInterval
    path = tmp_path / 'bot.db'
    repo = CurrencyRepository(path)
    scope = FactScope('acct', 'lease', 'generation', 2)
    repo.bind_scope(scope)
    repo.observe(BalanceInterval('coins', 990, 1010, scope, 10., 'frame'))
    repo.reserve('lab', 'coins', 200, wallet=990)
    result = currency_overview(path, account_id='acct', lease_id='lease', generation='generation', now=12.)
    assert result == {'coins_lower':990,'coins_upper':1010,'reserved':200,'available_lower':790,'gems':None}
    for changes in ({'account_id':'other'},{'generation':'old'},{'now':200.}):
        kwargs = dict(account_id='acct', lease_id='lease', generation='generation', now=12.)
        assert currency_overview(path, **{**kwargs, **changes})['coins_lower'] is None


def test_mission_producer_epoch_matches_authoritative_facts(tmp_path):
    from currencies import CurrencyRepository
    from evidence_scope import FactScope
    from notification_state import NotificationState
    from fleet.reroll_metrics import _mission_view
    repo = CurrencyRepository(tmp_path / 'tower_bot.db')
    scope = FactScope('acct','lease','generation',3)
    repo.bind_scope(scope)
    producer = NotificationState(tmp_path / 'mission-notification-state.json',
        scope=dict(account_id='acct',lease_id='lease',attempt_id='attempt',generation='generation',fact_epoch=3))
    producer.observe('missions',True,10.,frame_id='1')
    producer.observe('missions',True,11.,frame_id='2')
    args = dict(account_id='acct',lease_id='lease',attempt_id='attempt',generation='generation',now=12.)
    assert _mission_view(tmp_path,**args)['state'] == 'pending'
    repo.bind_scope(FactScope('acct','lease','generation',4))
    assert _mission_view(tmp_path,**args)['state'] == 'unknown'
    row = json.loads((tmp_path / 'mission-notification-state.json').read_text())
    row['scope']['fact_epoch'] = True
    (tmp_path / 'mission-notification-state.json').write_text(json.dumps(row))
    assert _mission_view(tmp_path,**args)['state'] == 'unknown'
