"""Durable membership for manually prepared, first-launch Tower emulators."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from threading import RLock
from typing import Callable
from uuid import uuid4

from bluestacks import HostInstance


class RerollPoolError(ValueError):
    """Pool membership or live host evidence is unsafe."""


class RerollPool:
    def __init__(self, root: Path, *, inventory: Callable[[], list[HostInstance]],
                 package_state: Callable[[str], str],
                 protected_names: Callable[[], set[str]],
                 probe_stopped: Callable[[HostInstance], str],
                 registered: Callable[[HostInstance], bool] | None = None) -> None:
        """``probe_stopped`` boots a stopped emulator, returns its ``package_state``
        and shuts it down again. A stopped emulator's Tower state can't be read
        otherwise, and a clone of a played emulator looks the same as a fresh one."""
        self.path = Path(root) / "reroll-pool.json"
        self.inventory = inventory
        self.package_state = package_state
        self.probe_stopped = probe_stopped
        self.protected_names = protected_names
        self.registered = registered or (lambda row: False)
        self._lock = RLock()

    def _members(self) -> list[dict[str, str]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise RerollPoolError("pool_state_unreadable") from exc
        if not isinstance(value, list) or any(
                not isinstance(item, dict) or set(item) != {"name", "endpoint", "lease_id"}
                or any(not isinstance(item[key], str) or not item[key]
                       for key in ("name", "endpoint", "lease_id"))
                for item in value):
            raise RerollPoolError("pool_state_unreadable")
        return value

    def _save(self, members: list[dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(members, file, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _rows(self) -> list[HostInstance]:
        try:
            rows = self.inventory()
        except Exception as exc:
            raise RerollPoolError("host_inventory_unavailable") from exc
        names = [row.name for row in rows]
        endpoints = [row.endpoint for row in rows]
        if len(set(names)) != len(names) or len(set(endpoints)) != len(endpoints):
            raise RerollPoolError("host_inventory_ambiguous")
        return rows

    def _state(self, row: HostInstance) -> str:
        if row.name in self.protected_names():
            return "protected_template"
        if row.state != "running":
            return "start_required" if row.state == "stopped" else "host_state_unavailable"
        try:
            state = self.package_state(row.endpoint)
        except Exception:
            return "tower_state_unavailable"
        return {"installed_unopened": "ready", "opened": "tower_already_opened",
                "not_installed": "tower_not_installed"}.get(state, "tower_state_unavailable")

    def snapshot(self) -> dict[str, list[dict[str, str]]]:
        with self._lock:
            rows = self._rows()
            by_name = {row.name: row for row in rows}
            members = self._members()
            registered = {item["name"] for item in members}
            candidates = [{"name": row.name, "endpoint": row.endpoint,
                           "state": self._state(row)} for row in rows
                          if row.name not in registered]
            current = []
            for item in members:
                row = by_name.get(item["name"])
                state = ("host_missing" if row is None else
                         "identity_changed" if (row.endpoint != item["endpoint"]
                                                or row.lease_id != item["lease_id"]) else
                         "tower_already_opened" if (row.state == "running"
                                                    and row.name not in self.protected_names()
                                                    and self.registered(row)) else
                         self._state(row))
                current.append({**item, "state": state})
            return {"candidates": candidates, "members": current}

    def members(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(item) for item in self._members()]

    def member(self, name: str) -> dict[str, str] | None:
        return next((item for item in self.members() if item["name"] == name), None)

    def _probe(self, names: list[str], rows: dict[str, HostInstance]) -> set[str]:
        """Prove each stopped addition's Tower is unopened. Runs outside the lock:
        a boot takes minutes and snapshots must stay responsive meanwhile."""
        proven = set()
        for name in names:
            row = rows[name]
            if self._state(row) != "start_required":
                continue
            try:
                state = self.probe_stopped(row)
            except Exception as exc:
                raise RerollPoolError(f"tower_state_unavailable: {name}: {exc}") from exc
            code = {"installed_unopened": "ready", "opened": "tower_already_opened",
                    "not_installed": "tower_not_installed"}.get(state, "tower_state_unavailable")
            if code != "ready":
                raise RerollPoolError(f"{code}: {name}")
            proven.add(name)
        return proven

    def _new_entries(self, names: list[str], existing: set[str],
                     proven: set[str] | None = None,
                     rows: dict[str, HostInstance] | None = None) -> list[dict[str, str]]:
        """``proven`` is None for a dry-run validation; when committing, every
        stopped addition must be in it."""
        if (not names or len(set(names)) != len(names)
                or any(not isinstance(name, str)
                       or re.fullmatch(r"[A-Za-z0-9_]+", name) is None for name in names)):
            raise RerollPoolError("invalid_pool_members")
        if rows is None:
            rows = {row.name: row for row in self._rows()}
        entries = []
        for name in names:
            row = rows.get(name)
            if row is None:
                raise RerollPoolError("instance_not_installed")
            if name in existing:
                raise RerollPoolError("instance_already_in_pool")
            state = self._state(row)
            if state not in {"ready", "start_required"}:
                raise RerollPoolError(state)
            if state == "start_required" and proven is not None and name not in proven:
                raise RerollPoolError(f"tower_state_unverified: {name}")
            entries.append({"name": row.name, "endpoint": row.endpoint,
                            "lease_id": row.lease_id})
        return entries

    def validate_add(self, names: list[str]) -> None:
        with self._lock:
            self._new_entries(names, {item["name"] for item in self._members()})

    def _checked_additions(self, names: list[str]) -> tuple[set[str], dict[str, HostInstance]]:
        """Validate, then probe stopped additions -- one inventory read for both."""
        rows = {row.name: row for row in self._rows()}
        with self._lock:
            self._new_entries(names, {item["name"] for item in self._members()}, rows=rows)
        return self._probe(names, rows), rows

    def prove(self, names: list[str]) -> set[str]:
        """Validate and probe ``names`` now; pass the result to ``replace`` later."""
        return self._checked_additions(names)[0]

    def add(self, names: list[str]) -> None:
        proven, rows = self._checked_additions(names)
        with self._lock:
            members = self._members()
            members.extend(self._new_entries(names, {item["name"] for item in members},
                                             proven, rows))
            self._save(members)

    def replace(self, keep: list[str], add: list[str],
                proven: set[str] | None = None) -> None:
        """Rewrite membership as ``keep`` (entries unchanged) followed by ``add``.

        Returns ``None``: the pool file is saved first, and the caller must not
        lose that saved state to a later host-inventory read failing (a
        trailing ``snapshot()`` re-reads the host, which can fail after the
        write already landed).
        """
        if proven is not None:
            rows = None
        else:
            proven, rows = self._checked_additions(add) if add else (set(), {})
        with self._lock:
            by_name = {item["name"]: item for item in self._members()}
            if len(set(keep)) != len(keep) or any(name not in by_name for name in keep):
                raise RerollPoolError("instance_not_in_pool")
            new = self._new_entries(add, set(by_name), proven, rows) if add else []
            if not keep and not new:
                raise RerollPoolError("run_would_be_empty")
            self._save([by_name[name] for name in keep] + new)

    def remove(self, name: str) -> None:
        """Remove ``name`` from the pool and save. Returns ``None`` -- see ``replace``."""
        with self._lock:
            members = self._members()
            retained = [item for item in members if item["name"] != name]
            if len(retained) == len(members):
                raise RerollPoolError("instance_not_in_pool")
            self._save(retained)
