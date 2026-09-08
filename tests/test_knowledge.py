"""The knowledge pack loader.

The pack is versioned, committed data with hard validation at import: a
malformed pack fails the import rather than degrading quietly, exactly as
catalog/concepts.v1.json does. There is deliberately no numeric confidence
field - verification is definition_verified/rule_verified plus a tri-state
validity, and numeric confidence in this codebase belongs to observations.

The load-bearing rule: a fact tainted by an unresolved conflict never
authorises anything, no matter how verified it claims to be.

Two more load-bearing rules get their own tests below:

- a JSON array in a fact's `value` must come out the other side as a tuple,
  never a mutable list sitting inside a frozen record (Correction A);
- any fact `value` shaped like <known concept id prefix>.<slug> - a single
  string or every element of a list - is checked against the real concept
  catalog at load time by default, with no annotation required, so a typo'd
  concept id fails the import loudly instead of quietly matching nothing
  forever. `value_is_concept_ref: false` is the explicit opt-out for a value
  that merely looks like a concept id (Correction B, fix round 1).
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import knowledge

# research/wiki-supplement.md's "Source conflict register" table has 11 data
# rows; research/wiki-audit.md's "Important source limitations" prose adds 3
# more non-duplicate items (the glossary-vs-dedicated-pages paragraph, the
# UW+ cost bullet, the within-page-disagreements bullet) - 14 conflicts
# total, per task 1's extraction. Of those 14, 2 (sub-effects rarity
# ambiguity; system-terminology mixing) were judged un-attachable to any
# fact id and are recorded as prose in knowledge/SOURCES.md instead of as
# conflicts[] entries, per task 1's own determination - honoured here rather
# than re-litigated. That leaves 12 conflicts task 1 judged "attachable" in
# the abstract, but task 1's attachability calls (protector cap, Free
# Upgrades cap, fleet spawn scope, module substat thresholds, currency
# count, Critical Coin trigger, Wave Accelerator/Energy Shield timing, crit
# enhancement increments, Attack Speed display, and the 6 non-Enemy-Balance
# names bundled in the glossary conflict, and the within-page-disagreements
# bundle) all presuppose facts about workshop/enemy/currency/module/card
# topics this T1-T4 route pack (tiers, milestones, lab/UW/gem/card
# priorities, hazards) never contains - those conflicts have no fact id to
# taint here and are not represented. The 3 that DO name a claim this pack's
# facts actually carry are: the glossary's Enemy Balance spawn-direction
# dispute (tainting priority.cards.unlock_order), IceTae's list being
# version-dated to 0.16-0.17 (tainting priority.gems.spend_order and
# priority.cards.unlock_order), and the UW+ ability cost dispute, escalating
# vs. a flat 975 stones (tainting priority.uw.plus_ability_cost).
EXPECTED_CONFLICTS = 3


def pack(**overrides: object) -> knowledge.Pack:
    """A minimal valid pack, mutated per test."""
    payload: dict[str, object] = {
        "schema_version": 1,
        "pack_version": "2026-09-05.1",
        "sources": [{
            "id": "wiki-tiers", "file": "wiki__tier__tiers.txt",
            "sha256": "a" * 64, "captured_at": "2026-09-05",
            "revision": None, "content_scope": "rules",
        }],
        "conflicts": [],
        "facts": [{
            "id": "tier.2.unlock_wave", "value": 100, "unit": "wave",
            "source_refs": ["wiki-tiers"],
            "source_url": "https://www.tower-hub.com/wiki/tier/tiers",
            "definition_verified": True, "rule_verified": True,
            "validity": {"status": "unknown", "game_version_min": None,
                         "game_version_max": None},
        }],
    }
    payload.update(overrides)
    return knowledge.Pack.from_payload(payload)


def _as_payload(fact: knowledge.Fact_) -> dict[str, object]:
    payload = dataclasses.asdict(fact)
    return payload


# -- shape ---------------------------------------------------------------
def test_a_minimal_pack_loads() -> None:
    assert pack().by_id("tier.2.unlock_wave").value == 100


def test_an_unexpected_top_level_key_is_refused() -> None:
    with pytest.raises(ValueError, match="unexpected"):
        pack(extra=1)


def test_a_missing_top_level_key_is_refused() -> None:
    payload = json.loads(json.dumps({
        "schema_version": 1, "pack_version": "x", "sources": [], "facts": []}))
    with pytest.raises(ValueError):
        knowledge.Pack.from_payload(payload)


def test_a_true_schema_version_is_not_a_one() -> None:
    """type(...) is int, not isinstance: bool subclasses int, so True would
    otherwise pass as version 1. The same trick concepts.py:113 uses."""
    with pytest.raises(ValueError, match="schema version"):
        pack(schema_version=True)


def test_an_unsupported_schema_version_is_refused() -> None:
    with pytest.raises(ValueError, match="schema version"):
        pack(schema_version=2)


def test_an_empty_pack_version_is_refused() -> None:
    with pytest.raises(ValueError, match="pack version"):
        pack(pack_version="")


# -- provenance ----------------------------------------------------------
def test_a_source_digest_must_look_like_a_digest() -> None:
    with pytest.raises(ValueError, match="sha256"):
        pack(sources=[{"id": "s", "file": "f", "sha256": "nope",
                       "captured_at": "2026-09-05", "revision": None,
                       "content_scope": "rules"}])


def test_a_source_capture_date_must_be_a_date() -> None:
    with pytest.raises(ValueError, match="captured_at"):
        pack(sources=[{"id": "s", "file": "f", "sha256": "a" * 64,
                       "captured_at": "yesterday", "revision": None,
                       "content_scope": "rules"}])


def test_a_fact_citing_an_unknown_source_is_refused() -> None:
    """Referential integrity, the way concepts.py:162 enforces it. A citation
    that resolves to nothing is worse than no citation - it looks sourced."""
    with pytest.raises(ValueError, match="unknown source"):
        pack(facts=[{
            "id": "tier.2.unlock_wave", "value": 100, "unit": "wave",
            "source_refs": ["nonexistent"], "source_url": None,
            "definition_verified": True, "rule_verified": True,
            "validity": {"status": "unknown", "game_version_min": None,
                         "game_version_max": None},
        }])


def test_a_fact_with_no_citation_at_all_is_refused() -> None:
    with pytest.raises(ValueError):
        pack(facts=[{
            "id": "tier.2.unlock_wave", "value": 100, "unit": "wave",
            "source_refs": [], "source_url": None,
            "definition_verified": True, "rule_verified": True,
            "validity": {"status": "unknown", "game_version_min": None,
                         "game_version_max": None},
        }])


def test_duplicate_fact_ids_are_refused() -> None:
    one = pack().facts[0]
    with pytest.raises(ValueError, match="duplicate"):
        pack(facts=[_as_payload(one), _as_payload(one)])


@pytest.mark.parametrize("bad", ["Tier.2", "tier2", "tier..2", "tier.2.", "", "TIER.2.X"])
def test_a_fact_id_must_be_namespaced_lowercase(bad: str) -> None:
    """domain.slug, the same regex concepts.py:155 enforces."""
    with pytest.raises(ValueError, match="fact id"):
        payload = _as_payload(pack().facts[0])
        payload["id"] = bad
        pack(facts=[payload])


# -- validity ------------------------------------------------------------
def test_an_unknown_validity_must_carry_null_bounds() -> None:
    payload = _as_payload(pack().facts[0])
    payload["validity"] = {"status": "unknown", "game_version_min": "0.16",
                           "game_version_max": None}
    with pytest.raises(ValueError, match="validity"):
        pack(facts=[payload])


def test_a_known_validity_must_carry_a_bound() -> None:
    payload = _as_payload(pack().facts[0])
    payload["validity"] = {"status": "known", "game_version_min": None,
                           "game_version_max": None}
    with pytest.raises(ValueError, match="validity"):
        pack(facts=[payload])


def test_there_is_no_confidence_field_anywhere() -> None:
    """Deliberate: verification is two booleans plus validity. A numeric
    confidence on a game rule invites "0.7 is probably fine to spend on"."""
    payload = _as_payload(pack().facts[0])
    payload["confidence"] = .9
    with pytest.raises(ValueError):
        pack(facts=[payload])


# -- authorisation, the load-bearing rule --------------------------------
def test_a_verified_unconflicted_fact_authorises() -> None:
    assert pack().authorises("tier.2.unlock_wave") is True


def test_an_unverified_rule_does_not_authorise() -> None:
    """definition_verified says we inventoried the name. rule_verified says we
    checked the rule. Only the second may authorise a spend."""
    payload = _as_payload(pack().facts[0])
    payload["rule_verified"] = False
    assert pack(facts=[payload]).authorises("tier.2.unlock_wave") is False


def test_a_conflicted_fact_does_not_authorise_however_verified() -> None:
    """The whole reason the conflicts block exists. The wiki contradicts
    itself in fourteen documented places; a fact caught in one of them is not
    a fact yet."""
    loaded = pack(conflicts=[{
        "id": "uw-plus-cost", "tainted": ["tier.2.unlock_wave"],
        "statement": "UW+ costs are given as both escalating and a flat 975 stones",
        "sources": ["wiki-tiers"],
        "quote": "Do not hardcode either without current in-game confirmation.",
    }])
    assert loaded.authorises("tier.2.unlock_wave") is False
    (conflict,) = loaded.conflicts_for("tier.2.unlock_wave")
    assert "975" in conflict.statement
    assert conflict.quote


def test_an_absent_fact_does_not_authorise() -> None:
    assert pack().authorises("tier.99.unlock_wave") is False


def test_a_conflict_tainting_an_unknown_fact_is_refused() -> None:
    """A conflict that names nothing real cannot withhold anything. It belongs
    in knowledge/SOURCES.md prose, not in the machine-readable block."""
    with pytest.raises(ValueError, match="unknown"):
        pack(conflicts=[{
            "id": "c", "tainted": ["nope.nope"], "statement": "s",
            "sources": ["wiki-tiers"], "quote": "q"}])


def test_a_conflict_citing_an_unknown_source_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown source"):
        pack(conflicts=[{
            "id": "c", "tainted": ["tier.2.unlock_wave"], "statement": "s",
            "sources": ["nope"], "quote": "q"}])


# -- the committed pack --------------------------------------------------
def test_the_committed_pack_loads_at_import() -> None:
    """A malformed committed pack must fail the import, not degrade quietly."""
    assert knowledge.KNOWLEDGE.schema_version == 1
    assert knowledge.KNOWLEDGE.pack_version


def test_the_payload_handed_out_is_detached() -> None:
    """concepts.py:225 does the same: a consumer that could mutate the
    singleton would make every later reader's answer depend on call order."""
    first = knowledge.KNOWLEDGE.payload()
    first["facts"] = []
    assert knowledge.KNOWLEDGE.payload()["facts"]


# -- Correction A: JSON arrays freeze to tuples ---------------------------
def test_a_list_value_is_coerced_to_a_tuple() -> None:
    """json.loads hands back a list; a frozen Fact_ must not hold one. Mirrors
    what _strings() already does for concepts.py's known list fields."""
    payload = _as_payload(pack().facts[0])
    payload["value"] = ["labs.game-speed", "labs.coins-wave", "cards.attack-speed"]
    payload["unit"] = None
    loaded = pack(facts=[payload])
    fact = loaded.by_id("tier.2.unlock_wave")
    assert isinstance(fact.value, tuple)
    assert fact.value == ("labs.game-speed", "labs.coins-wave", "cards.attack-speed")


def test_a_nested_list_value_is_frozen_all_the_way_down() -> None:
    payload = _as_payload(pack().facts[0])
    payload["value"] = [[1, 2], [3, 4]]
    payload["unit"] = None
    loaded = pack(facts=[payload])
    fact = loaded.by_id("tier.2.unlock_wave")
    assert fact.value == ((1, 2), (3, 4))
    assert all(isinstance(row, tuple) for row in fact.value)


def test_a_scalar_value_is_left_alone() -> None:
    """The coercion only touches lists - a plain number must round-trip
    unchanged, the way tier.2.unlock_wave's 100 always has."""
    assert pack().by_id("tier.2.unlock_wave").value == 100


# -- Correction B (fix round 1): concept ids validate by shape, default-on ---
def test_a_shaped_value_validates_without_any_annotation() -> None:
    """No value_is_concept_ref anywhere in the payload. A string shaped like
    <known-id-prefix>.<slug> is checked against the catalog automatically."""
    payload = _as_payload(pack().facts[0])
    payload["value"] = "labs.game-speed"
    payload["unit"] = None
    loaded = pack(facts=[payload])
    assert loaded.by_id("tier.2.unlock_wave").value == "labs.game-speed"


def test_a_shaped_list_value_validates_without_any_annotation() -> None:
    payload = _as_payload(pack().facts[0])
    payload["value"] = ["labs.game-speed", "labs.coins-wave"]
    payload["unit"] = None
    loaded = pack(facts=[payload])
    assert loaded.by_id("tier.2.unlock_wave").value == ("labs.game-speed", "labs.coins-wave")


def test_a_shaped_value_fails_loud_on_a_bad_id_with_no_annotation() -> None:
    """The governing rule applied to identifiers: a typo'd id is an unknown
    fact wearing a known fact's clothes, and must fail loudly at import - not
    silently resolve to nothing forever. No opt-in required to catch this."""
    payload = _as_payload(pack().facts[0])
    payload["value"] = "labs.game_speed"  # underscore typo; the real id is hyphenated
    payload["unit"] = None
    with pytest.raises(ValueError, match="labs.game_speed") as excinfo:
        pack(facts=[payload])
    assert "tier.2.unlock_wave" in str(excinfo.value)


def test_a_typo_anywhere_in_a_shaped_list_is_refused_with_no_annotation() -> None:
    payload = _as_payload(pack().facts[0])
    payload["value"] = ["labs.game-speed", "cards.attac-speed"]  # missing a k
    payload["unit"] = None
    with pytest.raises(ValueError, match="cards.attac-speed"):
        pack(facts=[payload])


def test_an_opt_out_fact_skips_validation_even_with_a_bad_id() -> None:
    """value_is_concept_ref: false is the escape hatch for a value that
    merely happens to look like a concept id. Proven with a genuinely bad id,
    not a valid one, so the test can't pass by accident."""
    payload = _as_payload(pack().facts[0])
    payload["value"] = "labs.game_speed"  # same typo as above
    payload["unit"] = None
    payload["value_is_concept_ref"] = False
    loaded = pack(facts=[payload])
    assert loaded.by_id("tier.2.unlock_wave").value == "labs.game_speed"


def test_an_unshaped_value_is_left_alone() -> None:
    """"not" is not a known concept id prefix, so this value never looks
    like a concept reference in the first place - nothing to check, no
    annotation needed either way."""
    payload = _as_payload(pack().facts[0])
    payload["value"] = "not.a.real.concept.id"
    payload["unit"] = None
    loaded = pack(facts=[payload])
    assert loaded.by_id("tier.2.unlock_wave").value == "not.a.real.concept.id"


def test_a_bare_domain_name_with_no_slug_is_left_alone() -> None:
    """A string that merely equals a known domain, with no dot at all, does
    not match the domain.slug shape and is not sent through the catalog."""
    payload = _as_payload(pack().facts[0])
    payload["value"] = "labs"
    payload["unit"] = None
    loaded = pack(facts=[payload])
    assert loaded.by_id("tier.2.unlock_wave").value == "labs"


def test_only_concept_domain_not_glossary_domain_seeds_the_shape() -> None:
    """Important reviewer caveat: glossary domains are separately
    title-cased ("Labs & Progression") and must never seed the prefix set.
    A value shaped like a glossary domain must be left alone, not checked."""
    payload = _as_payload(pack().facts[0])
    payload["value"] = "Labs & Progression.something"
    payload["unit"] = None
    loaded = pack(facts=[payload])
    assert loaded.by_id("tier.2.unlock_wave").value == "Labs & Progression.something"


# -- fix round 2: the prefix set must come from id namespaces, not domain ----
def test_a_reference_prefixed_value_validates_without_any_annotation() -> None:
    """reference.* is a real, 92-member id namespace in the catalog whose
    Concept.domain field says something else entirely (currencies, combat,
    ...). Seeding the shape from Concept.domain left this whole family
    permanently unvalidated - the exact silent-False hazard this correction
    exists to close, one level down. reference.ehp is a real catalog id."""
    payload = _as_payload(pack().facts[0])
    payload["value"] = "reference.ehp"
    payload["unit"] = None
    loaded = pack(facts=[payload])
    assert loaded.by_id("tier.2.unlock_wave").value == "reference.ehp"


def test_a_typo_d_reference_prefixed_value_fails_loud_naming_the_id() -> None:
    payload = _as_payload(pack().facts[0])
    payload["value"] = "reference.ehpp"  # typo; the real id is reference.ehp
    payload["unit"] = None
    with pytest.raises(ValueError, match="reference.ehpp") as excinfo:
        pack(facts=[payload])
    assert "tier.2.unlock_wave" in str(excinfo.value)


# -- fix round 1: Pack.load must raise the module's own ValueError -----------
def test_pack_load_wraps_a_missing_schema_version_as_a_value_error(tmp_path) -> None:
    """knowledge.py:276-277 used to index payloads[0]["schema_version"] and
    ["pack_version"] directly, outside any try/except - a pack file missing
    one of those keys raised a bare KeyError instead of this module's own
    ValueError("malformed knowledge pack: ..."). Every other malformed-pack
    path in this module raises ValueError; Pack.load must not be the
    exception."""
    (tmp_path / "broken.v1.json").write_text(json.dumps({
        "pack_version": "x", "sources": [], "conflicts": [], "facts": [],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="schema version"):
        knowledge.Pack.load(tmp_path)


# -- fix round 1: the default-on validation, exercised through Pack.load -----
def test_pack_load_validates_an_unannotated_shaped_reference(tmp_path) -> None:
    """Pack.load is the module's real entry point; nothing above exercises
    it. This proves the default-on shape validation runs on the path real
    callers (KNOWLEDGE = Pack.load(KNOWLEDGE_DIR)) actually take, not only
    through the from_payload shortcut the rest of this file uses."""
    base = json.loads(json.dumps({
        "schema_version": 1, "pack_version": "2026-09-05.1",
        "sources": [{"id": "wiki-tiers", "file": "wiki__tier__tiers.txt",
                     "sha256": "a" * 64, "captured_at": "2026-09-05",
                     "revision": None, "content_scope": "rules"}],
        "conflicts": [],
        "facts": [{
            "id": "tier.2.unlock_wave", "value": "labs.game-speed", "unit": None,
            "source_refs": ["wiki-tiers"], "source_url": None,
            "definition_verified": True, "rule_verified": True,
            "validity": {"status": "unknown", "game_version_min": None,
                         "game_version_max": None},
        }],
    }))
    (tmp_path / "pack.v1.json").write_text(json.dumps(base), encoding="utf-8")
    loaded = knowledge.Pack.load(tmp_path)
    assert loaded.by_id("tier.2.unlock_wave").value == "labs.game-speed"

    base["facts"][0]["value"] = "labs.game_speed"  # typo
    (tmp_path / "pack.v1.json").write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(ValueError, match="labs.game_speed"):
        knowledge.Pack.load(tmp_path)


# -- the committed pack's content ----------------------------------------
def test_every_tier_on_the_route_has_an_unlock_wave_and_a_multiplier() -> None:
    for tier in (2, 3, 4):
        assert knowledge.KNOWLEDGE.by_id(f"tier.{tier}.unlock_wave") is not None
        assert knowledge.KNOWLEDGE.by_id(f"tier.{tier}.coin_multiplier") is not None


def test_the_tier_two_gate_is_wave_one_hundred() -> None:
    """The single most load-bearing number in the whole plan: the account's
    best is wave 16, and everything downstream is funded by this gate."""
    assert knowledge.KNOWLEDGE.by_id("tier.2.unlock_wave").value == 100


def test_the_milestone_ladder_covers_the_five_gates_on_the_route() -> None:
    for fact_id in ("milestone.t1.w30", "milestone.t1.w100",
                    "milestone.t2.w150", "milestone.t3.w150",
                    "milestone.t4.w70"):
        assert knowledge.KNOWLEDGE.by_id(fact_id) is not None


def test_every_priority_list_is_ordered_and_non_empty() -> None:
    for fact_id in ("priority.labs.t1", "priority.uw.unlock_order",
                    "priority.gems.spend_order", "priority.cards.unlock_order"):
        fact = knowledge.KNOWLEDGE.by_id(fact_id)
        assert fact is not None
        assert isinstance(fact.value, tuple) and fact.value


def test_the_first_ultimate_weapon_is_golden_tower() -> None:
    """Every source agrees on this one without qualification - which is why
    it is the plan's single pre-approved one-way decision."""
    assert knowledge.KNOWLEDGE.by_id("priority.uw.unlock_order").value[0] == "golden_tower"


def test_the_black_hole_damage_lab_is_a_recorded_hazard() -> None:
    """It cannot be unresearched. Four of this pack's five hazards come from
    the wiki's Footguns page; this one is instead the Tier-Specific Guide's
    own "Pitfall: Black Hole Damage lab" section - Footguns never mentions
    this lab at all. This is the hazard that costs months."""
    hazard = knowledge.KNOWLEDGE.by_id("hazard.lab.black_hole_damage")
    assert hazard is not None
    assert hazard.source_url


def test_every_hazard_names_an_observable_safety_precondition() -> None:
    """A pre-approval is necessary but not sufficient. A hazard whose
    precondition cannot be READ must veto even a pre-approved action - so
    every hazard has to say what would have to be observed."""
    hazards = [f for f in knowledge.KNOWLEDGE.facts if f.id.startswith("hazard.")]
    assert hazards
    for hazard in hazards:
        assert isinstance(hazard.value, dict)
        assert hazard.value.get("requires_observable")


def test_the_documented_wiki_conflicts_are_all_recorded() -> None:
    """Fourteen, per the research branch's own audit. A conflict the pack does
    not know about is a fact it will wrongly authorise."""
    assert len(knowledge.KNOWLEDGE.conflicts) == EXPECTED_CONFLICTS


def test_no_fact_tainted_by_a_conflict_authorises_anything() -> None:
    tainted = {t for c in knowledge.KNOWLEDGE.conflicts for t in c.tainted}
    assert tainted, "the conflict block taints nothing - check task 1 step 4"
    for fact_id in tainted:
        assert knowledge.KNOWLEDGE.authorises(fact_id) is False


def test_every_source_file_named_by_the_pack_exists_in_the_corpus() -> None:
    """Skipped when the corpus is absent: it is deliberately untracked, so a
    fresh clone has the pack but not the 1.2MB of prose behind it."""
    corpus = Path("research/wiki-source-2026-09-05")
    if not corpus.is_dir():
        pytest.skip("wiki corpus not extracted; see knowledge/SOURCES.md")
    for source in knowledge.KNOWLEDGE.sources:
        assert (corpus / source.file).is_file(), source.file


def test_every_source_digest_matches_the_corpus() -> None:
    """The reason the digest is in the pack at all: a source page that changed
    under us must invalidate the facts citing it, loudly."""
    import hashlib

    corpus = Path("research/wiki-source-2026-09-05")
    if not corpus.is_dir():
        pytest.skip("wiki corpus not extracted; see knowledge/SOURCES.md")
    for source in knowledge.KNOWLEDGE.sources:
        actual = hashlib.sha256((corpus / source.file).read_bytes()).hexdigest()
        assert actual == source.sha256, source.file
