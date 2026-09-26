"""A read-only log of strategy assignments and edits, derived from history.

Nothing new is written to produce it. Route revisions are append-only and
already record who published what and when, so diffing each revision's
assignments against the one before recovers every assignment change; saved
strategy versions carry their own timestamp. A separate log file could drift
from what was actually published; a derivation cannot.

The ledger is a convenience page, not an authority: an unreadable history file
or library costs its own entries, never the whole page.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_LIMIT = 200


@dataclass(frozen=True)
class StrategyPin:
    """Which saved version a worker ran; the baseline is deliberately absent."""
    strategy_id: str
    strategy_version: int
    strategy_name: str

    def to_dict(self) -> dict[str, Any]:
        return {"strategy_id": self.strategy_id, "strategy_version": self.strategy_version,
                "strategy_name": self.strategy_name}


@dataclass(frozen=True)
class AssignmentChange:
    kind: str  # "assigned" | "reassigned" | "unassigned"
    at: float
    route_revision: int
    actor: str
    worker: str
    account_id: str
    before: StrategyPin | None
    after: StrategyPin | None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "at": self.at, "route_revision": self.route_revision,
                "actor": self.actor, "worker": self.worker, "account_id": self.account_id,
                "before": None if self.before is None else self.before.to_dict(),
                "after": None if self.after is None else self.after.to_dict()}


@dataclass(frozen=True)
class StrategySaved:
    at: float
    strategy_id: str
    strategy_name: str
    strategy_version: int
    source_template: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "saved", "at": self.at, "strategy_id": self.strategy_id,
                "strategy_name": self.strategy_name, "strategy_version": self.strategy_version,
                "source_template": self.source_template}


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected text")
    return value


def _number(value: object) -> float:
    # bool is an int subclass; a True timestamp is corruption, not 1970.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a number")
    return float(value)


def _revision_number(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("expected a positive revision")
    return value


def _pins(assignments: object) -> dict[str, tuple[str, StrategyPin]]:
    """worker -> (account_id, pin). Only identity fields are read, so a
    baseline shape that later schemas reject cannot hide an old change."""
    if not isinstance(assignments, Mapping):
        raise ValueError("expected assignments mapping")
    result: dict[str, tuple[str, StrategyPin]] = {}
    for worker, raw in assignments.items():
        if not isinstance(raw, Mapping):
            raise ValueError("expected assignment mapping")
        version = raw.get("strategy_version")
        if type(version) is not int:
            raise ValueError("expected strategy version")
        result[_text(worker)] = (_text(raw.get("account_id")), StrategyPin(
            _text(raw.get("strategy_id")), version, _text(raw.get("strategy_name"))))
    return result


def assignment_changes(records: Iterable[Mapping[str, Any]]) -> list[AssignmentChange]:
    """Diff consecutive history records, oldest first.

    Each record is one ``build-route-history/<n>.json`` document. Revision 0 is
    the compatibility route, which never has assignments. A malformed record is
    skipped and the next one is diffed against the last good one: its changes
    are then attributed to the later revision, which is the least-wrong answer
    once the in-between state is unknowable.
    """
    parsed: list[tuple[int, float, str, dict[str, tuple[str, StrategyPin]]]] = []
    for record in records:
        try:
            route, audit = record["route"], record["audit"]
            parsed.append((_revision_number(route["revision"]), _number(audit["at"]),
                           _text(audit["actor"]), _pins(route.get("assignments", {}))))
        except (KeyError, TypeError, ValueError):
            continue
    changes: list[AssignmentChange] = []
    previous: dict[str, tuple[str, StrategyPin]] = {}
    for revision, at, actor, current in sorted(parsed, key=lambda item: item[0]):
        for worker in sorted(set(previous) | set(current)):
            before, after = previous.get(worker), current.get(worker)
            if before == after:
                continue
            if before is None and after is not None:
                kind, account_id = "assigned", after[0]
            elif after is None and before is not None:
                # Name the account that lost its pin, not an empty one.
                kind, account_id = "unassigned", before[0]
            elif before is not None and after is not None:
                # Includes a version bump of the same strategy, and the same
                # pin moving to a replacement account on that worker.
                kind, account_id = "reassigned", after[0]
            else:
                continue
            changes.append(AssignmentChange(
                kind, at, revision, actor, worker, account_id,
                None if before is None else before[1], None if after is None else after[1]))
        previous = current
    return changes


def saved_versions(rows: Iterable[Mapping[str, Any]]) -> list[StrategySaved]:
    """Library rows saved before timestamps existed have no place on a
    timeline, so they are left out rather than given an invented time."""
    saved: list[StrategySaved] = []
    for row in rows:
        try:
            if "saved_at" not in row:
                continue
            version = row["version"]
            if type(version) is not int:
                raise ValueError("expected version")
            saved.append(StrategySaved(_number(row["saved_at"]), _text(row["id"]),
                                       _text(row["name"]), version, _text(row["source_template"])))
        except (KeyError, TypeError, ValueError):
            continue
    return saved


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _published_revision(root: Path) -> int | None:
    # publish() writes history before the current route, so a crash between
    # the two leaves a history entry that was never live. The current route
    # bounds what counts; if it is unreadable, show everything we have.
    try:
        return _revision_number(_read_json(root / "build-route.json")["revision"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def read_ledger(root: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Every strategy change under a fleet root, newest first."""
    root = Path(root)
    published = _published_revision(root)
    records: list[Any] = []
    history = root / "build-route-history"
    paths = history.glob("[0-9]*.json") if history.is_dir() else ()
    for path in paths:
        if not path.stem.isdigit() or (published is not None and int(path.stem) > published):
            continue
        try:
            records.append(_read_json(path))
        except (OSError, ValueError):
            continue
    entries: list[AssignmentChange | StrategySaved] = [*assignment_changes(records)]
    try:
        rows = _read_json(root / "strategy-library.json")["versions"]
        entries.extend(saved_versions(rows if isinstance(rows, list) else []))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    # Stable sort: changes published together keep their worker order.
    ordered = sorted(entries, key=lambda entry: entry.at, reverse=True)
    return [entry.to_dict() for entry in (ordered if limit is None else ordered[:limit])]
