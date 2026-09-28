from __future__ import annotations

import lab_catalog
from fleet import resource_blocks as rb
from fleet.build_route import RouteBaseline, RouteDocument

EXCLUDED = {"labs.starting-cash", "labs.cash-wave", "labs.black-hole-damage"}

# Ban Perks (labs.ban-perks) has levels: null in the catalog (its wiki row is
# malformed), so the spec's 19-row template table drops to 18 entries here.
TEMPLATE_ENTRY_COUNT = 18


def test_template_validates_with_pins_and_rules() -> None:
    (block,) = rb.template_lab_list()
    entries = block["entries"]
    assert entries[0] == {"id": "labs.list.game_speed", "lab_id": "labs.game-speed", "to_level": 7,
                          "tier": "S+", "pin_slot": 1, "label": "Game Speed to max"}
    assert [e["to_level"] for e in entries if e["lab_id"] == "labs.labs-speed"] == [50, 99]
    assert all(e.get("pin_slot") == 2 for e in entries if e["lab_id"] == "labs.labs-speed")
    raw = RouteDocument.compatibility().to_dict()["baseline"]
    raw["labs"].update(mode="blocks", blocks=list(rb.template_lab_list()))
    raw["rules"] = rb.template_lab_list_rules()
    assert RouteBaseline.from_dict(raw).rules.coins.lab_share.mode == "just_in_time"


def test_template_excludes_traps_and_irreversible_labs() -> None:
    (block,) = rb.template_lab_list()
    ids = {e["lab_id"] for e in block["entries"]}
    assert not ids & EXCLUDED
    assert not any("bot" in lab_id and "cooldown" in lab_id for lab_id in ids)


def test_template_labs_are_priced() -> None:
    (block,) = rb.template_lab_list()
    for entry in block["entries"]:
        assert lab_catalog.lab(entry["lab_id"]).levels is not None, entry["lab_id"]


def test_template_entry_count_excludes_unpriced_ban_perks() -> None:
    (block,) = rb.template_lab_list()
    ids = {e["lab_id"] for e in block["entries"]}
    assert "labs.ban-perks" not in ids
    assert lab_catalog.lab("labs.ban-perks").levels is None
    assert len(block["entries"]) == TEMPLATE_ENTRY_COUNT
