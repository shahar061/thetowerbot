"""Opening until wave 20, then the least-used turtle; manual choices stay."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import db
from fleet.auto_assign import (AutoAssigner, AutoAssignSettings, Pick, WorkerView, load_settings,
                               plan)
from fleet.build_route_store import RouteConflict
from fleet.setup import FleetSetupService

SETTINGS = AutoAssignSettings("opening", 20, ("turtle", "guide", "eco"))


def _worker(name: str, *, strategy: str | None = None, assigned_to: str = "A",
            wave: int | None = None, facts_for: str = "A", account: str = "A") -> WorkerView:
    return WorkerView(name, account,
                      None if strategy is None else (assigned_to, strategy),
                      None if wave is None else (facts_for, wave))


def test_new_account_gets_the_opening() -> None:
    assert plan(SETTINGS, [_worker("w1")]) == [Pick("w1", "A", "opening")]


def test_rerolled_account_gets_the_opening() -> None:
    worker = _worker("w1", strategy="eco", assigned_to="OLD")
    assert plan(SETTINGS, [worker]) == [Pick("w1", "A", "opening")]


def test_opening_below_threshold_stays() -> None:
    assert plan(SETTINGS, [_worker("w1", strategy="opening", wave=19)]) == []


def test_opening_at_threshold_gets_least_used_turtle() -> None:
    workers = [_worker("t1", strategy="turtle", account="B", assigned_to="B"),
               _worker("w1", strategy="opening", wave=20)]
    assert plan(SETTINGS, workers) == [Pick("w1", "A", "guide")]


def test_tie_goes_to_rotation_order() -> None:
    assert plan(SETTINGS, [_worker("w1", strategy="opening", wave=25)]) == [
        Pick("w1", "A", "turtle")]


def test_two_graduates_in_one_cycle_get_different_turtles() -> None:
    workers = [_worker("w1", strategy="opening", wave=20),
               _worker("w2", strategy="opening", wave=30, account="B", assigned_to="B",
                       facts_for="B")]
    assert [pick.strategy_id for pick in plan(SETTINGS, workers)] == ["turtle", "guide"]


def test_manual_and_turtle_assignments_are_untouched() -> None:
    workers = [_worker("w1", strategy="eco", wave=90),
               _worker("w2", strategy="some-manual-choice", wave=5)]
    assert plan(SETTINGS, workers) == []


def test_facts_for_another_account_do_not_graduate() -> None:
    worker = _worker("w1", strategy="opening", wave=40, facts_for="OLD")
    assert plan(SETTINGS, [worker]) == []


def test_missing_facts_do_not_graduate() -> None:
    assert plan(SETTINGS, [_worker("w1", strategy="opening")]) == []


def test_stale_assignments_do_not_count_toward_rotation() -> None:
    workers = [_worker("old", strategy="turtle", assigned_to="GONE", account="C"),
               _worker("w1", strategy="opening", wave=20)]
    picks = plan(SETTINGS, workers)
    assert Pick("w1", "A", "turtle") in picks


def _write(root: Path, value: object) -> None:
    (root / "auto-assign.json").write_text(json.dumps(value))


KNOWN = {"opening", "turtle", "guide", "eco"}
VALID = {"enabled": True, "opening": "opening", "threshold_wave": 20,
         "rotation": ["turtle", "guide", "eco"]}


def test_missing_or_disabled_settings_mean_off(tmp_path: Path) -> None:
    assert load_settings(tmp_path, KNOWN) is None
    _write(tmp_path, {**VALID, "enabled": False})
    assert load_settings(tmp_path, KNOWN) is None


def test_valid_settings_load(tmp_path: Path) -> None:
    _write(tmp_path, VALID)
    assert load_settings(tmp_path, KNOWN) == SETTINGS


@pytest.mark.parametrize("change", [
    {"rotation": []},
    {"rotation": ["turtle", "turtle"]},
    {"rotation": ["opening", "turtle"]},
    {"rotation": ["turtle", "unknown"]},
    {"opening": "unknown"},
    {"threshold_wave": 0},
    {"threshold_wave": True},
    {"enabled": "yes"},
    {"extra": 1},
])
def test_invalid_settings_raise(tmp_path: Path, change: dict[str, object]) -> None:
    _write(tmp_path, {**VALID, **change})
    with pytest.raises(ValueError):
        load_settings(tmp_path, KNOWN)


def test_unreadable_settings_raise(tmp_path: Path) -> None:
    (tmp_path / "auto-assign.json").write_text("{not json")
    with pytest.raises(ValueError):
        load_settings(tmp_path, KNOWN)


def _registered(root: Path, name: str, account_id: str, db_account_id: str | None = None) -> None:
    worker = root / "workers" / name
    checkpoints = worker / "checkpoints"
    checkpoints.mkdir(parents=True)
    binding = checkpoints / f"{uuid4().hex}.json"
    binding.write_text(json.dumps({"worker_id": name, "account_id": account_id,
                                   "endpoint": "127.0.0.1:5555", "lease_id": "lease"}))
    (worker / "fleet-registration.json").write_text(json.dumps({
        "state": "registered", "instance": name, "account_id": account_id,
        "web_port": 8081, "binding": str(binding), "endpoint": "127.0.0.1:5555",
        "lease_id": "lease"}))
    db.bind_account(worker / "tower_bot.db", db_account_id or account_id)


def _facts(root: Path, name: str, account_id: str, wave: int | None) -> None:
    (root / "workers" / name / "build-route-facts.json").write_text(json.dumps(
        {"account_id": account_id, "best_tier_1_wave": wave}))


def _fleet(root: Path, names: tuple[str, ...], hidden: set[str] = frozenset()) -> FleetSetupService:
    fleet = FleetSetupService(root, qualification_root=root / "qualifications")
    fleet._reroll_pool = SimpleNamespace(members=lambda: [{"name": n} for n in names])
    fleet._reroll_runs = SimpleNamespace(hidden_names=lambda: set(hidden))
    _write(root, {"enabled": True, "opening": "opening", "threshold_wave": 20,
                  "rotation": ["turtle", "labs_gems"]})
    return fleet


def _assigned(fleet: FleetSetupService) -> dict[str, str]:
    route = fleet.build_route_store().read()
    return {name: a.strategy_id for name, a in route.assignments.items()}


def test_runner_opens_then_graduates(tmp_path: Path) -> None:
    _registered(tmp_path, "w1", "ACCOUNT-A")
    fleet = _fleet(tmp_path, ("w1",))
    runner = AutoAssigner(tmp_path, fleet)
    runner.check_once()
    assert _assigned(fleet) == {"w1": "opening"}
    _facts(tmp_path, "w1", "ACCOUNT-A", 19)
    assert runner.check_once() == []
    _facts(tmp_path, "w1", "ACCOUNT-A", 20)
    runner.check_once()
    assert _assigned(fleet) == {"w1": "turtle"}
    history = sorted((tmp_path / "build-route-history").glob("*.json"))
    assert json.loads(history[-1].read_text())["audit"]["actor"] == "auto"


def test_null_wave_does_not_graduate(tmp_path: Path) -> None:
    _registered(tmp_path, "w1", "ACCOUNT-A")
    fleet = _fleet(tmp_path, ("w1",))
    runner = AutoAssigner(tmp_path, fleet)
    runner.check_once()
    _facts(tmp_path, "w1", "ACCOUNT-A", None)
    assert runner.check_once() == []
    assert _assigned(fleet) == {"w1": "opening"}


def test_hidden_and_unregistered_workers_are_ignored(tmp_path: Path) -> None:
    _registered(tmp_path, "shown", "ACCOUNT-A")
    _registered(tmp_path, "hidden", "ACCOUNT-B")
    fleet = _fleet(tmp_path, ("shown", "hidden", "ghost"), hidden={"hidden"})
    AutoAssigner(tmp_path, fleet).check_once()
    assert _assigned(fleet) == {"shown": "opening"}


def test_conflict_defers_the_cycle(tmp_path: Path) -> None:
    _registered(tmp_path, "w1", "ACCOUNT-A")
    fleet = _fleet(tmp_path, ("w1",))
    real = fleet.assign_strategy

    def conflicting(**kwargs: object) -> object:
        raise RouteConflict(fleet.build_route_store().read())

    fleet.assign_strategy = conflicting
    assert AutoAssigner(tmp_path, fleet).check_once() == []
    fleet.assign_strategy = real
    assert _assigned(fleet) == {}


def test_invalid_settings_disable_the_runner(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _registered(tmp_path, "w1", "ACCOUNT-A")
    fleet = _fleet(tmp_path, ("w1",))
    _write(tmp_path, {**VALID, "rotation": []})
    runner = AutoAssigner(tmp_path, fleet)
    assert runner.check_once() == []
    assert runner.check_once() == []
    assert _assigned(fleet) == {}
    assert sum("auto-assign disabled" in r.message for r in caplog.records) == 1


def test_live_override_counts_as_a_manual_choice() -> None:
    worker = WorkerView("w1", "A", None, None, overridden=True)
    assert plan(SETTINGS, [worker]) == []


def test_runner_keeps_an_operator_override(tmp_path: Path) -> None:
    from dataclasses import replace
    from fleet.build_route import AccountOverride

    _registered(tmp_path, "w1", "ACCOUNT-A")
    fleet = _fleet(tmp_path, ("w1",))
    store = fleet.build_route_store()
    current = store.read()
    store.publish(replace(current, overrides={"w1": AccountOverride("ACCOUNT-A", {})}),
                  current.revision, "operator")
    assert AutoAssigner(tmp_path, fleet).check_once() == []
    assert "w1" in fleet.build_route_store().read().overrides


def test_one_bad_binding_does_not_block_the_others(tmp_path: Path) -> None:
    _registered(tmp_path, "a_bad", "ACCOUNT-A", db_account_id="SOMEONE-ELSE")
    _registered(tmp_path, "b_good", "ACCOUNT-B")
    fleet = _fleet(tmp_path, ("a_bad", "b_good"))
    AutoAssigner(tmp_path, fleet).check_once()
    assert _assigned(fleet) == {"b_good": "opening"}
