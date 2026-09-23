"""Numbered reroll runs over the pool. The pool says who plays; runs say which reroll."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from fleet.reroll_retirement import RetireResult


class RerollRunsError(ValueError):
    """Run state or a requested run change is unsafe."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso(epoch: float) -> str:
    return (datetime.fromtimestamp(epoch, timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def run_numbers_from(path: Path) -> dict[str, list[int]]:
    """Every run each emulator belonged to. Archives must render even if this file is bad."""
    try:
        runs = json.loads(Path(path).read_text(encoding="utf-8"))["runs"]
        numbers: dict[str, list[int]] = {}
        for run in runs:
            for name in run["members"]:
                numbers.setdefault(name, []).append(int(run["number"]))
        return {name: sorted(set(values)) for name, values in numbers.items()}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


class RerollRuns:
    def __init__(self, root: Path, *, pool: Any,
                 retire: Callable[[dict[str, str]], RetireResult],
                 stop_instance: Callable[[str, str, str], None],
                 clock: Callable[[], str] = _now) -> None:
        self.root = Path(root)
        self.path = self.root / "reroll-runs.json"
        self.pool = pool
        self.retire = retire
        self.stop_instance = stop_instance
        self.clock = clock
        self.busy = False
        self._lock = RLock()

    # -- storage -------------------------------------------------------
    @staticmethod
    def _valid_run(run: Any) -> bool:
        """Validate run structure: dict with required keys and types."""
        if not isinstance(run, dict):
            return False
        if not isinstance(run.get("number"), int) or isinstance(run.get("number"), bool):
            return False
        if not isinstance(run.get("name"), str) or not run["name"]:
            return False
        if run.get("status") not in {"active", "closed"}:
            return False
        if not isinstance(run.get("started_at"), str):
            return False
        if not isinstance(run.get("members"), list) or not all(isinstance(m, str) for m in run["members"]):
            return False
        if not isinstance(run.get("retired"), list):
            return False
        for item in run["retired"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("instance"), str):
                return False
        if not isinstance(run.get("retire_failed"), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in run["retire_failed"].items()):
            return False
        return True

    def _read(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RerollRunsError("runs_state_unreadable") from exc
        if not isinstance(value, dict) or not isinstance(value.get("runs"), list):
            raise RerollRunsError("runs_state_unreadable")
        if not isinstance(value.get("retired_before_runs", []), list) or not all(isinstance(name, str) for name in value.get("retired_before_runs", [])):
            raise RerollRunsError("runs_state_unreadable")
        seen_numbers = set()
        for run in value["runs"]:
            if not self._valid_run(run):
                raise RerollRunsError("runs_state_unreadable")
            if run["number"] in seen_numbers:
                raise RerollRunsError("runs_state_unreadable")
            seen_numbers.add(run["number"])
        if sum(run.get("status") == "active" for run in value["runs"]) > 1:
            raise RerollRunsError("runs_state_unreadable")
        value.setdefault("retired_before_runs", [])
        return value

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(state, file, sort_keys=True, indent=1)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _started_at(self, names: list[str]) -> str:
        stamps = []
        for name in names:
            path = self.root / "workers" / name / "fleet-registration.json"
            try:
                value = json.loads(path.read_text(encoding="utf-8")).get("registered_at")
                stamps.append(float(value) if value is not None else path.stat().st_mtime)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return _iso(min(stamps)) if stamps else self.clock()

    def _migrate(self) -> dict[str, Any]:
        names = [item["name"] for item in self.pool.members()]
        workers = self.root / "workers"
        legacy = sorted(path.name for path in workers.iterdir()
                        if (path / "fleet-registration.json").is_file()
                        and path.name not in names) if workers.is_dir() else []
        runs = [self._new_run(1, names, started_at=self._started_at(names))] if names else []
        return {"runs": runs, "retired_before_runs": legacy}

    def _new_run(self, number: int, members: list[str], *, started_at: str | None = None,
                 name: str | None = None, retire_failed: dict[str, str] | None = None) -> dict[str, Any]:
        return {"number": number, "name": name or f"Reroll #{number}", "status": "active",
                "started_at": started_at or self.clock(), "members": list(members),
                "retired": [], "retire_failed": dict(retire_failed or {})}

    @staticmethod
    def _active_of(state: dict[str, Any]) -> dict[str, Any] | None:
        return next((run for run in state["runs"] if run["status"] == "active"), None)

    @staticmethod
    def _live(run: dict[str, Any]) -> list[str]:
        retired = {item["name"] for item in run["retired"]}
        return [name for name in run["members"] if name not in retired]

    def _repair(self, state: dict[str, Any]) -> bool:
        pool = [item["name"] for item in self.pool.members()]
        active = self._active_of(state)
        if active is None:
            if not pool:
                return False
            number = max((run["number"] for run in state["runs"]), default=0) + 1
            state["runs"].append(self._new_run(number, pool))
            return True
        changed = False
        for name in pool:
            if name not in active["members"]:
                active["members"].append(name)
                changed = True
        for name in self._live(active):
            if name not in pool:
                # Retirement leaves the pool only after the worker is proven stopped.
                active["retired"].append({"name": name, "at": self.clock(), "reason": "recovered",
                                          "endpoint": "", "lease_id": "", "instance": "unknown"})
                active["retire_failed"].pop(name, None)
                changed = True
        return changed

    def _load(self, *, repair: bool = True) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            changed = state is None
            if state is None:
                state = self._migrate()
            if repair and not self.busy:
                changed = self._repair(state) or changed
            if changed:
                self._save(state)
            return state

    # -- queries -------------------------------------------------------
    def active(self) -> dict[str, Any] | None:
        return self._active_of(self._load())

    def summaries(self) -> list[dict[str, Any]]:
        return [{"number": run["number"], "name": run["name"], "status": run["status"],
                 "started_at": run["started_at"], "closed_at": run.get("closed_at"),
                 "member_count": len(run["members"]), "retired_count": len(run["retired"]),
                 "members": list(run["members"])}
                for run in sorted(self._load()["runs"], key=lambda run: -run["number"])]

    def retired_names(self) -> set[str]:
        state = self._load()
        return set(state["retired_before_runs"]) | {
            item["name"] for run in state["runs"] for item in run["retired"]}

    def retire_failures(self) -> dict[str, str]:
        active = self.active()
        return dict(active["retire_failed"]) if active else {}

    def stop_failures(self) -> list[dict[str, str]]:
        return [{"name": item["name"], "error": item.get("error", "")}
                for run in self._load()["runs"] for item in run["retired"]
                if item["instance"] == "stop_failed"]
