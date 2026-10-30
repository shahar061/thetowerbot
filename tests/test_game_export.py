"""Synthetic exports exercise observed fields without private account data."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from game_export import parse_export


def sample(**changes: object) -> dict:
    return {"ExportVersion": 1, "VersionNumber": 1253, "DataVersion": 31,
            "ExportedAt": "2026-10-30T09:00:00.000Z", **changes}


def test_accepts_text_bytes_and_dict_without_mutating_source() -> None:
    raw = sample(Coins=12.75, Gems=0)
    expected = parse_export(raw)
    assert parse_export(json.dumps(raw)) == expected
    assert parse_export(json.dumps(raw).encode()) == expected
    assert expected["wallet"]["coins"] == 12.75
    assert expected["wallet"]["gems"] == 0
    assert expected["wallet"]["cells"] is None
    assert expected["exported_at"] == datetime(2026, 10, 30, 9, tzinfo=timezone.utc).timestamp()
    assert expected["versions"] == {"export": 1, "game": 1253, "data": 31}
    assert "wallet" not in raw


def test_research_uses_native_indexes_and_explicit_registry_ids() -> None:
    raw = sample(ResearchLevel=[3] + [0] * 29 + [4],
                 ResearchPercentComplete=[0.5],
                 ResearchCompleteDate=["2026-10-30T10:00:00Z"],
                 LabsUnlocked=4, LabResearchInt=[30, 0], LabActiveBool=[True, False],
                 LabLevel=[0, 0], LabSpeedUpSpeed=[2], LabSpeedUpRemainingSeconds=[60])
    labs = parse_export(raw)["labs"]
    assert labs["unlocked_raw"] == 4
    assert labs["research"][0] == {"index": 0, "id": "labs.damage", "name": "Damage",
                                    "level": 3, "percent_complete": 0.5,
                                    "completes_at": None}
    assert labs["research"][30]["id"] == "labs.game-speed"
    assert labs["slots"][0]["id"] == "labs.game-speed"
    assert labs["slots"][0]["slot"] == 1
    assert labs["slots"][0]["speed_up"] == 2
    assert labs["slots"][1]["active"] is False
    assert labs["slots"][1]["completes_at"] == labs["research"][0]["completes_at"]


def test_card_holes_flags_and_unknown_indexes_are_not_shifted() -> None:
    result = parse_export(sample(CardLevel=[1] * 41, CardCount=[0] * 41,
                                 CardUnlocked=[False] * 41, CardActive=[False] * 10 + [True],
                                 CardMasteryUnlocked=[False] * 41, SlotsUnlocked=9))
    cards = result["cards"]
    assert len(cards["items"]) == 41
    assert cards["items"][8]["name"] is None
    assert cards["items"][8]["id"] is None
    assert cards["items"][10]["id"] == "cards.critical-chance"
    assert cards["items"][10]["unlocked"] is False
    assert cards["items"][10]["active"] is True
    assert cards["items"][40]["index"] == 40
    assert cards["items"][40]["name"] is None
    assert cards["slots"] == 9
    assert any("unknown index" in warning for warning in result["warnings"])


def test_workshop_categories_preserve_padding_and_fractional_values() -> None:
    upgrades = parse_export(sample(UpgradeWorkshopLevel=[12.5] + [0] * 19,
                                    UpgradeWorkshopDefenseLevel=[7],
                                    UpgradeWorkshopUtilityLevel=[0, 0, 11]))["workshop"]["upgrades"]
    assert upgrades[0] == {"category": "attack", "index": 0, "id": "stats.damage", "name": "Damage", "level": 12.5}
    assert upgrades[17]["id"] is None
    assert upgrades[20]["id"] == "stats.health"
    assert upgrades[-1]["id"] == "stats.coins_per_kill_bonus"


def test_current_run_exposes_average_rate_without_spendable_cash_or_speed() -> None:
    run = parse_export(sample(RoundActiveBool=True, CurrentTier=2, CurrentWave=40,
                               RealTimeThisRound=100, GameplayTimeThisRound=420,
                               CoinsEarnedThisRound=2.25, CellsEarnedThisRound=0,
                               CashEarnedThisRound=5000))["current_run"]
    assert run == {"active": True, "tier": 2, "wave": 40, "elapsed_seconds": 100,
                   "game_elapsed_seconds": 420, "coins": 2.25, "cells": 0,
                   "effective_rate_average": 4.2}
    assert parse_export(sample(RealTimeThisRound=0, GameplayTimeThisRound=10))["current_run"]["effective_rate_average"] is None


def test_reports_and_completed_labs_preserve_originals_and_exact_event_times() -> None:
    report = {"BattleDate": "2026-10-30T08:00:00Z", "Tier": 4, "Wave": 100,
              "RealTime": 100, "GameTime": 400, "CoinsEarned": 1.125,
              "CellsEarned": 0.25, "KilledBy": 456, "NewField": {"value": 9}}
    completed = {"dateCompleted": "2026-10-30T07:00:00Z", "labNumber": 30, "level": 3}
    result = parse_export(sample(BattleHistory=[report], CompletedLabs=[completed]))
    battle = result["battle_history"][0]
    assert battle["raw"] == report
    assert battle["raw"] is not report
    assert battle["battle_at"] == datetime(2026, 10, 30, 8, tzinfo=timezone.utc).timestamp()
    assert battle["coins"] == 1.125
    assert battle["killed_by"] == 456
    lab = result["completed_labs"][0]
    assert lab["id"] == "labs.game-speed"
    assert lab["raw"] == completed
    assert lab["completed_at"] == datetime(2026, 10, 30, 7, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("value", [[], "bad json", b"\xff", {}, {"ExportedAt": "nope"},
                                  {"ExportedAt": "2026-10-30T09:00:00"},
                                  {"ExportedAt": "0001-01-01T00:00:00Z"},
                                  sample(Coins=float("nan"))])
def test_malformed_exports_fail_explicitly(value: object) -> None:
    with pytest.raises(ValueError):
        parse_export(value)


def test_partial_shapes_and_new_versions_warn_without_inventing_observations() -> None:
    result = parse_export(sample(ExportVersion=2, DataVersion=99,
                                 ResearchLevel="bad", CardUnlocked=[True],
                                 ResearchCompleteDate=["0001-01-01T00:00:00.000Z"]))
    assert result["labs"]["research"][0]["level"] is None
    assert result["labs"]["research"][0]["completes_at"] is None
    assert result["cards"]["items"][0]["level"] is None
    assert result["cards"]["items"][0]["unlocked"] is True
    assert result["current_run"]["active"] is None
    assert any("version" in warning for warning in result["warnings"])
    assert any("ResearchLevel" in warning for warning in result["warnings"])


def test_mapping_ids_are_explicit_and_present_in_registry() -> None:
    root = Path(__file__).resolve().parents[1]
    mappings = json.loads((root / "catalog/game-export-mappings.v1.json").read_text())
    concepts = json.loads((root / "catalog/concepts.v1.json").read_text())["concepts"]
    ids = {concept["concept_id"] for concept in concepts}
    assert mappings["source"]["commit"] == "f2afe6dddc41115b243889f9872ba87e478609e5"
    for rows in [mappings["labs"], mappings["cards"], *mappings["workshop"].values()]:
        for row in rows:
            assert row["id"] is None or row["id"] in ids
    assert mappings["cards"][8]["name"] is None
    assert (root / "third_party/game-export-sdk/LICENSE").is_file()
    assert (root / "third_party/game-export-sdk/NOTICE").is_file()


def test_extreme_numbers_cannot_create_nonfinite_normalized_values() -> None:
    with pytest.raises(ValueError):
        parse_export(sample(ExportedAt=10 ** 1000))
    result = parse_export(sample(RealTimeThisRound=1e-300, GameplayTimeThisRound=1e300))
    assert result["current_run"]["effective_rate_average"] is None
    assert any("effective rate" in warning for warning in result["warnings"])
    json.dumps(result, allow_nan=False)


def test_only_active_queue_research_dates_are_projected_completions() -> None:
    result = parse_export(sample(
        ResearchLevel=[3, 4, 5],
        ResearchCompleteDate=["2026-10-29T09:00:00Z", "2026-10-30T10:00:00Z",
                              "2026-10-29T10:00:00Z"],
        LabResearchInt=[1, 2], LabActiveBool=[True, False],
    ))
    research = result["labs"]["research"]
    slots = result["labs"]["slots"]
    assert research[0]["completes_at"] is None
    assert research[1]["completes_at"] == datetime(2026, 10, 30, 10, tzinfo=timezone.utc).timestamp()
    assert research[2]["completes_at"] is None
    assert slots[0]["completes_at"] == research[1]["completes_at"]
    assert slots[1]["completes_at"] is None
