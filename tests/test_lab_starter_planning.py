"""The plan asks for a rehearsal where the starter rollout needs one, and a start where it allows."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import lab_catalog
from fleet.resource_blocks import LabFacts, choose_lab_action, evaluate_lab_plan, research_automated
from lab_runtime import LabJobRecord, LabRuntimeSnapshot, LabScope
from lab_starter_rollout import LabStarterRollout
from tests.test_lab_list_eval import Route as _ListRoute, facts as _list_facts

ATTACK = "labs.attack-speed"
SCOPE = LabScope("acct", "lease", "gen")


def list_route() -> _ListRoute:
    """The lab-list route test_lab_list_eval.py builds: Game Speed pinned to slot 1, Labs Speed to slot 2."""
    return _ListRoute()


def list_facts(**changes: Any) -> LabFacts:
    """test_lab_list_eval.py's LabFacts builder: slot 2 is owned, idle, confirmed and current,
    with a wallet that covers its next (pinned) lab. Bind it to a lab-runtime scope so
    choose_lab_action can match it against a runtime snapshot."""
    changes.setdefault("scope", SCOPE)
    return _list_facts(**changes)


def runtime_for(facts: LabFacts) -> LabRuntimeSnapshot:
    """A runtime snapshot bound to the same scope as `facts`, with slot 2's observed record."""
    slot2 = (facts.slots or {}).get(2) or {}
    record = LabJobRecord(facts.scope, 2, state=slot2.get("state", "idle"),
                          confirmed=slot2.get("confirmed", True),
                          observed_at=slot2.get("observed_at"))
    return LabRuntimeSnapshot(facts.scope, (record,))


def test_without_facts_only_game_speed_in_slot_one_is_automated() -> None:
    assert research_automated("labs.game-speed", 1)
    assert not research_automated("labs.game-speed", 2)
    assert not research_automated(ATTACK, 1)


def test_an_idle_slot_at_dry_run_plans_a_rehearsal(tmp_path: Path) -> None:
    starter = LabStarterRollout(tmp_path).state()
    facts = replace(list_facts(), starter=starter, worker="Air_1")
    plan = evaluate_lab_plan(list_route(), facts)
    slot2 = plan.slots[1]
    assert not slot2.automated and slot2.rehearse
    assert slot2.capabilities["rehearse"] and not slot2.capabilities["execute"]
    assert any(line.startswith("Rehearsing slot 2") for line in slot2.why)
    action = choose_lab_action(plan, runtime_for(facts), available_coins=facts.available_coins)
    assert action is not None and (action.slot, action.operation) == (2, "rehearse")


def test_a_fleet_slot_with_a_rehearsed_lab_plans_a_start(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    facts = replace(list_facts(), worker="Air_1")
    lab = evaluate_lab_plan(list_route(), replace(facts, starter=rollout.state())).slots[1].next
    price = lab_catalog.level(lab.lab_id, lab.level).coins
    rollout.note_start_dry_run(2, "Air_1", facts.account_id, lab.lab_id, lab.level, price, 1., 1000.)
    rollout.note_start_dry_run(2, "Air_1", facts.account_id, lab.lab_id, lab.level, price, 1., 1600.)
    rollout.note_start(2, "Air_1", facts.account_id, "t", "bought", at=1700.)
    plan = evaluate_lab_plan(list_route(), replace(facts, starter=rollout.state()))
    assert plan.slots[1].automated and plan.slots[1].capabilities["execute"]
    action = choose_lab_action(plan, runtime_for(facts), available_coins=facts.available_coins)
    assert (action.slot, action.operation) == (2, "start")


def test_an_unfindable_lab_is_skipped_for_the_next_one(tmp_path: Path) -> None:
    rollout = LabStarterRollout(tmp_path)
    facts = replace(list_facts(), worker="Air_1")
    first = evaluate_lab_plan(list_route(), replace(facts, starter=rollout.state())).slots[1].next
    for at in (1000., 1200., 1400.):
        rollout.note_research_miss(first.lab_id, "Air_1", facts.account_id, at)
    second = evaluate_lab_plan(list_route(), replace(facts, starter=rollout.state())).slots[1].next
    assert second is not None and second.lab_id != first.lab_id
