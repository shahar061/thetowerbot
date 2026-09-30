"""The shared pieces of a fleet-wide staged rollout: dry run, canary, fleet, halted.

A rollout lives in one JSON file under the fleet root. Workers change it only
under an exclusive OS file lock, and read-modify-write the whole document so a
change to one key never drops another. A missing, unreadable or corrupt file
reads as nothing recorded, which every rollout treats as dry run: nothing taps.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterator, Mapping, TypeVar

from fleet.build_route_store import _write_json_atomic

logger = logging.getLogger(__name__)

STAGES = ("dry_run", "canary", "fleet", "halted")
PROMOTION_SPACING_SECONDS = 600.
MAX_EVIDENCE = 32

T = TypeVar("T")


class RolloutError(ValueError):
    """An owner action that does not apply to the current stage."""


def canary_present(fleet_root: Path, worker: str, account_id: str | None) -> bool | None:
    """Whether `worker` is still a pool member bound to `account_id`. None when unreadable."""
    from web.account_catalog import registered_worker
    try:
        members = json.loads((Path(fleet_root) / "reroll-pool.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    if not isinstance(members, list):
        return None
    if not any(isinstance(item, dict) and item.get("name") == worker for item in members):
        return False
    registration = registered_worker(Path(fleet_root) / "workers" / worker)
    if registration is None:
        return False
    return account_id is None or registration.account_id == account_id


class LockedJsonFile:
    """One rollout document, its lock file, and how a bad copy is set aside."""

    def __init__(self, path: Path, lock_path: Path, label: str) -> None:
        self.path, self.lock_path, self.label = Path(path), Path(lock_path), label

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _corrupt(self, quarantine: bool, exc: Exception) -> None:
        if quarantine:
            aside = self.path.with_name(f"{self.path.name}.corrupt-{time.time_ns()}")
            os.replace(self.path, aside)
            logger.warning("%s %s is corrupt (%s); moved to %s; everything is back at dry run",
                           self.label, self.path, exc, aside)
        else:
            logger.warning("%s %s is corrupt (%s); reading everything as dry run",
                           self.label, self.path, exc)

    def load(self, parse: Callable[[Any], T], *, quarantine: bool,
             for_write: bool = False) -> T | None:
        """The parsed document, or None when missing, unreadable or corrupt.

        `for_write` re-raises an OSError: writing back after a failed read would
        erase every record the read could not see, a halt included.
        """
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeDecodeError as exc:
            self._corrupt(quarantine, exc)
            return None
        except OSError as exc:
            if for_write:
                raise
            logger.warning("%s %s unreadable (%s); everything is at dry run", self.label, self.path, exc)
            return None
        try:
            return parse(json.loads(text))
        except (ValueError, TypeError, AttributeError) as exc:
            self._corrupt(quarantine, exc)
            return None

    def write(self, document: Mapping[str, Any]) -> None:
        _write_json_atomic(self.path, dict(document))
