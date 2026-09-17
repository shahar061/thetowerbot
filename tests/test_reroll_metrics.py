"""Reroll overview reports only verified observations."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path

import db as bot_db
from fleet.reroll_metrics import observed_metrics


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


def test_unverified_live_account_does_not_supply_wallet(tmp_path: Path) -> None:
    def fetch(_url: str, *, timeout: float) -> Response:
        return Response(b'{"active":"worker:other","accounts":[]}')

    result = observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                              web_port=10002, running=True, fetch=fetch)
    assert "battle_cash" not in result
    assert "game_screen" not in result


def test_plan_requires_bound_account_and_recent_observation(tmp_path: Path) -> None:
    bot_db.bind_account(tmp_path / "tower_bot.db", "42")
    path = tmp_path / "reroll-plan.json"
    path.write_text(json.dumps({"account_id": "other", "observed_at": time.time()}))
    read = lambda: observed_metrics(tmp_path, account_key="worker:Air_2", account_id="42",
                                    web_port=0, running=False)
    assert "reroll_plan" not in read()
    path.write_text(json.dumps({"account_id": "42", "observed_at": time.time() - 121}))
    assert "reroll_plan" not in read()
    path.write_text(json.dumps({"account_id": "42", "observed_at": time.time(),
                                "item": "Damage", "state": "buy"}))
    assert read()["reroll_plan"]["item"] == "Damage"


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
