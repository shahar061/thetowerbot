from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

import upgrades


def registry_module():
    import concepts
    return concepts


def test_registry_preserves_legacy_execution_and_covers_source_snapshots() -> None:
    concepts = registry_module()
    registry = concepts.REGISTRY
    assert len(upgrades.CATALOG) == 60
    assert sum(bool(c.execution_scopes) for c in registry.concepts) == 60
    imported = [c for c in registry.concepts if "wiki-inventory" in c.source_refs]
    assert len(imported) == 516
    assert all(not c.execution_scopes and c.legacy_id is None for c in imported)
    assert len(registry.glossary) == len({g.id for g in registry.glossary}) == 159
    assert all(registry.by_id(g.concept_id) is not None for g in registry.glossary)
    for upgrade in upgrades.CATALOG:
        concept = registry.by_id(upgrade.concept_id)
        assert concept.legacy_id == upgrade.id
        assert concept.execution_scopes == ("battle", "workshop")
        assert upgrades.resolve(upgrade.name, upgrade.category) == upgrade
    for concept in registry.concepts:
        assert concept.definition_verified is False
        assert concept.rule_verified is False
        assert concept.caps is None and concept.prerequisites is None
        assert concept.validity.status == "unknown"
        assert concept.validity.game_version_min is None
        assert concept.validity.game_version_max is None
    sources = registry.payload()["sources"]
    assert {s["id"] for s in sources} == {"wiki-inventory", "wiki-glossary", "legacy-upgrades"}
    assert all(len(s["sha256"]) == 64 for s in sources)


def test_resolution_is_unique_or_none_and_domain_scoped() -> None:
    registry = registry_module().REGISTRY
    assert registry.resolve("Damage") is None
    assert registry.resolve("Damage", domain="stats").concept_id == "stats.damage"
    assert registry.resolve("Damage", domain="cards").concept_id == "cards.damage"
    assert registry.resolve("Coins") is None
    assert registry.resolve("Coins", domain="currencies").concept_id == "currencies.coins"
    assert registry.resolve("Coins", kind="card").concept_id == "cards.coins"
    assert registry.resolve("nonsense OCR") is None
    assert registry.resolve("GT", domain="ultimate-weapons").name == "Golden Tower"
    assert upgrades.resolve("Golden Tower") is None
    coins = next(g for g in registry.glossary if g.id == "coins")
    assert coins.concept_id == "currencies.coins"


def test_labels_are_clean_but_source_units_and_aliases_are_retained() -> None:
    registry = registry_module().REGISTRY
    concept = registry.by_id("core-substats.chrono-field-duration-s")
    assert concept.name == "Chrono Field - Duration"
    assert concept.unit == "s"
    assert "Chrono Field - Duration [s]*" in concept.aliases


def test_uw_and_uw_plus_remain_distinct_semantic_aliases() -> None:
    registry = registry_module().REGISTRY
    assert registry.resolve("UW").concept_id == "reference.uw"
    assert registry.resolve("Ultimate Weapon").concept_id == "reference.uw"
    assert registry.resolve("UW+").concept_id == "reference.uw-plus"
    assert registry.resolve("CC").concept_id == "reference.cc"
    assert registry.resolve("GC").concept_id == "strategy.gc"


def test_registry_is_immutable_and_payload_is_detached() -> None:
    registry = registry_module().REGISTRY
    with pytest.raises(FrozenInstanceError):
        registry.concepts[0].name = "changed"
    payload = registry.payload()
    payload["concepts"][0]["aliases"].append("mutated")
    assert "mutated" not in registry.concepts[0].aliases


@pytest.mark.parametrize("corruption", [
    "duplicate_id", "duplicate_legacy", "bad_source", "bad_glossary", "duplicate_glossary",
    "bad_validity", "known_version", "verified_rule", "reference_execution", "bad_unlock",
])
def test_invalid_registry_is_rejected(corruption: str) -> None:
    concepts = registry_module()
    raw = json.loads(concepts.REGISTRY_PATH.read_text())
    record = raw["concepts"][0]
    if corruption == "duplicate_id":
        raw["concepts"].append(record.copy())
    elif corruption == "duplicate_legacy":
        raw["concepts"][1]["legacy_id"] = record["legacy_id"]
    elif corruption == "bad_source":
        record["source_refs"] = ["absent"]
    elif corruption == "bad_glossary":
        raw["glossary"][0]["concept_id"] = "missing.concept"
    elif corruption == "duplicate_glossary":
        raw["glossary"].append(raw["glossary"][0].copy())
    elif corruption == "bad_validity":
        record["validity"]["status"] = "probably"
    elif corruption == "known_version":
        record["validity"]["game_version_min"] = "1.0"
    elif corruption == "verified_rule":
        record["rule_verified"] = "true"
    elif corruption == "reference_execution":
        next(c for c in raw["concepts"] if c["legacy_id"] is None)["execution_scopes"] = ["workshop"]
    elif corruption == "bad_unlock":
        record["unlocks"] = ["missing.concept"]
    with pytest.raises(ValueError):
        concepts.Registry.from_payload(raw)


def test_legacy_resolver_refuses_distinct_candidates_but_deduplicates_aliases(monkeypatch) -> None:
    damage, speed = upgrades.CATALOG[:2]
    monkeypatch.setitem(upgrades._BY_NAME, "collision", (damage, damage, speed))
    monkeypatch.setitem(upgrades._BY_NAME, "duplicate", (damage, damage))
    assert upgrades.resolve("collision", "ATTACK") is None
    assert upgrades.resolve("duplicate", "ATTACK") == damage


def test_future_versioned_metadata_is_validated_and_resolves_only_in_range() -> None:
    concepts = registry_module()
    raw = concepts.REGISTRY.payload()
    card = next(c for c in raw["concepts"] if c["concept_id"] == "cards.damage")
    card.update(validity={"status": "known", "game_version_min": "1.2", "game_version_max": "2.0"},
                caps={"max_level": 7, "max_value": None}, prerequisites=["currencies.coins"],
                definition_verified=True, rule_verified=True)
    registry = concepts.Registry.from_payload(raw)
    assert registry.resolve("Damage", domain="cards", game_version="1.2.0").concept_id == "cards.damage"
    assert registry.resolve("Damage", domain="cards", game_version="1.10").concept_id == "cards.damage"
    assert registry.resolve("Damage", domain="cards", game_version="2.0").concept_id == "cards.damage"
    assert registry.resolve("Damage", domain="cards", game_version="2.1") is None
    assert registry.resolve("Damage", domain="cards", game_version="1.1") is None
    assert registry.resolve("Damage", domain="stats", game_version="1.5") is None
    assert registry.by_id("cards.damage").execution_scopes == ()
    with pytest.raises(ValueError):
        registry.resolve("Damage", game_version="unreadable")


@pytest.mark.parametrize("field,value", [
    ("validity", {"status": "known", "game_version_min": "2.0", "game_version_max": "1.0"}),
    ("validity", {"status": "known", "game_version_min": "unknown", "game_version_max": None}),
    ("caps", {"max_level": -1, "max_value": None}),
    ("prerequisites", ["absent.concept"]),
])
def test_future_metadata_rejects_inconsistent_bounds_and_references(field: str, value: object) -> None:
    concepts = registry_module()
    raw = concepts.REGISTRY.payload()
    raw["concepts"][0][field] = value
    with pytest.raises(ValueError):
        concepts.Registry.from_payload(raw)


def test_resolves_the_unlock_spellings_the_workshop_actually_prints() -> None:
    """The tab names every unlock "Unlock <thing> Upgrades"; the catalog does not.

    These three spellings were read off live Workshop frames while the bot
    reported no_match against the catalog names, so a rule naming the row on
    offer could never have been executed.
    """
    assert upgrades.resolve("Unlock Thorn Upgrades", "DEFENSE").id == "unlock_thorns"
    assert upgrades.resolve("Unlock Multishot Upgrades", "ATTACK").id == "unlock_multishot"
    assert upgrades.resolve("Unlock Upgrade Chances", "UTILITY").id == "unlock_free_upgrades"
    # Read off live Defense frames; the bot sat for hours unable to price it.
    assert upgrades.resolve("Unlock Lifesteal Upgrades", "DEFENSE").id == "unlock_lifesteal"
    assert upgrades.resolve("Unlock Knockback Upgrades", "DEFENSE").id == "unlock_knockback"
    assert upgrades.resolve("Unlock Orbs Upgrades", "DEFENSE").id == "unlock_orbs"
    assert upgrades.resolve("Unlock Rapid Fire Upgrades", "ATTACK").id == "unlock_rapid_fire"


def test_the_range_row_the_workshop_prints_resolves_to_the_range_identity() -> None:
    """"Attack Range" was read off the ATTACK tab and matched nothing, so the
    row Unlock Range Upgrades reveals could not be named by a shopping rule."""
    assert upgrades.resolve("Attack Range", "ATTACK").id == "range"
