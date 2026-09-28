from __future__ import annotations

from typing import Any

import pytest

from tools.import_wiki_lab_tables import build, parse_coins, parse_duration, parse_page

LAB_SPEED = """Unlock [[Lab Upgrades|lab]] in [[Milestones]] at tier 1 wave 150. Has 99 levels.
{| class="mw-collapsible mw-collapsed fandom-table"
!Level
!Time
!Cost
!Value
|-
|1
|0d  0h  0m
|                                    40
|     1.02
|-
|2
|0d  0h  9m
|                                    83
|     1.04
|}"""

GAME_SPEED = """Unlocks with labs at tier 1 wave 30.
{| class="fandom-table"
!Level
!Cost
!Time
!Value
|-
|1
|300
|9m
|x2.0
|-
|4
|50,000
|1d 10h 2m
|x3.5
|}"""


@pytest.mark.parametrize(("text", "seconds"), [
    ("0d  0h  0m", 0), ("9m", 540), ("2h 30m", 9000), ("1d 10h 2m", 122520), ("3d 11h 19m", 299940),
    ("0d  0h  6m", 360),
])
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["41d 16h 0m 15m", "5m 1h", "", "soon"])
def test_parse_duration_rejects_unreadable_times(text: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(text)


@pytest.mark.parametrize(("text", "coins"), [
    ("                 1,500,000", 1_500_000), ("40", 40), ("2.5M", 2_500_000), ("1.2B", 1_200_000_000),
    ("    1,000", 1_000),
])
def test_parse_coins(text: str, coins: int) -> None:
    assert parse_coins(text) == coins


@pytest.mark.parametrize("text", ["1.00q", "", "free"])
def test_parse_coins_rejects_unreadable_costs(text: str) -> None:
    with pytest.raises(ValueError, match="cost"):
        parse_coins(text)


def test_parse_page_reads_rows_and_tier_unlock() -> None:
    unlock, levels = parse_page(LAB_SPEED)
    assert unlock == [{"tier": 1, "wave": 150}]
    assert levels == [{"level": 1, "coins": 40, "seconds": 0, "value": "1.02"},
                      {"level": 2, "coins": 83, "seconds": 540, "value": "1.04"}]


def test_parse_page_matches_columns_by_header() -> None:
    _, levels = parse_page(GAME_SPEED)
    assert levels[1] == {"level": 4, "coins": 50_000, "seconds": 122_520, "value": "x3.5"}


def test_parse_page_without_table_is_empty() -> None:
    assert parse_page("no table here") == ([], [])


@pytest.mark.parametrize(("text", "message"), [
    (LAB_SPEED.replace("|0d  0h  9m", "|41d 16h 0m 15m"), "duration"),
    (LAB_SPEED.replace("83", "1.00q"), "cost"),
    ("Damage lab is unlocked with labs in [[Milestones]] at tier 1.\n" + LAB_SPEED[LAB_SPEED.find("{|"):],
     "unlock"),
])
def test_parse_page_refuses_what_it_cannot_read(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_page(text)


# --- build(): R-F10, an unlock the importer could not read is never written as [] ---

def _page(unlock_prose: str, levels: int = 2) -> str:
    rows = "".join(f"|-\n|{n}\n|{n}h\n|{n * 100}\n|{n}%\n" for n in range(1, levels + 1))
    return f"{unlock_prose} Has {levels} levels.\n{{|\n!Level\n!Time\n!Cost\n!Value\n{rows}|}}"


def _source() -> dict[str, Any]:
    game_speed = {"id": "labs.game-speed", "name": "Game Speed", "unlock": None, "max_level": 1,
                  "levels": [{"level": 1, "coins": 300, "seconds": 540, "max_speed": 2.0}],
                  "source_url": "https://example.org/gs", "checked": "2026-09-26"}
    labs_speed = {"id": "labs.labs-speed", "name": "Labs Speed", "unlock": {"best_tier_1_wave": 150},
                  "max_level": None, "levels": None, "source_url": "https://example.org/ls",
                  "checked": "2026-09-26"}
    return {"version": 1, "scope": "old", "labs_unlock": {"best_tier_1_wave": 30}, "labs": [game_speed, labs_speed]}


def _build(pages: dict[str, tuple[str, str]], texts: dict[str, str]) -> dict[str, dict[str, Any]]:
    def fetch(page: str) -> str:
        if page not in texts:
            raise OSError("offline")
        return texts[page]

    payload = build(_source(), "2026-09-28", fetch, pages)
    assert payload["version"] == 2
    return {lab["id"]: lab for lab in payload["labs"]}


def test_build_prices_labs_and_converts_v1_unlocks() -> None:
    labs = _build({"labs.health": ("Health", "Lab/Health")},
                  {"Lab/Health": _page("The health lab is unlocked at the start.")})
    assert labs["labs.health"]["unlock"] == [] and labs["labs.health"]["max_level"] == 2
    assert labs["labs.health"]["levels"][1] == {"level": 2, "coins": 200, "seconds": 7200, "value": "2%"}
    assert labs["labs.health"]["checked"] == "2026-09-28"
    assert labs["labs.labs-speed"]["unlock"] == [{"tier": 1, "wave": 150}]
    assert labs["labs.game-speed"]["unlock"] == []


def test_build_failed_fetch_keeps_the_existing_entry() -> None:
    labs = _build({"labs.labs-speed": ("Labs Speed", "Lab/Lab_Speed")}, {})
    assert labs["labs.labs-speed"]["unlock"] == [{"tier": 1, "wave": 150}]
    assert labs["labs.labs-speed"]["levels"] is None and labs["labs.labs-speed"]["checked"] == "2026-09-26"


def test_build_failed_fetch_of_a_new_lab_uses_its_known_unlock_unpriced() -> None:
    labs = _build({"labs.perk-option-quantity": ("Perk Option Quantity", "Perk_Labs/Perk_Option_Quantity")}, {})
    assert labs["labs.perk-option-quantity"]["unlock"] == [{"tier": 4, "wave": 80},
                                                           {"lab": "labs.unlock-perks", "level": 1}]
    assert labs["labs.perk-option-quantity"]["levels"] is None


def test_build_failed_fetch_of_a_new_lab_with_no_known_unlock_fails_loudly() -> None:
    with pytest.raises(RuntimeError, match="labs.critical-factor"):
        _build({"labs.critical-factor": ("Critical Factor", "Lab/Critical_Factor")}, {})


BAN_PERKS = _page("Unlocked in Milestones at tier 5 wave 40 and after Unlock Perk completed.", levels=3)


@pytest.mark.parametrize("broken", [
    BAN_PERKS.replace("|2h", "|41d 16h 0m 15m"),     # unreadable cell
    BAN_PERKS.replace("Has 3 levels", "Has 8 levels"),  # prose count disagrees with the table
    BAN_PERKS.replace("|-\n|2\n", "|-\n|4\n"),        # a gap in the level numbers
    BAN_PERKS.replace("|300", "|150"),                # a price that falls
    BAN_PERKS.replace("|3h", "|1h"),                  # a time that falls
    BAN_PERKS.replace("!Cost", "!Price"),             # no cost column
])
def test_build_unreadable_table_keeps_the_unlock_and_stays_unpriced(broken: str) -> None:
    labs = _build({"labs.ban-perks": ("Ban Perks", "Perk_Labs/Ban_Perks")}, {"Perk_Labs/Ban_Perks": broken})
    assert labs["labs.ban-perks"]["unlock"] == [{"tier": 5, "wave": 40}, {"lab": "labs.unlock-perks", "level": 1}]
    assert labs["labs.ban-perks"]["levels"] is None and labs["labs.ban-perks"]["max_level"] is None


def test_build_unreadable_unlock_prose_fails_loudly() -> None:
    with pytest.raises(ValueError, match="labs.critical-factor"):
        _build({"labs.critical-factor": ("Critical Factor", "Lab/Critical_Factor")},
               {"Lab/Critical_Factor": _page("Unlocked with labs in Milestones at tier 1.")})


def test_build_prose_contradicting_a_known_unlock_fails_loudly() -> None:
    with pytest.raises(ValueError, match="labs.light-speed-shots"):
        _build({"labs.light-speed-shots": ("Light Speed Shots", "Lab/Light_Speed_Shots")},
               {"Lab/Light_Speed_Shots": _page("Unlocked in Milestones at tier 6 wave 10.", levels=1)})


def test_build_uses_the_known_unlock_when_the_page_is_silent() -> None:
    labs = _build({"labs.unlock-perks": ("Unlock Perks", "Perk_Labs/Unlock_Perks")},
                  {"Perk_Labs/Unlock_Perks": _page("Unlocks perks to be accessed in rounds.", levels=1)})
    assert labs["labs.unlock-perks"]["unlock"] == [{"tier": 2, "wave": 150}]
    assert labs["labs.unlock-perks"]["max_level"] == 1


def test_build_drops_a_tier_condition_the_labs_milestone_already_implies() -> None:
    labs = _build({"labs.cash-bonus": ("Cash Bonus", "Lab/Cash_Bonus"),
                   "labs.workshop-attack-discount": ("Workshop Attack Discount", "Lab/Workshop_Attack_Discount")},
                  {"Lab/Cash_Bonus": _page("Unlocked with labs in Milestones on tier 1 wave 30."),
                   "Lab/Workshop_Attack_Discount": _page("Unlock lab in Milestones on tier 1 wave 40.")})
    assert labs["labs.cash-bonus"]["unlock"] == []
    assert labs["labs.workshop-attack-discount"]["unlock"] == [{"tier": 1, "wave": 40}]
