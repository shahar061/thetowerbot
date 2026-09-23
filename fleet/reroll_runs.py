"""Numbered reroll runs over the pool. The pool says who plays; runs say which reroll.

Emulators leave a run with their bot stopped and can always be added back.
``retired`` entries and ``retired_before_runs`` are read only for history.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
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
                 release: Callable[[dict[str, str]], RetireResult],
                 remove: Callable[[dict[str, str]], RetireResult],
                 clock: Callable[[], str] = _now,
                 assign: Callable[[str], str] | None = None) -> None:
        self.root = Path(root)
        self.path = self.root / "reroll-runs.json"
        self.pool = pool
        self.release = release
        self.remove = remove
        self.clock = clock
        self.assign = assign
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
        # "removed" was added later; files written before it have no such key.
        if not isinstance(run.get("removed", []), list) or not all(isinstance(m, str) for m in run.get("removed", [])):
            return False
        # "hidden" (dashboard-only) was added later too.
        if not isinstance(run.get("hidden", []), list) or not all(isinstance(m, str) for m in run.get("hidden", [])):
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
        for run in value["runs"]:
            run.setdefault("removed", [])
            run.setdefault("hidden", [])
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
                "retired": [], "removed": [], "hidden": [], "retire_failed": dict(retire_failed or {})}

    @staticmethod
    def _active_of(state: dict[str, Any]) -> dict[str, Any] | None:
        return next((run for run in state["runs"] if run["status"] == "active"), None)

    @staticmethod
    def _live(run: dict[str, Any]) -> list[str]:
        out = {item["name"] for item in run["retired"]} | set(run["removed"])
        return [name for name in run["members"] if name not in out]

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
            if name in active["removed"]:
                # The run records a removal before the pool drops the member; a
                # member still in the pool was never removed (it's just paused).
                active["removed"].remove(name)
                changed = True
        for name in self._live(active):
            if name not in pool:
                # A member leaves the pool only after its worker is proven stopped.
                active["removed"].append(name)
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
                 "member_count": len(run["members"]),
                 "left_count": len(run["retired"]) + len(run["removed"]),
                 "members": list(run["members"])}
                for run in sorted(self._load()["runs"], key=lambda run: -run["number"])]

    def leave_failures(self) -> dict[str, str]:
        """Members whose bot couldn't be proven stopped when a new run started."""
        active = self.active()
        return dict(active["retire_failed"]) if active else {}

    def hidden_names(self) -> set[str]:
        """Members the dashboard leaves off its device list; they still run."""
        active = self.active()
        return set(active["hidden"]) if active else set()

    # -- mutations -----------------------------------------------------
    def validate_new(self, keep: list[str], add: list[str]) -> None:
        state = self._load()
        active = self._active_of(state)
        live = self._live(active) if active else []
        if len(set(keep)) != len(keep) or any(name not in live for name in keep):
            raise RerollRunsError("keep_not_in_active_run")
        if not keep and not add:
            raise RerollRunsError("run_would_be_empty")
        if add:
            self.pool.validate_add(add)

    def _try_release(self, name: str) -> tuple[dict[str, str] | None, RetireResult | dict[str, str]]:
        member = self.pool.member(name)
        if member is None:
            return None, {"name": name, "error": "instance_not_in_pool"}
        try:
            return member, self.release(member)
        except Exception as exc:
            return None, {"name": name, "error": str(exc) or type(exc).__name__}

    def _assign_variants(self, names: list[str]) -> tuple[dict[str, str], dict[str, str]]:
        """Give each newly added emulator its opening variant.

        After the run is saved, so a failure never undoes a run: the emulator
        plays the build's own caps and the caller journals why.
        """
        assigned: dict[str, str] = {}
        errors: dict[str, str] = {}
        if self.assign is None:
            return assigned, errors
        for name in names:
            try:
                assigned[name] = self.assign(name)
            except (OSError, ValueError) as exc:
                errors[name] = str(exc) or type(exc).__name__
        return assigned, errors

    def start_new(self, keep: list[str], add: list[str], name: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self.busy:
                raise RerollRunsError("reroll_start_in_progress")
            self.validate_new(keep, add)
            self.busy = True
        try:
            # Prove additions (a stopped one is booted) before stopping anyone's
            # bot, so a rejected addition leaves the old run playing.
            proven = self.pool.prove(add) if add else set()
            active = self._active_of(self._load(repair=False))
            leaving = [item for item in (self._live(active) if active else []) if item not in keep]
            with ThreadPoolExecutor(max_workers=max(1, min(4, len(leaving)))) as workers:
                outcomes = list(workers.map(self._try_release, leaving))
            failed = {result["name"]: result["error"] for member, result in outcomes if member is None}
            with self._lock:
                state = self._load(repair=False)
                active = self._active_of(state)
                old_flags = dict(active["retire_failed"]) if active is not None else {}
                if active is not None:
                    active["removed"].extend(member["name"] for member, _ in outcomes
                                             if member is not None)
                # A failed name still has to be a pool member to be carried forward --
                # _try_release can report "instance_not_in_pool" for a name that never
                # made it into self.pool at all, and pool.replace would reject it.
                carried = list(keep) + [item for item in leaving
                                        if item in failed and self.pool.member(item) is not None]
                try:
                    self.pool.replace(carried, add, proven)
                except ValueError:
                    self._save(state)   # stopped bots stay recorded; the old run stays active
                    raise
                number = max((run["number"] for run in state["runs"]), default=0) + 1
                if active is not None:
                    active["status"] = "closed"
                    active["closed_at"] = self.clock()
                    active["retire_failed"] = {}
                retire_failed = {item: failed[item] for item in carried if item in failed}
                retire_failed.update({kept: old_flags[kept] for kept in keep if kept in old_flags})
                state["runs"].append(self._new_run(number, carried + list(add), name=name,
                                                   retire_failed=retire_failed))
                self._save(state)
            variants, variant_errors = self._assign_variants(add)
            return {"number": number, "results": [result for _, result in outcomes],
                    "variants": variants, "variant_errors": variant_errors}
        finally:
            self.busy = False

    def validate_remove(self, name: str) -> None:
        active = self.active()
        if active is None or name not in self._live(active):
            raise RerollRunsError("instance_not_in_active_run")

    def remove_member(self, name: str) -> RetireResult:
        """Take ``name`` out of the active run so it can be added back later."""
        with self._lock:
            if self.busy:
                raise RerollRunsError("reroll_start_in_progress")
            self.validate_remove(name)
            self.busy = True
        try:
            member = self.pool.member(name)
            if member is None:
                raise RerollRunsError("instance_not_in_pool")
            result = self.remove(member)
            with self._lock:
                # Record the removal before the pool drops the member, so a crash in
                # between reads the same once _repair runs.
                state = self._load(repair=False)
                active = self._active_of(state)
                assert active is not None
                active["removed"].append(name)
                active["retire_failed"].pop(name, None)
                self._save(state)
                self.pool.remove(name)
                return result
        finally:
            self.busy = False

    def add_members(self, names: list[str]) -> None:
        with self._lock:
            if self.busy:
                raise RerollRunsError("reroll_start_in_progress")
            if self.active() is None:
                self.start_new([], names)
                return
            self.pool.add(names)
            state = self._load(repair=False)
            active = self._active_of(state)
            for name in names:
                if name in active["hidden"]:
                    active["hidden"].remove(name)
                if name in active["removed"]:
                    active["removed"].remove(name)
                elif name not in active["members"]:
                    active["members"].append(name)
            self._save(state)

    def set_hidden(self, names: list[str], hidden: bool) -> None:
        """Hide or restore members on the dashboard only; nothing is stopped.

        Not refused while an operation runs: every mutation reloads the file
        under the lock before saving, so this can't clobber one.
        """
        with self._lock:
            state = self._load()
            active = self._active_of(state)
            if active is None:
                raise RerollRunsError("no_active_run")
            live = self._live(active)
            if not names or any(name not in live for name in names):
                raise RerollRunsError("instance_not_in_active_run")
            if hidden:
                active["hidden"].extend(dict.fromkeys(n for n in names if n not in active["hidden"]))
            else:
                active["hidden"] = [n for n in active["hidden"] if n not in names]
            self._save(state)
