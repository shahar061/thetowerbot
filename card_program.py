"""Parse one card program and migrate legacy gem blocks on explicit saves."""

from __future__ import annotations

from typing import Any, Mapping

import card_catalog
from card_models import CardProgram


def parse_program(raw: object) -> CardProgram:
    """Validate a JSON-shaped program without adding spending authority."""
    if not isinstance(raw, Mapping):
        raise ValueError("card program must be an object")
    return CardProgram.model_validate(raw)


def normalize_legacy_gems(
    blocks: list[dict[str, object]], program: CardProgram | None,
) -> tuple[CardProgram | None, list[dict[str, object]]]:
    """Replace legacy card/slot blocks with goal references for a saved copy.

    A missing program migrates with a zero gem cap, so translating old content
    cannot grant automatic spending. Mission blocks stay visible and unavailable.
    """
    goals: list[dict[str, Any]] = [goal.model_dump(mode="json") for goal in program.goals] if program else []
    existing = {goal["id"] for goal in goals}
    normalized: list[dict[str, object]] = []
    changed = False
    for original in blocks:
        block = dict(original)
        kind = block.get("type")
        identity = block.get("id")
        if kind == "buy_cards" and block.get("purpose") == "until_cards":
            if not isinstance(identity, str) or not identity:
                raise ValueError("legacy card block requires an id")
            names = block.get("cards")
            if not isinstance(names, (list, tuple)) or not names:
                raise ValueError("legacy card block needs card names")
            targets: list[dict[str, object]] = []
            for name in names:
                resolved = card_catalog.resolve_legacy_name(name) if isinstance(name, str) else None
                if resolved is None:
                    raise ValueError(f"unknown or ambiguous legacy card name: {name}")
                targets.append({"card_id": resolved, "min_level": None})
            goal: dict[str, Any] = {"id": identity, "kind": "acquire", "targets": targets}
        elif kind == "card_slots":
            if not isinstance(identity, str) or not identity:
                raise ValueError("legacy slot block requires an id")
            goal = {"id": identity, "kind": "slots", "capacity": block.get("up_to"),
                    "when_usable_card": block.get("when_usable_card", False)}
        else:
            normalized.append(block)
            continue
        if identity in existing:
            if next(old for old in goals if old["id"] == identity) != goal:
                raise ValueError(f"legacy block conflicts with goal {identity}")
        else:
            goals.append(goal)
            existing.add(identity)
        normalized.append({"id": identity, "type": "card_goal", "goal_id": identity,
                           **({"label": block["label"]} if "label" in block else {})})
        changed = True
    if not changed:
        return program, normalized
    raw = program.model_dump(mode="json") if program else {
        "version": 1, "gem_cap": 0, "loadouts": [], "selected_loadout_id": None}
    raw["goals"] = goals
    return parse_program(raw), normalized
