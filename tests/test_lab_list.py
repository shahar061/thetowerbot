from __future__ import annotations

from copy import deepcopy

import pytest

from fleet import resource_blocks as rb

BASE = {"id": "labs.list", "type": "lab_list", "label": "Early game", "entries": [
    {"id": "gs", "lab_id": "labs.game-speed", "to_level": 7, "tier": "S+", "pin_slot": 1},
    {"id": "ls50", "lab_id": "labs.labs-speed", "to_level": 50, "tier": "S", "pin_slot": 2},
    {"id": "ckb", "lab_id": "labs.coins-kill-bonus", "to_level": 30, "tier": "A"},
    {"id": "ls99", "lab_id": "labs.labs-speed", "to_level": 99, "tier": "A", "pin_slot": 2},
]}


def block(**changes: object) -> dict:
    result = deepcopy(BASE)
    result.update(changes)
    return result


def test_valid_list_round_trips() -> None:
    (validated,) = rb.validate_labs([block()])
    assert validated["type"] == "lab_list" and [entry["id"] for entry in validated["entries"]] == [
        "gs", "ls50", "ckb", "ls99"]


def test_list_cannot_mix_with_slot_tracks() -> None:
    with pytest.raises(ValueError, match="only block"):
        rb.validate_labs([block(), *deepcopy(list(rb.template_lab_blocks()))])


def test_first_entry_must_be_game_speed_on_slot_one() -> None:
    entries = deepcopy(BASE["entries"])
    entries[0]["pin_slot"] = 2
    with pytest.raises(ValueError, match="Game Speed pinned to slot 1"):
        rb.validate_labs([block(entries=entries)])
    with pytest.raises(ValueError, match="Game Speed pinned to slot 1"):
        rb.validate_labs([block(entries=entries[1:])])


def test_repeated_lab_needs_higher_level() -> None:
    entries = deepcopy(BASE["entries"])
    entries[3]["to_level"] = 40
    with pytest.raises(ValueError, match="higher to_level"):
        rb.validate_labs([block(entries=entries)])


@pytest.mark.parametrize(("key", "value", "match"), [
    ("tier", "D", "tier"), ("lab_id", "labs.nope", "unknown lab id"), ("pin_slot", 6, "pin_slot"),
    ("to_level", 0, "to_level"), ("extra", 1, "unknown lab list entry field"),
])
def test_rejects_bad_entries(key: str, value: object, match: str) -> None:
    entries = deepcopy(BASE["entries"])
    entries[2][key] = value
    with pytest.raises(ValueError, match=match):
        rb.validate_labs([block(entries=entries)])


def test_entry_ids_are_unique() -> None:
    entries = deepcopy(BASE["entries"])
    entries[2]["id"] = "gs"
    with pytest.raises(ValueError, match="unique"):
        rb.validate_labs([block(entries=entries)])
