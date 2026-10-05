"""The saved blender strategy adopts the v2 Workshop program through the coordinator API."""
from __future__ import annotations

from copy import deepcopy

import pytest

import fleet.blender_v2_update as updater
from fleet.blender_v2_update import blender_v2_workshop
from fleet.build_route import RouteBaseline, RouteDocument


def v7_baseline() -> dict:
    baseline = RouteDocument.compatibility().to_dict()["baseline"]
    baseline["workshop"].update(mode="blocks", banned_upgrade_ids=[], blocks=[
        {"id": "block.old", "type": "pool", "selection": "weighted", "count_scope": "account",
         "decay_pct": 20, "weight_floor": 1, "upgrade_ids": ["attack_speed", "thorns"],
         "weights": {"attack_speed": 1, "thorns": 1}}])
    return baseline


def test_v2_replaces_only_the_workshop_program_and_is_idempotent() -> None:
    baseline = v7_baseline()
    original = deepcopy(baseline)
    updated = blender_v2_workshop(baseline)
    assert baseline == original
    assert updated["battle"] == baseline["battle"] and updated["labs"] == baseline["labs"]
    assert [block["id"] for block in updated["workshop"]["blocks"]] == ["bv2.unlocks", "bv2.value", "bv2.wait"]
    assert updated["workshop"]["blocks"][1]["selection"] == "value"
    RouteBaseline.from_dict(updated)
    assert blender_v2_workshop(updated) == updated


def test_v2_refuses_a_banned_pool_skill() -> None:
    baseline = v7_baseline()
    baseline["workshop"]["banned_upgrade_ids"] = ["thorns"]
    with pytest.raises(ValueError, match="banned"):
        blender_v2_workshop(baseline)


def fake_coordinator(baseline: dict) -> tuple[dict, dict, object]:
    library = {"revision": 0, "strategies": [
        {"id": "b", "name": "blender", "version": 7, "source_template": "scratch", "baseline": baseline}]}
    route = {"revision": 0, "assignments": {
        worker: {"account_id": worker, "strategy_id": "b", "strategy_version": 7, "baseline": baseline}
        for worker in ("Tiramisu64_82", "Tiramisu64_83")}}

    def request(_: str, path: str, payload: dict | None = None) -> dict:
        if payload is None:
            return deepcopy(library if path.endswith("/strategies") else route)
        if path.endswith("/strategies"):
            assert payload["expected_revision"] == library["revision"]
            library["revision"] += 1
            row = library["strategies"][0]
            row.update(version=row["version"] + 1, baseline=payload["baseline"])
            return deepcopy(library)
        assert path.endswith("/strategies/assign")
        assert payload["expected_revision"] == route["revision"]
        route["revision"] += 1
        for target in payload["workers"]:
            route["assignments"][target["worker"]].update(
                strategy_version=payload["strategy_version"], baseline=library["strategies"][0]["baseline"])
        return deepcopy(route)
    return library, route, request


def test_preview_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    library, route, request = fake_coordinator(v7_baseline())
    monkeypatch.setattr(updater, "_request", request)
    change = updater.update_blender("unused")
    assert (change["current_version"], change["target_version"], change["save"]) == (7, 8, True)
    assert change["assign_workers"] == ["Tiramisu64_82", "Tiramisu64_83"]
    assert (library["revision"], route["revision"]) == (0, 0)


def test_apply_saves_v8_and_assigns_both_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    library, route, request = fake_coordinator(v7_baseline())
    monkeypatch.setattr(updater, "_request", request)
    updater.update_blender("unused", apply=True)
    assert library["strategies"][0]["version"] == 8
    for worker in ("Tiramisu64_82", "Tiramisu64_83"):
        assert route["assignments"][worker]["strategy_version"] == 8
        assert route["assignments"][worker]["baseline"]["workshop"]["blocks"][0]["type"] == "unlock"
    monkeypatch.setattr(updater, "_request", request)
    again = updater.update_blender("unused")
    assert (again["save"], again["assign_workers"]) == (False, [])
