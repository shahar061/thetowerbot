"""Saved copies are versioned independently of published account assignments."""
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from fleet.build_route import RouteBaseline, RouteDocument, resolve_route
from fleet.build_route_store import RouteUnavailable
from fleet.strategy_library import StrategyLibrary, LibraryConflict


def test_card_program_pinned_to_saved_library_version(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    baseline = library.read()["templates"][0]["baseline"]
    baseline["cards"] = {"version": 1, "gem_cap": 100, "goals": [], "loadouts": []}
    saved = library.save(expected_revision=0, name="Cards", source_template="opening",
                         baseline=baseline)["strategies"][0]
    baseline["cards"]["gem_cap"] = 200
    library.save(expected_revision=1, name="Cards", source_template="opening",
                 baseline=baseline, strategy_id=saved["id"])
    assert library.version(saved["id"], 1)["baseline"]["cards"]["gem_cap"] == 100
    assert library.version(saved["id"], 2)["baseline"]["cards"]["gem_cap"] == 200


def test_legacy_cards_project_on_read_and_migrate_only_when_saved(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    baseline = library.read()["templates"][0]["baseline"]
    baseline["gems"].update(mode="blocks", blocks=[
        {"id": "slot2", "type": "unlock_lab_slot", "slot": 2},
        {"id": "old", "type": "buy_cards", "purpose": "until_cards", "cards": ["Damage"]},
    ])
    projected = RouteBaseline.from_dict(baseline)
    assert projected.cards is not None
    assert projected.cards.gem_cap == 0
    assert projected.gems.blocks[1]["type"] == "buy_cards"
    saved = library.save(expected_revision=0, name="Old", source_template="opening",
                         baseline=baseline)["strategies"][0]
    assert saved["baseline"]["gems"]["blocks"][1] == {
        "id": "old", "type": "card_goal", "goal_id": "old"}
    assert saved["baseline"]["cards"]["gem_cap"] == 0


def test_card_goal_requires_matching_program_goal() -> None:
    baseline = RouteDocument.compatibility().baseline.to_dict()
    baseline["gems"].update(mode="blocks", blocks=[
        {"id": "slot2", "type": "unlock_lab_slot", "slot": 2},
        {"id": "card", "type": "card_goal", "goal_id": "absent"},
    ])
    with pytest.raises(ValueError, match="goal"):
        RouteDocument.from_dict({**RouteDocument.compatibility().to_dict(), "baseline": baseline})


def test_effective_card_program_uses_assignment_only_for_matching_account() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["cards"] = {"version": 1, "gem_cap": 100, "goals": [], "loadouts": []}
    assigned = RouteDocument.compatibility().baseline.to_dict()
    assigned["cards"] = {"version": 1, "gem_cap": 200, "goals": [], "loadouts": []}
    raw["assignments"] = {"Air_1": {"account_id": "A", "strategy_id": "custom-1",
        "strategy_version": 1, "strategy_name": "Pinned", "baseline": assigned}}
    route = RouteDocument.from_dict(raw)
    assert resolve_route(route, "Air_1", "A").cards.gem_cap == 200
    assert resolve_route(route, "Air_2", "A").cards.gem_cap == 100
    assert resolve_route(route, "Air_1", "B").cards is None


def test_save_versions_do_not_publish_and_old_version_is_immutable(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    template = library.read()["templates"][0]
    first = library.save(expected_revision=0, name="My opening", source_template="opening",
                         baseline=template["baseline"])
    saved = first["strategies"][0]
    baseline = saved["baseline"]
    baseline["workshop"]["coin_spend_limit_pct"] = 35
    second = library.save(expected_revision=1, name="My opening", source_template="opening",
                          baseline=baseline, strategy_id=saved["id"])
    assert second["strategies"][0]["version"] == 2
    assert library.version(saved["id"], 1)["baseline"]["workshop"]["coin_spend_limit_pct"] == 100
    assert not (tmp_path / "build-route.json").exists()
    with pytest.raises(LibraryConflict):
        library.save(expected_revision=0, name="Stale", source_template="opening", baseline=baseline)


def test_templates_are_protected_and_invalid_blocks_rejected(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    templates = library.read()["templates"]
    assert [(item["id"], item["builtin"]) for item in templates] == [
        ("opening", True), ("turtle", True), ("labs_gems", True)]
    baseline = templates[0]["baseline"]
    assert baseline["workshop"]["mode"] == "blocks"
    with pytest.raises(ValueError, match="protected"):
        library.save(expected_revision=0, name="Overwrite", source_template="opening",
                     baseline=baseline, strategy_id="opening")
    baseline["workshop"]["blocks"] = [{"id": "bad", "type": "script", "code": "run()"}]
    with pytest.raises(ValueError):
        library.save(expected_revision=0, name="Unsafe", source_template="opening", baseline=baseline)


def test_scratch_strategy_saves_empty_programs_as_immutable_versions(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    baseline = library.read()["templates"][0]["baseline"]
    baseline["workshop"].update(mode="blocks", blocks=[])
    baseline["battle"].update(mode="blocks", branches=[], blocks=[])

    first = library.save(expected_revision=0, name="From zero", source_template="scratch",
                         baseline=baseline)
    saved = first["strategies"][0]
    assert saved["source_template"] == "scratch"
    assert saved["baseline"]["workshop"]["blocks"] == []
    assert saved["baseline"]["battle"]["blocks"] == ()
    assert library.version(saved["id"], 1)["source_template"] == "scratch"

    revised = library.save(expected_revision=1, name="From zero", source_template="scratch",
                           baseline=baseline, strategy_id=saved["id"])
    assert revised["strategies"][0]["version"] == 2
    assert library.version(saved["id"], 1)["baseline"]["workshop"]["blocks"] == []


def test_assignment_takes_precedence_and_replacement_falls_back_safely() -> None:
    raw = RouteDocument.compatibility().to_dict()
    assigned = RouteDocument.compatibility().to_dict()["baseline"]
    assigned["workshop"]["coin_spend_limit_pct"] = 35
    raw["baseline"]["workshop"]["coin_spend_limit_pct"] = 80
    raw["overrides"] = {"Air_1": {"account_id": "A", "patches": {
        "workshop.default": {"coin_spend_limit_pct": 5}}}}
    raw["assignments"] = {"Air_1": {"account_id": "A", "strategy_id": "custom-1",
        "strategy_version": 1, "strategy_name": "Safe", "baseline": assigned}}
    route = RouteDocument.from_dict(raw)
    assert resolve_route(route, "Air_1", "A").workshop.coin_spend_limit_pct == 35
    replacement = resolve_route(route, "Air_1", "B")
    assert replacement.workshop.coin_spend_limit_pct == 100
    assert replacement.override_state == "inactive_account_changed"
    assert RouteDocument.from_dict(route.to_dict()) == route


def test_workshop_blocks_round_trip_through_saved_library(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    baseline = library.read()["templates"][0]["baseline"]
    workshop_blocks = [
        {"id": "econ", "type": "budget", "metric": "utility_spent", "target": 350, "ceiling": 400,
         "blocks": [{"id": "g", "type": "save_for", "goal": [
             {"id": "g.p", "type": "pool", "upgrade_ids": ["cash_per_wave"], "selection": "priority",
              "level_caps": {"cash_per_wave": {"base": 2}}}]}]},
        {"id": "ws", "type": "while_saving", "blocks": [
            {"id": "f", "type": "pool", "upgrade_ids": ["damage"], "selection": "priority",
             "wallet_share_pct": 20}]},
    ]
    baseline["workshop"].update(mode="blocks", blocks=workshop_blocks)
    saved = library.save(expected_revision=0, name="Round trip", source_template="opening", baseline=baseline)
    strategy_id = saved["strategies"][0]["id"]
    loaded = library.version(strategy_id, 1)
    assert loaded["baseline"]["workshop"]["blocks"] == workshop_blocks


def test_builtin_versions_are_assignable_but_only_version_one_exists(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    assert library.version("opening", 1)["builtin"] is True
    assert library.version("turtle", 1)["source_template"] == "turtle"
    with pytest.raises(ValueError):
        library.version("opening", 2)


def test_strategy_names_are_unique_casefolded_and_reserved(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    baseline = library.read()["templates"][0]["baseline"]
    for name in (" Opening ", "TURTLE"):
        with pytest.raises(ValueError, match="name.*exists"):
            library.save(expected_revision=0, name=name, source_template="opening", baseline=baseline)
    saved = library.save(expected_revision=0, name="My build", source_template="opening", baseline=baseline)
    with pytest.raises(ValueError, match="name.*exists"):
        library.save(expected_revision=1, name=" MY BUILD ", source_template="opening", baseline=baseline)
    assert library.save(expected_revision=1, name="MY BUILD", source_template="opening", baseline=baseline,
                        strategy_id=saved["strategies"][0]["id"])["revision"] == 2


def test_concurrent_duplicate_saves_cannot_create_two_names(tmp_path: Path) -> None:
    baseline = StrategyLibrary(tmp_path).read()["templates"][0]["baseline"]
    ready = Barrier(2)

    def save_copy(name: str) -> str:
        library = StrategyLibrary(tmp_path)
        ready.wait(timeout=5)
        try:
            library.save(expected_revision=0, name=name, source_template="opening", baseline=baseline)
            return "saved"
        except LibraryConflict:
            # A caller retrying against the fresh revision must still lose the duplicate-name race.
            with pytest.raises(ValueError, match="name.*exists"):
                library.save(expected_revision=1, name=name, source_template="opening", baseline=baseline)
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save_copy, ["My build", "MY BUILD"]))
    assert sorted(outcomes) == ["duplicate", "saved"]
    assert len(StrategyLibrary(tmp_path).read()["strategies"]) == 1


def test_save_stamps_saved_at_and_old_rows_without_it_still_load(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    baseline = library.read()["templates"][0]["baseline"]
    # A library written before saves were timestamped.
    (tmp_path / "strategy-library.json").write_text(json.dumps({"revision": 1, "versions": [
        {"id": "strategy-old", "name": "Old", "version": 1, "source_template": "opening",
         "baseline": baseline, "builtin": False}]}), encoding="utf-8")
    assert library.version("strategy-old", 1)["name"] == "Old"
    before = time.time()
    saved = library.save(expected_revision=1, name="Old", source_template="opening",
                         baseline=baseline, strategy_id="strategy-old")
    latest = saved["strategies"][0]
    assert latest["version"] == 2 and before <= latest["saved_at"] <= time.time()
    assert "saved_at" not in library.version("strategy-old", 1)


def test_invalid_historical_baseline_does_not_hide_a_valid_latest_version(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    baseline = library.read()["templates"][0]["baseline"]
    baseline["labs"].update(mode="blocks", blocks=[
        {"id": "lab.track", "type": "slot_track", "slots": [1], "children": [
            {"id": "lab.speed", "type": "research", "lab_id": "labs.game-speed", "to_level": 7},
            {"id": "lab.pool", "type": "lab_pool", "lab_ids": ["labs.coins-wave"]}]}])
    first = library.save(expected_revision=0, name="Saved", source_template="scratch",
                         baseline=baseline)["strategies"][0]
    library.save(expected_revision=1, name="Saved", source_template="scratch",
                 strategy_id=first["id"], baseline=baseline)

    state = json.loads(library.path.read_text(encoding="utf-8"))
    state["versions"][0]["baseline"]["labs"]["blocks"][0]["children"][1]["max_price"] = 100
    library.path.write_text(json.dumps(state), encoding="utf-8")

    loaded = library.read()
    assert loaded["revision"] == 2
    assert [(row["id"], row["version"]) for row in loaded["strategies"]] == [(first["id"], 2)]
    assert library.version(first["id"], 2)["version"] == 2
    with pytest.raises(RouteUnavailable, match="strategy version unavailable"):
        library.version(first["id"], 1)
    saved = library.save(expected_revision=2, name="Saved", source_template="scratch",
                         strategy_id=first["id"], baseline=baseline)
    assert saved["strategies"][0]["version"] == 3
    assert "max_price" in library.path.read_text(encoding="utf-8")


def test_invalid_latest_baseline_still_blocks_library_read(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    baseline = library.read()["templates"][0]["baseline"]
    library.save(expected_revision=0, name="Saved", source_template="opening", baseline=baseline)
    state = json.loads(library.path.read_text(encoding="utf-8"))
    state["versions"][0]["baseline"]["rules"]["coins"]["workshop_spend_limit_pct"] = "all"
    library.path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(RouteUnavailable, match="strategy library unavailable"):
        library.read()


def test_common_labs_and_gems_template_is_protected_and_copyable(tmp_path: Path) -> None:
    library = StrategyLibrary(tmp_path)
    templates = library.read()["templates"]
    template = next(item for item in templates if item["id"] == "labs_gems")
    opening = next(item for item in templates if item["id"] == "opening")
    baseline = template["baseline"]
    assert template["name"] == "Common Labs & Gems path"
    assert baseline["workshop"]["blocks"] == opening["baseline"]["workshop"]["blocks"]
    assert baseline["battle"]["blocks"] == opening["baseline"]["battle"]["blocks"]
    assert (baseline["gems"]["mode"], baseline["labs"]["mode"]) == ("blocks", "blocks")
    assert baseline["labs"]["blocks"][0]["type"] == "lab_list"
    assert baseline["rules"]["coins"]["lab_share"] == {"mode": "save_pct", "pct": 25}
    assert baseline["rules"]["labs"]["filler"] == {"enabled": True, "max_price_pct_of_wallet": 10, "min_hours": 1.0}
    assert baseline["rules"]["labs"]["auto_start"] and baseline["rules"]["gems"]["auto_unlock_lab_slots"]
    with pytest.raises(ValueError, match="protected"):
        library.save(expected_revision=0, name="Overwrite", source_template="labs_gems",
                     baseline=baseline, strategy_id="labs_gems")
    with pytest.raises(ValueError, match="already exists"):
        library.save(expected_revision=0, name="common labs & gems path", source_template="labs_gems",
                     baseline=baseline)
    saved = library.save(expected_revision=0, name="My labs path", source_template="labs_gems",
                         baseline=baseline)["strategies"][0]
    assert saved["source_template"] == "labs_gems"
    assert saved["baseline"]["rules"] == baseline["rules"]
    assert library.version("labs_gems", 1)["name"] == "Common Labs & Gems path"
    assert StrategyLibrary(tmp_path).read()["strategies"][0]["id"] == saved["id"]  # reload validates
