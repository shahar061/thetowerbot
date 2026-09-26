"""Reroll overview reports only verified observations."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path

import db as bot_db
from fleet.reroll_metrics import observed_metrics, play_to_t1w20, read_play
from fleet.build_route import RouteDocument
from fleet.build_route_runtime import BuildRouteRuntime
from fleet.build_route_store import BuildRouteStore


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
