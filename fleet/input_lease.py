"""Durable generation fence checked immediately before device input."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


class InputLeaseExpired(RuntimeError):
    """The process no longer owns permission to send device input."""


class InputLease:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise InputLeaseExpired("input lease unreadable") from exc
        if (not isinstance(value, dict) or not isinstance(value.get("generation"), str)
                or not value["generation"] or type(value.get("current")) is not bool):
            raise InputLeaseExpired("input lease invalid")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def grant(self, generation: str) -> None:
        if not generation:
            raise ValueError("generation required")
        with self._locked():
            self._read()
            self._write({"generation": generation, "current": True, "reason": None})

    def revoke(self, generation: str, reason: str) -> None:
        if not generation or not reason:
            raise ValueError("generation and reason required")
        with self._locked():
            current = self._read()
            if current is None or current["generation"] != generation:
                raise InputLeaseExpired("input generation changed")
            self._write({"generation": generation, "current": False, "reason": reason})

    def rotate(self, old_generation: str, new_generation: str) -> None:
        """Let a stopped in-process scan owner move to a fresh attempt."""
        if not old_generation or not new_generation or old_generation == new_generation:
            raise ValueError("distinct generations required")
        with self._locked():
            current = self._read()
            if (current is None or not current["current"]
                    or current["generation"] != old_generation):
                raise InputLeaseExpired("input generation revoked")
            self._write({"generation": new_generation, "current": True, "reason": None})

    def handoff(self, revoked_generation: str, new_generation: str) -> None:
        """Grant a replacement only from the exact fenced generation."""
        if (not revoked_generation or not new_generation
                or revoked_generation == new_generation):
            raise ValueError("distinct generations required")
        with self._locked():
            current = self._read()
            if (current is None or current["current"]
                    or current["generation"] != revoked_generation):
                raise InputLeaseExpired("input generation changed")
            self._write({"generation": new_generation, "current": True, "reason": None})

    def assert_current(self, generation: str) -> None:
        with self._locked():
            current = self._read()
            if (current is None or not current["current"]
                    or current["generation"] != generation):
                raise InputLeaseExpired("input generation revoked")
