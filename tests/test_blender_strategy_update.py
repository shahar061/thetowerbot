"""Saved custom strategies can adopt Blender without losing early-wave rules."""

from __future__ import annotations

from copy import deepcopy

import pytest

import fleet.blender_strategy_update as updater
from fleet.blender_strategy_update import add_blender_gate
from fleet.build_route import RouteBaseline, RouteDocument
from fleet.reroll_planner import _ban_closure


@pytest.mark.parametrize(("prefix", "threshold"), [("sl", 450), ("br", 400)])
def test_blender_gate_precedes_old_rules_and_is_idempotent(prefix: str, threshold: int) -> None:
    baseline = RouteDocument.compatibility().to_dict()["baseline"]
    old_block = {"id": f"{prefix}.old.defense", "type": "pool",
                 "upgrade_ids": ["defense_absolute"], "selection": "priority"}
    baseline["workshop"].update(mode="blocks", blocks=[old_block],
                                banned_upgrade_ids=["unlock_range_upgrades", "interest_per_wave"])
    original = deepcopy(baseline)

    updated = add_blender_gate(baseline, prefix=prefix, threshold=threshold)

    assert baseline == original
    assert updated["labs"] == baseline["labs"]
    assert updated["workshop"]["blocks"][1:] == [old_block]
    gate = updated["workshop"]["blocks"][0]
    assert (gate["type"], gate["field"], gate["op"], gate["value"], gate["else"]) == (
        "condition", "best_tier_1_wave", "gte", threshold, [])
    unlocks, priorities, stop = gate["then"]
    assert [block["id"] for block in gate["then"]] == [
        f"{prefix}.blender.unlocks", f"{prefix}.blender.value", f"{prefix}.blender.wait"]
    assert unlocks["type"] == "unlock" and {"knockback_chance", "orbs"} <= set(unlocks["upgrade_ids"])
    assert priorities["selection"] == "value"
    assert "defense_absolute" not in priorities["upgrade_ids"]
    assert stop["type"] == "wait"
    assert "unlock_range_upgrades" not in updated["workshop"]["banned_upgrade_ids"]
    assert "interest_per_wave" in updated["workshop"]["banned_upgrade_ids"]
    assert not (_ban_closure(frozenset(updated["workshop"]["banned_upgrade_ids"]))
                & set(priorities["upgrade_ids"]))
    RouteBaseline.from_dict(updated)
    assert add_blender_gate(updated, prefix=prefix, threshold=threshold) == updated


def test_preview_targets_only_the_two_requested_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = RouteDocument.compatibility().to_dict()["baseline"]
    baseline["workshop"].update(mode="blocks", blocks=[
        {"id": "old.defense", "type": "pool", "upgrade_ids": ["defense_absolute"]}])
    survivor = add_blender_gate(baseline, prefix="sl", threshold=450)
    blender = add_blender_gate(baseline, prefix="br", threshold=400)
    library = {"strategies": [
        {"id": "sl", "name": "Survivor Ladder", "version": 13, "baseline": survivor},
        {"id": "br", "name": "Blender Road", "version": 14, "baseline": blender},
    ]}
    route = {"assignments": {
        "Tiramisu64_82": {"account_id": "82", "strategy_id": "sl",
                          "strategy_version": 13, "baseline": survivor},
        "Tiramisu64_74": {"account_id": "74", "strategy_id": "sl",
                          "strategy_version": 12, "baseline": baseline},
        "Tiramisu64_83": {"account_id": "83", "strategy_id": "br",
                          "strategy_version": 14, "baseline": blender},
        "Tiramisu64_79": {"account_id": "79", "strategy_id": "br",
                          "strategy_version": 13, "baseline": baseline},
    }}

    def request(_: str, path: str, payload: dict | None = None) -> dict:
        assert payload is None
        return library if path.endswith("/strategies") else route

    monkeypatch.setattr(updater, "_request", request)
    changes = updater.update_saved_strategies("unused")
    assert [change["assign_workers"] for change in changes] == [[], []]


def test_apply_saves_versions_and_assigns_only_requested_workers(
        monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = RouteDocument.compatibility().to_dict()["baseline"]
    baseline["workshop"].update(mode="blocks", blocks=[
        {"id": "old.defense", "type": "pool", "upgrade_ids": ["defense_absolute"]}],
        banned_upgrade_ids=["unlock_range_upgrades"])
    library = {"revision": 0, "strategies": [
        {"id": "sl", "name": "Survivor Ladder", "version": 1,
         "source_template": "scratch", "baseline": baseline},
        {"id": "br", "name": "Blender Road", "version": 1,
         "source_template": "scratch", "baseline": baseline},
    ]}
    route = {"revision": 0, "assignments": {
        worker: {"account_id": worker, "strategy_id": strategy,
                 "strategy_version": 1, "baseline": baseline}
        for worker, strategy in (("Tiramisu64_82", "sl"), ("Tiramisu64_74", "sl"),
                                 ("Tiramisu64_83", "br"))}}

    def request(_: str, path: str, payload: dict | None = None) -> dict:
        if payload is None:
            return deepcopy(library if path.endswith("/strategies") else route)
        if path.endswith("/strategies"):
            assert payload["expected_revision"] == library["revision"]
            library["revision"] += 1
            row = next(row for row in library["strategies"] if row["id"] == payload["strategy_id"])
            row.update(version=row["version"] + 1, baseline=payload["baseline"])
            return deepcopy(library)
        assert path.endswith("/strategies/assign")
        assert payload["expected_revision"] == route["revision"]
        route["revision"] += 1
        for target in payload["workers"]:
            row = next(row for row in library["strategies"] if row["id"] == payload["strategy_id"])
            route["assignments"][target["worker"]].update(
                strategy_version=row["version"], baseline=row["baseline"])
        return deepcopy(route)

    monkeypatch.setattr(updater, "_request", request)
    changes = updater.update_saved_strategies("unused", apply=True)
    assert [change["assign_workers"] for change in changes] == [
        ["Tiramisu64_82"], ["Tiramisu64_83"]]
    assert (library["revision"], route["revision"]) == (2, 2)
    assert route["assignments"]["Tiramisu64_74"]["strategy_version"] == 1
    assert route["assignments"]["Tiramisu64_82"]["baseline"]["workshop"]["blocks"][0]["value"] == 450
    assert route["assignments"]["Tiramisu64_83"]["baseline"]["workshop"]["blocks"][0]["value"] == 400
