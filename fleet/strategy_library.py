"""Immutable named strategy versions; saving never publishes a worker route."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from fleet.build_route import RouteBaseline, RouteDocument
from fleet.build_route_store import RouteUnavailable, _write_json_atomic


class LibraryConflict(ValueError):
    def __init__(self, revision: int) -> None:
        super().__init__("strategy_library_revision_changed")
        self.revision = revision


def templates() -> list[dict[str, Any]]:
    from fleet.strategy_blocks import template_program
    result = []
    for policy in ("opening", "turtle"):
        baseline = RouteDocument.compatibility().baseline.to_dict()
        baseline["workshop"].update(mode="blocks", blocks=template_program(policy, "workshop"))
        baseline["battle"].update(mode="blocks", branches=[], blocks=template_program(policy, "battle"))
        result.append({"id": policy, "name": policy.title(), "version": 1,
                       "source_template": policy, "baseline": RouteBaseline.from_dict(baseline).to_dict(),
                       "builtin": True})
    return result


class StrategyLibrary:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "strategy-library.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".strategy-library.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _state(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"revision": 0, "versions": []}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (type(state.get("revision")) is not int or state["revision"] < 0
                    or not isinstance(state.get("versions"), list)):
                raise ValueError("invalid library structure")
            seen: dict[str, int] = {}
            for row in state["versions"]:
                if (not isinstance(row["id"], str) or not row["id"].startswith("strategy-")
                        or row["builtin"] is not False or row["source_template"] not in {"opening", "turtle"}
                        or not isinstance(row["name"], str) or not row["name"].strip()
                        or type(row["version"]) is not int
                        or row["version"] != seen.get(row["id"], 0) + 1):
                    raise ValueError("invalid saved strategy")
                row["baseline"] = RouteBaseline.from_dict(row["baseline"]).to_dict()
                seen[row["id"]] = row["version"]
            return state
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise RouteUnavailable("strategy library unavailable") from exc

    def read(self) -> dict[str, Any]:
        state = self._state()
        latest = {row["id"]: row for row in state["versions"]}
        return {"revision": state["revision"], "templates": templates(), "strategies": list(latest.values())}

    def version(self, strategy_id: str, version: int) -> dict[str, Any]:
        if type(version) is not int or version < 1:
            raise ValueError("invalid strategy version")
        if strategy_id in {"opening", "turtle"}:
            if version != 1:
                raise ValueError("built-in strategy version not found")
            return next(row for row in templates() if row["id"] == strategy_id)
        row = next((row for row in self._state()["versions"]
                    if row["id"] == strategy_id and row["version"] == version), None)
        if row is None:
            raise ValueError("saved strategy version not found")
        return row

    def save(self, *, expected_revision: int, name: str, source_template: str,
             baseline: object, strategy_id: str | None = None) -> dict[str, Any]:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid expected library revision")
        if strategy_id in {"opening", "turtle"}:
            raise ValueError("built-in templates are protected; save a copy")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("strategy name must contain 1 to 100 characters")
        if source_template not in {"opening", "turtle"}:
            raise ValueError("unknown source template")
        validated = RouteBaseline.from_dict(baseline).to_dict()
        with self._locked():
            state = self._state()
            if state["revision"] != expected_revision:
                raise LibraryConflict(state["revision"])
            latest = {row["id"]: row for row in state["versions"]}
            taken_names = {"opening", "turtle"} | {
                row["name"].strip().casefold() for row in latest.values() if row["id"] != strategy_id}
            if name.strip().casefold() in taken_names:
                raise ValueError("strategy name already exists")
            previous = [row for row in state["versions"] if row["id"] == strategy_id]
            if strategy_id is not None and not previous:
                raise ValueError("saved strategy not found")
            if previous and previous[-1]["source_template"] != source_template:
                raise ValueError("cannot change strategy source template")
            state["versions"].append({
                "id": strategy_id or f"strategy-{uuid4().hex}", "name": name.strip(),
                "version": len(previous) + 1, "source_template": source_template,
                "baseline": validated, "builtin": False,
            })
            state["revision"] += 1
            _write_json_atomic(self.path, state)
            latest = {row["id"]: row for row in state["versions"]}
            return {"revision": state["revision"], "templates": templates(), "strategies": list(latest.values())}
