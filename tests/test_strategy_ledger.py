"""The strategy ledger is derived from history the fleet already keeps.

Route revisions are append-only, so diffing each one's assignments against the
previous revision recovers every assignment change without a second log that
could drift from what was actually published.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet.strategy_ledger import assignment_changes, read_ledger, saved_versions


def _pin(strategy_id: str, version: int, name: str, account_id: str = "ACC-A") -> dict[str, Any]:
    # The baseline is deliberately junk: the ledger must never look inside it.
    return {"account_id": account_id, "strategy_id": strategy_id, "strategy_version": version,
            "strategy_name": name, "baseline": {"ignored": True}}


def _record(revision: int, assignments: dict[str, Any], at: float,
            actor: str = "operator") -> dict[str, Any]:
    return {"route": {"revision": revision, "assignments": assignments},
            "audit": {"actor": actor, "source_revision": revision - 1,
                      "target_revision": revision, "at": at, "changed_rule_ids": []}}


def _write_history(root: Path, records: list[dict[str, Any]]) -> None:
    history = root / "build-route-history"
    history.mkdir(parents=True, exist_ok=True)
    for record in records:
        revision = record["route"]["revision"]
        (history / f"{revision}.json").write_text(json.dumps(record), encoding="utf-8")


def _slim(entry: dict[str, Any]) -> tuple[Any, ...]:
    return (entry["kind"], entry["route_revision"], entry["worker"],
            None if entry["before"] is None else (entry["before"]["strategy_name"],
                                                  entry["before"]["strategy_version"]),
            None if entry["after"] is None else (entry["after"]["strategy_name"],
                                                 entry["after"]["strategy_version"]))


def test_first_assignment_is_assigned_against_an_empty_revision_zero() -> None:
    entries = [entry.to_dict() for entry in assignment_changes([
        _record(1, {"Air_38": _pin("turtle", 1, "Turtle")}, at=100.0)])]
    assert entries == [{
        "kind": "assigned", "at": 100.0, "route_revision": 1, "actor": "operator",
        "worker": "Air_38", "account_id": "ACC-A", "before": None,
        "after": {"strategy_id": "turtle", "strategy_version": 1, "strategy_name": "Turtle"}}]


def test_reassignment_version_bump_and_unassignment_are_distinguished() -> None:
    entries = [entry.to_dict() for entry in assignment_changes([
        _record(1, {"Air_38": _pin("turtle", 1, "Turtle"),
                    "Air_39": _pin("strategy-x", 1, "Eco")}, at=1.0),
        _record(2, {"Air_38": _pin("strategy-x", 1, "Eco"),
                    "Air_39": _pin("strategy-x", 2, "Eco")}, at=2.0),
        _record(3, {"Air_39": _pin("strategy-x", 2, "Eco")}, at=3.0),
    ])]
    assert [_slim(entry) for entry in entries] == [
        ("assigned", 1, "Air_38", None, ("Turtle", 1)),
        ("assigned", 1, "Air_39", None, ("Eco", 1)),
        ("reassigned", 2, "Air_38", ("Turtle", 1), ("Eco", 1)),
        ("reassigned", 2, "Air_39", ("Eco", 1), ("Eco", 2)),
        ("unassigned", 3, "Air_38", ("Eco", 1), None),
    ]
    # An unassignment still says which account lost its pin.
    assert entries[-1]["account_id"] == "ACC-A"


def test_unchanged_pins_and_baseline_only_differences_are_silent() -> None:
    first = _pin("turtle", 1, "Turtle")
    second = {**first, "baseline": {"something": "else"}}
    entries = assignment_changes([_record(1, {"Air_38": first}, at=1.0),
                                  _record(2, {"Air_38": second}, at=2.0)])
    assert [entry.route_revision for entry in entries] == [1]


def test_same_strategy_on_a_new_account_is_a_reassignment() -> None:
    entries = [entry.to_dict() for entry in assignment_changes([
        _record(1, {"Air_38": _pin("turtle", 1, "Turtle", "ACC-A")}, at=1.0),
        _record(2, {"Air_38": _pin("turtle", 1, "Turtle", "ACC-B")}, at=2.0)])]
    assert [(entry["kind"], entry["account_id"]) for entry in entries] == [
        ("assigned", "ACC-A"), ("reassigned", "ACC-B")]


def test_saved_rows_without_a_timestamp_are_left_out() -> None:
    rows = [{"id": "strategy-x", "name": "Eco", "version": 1, "source_template": "turtle",
             "builtin": False, "baseline": {}},
            {"id": "strategy-x", "name": "Eco Wall", "version": 2, "source_template": "turtle",
             "builtin": False, "baseline": {}, "saved_at": 50.0}]
    assert [entry.to_dict() for entry in saved_versions(rows)] == [{
        "kind": "saved", "at": 50.0, "strategy_id": "strategy-x", "strategy_name": "Eco Wall",
        "strategy_version": 2, "source_template": "turtle"}]


def test_read_ledger_merges_newest_first_and_honours_the_limit(tmp_path: Path) -> None:
    _write_history(tmp_path, [_record(1, {"Air_38": _pin("turtle", 1, "Turtle")}, at=10.0),
                              _record(2, {"Air_38": _pin("strategy-x", 1, "Eco")}, at=30.0)])
    (tmp_path / "strategy-library.json").write_text(json.dumps({"revision": 1, "versions": [
        {"id": "strategy-x", "name": "Eco", "version": 1, "source_template": "turtle",
         "builtin": False, "baseline": {}, "saved_at": 20.0}]}), encoding="utf-8")
    entries = read_ledger(tmp_path)
    assert [(entry["kind"], entry["at"]) for entry in entries] == [
        ("reassigned", 30.0), ("saved", 20.0), ("assigned", 10.0)]
    assert [entry["kind"] for entry in read_ledger(tmp_path, limit=1)] == ["reassigned"]


def test_read_ledger_skips_unreadable_history_instead_of_failing(tmp_path: Path) -> None:
    _write_history(tmp_path, [_record(1, {"Air_38": _pin("turtle", 1, "Turtle")}, at=1.0),
                              _record(3, {"Air_38": _pin("turtle", 1, "Turtle"),
                                          "Air_39": _pin("turtle", 1, "Turtle")}, at=3.0)])
    # Revision 2 is missing and a stray file is corrupt; the page must still render.
    (tmp_path / "build-route-history" / "4.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "strategy-library.json").write_text("{also not json", encoding="utf-8")
    entries = read_ledger(tmp_path)
    assert [_slim(entry) for entry in entries] == [
        ("assigned", 3, "Air_39", None, ("Turtle", 1)),
        ("assigned", 1, "Air_38", None, ("Turtle", 1))]


def test_read_ledger_is_empty_for_a_fresh_fleet(tmp_path: Path) -> None:
    assert read_ledger(tmp_path) == []
