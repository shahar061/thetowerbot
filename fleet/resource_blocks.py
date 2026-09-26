"""Labs and Gems lane programs: validation, legacy translation and a pure plan.

Kept apart from strategy_blocks.py, which is Workshop/Battle purchase logic.
The limits are shared so both editors behave the same. Only the blocks in
AUTOMATED are executed by a worker; everything else is planned and shown.
"""

from __future__ import annotations

import math
import operator
from typing import Any, Iterator, Mapping, Sequence

import lab_catalog
from fleet.strategy_blocks import COMPARISONS as _PURCHASE_COMPARISONS, MAX_BLOCKS, MAX_DEPTH

GAME_SPEED = lab_catalog.GAME_SPEED
LAB_SLOTS = (1, 2, 3, 4, 5)
COMPARISONS = {**_PURCHASE_COMPARISONS, "eq": operator.eq}
MAX_POOL_SECONDS = 30 * 86400
# A legacy "slot2_research" step named no lab. It becomes a short economy pool.
LEGACY_SLOT2_POOL = ("labs.coins-wave", "labs.cash-bonus", "labs.coins-kill-bonus")

# The only blocks a worker executes, in one place so no screen invents its own:
# (lane, block type, lab id or "", slot).
AUTOMATED: frozenset[tuple[str, str, str, int]] = frozenset({
    ("labs", "research", GAME_SPEED, 1),
    ("gems", "unlock_lab_slot", "", 2),
})


def automated_list() -> list[dict[str, Any]]:
    return [{"lane": lane, "type": kind, **({"lab_id": lab_id} if lab_id else {}), "slot": slot}
            for lane, kind, lab_id, slot in sorted(AUTOMATED)]


def research_automated(lab_id: str, slot: int) -> bool:
    return ("labs", "research", lab_id, slot) in AUTOMATED


def gem_automated(block: Mapping[str, Any]) -> bool:
    return (block.get("type") == "unlock_lab_slot"
            and ("gems", "unlock_lab_slot", "", block.get("slot")) in AUTOMATED)


class _Ids:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    def claim(self, block: Mapping[str, Any]) -> None:
        identity = block.get("id")
        if (not isinstance(identity, str) or not identity or len(identity) > 100
                or any(char.isspace() for char in identity) or identity in self.seen):
            raise ValueError("resource block IDs must be unique nonempty tokens")
        self.seen.add(identity)
        if len(self.seen) > MAX_BLOCKS:
            raise ValueError("too many resource blocks")
        if "label" in block:
            label = block["label"]
            if not isinstance(label, str) or not label.strip() or len(label) > 60:
                raise ValueError("block label must be 1 to 60 characters")


def _int(raw: object, name: str, low: int, high: int) -> int:
    if type(raw) is not int or not low <= raw <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return raw


def _lab_id(raw: object) -> str:
    if not isinstance(raw, str) or lab_catalog.lab(raw) is None:
        raise ValueError("unknown lab id")
    return raw


def _max_level(lab_id: str) -> int:
    entry = lab_catalog.lab(lab_id)
    return entry.max_level if entry is not None and entry.max_level is not None else 10_000


def _fields(block: Mapping[str, Any], allowed: set[str]) -> None:
    if set(block) - allowed - {"id", "type", "label"}:
        raise ValueError("unknown resource block field")


def _lab_children(items: object, ids: _Ids, depth: int) -> tuple[dict[str, Any], ...]:
    if depth > MAX_DEPTH or not isinstance(items, (list, tuple)):
        raise ValueError("lab blocks must be a bounded list")
    result: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            raise ValueError("lab block must be an object")
        block = dict(raw)
        ids.claim(block)
        kind = block.get("type")
        if kind == "research":
            _fields(block, {"lab_id", "to_level"})
            lab_id = _lab_id(block.get("lab_id"))
            _int(block.get("to_level"), "to_level", 1, _max_level(lab_id))
        elif kind == "lab_pool":
            _fields(block, {"lab_ids", "selection", "max_seconds", "max_price_pct_of_wallet", "caps"})
            lab_ids = block.get("lab_ids")
            if not isinstance(lab_ids, (list, tuple)) or not 1 <= len(lab_ids) <= 20:
                raise ValueError("lab pool needs 1 to 20 labs")
            block["lab_ids"] = [_lab_id(item) for item in lab_ids]
            if len(set(block["lab_ids"])) != len(block["lab_ids"]):
                raise ValueError("duplicate lab in pool")
            if "selection" in block and block["selection"] not in {"ordered", "cheapest"}:
                raise ValueError("unknown lab pool selection")
            if "max_seconds" in block:
                _int(block["max_seconds"], "max_seconds", 60, MAX_POOL_SECONDS)
            if "max_price_pct_of_wallet" in block:
                _int(block["max_price_pct_of_wallet"], "max_price_pct_of_wallet", 1, 100)
            caps = block.get("caps", {})
            if not isinstance(caps, Mapping) or set(caps) - set(block["lab_ids"]):
                raise ValueError("lab pool caps must name pool labs")
            for lab_id, cap in caps.items():
                _int(cap, "lab cap", 1, _max_level(lab_id))
            if "caps" in block:
                block["caps"] = dict(caps)
        elif kind == "condition":
            _fields(block, {"field", "cmp", "value", "lab_id", "then", "else"})
            field = block.get("field")
            if field not in {"best_tier_1_wave", "game_speed_maxed", "lab_level"}:
                raise ValueError("unknown lab condition fact")
            if field == "lab_level":
                _lab_id(block.get("lab_id"))
            elif "lab_id" in block:
                raise ValueError("only lab level conditions name a lab")
            if block.get("cmp") not in COMPARISONS:
                raise ValueError("unknown condition comparison")
            value = block.get("value")
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 1_000_000_000_000):
                raise ValueError("condition value must be a number between 0 and 1000000000000")
            if field == "game_speed_maxed" and value not in (0, 1):
                raise ValueError("game_speed_maxed compares with 0 or 1")
            block["then"] = list(_lab_children(block.get("then", []), ids, depth + 1))
            block["else"] = list(_lab_children(block.get("else", []), ids, depth + 1))
        elif kind == "wait":
            _fields(block, set())
        else:
            raise ValueError("unknown lab block type")
        result.append(block)
    return tuple(result)


def validate_labs(value: object) -> tuple[dict[str, Any], ...]:
    """Top level is slot tracks; each slot belongs to at most one track."""
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("labs program needs at least one slot track")
    ids = _Ids()
    owners: dict[int, str] = {}
    tracks: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or raw.get("type") != "slot_track":
            raise ValueError("the labs lane holds slot tracks")
        block = dict(raw)
        ids.claim(block)
        _fields(block, {"slots", "children"})
        slots = block.get("slots")
        if (not isinstance(slots, (list, tuple)) or not slots
                or any(type(slot) is not int or slot not in LAB_SLOTS for slot in slots)
                or len(set(slots)) != len(slots)):
            raise ValueError("slot track slots must be distinct lab slots 1 to 5")
        for slot in slots:
            if slot in owners:
                raise ValueError(f"lab slot {slot} belongs to two tracks")
            owners[slot] = block["id"]
        block["slots"] = sorted(slots)
        block["children"] = list(_lab_children(block.get("children", []), ids, 1))
        tracks.append(block)
    first = next((track for track in tracks if 1 in track["slots"]), None)
    if (first is None or not first["children"] or first["children"][0].get("type") != "research"
            or first["children"][0].get("lab_id") != GAME_SPEED):
        raise ValueError("the slot 1 track must start with Game Speed research")
    return tuple(tracks)


def validate_gems(value: object) -> tuple[dict[str, Any], ...]:
    """Flat, top-to-bottom. Lab slots unlock in order, starting with slot 2."""
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("gems program needs at least one block")
    ids = _Ids()
    result: list[dict[str, Any]] = []
    last_slot = 1
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("gem block must be an object")
        block = dict(raw)
        ids.claim(block)
        kind = block.get("type")
        if kind == "unlock_lab_slot":
            _fields(block, {"slot"})
            slot = _int(block.get("slot"), "lab slot", 2, 5)
            if slot <= last_slot:
                raise ValueError("lab slots unlock in order")
            last_slot = slot
        elif kind == "card_slots":
            _fields(block, {"up_to", "when_usable_card"})
            _int(block.get("up_to"), "card slots up_to", 2, 10)
            if type(block.get("when_usable_card", False)) is not bool:
                raise ValueError("when_usable_card must be boolean")
            block.setdefault("when_usable_card", False)
        elif kind == "buy_cards":
            _fields(block, {"purpose", "cards"})
            if block.get("purpose") == "card_missions":
                if "cards" in block:
                    raise ValueError("card missions do not name cards")
            elif block.get("purpose") == "until_cards":
                cards = block.get("cards")
                if (not isinstance(cards, (list, tuple)) or not 1 <= len(cards) <= 20
                        or any(not isinstance(card, str) or not card.strip() or len(card) > 60
                               for card in cards)):
                    raise ValueError("until_cards needs 1 to 20 card names")
                block["cards"] = list(cards)
            else:
                raise ValueError("unknown card purpose")
        elif kind == "save_for":
            _fields(block, {"target"})
            if block.get("target") != "modules":
                raise ValueError("gems can only be saved for modules")
        elif kind == "wait":
            _fields(block, set())
        else:
            raise ValueError("unknown gem block type")
        result.append(block)
    if result[0].get("type") != "unlock_lab_slot" or result[0].get("slot") != 2:
        raise ValueError("the gem path must start by unlocking lab slot 2")
    return tuple(result)


def walk_labs(blocks: Sequence[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    for block in blocks:
        yield block
        for key in ("children", "then", "else"):
            yield from walk_labs(block.get(key, ()))


def check_pool_limits(blocks: Sequence[Mapping[str, Any]], *, max_seconds: int | None,
                      max_price_pct: int | None) -> None:
    """A lab pool may tighten the strategy's pool rule, never loosen it."""
    for block in walk_labs(blocks):
        if block.get("type") != "lab_pool":
            continue
        if max_seconds is not None and block.get("max_seconds", max_seconds) > max_seconds:
            raise ValueError("lab pool max_seconds is looser than the strategy rule")
        if (max_price_pct is not None
                and block.get("max_price_pct_of_wallet", max_price_pct) > max_price_pct):
            raise ValueError("lab pool max_price_pct_of_wallet is looser than the strategy rule")


def legacy_gem_blocks(steps: Sequence[str]) -> tuple[dict[str, Any], ...]:
    """Steps-mode gems as blocks. Lab slots keep their positions, bought in order."""
    slots = iter(sorted(int(step.rsplit("_", 1)[1]) for step in steps
                        if step.startswith("unlock_lab_slot_")))
    blocks: list[dict[str, Any]] = []
    for step in steps:
        identity = f"legacy.gems.{step}"
        if step.startswith("unlock_lab_slot_"):
            blocks.append({"id": identity, "type": "unlock_lab_slot", "slot": next(slots)})
        elif step == "card_slot":
            blocks.append({"id": identity, "type": "card_slots", "up_to": 2, "when_usable_card": False})
        elif step == "cards":
            blocks.append({"id": identity, "type": "buy_cards", "purpose": "card_missions"})
    return validate_gems(blocks)


def legacy_lab_blocks(steps: Sequence[str]) -> tuple[dict[str, Any], ...]:
    tracks: list[dict[str, Any]] = [
        {"id": "legacy.labs.slot1", "type": "slot_track", "slots": [1], "children": [
            {"id": "legacy.labs.game_speed", "type": "research", "lab_id": GAME_SPEED,
             "to_level": _max_level(GAME_SPEED)}]}]
    if "slot2_research" in steps:
        tracks.append({"id": "legacy.labs.slot2", "type": "slot_track", "slots": [2], "children": [
            {"id": "legacy.labs.slot2.pool", "type": "lab_pool",
             "lab_ids": list(LEGACY_SLOT2_POOL), "selection": "cheapest"}]})
    return validate_labs(tracks)


def template_gem_blocks() -> tuple[dict[str, Any], ...]:
    """The community gem path: all five lab slots first, then cards, then modules."""
    return validate_gems([
        {"id": "gems.lab2", "type": "unlock_lab_slot", "slot": 2, "label": "Lab slot 2"},
        {"id": "gems.lab3", "type": "unlock_lab_slot", "slot": 3, "label": "Lab slot 3"},
        {"id": "gems.lab4", "type": "unlock_lab_slot", "slot": 4, "label": "Lab slot 4"},
        {"id": "gems.lab5", "type": "unlock_lab_slot", "slot": 5, "label": "Lab slot 5"},
        {"id": "gems.card_slots", "type": "card_slots", "up_to": 10, "when_usable_card": True,
         "label": "Card slots to 10 when a usable card waits"},
        {"id": "gems.card_missions", "type": "buy_cards", "purpose": "card_missions",
         "label": "Cards for card-buy missions"},
        {"id": "gems.modules", "type": "save_for", "target": "modules", "label": "Save for modules"},
    ])


def template_lab_blocks() -> tuple[dict[str, Any], ...]:
    """The community lab path by slot; open disagreements are in the labels."""
    return validate_labs([
        {"id": "labs.slot1", "type": "slot_track", "slots": [1],
         "label": "Slot 1 · Game Speed, then Attack Speed", "children": [
             {"id": "labs.slot1.game_speed", "type": "research", "lab_id": GAME_SPEED,
              "to_level": 7, "label": "Game Speed to max"},
             {"id": "labs.slot1.attack_speed", "type": "research", "lab_id": "labs.attack-speed",
              "to_level": 50, "label": "Attack Speed to 50 · some pick Coins / Kill"}]},
        {"id": "labs.slot2", "type": "slot_track", "slots": [2],
         "label": "Slot 2 · short labs, then Labs Speed", "children": [
             {"id": "labs.slot2.after_game_speed", "type": "condition", "field": "game_speed_maxed",
              "cmp": "eq", "value": 1, "label": "Once Game Speed is maxed",
              "then": [{"id": "labs.slot2.labs_speed", "type": "research", "lab_id": "labs.labs-speed",
                        "to_level": 30, "label": "Labs Speed to 30 · Discord splits 30 vs 50"}],
              "else": [{"id": "labs.slot2.short", "type": "lab_pool", "selection": "cheapest",
                        "max_seconds": 1800, "label": "Short labs",
                        "lab_ids": ["labs.coins-wave", "labs.cash-bonus", "labs.coins-kill-bonus",
                                    "labs.buy-multiplier"]}]}]},
        {"id": "labs.economy", "type": "slot_track", "slots": [3, 4],
         "label": "Slots 3–4 · economy, skip Starting Cash", "children": [
             {"id": "labs.economy.coins_wave", "type": "research", "lab_id": "labs.coins-wave",
              "to_level": 20, "label": "Coins / Wave to about 20"},
             {"id": "labs.economy.then", "type": "lab_pool", "selection": "ordered",
              "lab_ids": ["labs.cash-bonus", "labs.coins-kill-bonus"],
              "label": "Then Cash Bonus, then Coins / Kill"}]},
        {"id": "labs.flex", "type": "slot_track", "slots": [5], "label": "Slot 5 · flex", "children": [
            {"id": "labs.flex.pool", "type": "lab_pool", "selection": "cheapest",
             "lab_ids": ["labs.buy-multiplier", "labs.workshop-attack-discount",
                         "labs.workshop-defense-discount", "labs.workshop-utility-discount"],
             "label": "Buy Multiplier, Workshop discounts"}]},
    ])


def template_rules() -> dict[str, Any]:
    """Save 25% toward each Game Speed level instead of pausing Workshop for 1M."""
    return {
        "coins": {"lab_share": {"mode": "save_pct", "pct": 25}, "workshop_spend_limit_pct": 100},
        "labs": {"auto_start": True, "idle_fill": "shortest_under_30m",
                 "pool": {"selection": "cheapest", "max_price_pct_of_wallet": 10, "max_seconds": None}},
        "gems": {"auto_unlock_lab_slots": True, "spend_limit_pct": 100, "keep": 0},
    }
