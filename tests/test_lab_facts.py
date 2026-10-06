"""Persisted lab facts: coin income, best waves, and known levels from disk."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import db
from fleet import lab_facts


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "tower_bot.db"
    db.connect(path).close()
    return path


def test_best_waves_groups_finished_runs_by_tier(tmp_path: Path) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO runs (started_at, tier, wave, ended_at) VALUES (?, ?, ?, ?)",
            [(1.0, 1, 120, 1.0), (1.0, 1, 180, 2.0), (1.0, 2, 40, 3.0), (1.0, 2, 90, None)])
    assert lab_facts.best_waves(path) == {1: 180, 2: 40}


def test_best_waves_unreadable_is_empty(tmp_path: Path) -> None:
    assert lab_facts.best_waves(tmp_path / "missing.db") == {}


def test_coins_per_hour_reads_lifetime_record(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lab_facts, "read_lifetime",
                        lambda root, account: {"recent_coins_per_hour": 12_000.0})
    assert lab_facts.coins_per_hour(tmp_path, "acct") == 12_000.0
    monkeypatch.setattr(lab_facts, "read_lifetime", lambda root, account: None)
    assert lab_facts.coins_per_hour(tmp_path, "acct") is None


def test_persisted_lab_facts_mirrors_tier_1_best_into_best_waves(tmp_path: Path) -> None:
    path = _db(tmp_path)
    db.bind_account(path, "acct")
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO runs (started_at, tier, wave, ended_at) VALUES (?, ?, ?, ?)",
            [(1.0, 1, 150, 1.0), (1.0, 2, 40, 2.0)])
    facts = lab_facts.persisted_lab_facts(tmp_path, "acct", now=1000., coins=100, gems=10,
                                          db_path=path)
    assert facts.best_waves == {1: 150, 2: 40}
    assert facts.best_tier_1_wave == 150
    assert facts.best_waves[1] == facts.best_tier_1_wave


def test_persisted_lab_facts_fills_completed_levels_from_the_persisted_revision(tmp_path: Path) -> None:
    """R-F6: completed_levels comes from the same ``lab_levels`` source as
    fleet/reroll_progress.py's discount signature and fleet/state_view.py's build_labs."""
    path = _db(tmp_path)
    db.bind_account(path, "acct")
    revision = {
        "account_id": "acct",
        "lab_levels": [
            {"concept_id": "labs.game-speed", "status": "verified", "value": 3},
            # An "available" picker reads one level ahead of what is owned.
            {"concept_id": "labs.crit-chance", "status": "available", "value": 5},
        ],
    }
    with db.connect(path) as connection:
        connection.execute("INSERT INTO account_revisions(detail) VALUES (?)",
                           (json.dumps(revision),))
    facts = lab_facts.persisted_lab_facts(tmp_path, "acct", now=1000., coins=None, gems=None,
                                          db_path=path)
    assert facts.completed_levels == {"labs.game-speed": 3, "labs.crit-chance": 4}


def test_persisted_lab_facts_completed_levels_missing_revision_is_none(tmp_path: Path) -> None:
    path = _db(tmp_path)
    db.bind_account(path, "acct")
    facts = lab_facts.persisted_lab_facts(tmp_path, "acct", now=1000., coins=None, gems=None,
                                          db_path=path)
    assert facts.completed_levels is None


def test_persisted_lab_facts_reads_an_unaffordable_row_like_an_available_one(tmp_path: Path) -> None:
    """One rule with the bot: an unaffordable picker Lv.N also prices level N, so N-1 is
    completed; another account's scoped level and an unclaimable status are ignored."""
    path = _db(tmp_path)
    db.bind_account(path, "acct")
    revision = {
        "account_id": "acct",
        "lab_levels": [
            {"concept_id": "labs.coins-kill-bonus", "status": "unavailable", "value": 3},
            {"concept_id": "labs.damage", "status": "verified", "value": 9,
             "scope": {"account_id": "other", "lease_id": "l", "generation": "g", "epoch": 0}},
            {"concept_id": "labs.health", "status": "verified", "value": 4,
             "scope": {"account_id": "acct", "lease_id": "l", "generation": "old", "epoch": 0}},
            {"concept_id": "labs.attack-speed", "status": "unreadable", "value": 2},
        ],
    }
    with db.connect(path) as connection:
        connection.execute("INSERT INTO account_revisions(detail) VALUES (?)",
                           (json.dumps(revision),))
    facts = lab_facts.persisted_lab_facts(tmp_path, "acct", now=1000., coins=None, gems=None,
                                          db_path=path)
    assert facts.completed_levels == {"labs.coins-kill-bonus": 2, "labs.health": 4}
