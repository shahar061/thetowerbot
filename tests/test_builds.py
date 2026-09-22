"""The port of fleet/reroll_planner.py's weights into knowledge/builds.v1.json.

The load-bearing tests here are the ones that compare the pack against
`fleet.reroll_planner`'s own module constants. Everything else checks that a
malformed pack fails loudly; those comparisons check that the faithful pack
says exactly what the planner already says, which is the only evidence that
the copy is a port rather than a rewrite. They are expected to fail the day
somebody edits one side without the other - that is the point of them.
"""

from __future__ import annotations

import copy
import json

import pytest

import builds
import strategy
import upgrades
from fleet import reroll_planner


def _payload() -> dict:
    """The committed pack as mutable JSON, for tests that break it on purpose."""
    return json.loads(builds.PACK_PATH.read_text(encoding="utf-8"))


def _a_build(payload: dict, build_id: str = "opening") -> dict:
    return next(b for b in payload["builds"] if b["id"] == build_id)


def test_the_committed_pack_loads_at_import() -> None:
    assert builds.REGISTRY.schema_version == 1
    assert builds.REGISTRY.builds


def test_both_ported_builds_are_present() -> None:
    assert builds.ids() == ("opening", "turtle")
    assert builds.by_id("opening") is not None
    assert builds.by_id("turtle") is not None


def test_an_unknown_build_id_resolves_to_none_rather_than_raising() -> None:
    """strategy.py turns this None into a ControlError naming the field; a
    raise here would make that a try/except across a validation boundary."""
    assert builds.by_id("no_such_build") is None


def test_the_opening_weights_match_the_reroll_planner_exactly() -> None:
    """The regression that proves the port is faithful, order included."""
    assert builds.by_id("opening").weights == tuple(
        (upgrade_id, float(weight)) for upgrade_id, weight in reroll_planner._OPENING
    )


def test_the_turtle_weights_match_the_reroll_planner_exactly() -> None:
    assert builds.by_id("turtle").weights == tuple(
        (upgrade_id, float(weight)) for upgrade_id, weight in reroll_planner._TURTLE
    )


def test_weight_order_is_preserved_and_is_not_alphabetical_or_sorted_by_weight() -> None:
    """Order is the planner's tiebreak between two upgrades on equal effective
    weight, so a loader that sorted would change what the bot buys while every
    value-level assertion still passed."""
    ids = builds.by_id("opening").upgrade_ids
    assert ids[:3] == ("damage", "attack_speed", "unlock_defense_upgrades")
    assert list(ids) != sorted(ids)
    weights = [w for _, w in builds.by_id("opening").weights]
    assert weights != sorted(weights, reverse=True)


def test_the_prerequisite_map_matches_the_reroll_planner_exactly() -> None:
    assert dict(builds.prerequisites()) == dict(reroll_planner._PREREQUISITES)


def test_the_prerequisite_map_is_shared_rather_than_held_per_build() -> None:
    """It describes the game's unlock graph, not anyone's strategy, so there
    is deliberately no per-build copy for two builds to disagree over."""
    assert not any(hasattr(build, "prerequisites") for build in builds.REGISTRY.builds)
    assert builds.prerequisites() is builds.REGISTRY.prerequisites.requires


def test_the_prerequisite_map_cannot_be_mutated_through_the_public_accessor() -> None:
    """It is a process-wide singleton; a writable view would let one caller
    repoint the unlock graph for every later reader."""
    with pytest.raises(TypeError):
        builds.prerequisites()["thorns"] = "damage"


def test_every_build_target_comes_from_the_reroll_planners_targets() -> None:
    for build in builds.REGISTRY.builds:
        for upgrade_id, target in build.targets.items():
            assert reroll_planner._TARGETS[upgrade_id] == target


def test_focus_strings_match_the_reroll_planners_focus_text() -> None:
    for build in builds.REGISTRY.builds:
        assert dict(build.focus) == {
            upgrade_id: reroll_planner._FOCUS[upgrade_id]
            for upgrade_id in build.upgrade_ids
        }


def test_every_upgrade_id_in_the_pack_resolves_in_the_upgrade_catalog() -> None:
    mentioned = {u for b in builds.REGISTRY.builds for u in b.upgrade_ids}
    mentioned |= {u for b in builds.REGISTRY.builds for u in b.targets}
    mentioned |= set(builds.prerequisites()) | set(builds.prerequisites().values())
    assert mentioned
    for upgrade_id in mentioned:
        assert upgrades.by_id(upgrade_id) is not None, upgrade_id


def test_the_ported_weights_are_honestly_marked_unverified() -> None:
    """They came from a working planner, so they exist (definition_verified);
    nobody has shown they are optimal, so rule_verified stays false."""
    for build in builds.REGISTRY.builds:
        assert build.definition_verified is True
        assert build.rule_verified is False


def test_every_build_cites_a_declared_source() -> None:
    declared = {source.id for source in builds.REGISTRY.sources}
    for build in builds.REGISTRY.builds:
        assert build.source_refs and set(build.source_refs) <= declared


def test_an_unknown_upgrade_id_in_the_weights_fails_validation() -> None:
    """The failure this whole module exists for: an unknown id is skipped by
    the ranking loop, so a typo plans nothing while looking like patience."""
    payload = _payload()
    _a_build(payload)["weights"][0][0] = "damge"
    with pytest.raises(ValueError, match="damge"):
        builds.BuildPack.from_payload(payload)


def test_an_unknown_upgrade_id_in_a_target_fails_validation() -> None:
    payload = _payload()
    _a_build(payload)["targets"] = {"thorn": 51.0}
    with pytest.raises(ValueError, match="thorn"):
        builds.BuildPack.from_payload(payload)


def test_an_unknown_upgrade_id_in_the_prerequisite_map_fails_validation() -> None:
    payload = _payload()
    payload["prerequisites"]["requires"]["thorns"] = "unlock_thorn"
    with pytest.raises(ValueError, match="unlock_thorn"):
        builds.BuildPack.from_payload(payload)


def test_a_target_for_an_unweighted_upgrade_fails_validation() -> None:
    """The planner reads targets only for ids already in the weight list, so
    such a target caps nothing and the file would be claiming otherwise."""
    payload = _payload()
    _a_build(payload)["targets"] = {"defense_percent": 50.0}
    with pytest.raises(ValueError, match="unweighted"):
        builds.BuildPack.from_payload(payload)


def test_an_empty_build_fails_validation() -> None:
    payload = _payload()
    _a_build(payload)["weights"] = []
    with pytest.raises(ValueError, match="no weights"):
        builds.BuildPack.from_payload(payload)


def test_a_pack_with_no_builds_fails_validation() -> None:
    payload = _payload()
    payload["builds"] = []
    with pytest.raises(ValueError, match="no builds"):
        builds.BuildPack.from_payload(payload)


@pytest.mark.parametrize("weight", [0, -1, "12"])
def test_a_non_positive_or_non_numeric_weight_fails_validation(weight: object) -> None:
    payload = _payload()
    _a_build(payload)["weights"][0][1] = weight
    with pytest.raises(ValueError):
        builds.BuildPack.from_payload(payload)


def test_a_boolean_weight_fails_validation() -> None:
    """bool is a subclass of int, so an unguarded numeric check would read
    True as a weight of 1.0 and silently reorder the build."""
    payload = _payload()
    _a_build(payload)["weights"][0][1] = True
    with pytest.raises(ValueError):
        builds.BuildPack.from_payload(payload)


def test_a_duplicate_upgrade_in_one_build_fails_validation() -> None:
    payload = _payload()
    _a_build(payload)["weights"].append(["damage", 1.0])
    with pytest.raises(ValueError, match="twice"):
        builds.BuildPack.from_payload(payload)


def test_a_duplicate_build_id_fails_validation() -> None:
    payload = _payload()
    payload["builds"].append(copy.deepcopy(_a_build(payload)))
    with pytest.raises(ValueError, match="duplicate build id"):
        builds.BuildPack.from_payload(payload)


def test_a_misspelled_pack_field_fails_validation() -> None:
    """Unknown keys are an error rather than ignored: a hand-edited pack is
    exactly where the file says one thing and the loader reads another."""
    payload = _payload()
    payload["buildz"] = payload.pop("builds")
    with pytest.raises(ValueError, match="unexpected build pack fields"):
        builds.BuildPack.from_payload(payload)


def test_a_build_citing_an_undeclared_source_fails_validation() -> None:
    payload = _payload()
    _a_build(payload)["source_refs"] = ["wherever"]
    with pytest.raises(ValueError, match="unknown build source"):
        builds.BuildPack.from_payload(payload)


def test_a_prerequisite_cycle_fails_validation() -> None:
    """A cycle does not loop or crash the planner, it makes every upgrade in
    the cycle permanently unbuyable - a silent no-op spelled with real ids."""
    payload = _payload()
    payload["prerequisites"]["requires"]["unlock_defense_upgrades"] = "defense_absolute"
    with pytest.raises(ValueError, match="cycle"):
        builds.BuildPack.from_payload(payload)


def test_the_pack_shares_the_knowledge_directorys_schema_and_pack_version() -> None:
    """knowledge.py globs knowledge/*.v1.json and refuses the union unless
    every file agrees on both, so a bump to the other packs that skips this
    one breaks `import knowledge` outright. Named here so the breakage
    arrives as this assertion rather than as an import error somewhere else.
    """
    import knowledge

    neighbours = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(knowledge.KNOWLEDGE_DIR.glob("*.v1.json"))
        if path != builds.PACK_PATH
    ]
    assert neighbours
    assert {p["schema_version"] for p in neighbours} == {builds.REGISTRY.schema_version}
    assert {p["pack_version"] for p in neighbours} == {builds.REGISTRY.pack_version}


def test_the_build_pack_contributes_nothing_to_the_knowledge_pack() -> None:
    """Its provenance lives under `build_sources`, not `sources`, precisely so
    the merge in knowledge.Pack.load ignores it: a knowledge source must
    resolve to a file in the untracked wiki corpus, and this one points at a
    .py file in this repo."""
    import knowledge

    assert "sources" not in _payload()
    assert not any(s.file.endswith(".py") for s in knowledge.KNOWLEDGE.sources)


def test_a_strategy_may_name_a_committed_build() -> None:
    tuned = strategy.Strategy.from_config("mine").merged({"build": "turtle"})
    assert tuned.build == "turtle"
    assert strategy.Strategy.from_dict(tuned.to_dict()) == tuned


def test_a_strategy_names_no_build_by_default() -> None:
    """A default build id would silently change what every profile written
    before this existed says it wants."""
    assert strategy.Strategy.from_config("mine").build is None


def test_a_strategy_naming_an_unknown_build_is_rejected() -> None:
    with pytest.raises(strategy.ControlError) as caught:
        strategy.Strategy.from_config("mine").merged({"build": "sniper"})
    assert caught.value.field == "build"
