"""Card identities from the committed registry, with separately sourced limits.

The registry inventories identities but has no numeric card maxima. The linked
Cards page documents seven levels for ordinary cards and twenty-one slots
purchasable with gems. These limits do not establish UI controls or prices.
"""

from __future__ import annotations

from concepts import REGISTRY

LEVEL_SOURCE_URL = "https://the-tower-idle-tower-defense.game-vault.net/wiki/Cards"
SLOT_SOURCE_URL = LEVEL_SOURCE_URL
NORMAL_CARD_MAX_LEVEL = 7
MAX_GEM_CARD_SLOTS = 21


def card_ids() -> frozenset[str]:
    return frozenset(concept.concept_id for concept in REGISTRY.concepts
                     if concept.kind == "card")


def max_level(card_id: str) -> int | None:
    """Unknown identities and nonstandard card types have no inferred cap."""
    entry = REGISTRY.by_id(card_id)
    if entry is None or entry.kind != "card":
        return None
    if entry.caps is not None and entry.caps.max_level is not None:
        return entry.caps.max_level
    return NORMAL_CARD_MAX_LEVEL


def max_card_slots() -> int:
    """Gem-purchasable slot-goal maximum, not total observed equipment capacity."""
    return MAX_GEM_CARD_SLOTS


def resolve_legacy_name(name: str) -> str | None:
    """Only an unambiguous registry name, alias, or exact ID may migrate."""
    entry = REGISTRY.resolve(name, kind="card")
    return entry.concept_id if entry is not None else None
