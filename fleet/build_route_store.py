"""Coordinator-owned atomic Build Route revisions and audit history."""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from fleet.build_route import RouteDocument


class RouteUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RouteConflict(ValueError):
    def __init__(self, current: RouteDocument) -> None:
        super().__init__(f"route revision changed to {current.revision}")
        self.current = current


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class BuildRouteStore:
    """One local coordinator, serialized by an OS file lock."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "build-route.json"
        self.history = self.root / "build-route-history"
        self.lock_path = self.root / ".build-route.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def read(self) -> RouteDocument:
        if not self.path.is_file():
            if self.history.is_dir() and any(self.history.glob("[0-9]*.json")):
                raise RouteUnavailable("current route missing after publication")
            return RouteDocument.compatibility()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return RouteDocument.from_dict(raw)
        except (OSError, ValueError, TypeError) as exc:
            raise RouteUnavailable(f"current route invalid: {exc}") from exc

    def _revision(self, revision: int) -> RouteDocument:
        path = self.history / f"{revision}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return RouteDocument.from_dict(raw["route"])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RouteUnavailable(f"route revision {revision} unavailable: {exc}") from exc

    def revisions(self) -> tuple[RouteDocument, ...]:
        current = self.read()
        return tuple(self._revision(number) for number in range(1, current.revision + 1))

    def publish(self, draft: RouteDocument, expected_revision: int, actor: str) -> RouteDocument:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("route actor is required")
        # Reparse to validate even a caller that constructed dataclasses directly.
        validated = RouteDocument.from_dict(draft.to_dict())
        with self._locked():
            current = self.read()
            if current.revision != expected_revision:
                raise RouteConflict(current)
            saved = replace(validated, revision=current.revision + 1,
                            authored_at=time.time())
            changed = _changed_rules(current, saved)
            record = {"route": saved.to_dict(), "audit": {
                "actor": actor.strip(), "source_revision": current.revision,
                "target_revision": saved.revision, "at": saved.authored_at,
                "changed_rule_ids": changed,
            }}
            # A failed current-file replacement can leave an unpublished history
            # entry. It is not part of revisions(); the next writer may replace it.
            _write_json_atomic(self.history / f"{saved.revision}.json", record)
            _write_json_atomic(self.path, saved.to_dict())
            return saved

    def rollback(self, revision: int, expected_revision: int, actor: str) -> RouteDocument:
        if type(revision) is not int or revision < 1:
            raise ValueError("rollback revision must be positive")
        current = self.read()
        if revision > current.revision:
            raise ValueError("rollback revision not published")
        return self.publish(self._revision(revision), expected_revision, actor)


def _changed_rules(before: RouteDocument, after: RouteDocument) -> list[str]:
    changed: list[str] = []
    if before.baseline.workshop != after.baseline.workshop:
        changed.append(after.baseline.workshop.id)
    if before.baseline.battle != after.baseline.battle:
        changed.extend(branch.id for branch in after.baseline.battle.branches)
    if before.baseline.gems != after.baseline.gems:
        changed.append("gems.path")
    if before.baseline.labs != after.baseline.labs:
        changed.append("labs.path")
    for worker in sorted(set(before.overrides) | set(after.overrides)):
        if before.overrides.get(worker) != after.overrides.get(worker):
            changed.append(f"override.{worker}")
    return changed
