"""Lab and gem price facts from catalog/labs.v1.json, validated at import.

Only Game Speed has a price table. Every other lab has `levels: null`, which
means its price is unknown - never zero. A bad file stops the import, the same
way the Workshop price catalog does.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import concepts

CATALOG_PATH = Path(__file__).resolve().parent / "catalog" / "labs.v1.json"
GAME_SPEED = "labs.game-speed"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class LabLevel:
    level: int
    coins: int
    seconds: int
    max_speed: float


@dataclass(frozen=True)
class CatalogLab:
    id: str
    name: str
    unlock: Mapping[str, int] | None
    max_level: int | None
    levels: tuple[LabLevel, ...] | None
    source_url: str
    checked: str


@dataclass(frozen=True)
class GemPrice:
    slot: int
    gems: int
    source_url: str
    checked: str


@dataclass(frozen=True)
class LabCatalog:
    labs: tuple[CatalogLab, ...]
    lab_slots: tuple[GemPrice, ...]
    card_slots: tuple[GemPrice, ...]
    card_gems: int
    card_gems_source: tuple[str, str]
    labs_unlock_wave: int
    labs_unlock_source: tuple[str, str]


def _provenance(raw: Mapping[str, Any], where: str) -> tuple[str, str]:
    url, checked = raw.get("source_url"), raw.get("checked")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(f"{where}: source_url must be an https URL")
    if not isinstance(checked, str) or _DATE.fullmatch(checked) is None:
        raise ValueError(f"{where}: checked must be a YYYY-MM-DD date")
    return url, checked


def _positive(value: object, where: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{where} must be a positive integer")
    return value


def _gem_prices(raw: object, slots: range, where: str) -> tuple[GemPrice, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{where} must be a list")
    prices: list[GemPrice] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"{where} entries must be objects")
        url, checked = _provenance(entry, where)
        prices.append(GemPrice(_positive(entry.get("slot"), f"{where} slot"),
                               _positive(entry.get("gems"), f"{where} gems"), url, checked))
    if [price.slot for price in prices] != list(slots):
        raise ValueError(f"{where} must list slots {slots.start} to {slots.stop - 1} in order")
    return tuple(prices)


def _lab(raw: object) -> CatalogLab:
    if not isinstance(raw, dict):
        raise ValueError("lab entries must be objects")
    lab_id, name = raw.get("id"), raw.get("name")
    if (not isinstance(lab_id, str) or not lab_id.startswith("labs.")
            or concepts.REGISTRY.by_id(lab_id) is None):
        raise ValueError(f"unknown lab concept id: {lab_id!r}")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{lab_id}: name is required")
    url, checked = _provenance(raw, lab_id)
    unlock = raw.get("unlock")
    if unlock is not None:
        if not isinstance(unlock, dict) or set(unlock) != {"best_tier_1_wave"}:
            raise ValueError(f"{lab_id}: unlock must be {{best_tier_1_wave: int}} or null")
        _positive(unlock["best_tier_1_wave"], f"{lab_id} unlock wave")
    levels_raw = raw.get("levels")
    if levels_raw is None:
        return CatalogLab(lab_id, name, unlock, None, None, url, checked)
    max_level = _positive(raw.get("max_level"), f"{lab_id} max_level")
    if not isinstance(levels_raw, list) or len(levels_raw) != max_level:
        raise ValueError(f"{lab_id}: levels must list every level up to max_level")
    levels: list[LabLevel] = []
    for number, entry in enumerate(levels_raw, start=1):
        if not isinstance(entry, dict) or entry.get("level") != number:
            raise ValueError(f"{lab_id}: levels must run 1 to max_level in order")
        speed = entry.get("max_speed")
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed <= 0:
            raise ValueError(f"{lab_id}: max_speed must be a positive number")
        levels.append(LabLevel(number, _positive(entry.get("coins"), f"{lab_id} coins"),
                               _positive(entry.get("seconds"), f"{lab_id} seconds"),
                               float(speed)))
    if any(later.coins <= earlier.coins or later.max_speed <= earlier.max_speed
           for earlier, later in zip(levels, levels[1:])):
        raise ValueError(f"{lab_id}: level prices and speeds must rise")
    return CatalogLab(lab_id, name, unlock, max_level, tuple(levels), url, checked)


def load(payload: object) -> LabCatalog:
    """Validate a catalog payload; any problem raises ValueError."""
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("unsupported lab catalog version")
    labs_raw = payload.get("labs")
    if not isinstance(labs_raw, list):
        raise ValueError("labs must be a list")
    labs = tuple(_lab(item) for item in labs_raw)
    ids = [entry.id for entry in labs]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate lab id")
    game_speed = next((entry for entry in labs if entry.id == GAME_SPEED), None)
    if game_speed is None or game_speed.levels is None:
        raise ValueError("Game Speed needs its price table")
    unlock, card = payload.get("labs_unlock"), payload.get("card_gems")
    if not isinstance(unlock, dict) or not isinstance(card, dict):
        raise ValueError("labs_unlock and card_gems must be objects")
    return LabCatalog(
        labs,
        _gem_prices(payload.get("lab_slots"), range(2, 6), "lab_slots"),
        _gem_prices(payload.get("card_slots"), range(2, 11), "card_slots"),
        _positive(card.get("gems"), "card_gems"), _provenance(card, "card_gems"),
        _positive(unlock.get("best_tier_1_wave"), "labs_unlock wave"),
        _provenance(unlock, "labs_unlock"))


CATALOG = load(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
_BY_ID = {entry.id: entry for entry in CATALOG.labs}


def lab(lab_id: str) -> CatalogLab | None:
    return _BY_ID.get(lab_id)


def lab_ids() -> tuple[str, ...]:
    return tuple(_BY_ID)


def level(lab_id: str, number: int) -> LabLevel | None:
    entry = _BY_ID.get(lab_id)
    if entry is None or entry.levels is None or type(number) is not int:
        return None
    return entry.levels[number - 1] if 1 <= number <= len(entry.levels) else None


def lab_slot_gems(slot: int) -> int | None:
    return next((price.gems for price in CATALOG.lab_slots if price.slot == slot), None)


def card_slot_gems(slot: int) -> int | None:
    return next((price.gems for price in CATALOG.card_slots if price.slot == slot), None)


def reference() -> dict[str, Any]:
    """Everything the Labs & Gems page shows as reference, with sources."""
    game_speed = _BY_ID[GAME_SPEED]
    assert game_speed.levels is not None
    sources = {(entry.source_url, entry.checked) for entry in CATALOG.labs}
    sources |= {(price.source_url, price.checked) for price in (*CATALOG.lab_slots, *CATALOG.card_slots)}
    sources |= {CATALOG.card_gems_source, CATALOG.labs_unlock_source}
    return {
        "labs": [{"id": entry.id, "name": entry.name, "max_level": entry.max_level,
                  "priced": entry.levels is not None} for entry in CATALOG.labs],
        "game_speed": [asdict(row) for row in game_speed.levels],
        "lab_slots": [{"slot": price.slot, "gems": price.gems} for price in CATALOG.lab_slots],
        "card_slots": [{"slot": price.slot, "gems": price.gems} for price in CATALOG.card_slots],
        "card_gems": CATALOG.card_gems,
        "labs_unlock_wave": CATALOG.labs_unlock_wave,
        "sources": [{"url": url, "checked": checked} for url, checked in sorted(sources)],
    }
