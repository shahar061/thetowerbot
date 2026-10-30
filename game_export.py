"""Normalize official game exports for reporting only.

No value from this module authorizes a game action. Indexed names come from a
pinned community mapping; unknown indexes and empty positions stay intact.
Chronology, account authority and freshness are enforced by the reporting store.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any


MAPPING_PATH = Path(__file__).resolve().parent / "catalog/game-export-mappings.v1.json"
_MAPPINGS = json.loads(MAPPING_PATH.read_text())
_LABS = {row["index"]: row for row in _MAPPINGS["labs"]}
_CARDS = {row["index"]: row for row in _MAPPINGS["cards"]}
_WORKSHOP = {category: {row["index"]: row for row in rows}
             for category, rows in _MAPPINGS["workshop"].items()}


def _validate_json(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json(item)
        return
    raise ValueError("export must contain only finite JSON values")


def _timestamp(value: Any) -> float | None:
    """An absent/minimum game date is unknown; timezone-free dates are invalid."""
    if value is None or value == "" or (isinstance(value, str) and value.startswith("0001-01-01T")):
        return None
    if type(value) in (int, float):
        try:
            if math.isfinite(value) and value > 0:
                return float(value)
        except OverflowError as exc:
            raise ValueError("invalid game timestamp") from exc
        raise ValueError("invalid game timestamp")
    if not isinstance(value, str):
        raise ValueError("expected a timezone-aware timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("timestamp requires a timezone")
        result = stamp.timestamp()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("invalid game timestamp") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("invalid game timestamp")
    return result


class _Reader:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def array(self, field: str) -> list[Any]:
        value = self.data.get(field)
        if value is None:
            return []
        if not isinstance(value, list):
            self.warn(f"{field}: expected an array; observations unavailable")
            return []
        return value

    def number(self, value: Any, field: str) -> int | float | None:
        if value is None:
            return None
        if type(value) in (int, float):
            return value
        self.warn(f"{field}: expected a number; observation unavailable")
        return None

    def flag(self, value: Any, field: str) -> bool | None:
        if value is None or type(value) is bool:
            return value
        self.warn(f"{field}: expected a boolean; observation unavailable")
        return None

    def date(self, value: Any, field: str) -> float | None:
        try:
            return _timestamp(value)
        except ValueError:
            self.warn(f"{field}: invalid timestamp; observation unavailable")
            return None

    def identity(self, index: Any, mapping: dict[int, dict], field: str) -> dict:
        row = mapping.get(index) if type(index) is int and index >= 0 else None
        if row is None:
            self.warn(f"{field}: unknown index {index!r}; identity unavailable")
        return {"index": index, "id": row["id"] if row else None,
                "name": row["name"] if row else None}


def _at(rows: list[Any], index: int) -> Any:
    return rows[index] if index < len(rows) else None


def _labs(reader: _Reader) -> dict:
    queue = {name: reader.array(name) for name in
             ("LabResearchInt", "LabActiveBool", "LabLevel", "LabSpeedUpSpeed", "LabSpeedUpRemainingSeconds")}
    active_indexes = {native_index for slot, native_index in enumerate(queue["LabResearchInt"])
                      if type(native_index) is int and native_index >= 0
                      and _at(queue["LabActiveBool"], slot) is True}
    fields = {name: reader.array(name) for name in
              ("ResearchLevel", "ResearchPercentComplete", "ResearchCompleteDate")}
    research = []
    for index in range(max(map(len, fields.values()))):
        item = reader.identity(index, _LABS, "ResearchLevel")
        item.update(level=reader.number(_at(fields["ResearchLevel"], index), "ResearchLevel"),
                    percent_complete=reader.number(_at(fields["ResearchPercentComplete"], index), "ResearchPercentComplete"),
                    # Inactive research retains old game timestamps. Only the
                    # active queue establishes that this date projects a finish.
                    completes_at=reader.date(_at(fields["ResearchCompleteDate"], index), "ResearchCompleteDate")
                    if index in active_indexes else None)
        research.append(item)
    slots = []
    for index in range(max(map(len, queue.values()))):
        native_index = _at(queue["LabResearchInt"], index)
        item = reader.identity(native_index, _LABS, "LabResearchInt")
        completion = research[native_index]["completes_at"] if (
            _at(queue["LabActiveBool"], index) is True and type(native_index) is int
            and 0 <= native_index < len(research)) else None
        item.update(slot=index + 1,
                    active=reader.flag(_at(queue["LabActiveBool"], index), "LabActiveBool"),
                    level=reader.number(_at(queue["LabLevel"], index), "LabLevel"),
                    speed_up=reader.number(_at(queue["LabSpeedUpSpeed"], index), "LabSpeedUpSpeed"),
                    speed_up_remaining_seconds=reader.number(_at(queue["LabSpeedUpRemainingSeconds"], index), "LabSpeedUpRemainingSeconds"),
                    completes_at=completion)
        slots.append(item)
    return {"research": research, "slots": slots, "unlocked_raw": reader.data.get("LabsUnlocked")}


def _cards(reader: _Reader) -> dict:
    fields = {name: reader.array(name) for name in
              ("CardLevel", "CardCount", "CardUnlocked", "CardActive", "CardMasteryUnlocked")}
    items = []
    for index in range(max(map(len, fields.values()))):
        item = reader.identity(index, _CARDS, "CardLevel")
        for field, name in (("CardLevel", "level"), ("CardCount", "count")):
            item[name] = reader.number(_at(fields[field], index), field)
        for field, name in (("CardUnlocked", "unlocked"), ("CardActive", "active"), ("CardMasteryUnlocked", "mastery_unlocked")):
            item[name] = reader.flag(_at(fields[field], index), field)
        items.append(item)
    return {"items": items, "slots": reader.number(reader.data.get("SlotsUnlocked"), "SlotsUnlocked")}


def _workshop(reader: _Reader) -> dict:
    upgrades = []
    for category, field in (("attack", "UpgradeWorkshopLevel"),
                            ("defense", "UpgradeWorkshopDefenseLevel"),
                            ("utility", "UpgradeWorkshopUtilityLevel")):
        for index, value in enumerate(reader.array(field)):
            item = reader.identity(index, _WORKSHOP[category], field)
            item.update(category=category, level=reader.number(value, field))
            upgrades.append(item)
    return {"upgrades": upgrades}


def _current_run(reader: _Reader) -> dict:
    fields = {"tier": "CurrentTier", "wave": "CurrentWave", "elapsed_seconds": "RealTimeThisRound",
              "game_elapsed_seconds": "GameplayTimeThisRound", "coins": "CoinsEarnedThisRound",
              "cells": "CellsEarnedThisRound"}
    run = {name: reader.number(reader.data.get(field), field) for name, field in fields.items()}
    run["active"] = reader.flag(reader.data.get("RoundActiveBool"), "RoundActiveBool")
    real, game = run["elapsed_seconds"], run["game_elapsed_seconds"]
    average = None
    if real is not None and real > 0 and game is not None and game >= 0:
        try:
            rate = game / real
            if math.isfinite(rate):
                average = rate
            else:
                reader.warn("Current run effective rate is outside the supported numeric range")
        except OverflowError:
            reader.warn("Current run effective rate is outside the supported numeric range")
    run["effective_rate_average"] = average
    return run


def _reports(reader: _Reader) -> list[dict]:
    reports = []
    for raw in reader.array("BattleHistory"):
        if not isinstance(raw, dict):
            reader.warn("BattleHistory: expected report objects; malformed row omitted")
            continue
        fields = {"tier": "Tier", "wave": "Wave", "elapsed_seconds": "RealTime",
                  "game_elapsed_seconds": "GameTime", "coins": "CoinsEarned",
                  "cells": "CellsEarned", "killed_by": "KilledBy"}
        item = {name: reader.number(raw.get(field), f"BattleHistory.{field}") for name, field in fields.items()}
        item.update(battle_at=reader.date(raw.get("BattleDate"), "BattleHistory.BattleDate"),
                    is_tournament=reader.flag(raw.get("IsTournament"), "BattleHistory.IsTournament"), raw=raw)
        reports.append(item)
    return reports


def _completed_labs(reader: _Reader) -> list[dict]:
    completed = []
    for raw in reader.array("CompletedLabs"):
        if not isinstance(raw, dict):
            reader.warn("CompletedLabs: expected completion objects; malformed row omitted")
            continue
        item = reader.identity(raw.get("labNumber"), _LABS, "CompletedLabs.labNumber")
        item.update(level=reader.number(raw.get("level"), "CompletedLabs.level"),
                    completed_at=reader.date(raw.get("dateCompleted"), "CompletedLabs.dateCompleted"), raw=raw)
        completed.append(item)
    return completed


def parse_export(raw: bytes | str | dict) -> dict:
    """Return independent JSON-compatible reporting data, retaining raw reports.

    A usable exporter timestamp is mandatory. Invalid optional shapes warn and
    remain unknown; missing observations are never replaced by zeros or OCR.
    """
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else deepcopy(raw)
        if not isinstance(data, dict):
            raise ValueError("export root must be an object")
        _validate_json(data)
        exported_at = _timestamp(data.get("ExportedAt"))
        if exported_at is None:
            raise ValueError("ExportedAt is required")
    except (TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid game export JSON") from exc
    reader = _Reader(data)
    versions = {name: data.get(field) for name, field in
                (("export", "ExportVersion"), ("game", "VersionNumber"), ("data", "DataVersion"))}
    for name, value in versions.items():
        if value not in _MAPPINGS["verified_versions"][name]:
            reader.warn(f"Unverified {name} version {value!r}; indexed mappings may differ")
    result = {"versions": versions, "exported_at": exported_at,
              "wallet": {name: reader.number(data.get(name.title()), name.title())
                         for name in ("coins", "gems", "stones", "medals", "cells", "keys", "tokens", "bits")},
              "labs": _labs(reader), "workshop": _workshop(reader), "cards": _cards(reader),
              "current_run": _current_run(reader), "battle_history": _reports(reader),
              "completed_labs": _completed_labs(reader), "warnings": reader.warnings}
    return result
