"""The port of fleet/reroll_planner.py's weights into knowledge/builds.v1.json.

These tests were written while the port had two sides: `_OPENING`,
`_TURTLE`, `_PREREQUISITES`, `_TARGETS` and `_FOCUS` still stood in
`fleet/reroll_planner.py`, and the comparisons below read them from that
module so that editing either side without the other failed here.

That second side is deliberately gone: the planner is now an adapter over
this pack and holds no numbers at all, which is the whole point of the
phase - one copy, in the committed file. The comparisons are kept, re-aimed
at the two things that can still disagree: what `knowledge/builds.v1.json`
says on disk, and what `builds.REGISTRY` serves after loading it. That is a
weaker claim than the one they made before, and it is the strongest one
still available - the planner's tables survive only in git history, at the
revision `build_sources` pins (`fc3e883`), and a test that shelled out to
`git show` to recover them would be pinning a commit rather than a
behaviour. The behaviour that the pack reproduces the planner's ordering is
pinned instead by `tests/test_reroll_planner.py`, which runs the adapter,
and by `tests/test_value_propagation.py`, which builds the real turtle graph
out of this pack.

Everything else here checks that a malformed pack fails loudly.
"""

from __future__ import annotations

import copy
import json

import pytest

import builds
import strategy
import upgrades


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


@pytest.mark.parametrize("build_id", ["opening", "turtle"])
def test_the_loaded_weights_match_the_committed_file_exactly(build_id: str) -> None:
    """The regression that proves the load is faithful, order included.

    Was a comparison against `reroll_planner._OPENING` / `._TURTLE`; see the
    module docstring for where that side went. The pairs are compared as
    written rather than through a dict, because the ORDER of these rows
    breaks ties between equal effective weights and a loader that sorted
    would change what the bot buys while every value-level assertion passed.
    """
    assert builds.by_id(build_id).weights == tuple(
        (upgrade_id, float(weight))
        for upgrade_id, weight in _a_build(_payload(), build_id)["weights"]
    )


def test_weight_order_is_preserved_and_is_not_alphabetical_or_sorted_by_weight() -> None:
    """Order is the planner's tiebreak between two upgrades on equal effective
    weight, so a loader that sorted would change what the bot buys while every
    value-level assertion still passed."""
    ids = builds.by_id("opening").upgrade_ids
    assert ids[:3] == ("damage", "attack_speed", "unlock_cash_bonuses")
    assert list(ids) != sorted(ids)
    weights = [w for _, w in builds.by_id("opening").weights]
    assert weights != sorted(weights, reverse=True)


def test_the_prerequisite_map_matches_the_committed_file_exactly() -> None:
    """Was a comparison against `reroll_planner._PREREQUISITES`; the map the
    planner declared is now this one, read through `builds.prerequisites()`
    by `workshop_objectives`. `defense_percent -> unlock_defense_upgrades`
    is still here, unlike the dead `defense_percent` TARGET below: the
    unlock graph is a fact about the game whether or not a build weights the
    row, and it is shared rather than held per build for that reason."""
    assert dict(builds.prerequisites()) == dict(_payload()["prerequisites"]["requires"])


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


def test_every_build_target_matches_the_committed_file_exactly() -> None:
    """Was `reroll_planner._TARGETS[upgrade_id] == target`.

    The second assertion is the surviving half of that comparison and the
    more interesting one: the planner's `_TARGETS` also carried
    `defense_percent: 50.0`, which was DEAD - `defense_percent` is weighted
    by neither build, and the old ranking loop consulted `_TARGETS` only for
    ids already in the candidate list, so it was never once evaluated. It
    was left out of the port on purpose, `builds.Build._validate` now
    rejects a target for an unweighted upgrade outright, and this pins that
    nobody reintroduces it under either rule.
    """
    for build in builds.REGISTRY.builds:
        assert dict(build.targets) == _a_build(_payload(), build.id)["targets"]
        assert "defense_percent" not in build.targets


def test_focus_strings_match_the_committed_file_and_cover_every_weighted_row() -> None:
    """Was a lookup into `reroll_planner._FOCUS`, one line per weighted row.

    The coverage half of that assertion is kept explicitly: `_FOCUS` was a
    single map the two builds shared, so every weighted id necessarily had a
    line, and a per-build `focus` map could silently lose one -
    `project_next` would then publish "Advance the reroll account" to the
    dashboard instead of the sentence a human wrote.
    """
    for build in builds.REGISTRY.builds:
        assert dict(build.focus) == _a_build(_payload(), build.id)["focus"]
        assert set(build.focus) == set(build.upgrade_ids)


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


# --- the port's fidelity, pinned as literals ------------------------------
#
# These tables are transcribed from fleet/reroll_planner.py as it stood at
# 0d202ee, the last revision before phase 6 deleted _OPENING, _TURTLE,
# _PREREQUISITES and _TARGETS in favour of this pack.
#
# They exist because deleting those constants removed the only independent
# statement of what the bot buys. Comparing the loaded REGISTRY against the
# committed JSON checks the LOADER, not the port: both sides move together
# the moment anyone edits the file, so that pair would stay green through an
# edit that silently changed the first purchase on every reroll account.
#
# Written out by hand, from git, on purpose. A test that shelled out to
# `git show` would pin a commit rather than a behaviour, and would start
# failing for reasons that have nothing to do with the numbers.
# The opening is no longer the legacy planner's: it is a strict priority -
# Damage > Attack Speed > Coins/Wave (with its unlocks) > the Thorns chain
# with two levels of Defense Absolute - with each group spaced far enough
# apart that value_propagation's 0.6 credit to an unlock cannot reorder them
# (unlock_cash_bonuses inherits to 81.6, below attack_speed's 90; the whole
# thorns chain tops out at unlock_thorns' 44, below coins_per_wave's 60).
_PLANNER_OPENING = (
    ("damage", 100.), ("attack_speed", 90.),
    ("unlock_cash_bonuses", 30.), ("unlock_coin_bonuses", 50.),
    ("coins_per_wave", 60.),
    ("unlock_defense_upgrades", 10.), ("unlock_thorns", 20.),
    ("defense_absolute", 42.), ("thorns", 40.),
)
_PLANNER_TURTLE = (
    ("unlock_defense_upgrades", 10.), ("defense_absolute", 16.),
    ("unlock_thorns", 9.), ("thorns", 12.),
    ("cash_bonus", 5.), ("coins_per_kill_bonus", 5.), ("health", 3.),
)
_PLANNER_PREREQUISITES = {
    "defense_absolute": "unlock_defense_upgrades",
    "unlock_thorns": "unlock_defense_upgrades",
    "thorns": "unlock_thorns",
    "cash_bonus": "unlock_cash_bonuses",
    "unlock_coin_bonuses": "unlock_cash_bonuses",
    "coins_per_kill_bonus": "unlock_coin_bonuses",
    "coins_per_wave": "unlock_coin_bonuses",
    "defense_percent": "unlock_defense_upgrades",
}


@pytest.mark.parametrize("build_id,expected", [
    ("opening", _PLANNER_OPENING),
    ("turtle", _PLANNER_TURTLE),
])
def test_the_pack_still_carries_the_planners_weights_in_its_original_order(
    build_id: str, expected: tuple[tuple[str, float], ...],
) -> None:
    """Order is asserted, not just membership: the ranking breaks ties on
    declaration order, so a reordered pack changes what the bot buys while
    every set-comparison stays green."""
    build = builds.by_id(build_id)
    assert build is not None
    assert tuple((row, float(weight)) for row, weight in build.weights) == expected


def test_the_pack_still_carries_the_planners_prerequisite_map() -> None:
    assert dict(builds.prerequisites()) == _PLANNER_PREREQUISITES


def test_only_the_live_target_was_carried_over() -> None:
    """_TARGETS held thorns 51 and defense_percent 50. The second was dead:
    defense_percent is weighted by neither build, and the old choose_next
    read _TARGETS only for ids already in its candidate list, so that cap
    was never once evaluated. Carrying it would have re-advertised a limit
    that acts on nothing."""
    for build_id in ("opening", "turtle"):
        build = builds.by_id(build_id)
        assert build is not None
        assert dict(build.targets) == {"thorns": 51.}


def test_the_opening_paces_attack_against_coins_per_wave() -> None:
    build = builds.by_id("opening")
    assert build is not None
    caps = build.level_caps
    assert set(caps) == {"damage", "attack_speed", "coins_per_wave", "defense_absolute"}
    assert caps["damage"].allowance({}) == 2
    assert caps["damage"].allowance({"coins_per_wave": 3}) == 5
    assert caps["attack_speed"].allowance({"coins_per_wave": 1, "damage": 9}) == 3
    assert caps["coins_per_wave"].allowance({"coins_per_wave": 99}) == 3
    assert caps["defense_absolute"].allowance({}) == 2


def test_a_build_without_level_caps_loads_with_none() -> None:
    build = builds.by_id("turtle")
    assert build is not None
    assert dict(build.level_caps) == {}


@pytest.mark.parametrize("cap,match", [
    ({"health": {"base": 2}}, "unweighted"),
    ({"thorns": {"base": 2}}, "also targets"),
    ({"unlock_thorns": {"base": 1}}, "unlock"),
    ({"damage": {"base": -1}}, "base"),
    ({"damage": {"base": 1, "ratio": -1, "per": ["coins_per_wave"]}}, "ratio"),
    ({"damage": {"base": 1, "ratio": 1}}, "per"),
    ({"damage": {"base": 1, "ratio": 1, "per": ["not_an_upgrade"]}}, "unknown"),
    ({"damage": {"base": 1, "ratio": 1, "per": ["damage"]}}, "itself"),
    ({"damage": {"base": 1, "bogus": 1}}, "fields"),
])
def test_a_malformed_level_cap_fails_validation(cap: dict, match: str) -> None:
    payload = _payload()
    _a_build(payload)["level_caps"] = cap
    with pytest.raises(ValueError, match=match):
        builds.BuildPack.from_payload(payload)


def test_the_opening_offers_three_variants_of_its_caps() -> None:
    opening = builds.by_id("opening")
    assert [v.id for v in opening.variants] == ["baseline", "income_first", "attack_heavy"]
    assert opening.variant("baseline").level_caps == opening.level_caps
    income = opening.variant("income_first").level_caps
    assert income["damage"].allowance({"coins_per_wave": 2}) == 3
    assert income["coins_per_wave"].base == 5
    heavy = opening.variant("attack_heavy").level_caps
    assert heavy["attack_speed"].allowance({"coins_per_wave": 1}) == 5
    assert heavy["coins_per_wave"].base == 2
    assert opening.variant("gone") is None and opening.variant(None) is None
    assert builds.by_id("turtle").variants == ()


@pytest.mark.parametrize("variants,match", [
    ([{"id": "a", "name": "A", "level_caps": {}},
      {"id": "a", "name": "B", "level_caps": {}}], "twice"),
    ([{"id": "Not A Slug", "name": "A", "level_caps": {}}], "slug"),
    ([{"id": "a", "name": "", "level_caps": {}}], "name"),
    ([{"id": "a", "name": "A", "level_caps": {}, "bogus": 1}], "variant 'a' has fields"),
    ([{"id": "a", "name": "A", "level_caps": {"health": {"base": 2}}}], "unweighted"),
    ([{"id": "a", "name": "A", "level_caps": {"damage": {"base": -1}}}], "base"),
])
def test_a_malformed_variant_fails_validation(variants: list, match: str) -> None:
    payload = _payload()
    _a_build(payload)["variants"] = variants
    with pytest.raises(ValueError, match=match):
        builds.BuildPack.from_payload(payload)
