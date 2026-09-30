"""The fleet's lab-start rollout: per-slot stages plus a per-lab rehearsal map."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import lab_catalog
from lab_starter_rollout import (LabStarterRollout, RolloutError, StageRecord, StarterState,
                                 price_matches, starter_gate)

ATTACK = "labs.attack-speed"
PRICE = lab_catalog.level(ATTACK, 1).coins


def dry(rollout: LabStarterRollout, at: float, *, slot: int = 2, worker: str = "Air_1",
        account: str = "acct-a", lab: str = ATTACK, level: int = 1, price: int = PRICE):
    return rollout.note_start_dry_run(slot, worker, account, lab, level, price, 15., at)


def canary(rollout: LabStarterRollout, slot: int = 2) -> None:
    dry(rollout, 1000., slot=slot)
    dry(rollout, 1600., slot=slot)


def test_a_missing_file_blocks_every_start_but_game_speed_in_slot_one(tmp_path: Path) -> None:
    state = LabStarterRollout(tmp_path).state()
    assert starter_gate(state, 1, "labs.game-speed", "Air_1", "acct-a").enabled
    gate = starter_gate(state, 2, ATTACK, "Air_1", "acct-a")
    assert gate.mode == "rehearse" and gate.reason == "Rehearsing slot 2 · 0/2 dry runs"
    assert starter_gate(None, 2, ATTACK, "Air_1", "acct-a").mode == "blocked"
    assert starter_gate(None, 2, ATTACK, "Air_1", "acct-a").reason.startswith("Planning only:")


def test_one_clean_rehearsal_marks_the_lab_rehearsed(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    dry(rollout, 1000.)
    lab = rollout.state().lab(ATTACK)
    assert lab.status == "rehearsed" and (lab.level, lab.price, lab.catalog_price) == (1, PRICE, PRICE)


def test_two_rehearsals_ten_minutes_apart_promote_the_slot_to_canary(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    assert dry(rollout, 1000.).promoted is None
    assert dry(rollout, 1599.).promoted is None
    assert dry(rollout, 1700., worker="Air_2", account="acct-b").promoted is None
    change = dry(rollout, 1600., lab="labs.damage", price=lab_catalog.level("labs.damage", 1).coins)
    assert change.promoted == "canary"
    state = rollout.state()
    record = state.rollout("start:2")
    assert (record.stage, record.canary_worker, record.canary_account) == ("canary", "Air_1", "acct-a")
    assert starter_gate(state, 2, ATTACK, "Air_1", "acct-a").enabled
    assert starter_gate(state, 2, ATTACK, "Air_2", "acct-b").reason == "Planning only: Waiting for canary"
    assert starter_gate(state, 3, ATTACK, "Air_1", "acct-a").mode == "rehearse"


def test_a_canary_on_another_account_is_not_the_canary(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    canary(rollout)
    assert not starter_gate(rollout.state(), 2, ATTACK, "Air_1", "acct-z").enabled


def test_an_unrehearsed_lab_is_rehearsed_even_at_fleet_stage(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    canary(rollout)
    rollout.note_start(2, "Air_1", "acct-a", "txn-1", "bought", at=1700.)
    state = rollout.state()
    assert state.rollout("start:2").stage == "fleet"
    assert starter_gate(state, 2, ATTACK, "Air_9", "acct-9").enabled
    gate = starter_gate(state, 2, "labs.damage", "Air_9", "acct-9")
    assert (gate.mode, gate.reason) == ("rehearse", "Lab not rehearsed yet")


def test_a_catalog_price_mismatch_halts_the_slot(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    change = dry(rollout, 1000., price=PRICE + 7)
    assert change.halted
    record = rollout.state().rollout("start:2")
    assert record.stage == "halted" and str(PRICE + 7) in record.halted_reason
    assert rollout.state().lab(ATTACK) is None


def test_an_abbreviated_price_matches_its_catalog_price() -> None:
    assert price_matches(1120, 1120)
    assert price_matches(1120, 1123)       # "1.12K" read for a catalog 1123
    assert price_matches(1_120_000, 1_123_456)
    assert not price_matches(37, 30)
    assert not price_matches(1120, 1400)


def test_a_lab_without_catalog_prices_needs_review_before_it_is_rehearsed(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    rollout.note_start_dry_run(2, "Air_1", "acct-a", "labs.ban-perks", 1, 5000, 60., 1000.)
    state = rollout.state()
    assert state.lab("labs.ban-perks").status == "needs_review"
    assert starter_gate(state, 2, "labs.ban-perks", "Air_1", "acct-a").mode == "blocked"
    assert "labs.ban-perks" in state.blocked_labs()
    rollout.accept_lab("labs.ban-perks")
    assert rollout.state().lab("labs.ban-perks").status == "rehearsed"


def test_three_spaced_misses_make_a_lab_unfindable_and_reset_clears_it(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    rollout.note_research_miss(ATTACK, "Air_1", "acct-a", 1000.)
    rollout.note_research_miss(ATTACK, "Air_1", "acct-a", 1050.)   # same visit: not counted
    rollout.note_research_miss(ATTACK, "Air_1", "acct-a", 1200.)
    assert rollout.state().lab(ATTACK).status == "missing"
    lab = rollout.note_research_miss(ATTACK, "Air_1", "acct-a", 1400., ("/e/lab-search-1.png",))
    assert lab.status == "unfindable" and lab.evidence == ("/e/lab-search-1.png",)
    assert starter_gate(rollout.state(), 2, ATTACK, "Air_1", "acct-a").reason == \
        "Planning only: Attack Speed not found in picker"
    rollout.reset_lab(ATTACK)
    assert rollout.state().lab(ATTACK) is None
    with pytest.raises(RolloutError):
        rollout.reset_lab(ATTACK)


def test_the_canary_start_promotes_and_a_second_miss_halts(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    canary(rollout)
    assert rollout.note_start(2, "Air_2", "acct-b", "t0", "bought", at=1.).promoted is None
    rollout.note_start(2, "Air_1", "acct-a", "t1", "not_charged", at=2.)
    assert rollout.state().rollout("start:2").stage == "canary"
    change = rollout.note_start(2, "Air_1", "acct-a", "t2", "not_charged", at=3.)
    assert change.halted
    assert rollout.state().rollout("start:2").halted_reason == "The canary's start tap did not land twice"


def test_halt_canary_only_halts_that_workers_canary(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    canary(rollout)
    assert not rollout.halt_canary("start:2", "Air_2", "acct-b", "x").halted
    change = rollout.halt_canary("start:2", "Air_1", "acct-a", "Start was not proven", ("/e/1.png",))
    assert change.halted and rollout.state().rollout("start:2").evidence == ("/e/1.png",)
    rollout.reset("start:2")
    assert rollout.state().rollout("start:2") == StageRecord()
    with pytest.raises(RolloutError):
        rollout.reset("start:2")


def test_a_corrupt_file_reads_as_dry_run_and_is_quarantined(tmp_path: Path) -> None:
    (tmp_path / "lab-starter-rollout.json").write_text("{oops")
    state = LabStarterRollout(tmp_path).state()
    assert state == StarterState({}, {})
    assert len(list(tmp_path.glob("lab-starter-rollout.json.corrupt-*"))) == 1


def test_rows_for_the_dashboard(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    canary(rollout)
    snapshot = rollout.snapshot()
    assert [row["key"] for row in snapshot["starter_rollout"]] == [
        "start:1", "start:2", "start:3", "start:4", "start:5"]
    row = snapshot["starter_rollout"][1]
    assert (row["stage"], row["canary_worker"], row["dry_runs"]) == ("canary", "Air_1", 2)
    (lab,) = snapshot["rehearsed_labs"]
    assert lab == {"lab_id": ATTACK, "name": "Attack Speed", "status": "rehearsed", "level": 1,
                   "price": PRICE, "catalog_price": PRICE, "seconds": 15., "mismatch": False,
                   "worker": "Air_1", "misses": 0, "evidence": []}
    json.dumps(snapshot)
