"""Durable, fail-closed recovery of one verified ADB endpoint and action."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from device import EmulatorError, IdentityError, endpoint_matches


class RecoveryState(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class RecoveryStatus:
    state: RecoveryState
    reason: str
    attempts: int
    pending_action: bool
    serial: str | None
    retry_at: float | None


class RecoveryBlocked(EmulatorError):
    """No fresh evidence authorizes a device action."""


class DeviceSupervisor:
    """Own a reconnectable transport and a write-ahead action checkpoint.

    A changed post-action frame is evidence only that the previous tap has
    landed somewhere. Purchase confirmation remains the B05 journal's job.
    """

    def __init__(
        self, *, path: Path, endpoint: str, connect: Callable[[], Any],
        expected_account: str | None, clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep, max_attempts: int = 3,
        base_backoff: float = 1.0, game_package: str | None = None,
        quarantine_on_exhaustion: bool = False,
    ) -> None:
        if not endpoint or max_attempts < 1 or base_backoff < 0:
            raise ValueError("valid endpoint and bounded retry policy required")
        self.path = Path(path)
        self.endpoint = endpoint
        self.connect = connect
        self.expected_account = expected_account
        self.clock = clock
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.game_package = game_package
        self.quarantine_on_exhaustion = quarantine_on_exhaustion
        self._device: Any | None = None
        self._connected_at: float | None = None
        self._state = RecoveryState.BLOCKED
        self._reason = "awaiting_device"
        self._attempts = 0
        self._next_retry_at: float | None = None
        self._serial: str | None = None
        self._last_digest: str | None = None
        self._last_observed_at: float | None = None
        self._pending_digest: str | None = None
        self.current_account: str | None = None
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data["endpoint"] != self.endpoint or data["expected_account"] != self.expected_account:
                raise ValueError("identity checkpoint mismatch")
            self._pending_digest = data.get("pending_digest")
            self._last_digest = data.get("last_digest")
            self._serial = data.get("serial")
            self._attempts = int(data.get("attempts", 0))
            if not 0 <= self._attempts <= self.max_attempts:
                raise ValueError("invalid retry budget")
            retry_at = data.get("next_retry_at")
            self._next_retry_at = float(retry_at) if retry_at is not None else None
            if self._next_retry_at is not None and (
                not math.isfinite(self._next_retry_at) or self._next_retry_at < 0
            ):
                raise ValueError("invalid retry deadline")
            if self._pending_digest is not None and not isinstance(self._pending_digest, str):
                raise ValueError("invalid pending action")
            if data.get("state") == RecoveryState.QUARANTINED.value:
                self._state = RecoveryState.QUARANTINED
                self._reason = str(data.get("reason", "quarantined"))
            else:
                self._state = RecoveryState.BLOCKED
                if data.get("reason") == "device_unavailable":
                    self._reason = "device_unavailable"
                else:
                    self._reason = "restart_evidence_required" if self._pending_digest else "fresh_evidence_required"
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            self._state = RecoveryState.QUARANTINED
            self._reason = "invalid_checkpoint"
            self._attempts = 0
            self._next_retry_at = None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "endpoint": self.endpoint, "expected_account": self.expected_account,
            "serial": self._serial, "state": self._state.value,
            "reason": self._reason, "attempts": self._attempts,
            "next_retry_at": self._next_retry_at,
            "pending_digest": self._pending_digest,
            "last_digest": self._last_digest,
        }
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(data, file, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def status(self) -> RecoveryStatus:
        return RecoveryStatus(self._state, self._reason, self._attempts,
                              self._pending_digest is not None, self._serial,
                              self._next_retry_at)

    def disconnected(self) -> None:
        self._device = None
        self._connected_at = None
        self.current_account = None
        self._attempts = 0
        self._next_retry_at = None
        if self._state is not RecoveryState.QUARANTINED:
            self._state = RecoveryState.BLOCKED
            self._reason = "device_disconnected"
            self._save()

    def recover(self) -> RecoveryState:
        """Reconnect at most max_attempts times; never adopt another serial."""
        if self._state is RecoveryState.QUARANTINED:
            return self._state
        if self._attempts >= self.max_attempts:
            if self.quarantine_on_exhaustion:
                self._state, self._reason = RecoveryState.QUARANTINED, "host_recovery_exhausted"
                self._save()
            return self._state
        while self._attempts < self.max_attempts:
            if self._next_retry_at is not None:
                remaining = self._next_retry_at - self.clock()
                if remaining > 0:
                    self.sleep(remaining)
                self._next_retry_at = None
            self._attempts += 1
            try:
                device = self.connect()
            except IdentityError:
                self._state, self._reason = RecoveryState.QUARANTINED, "host_identity_mismatch"
                self._save()
                return self._state
            except Exception:  # noqa: BLE001 - any ADB failure is bounded here
                self._state = RecoveryState.BLOCKED
                self._reason = "device_unavailable"
                if self._attempts < self.max_attempts:
                    self._next_retry_at = self.clock() + self.base_backoff * 2 ** (self._attempts - 1)
                self._save()
                continue
            serial = getattr(device, "serial", None)
            if not endpoint_matches(self.endpoint, serial):
                self._device = None
                self._state = RecoveryState.QUARANTINED
                self._reason = "wrong_device"
                self._save()
                return self._state
            self._device = device
            self._connected_at = self.clock()
            self.current_account = None
            self._attempts = 0
            self._next_retry_at = None
            self._serial = serial
            self._state = RecoveryState.BLOCKED
            self._reason = "fresh_evidence_required"
            if self.game_package is not None:
                try:
                    current = device.app_current()
                    package = getattr(current, "package", None)
                    if package != self.game_package:
                        device.app_start(self.game_package)
                        self._reason = "game_relaunched"
                except Exception:  # noqa: BLE001 - no guessing on an unreadable app state
                    self._reason = "game_state_unavailable"
                    self._save()
                    return self._state
            self._save()
            return self._state
        if self.quarantine_on_exhaustion:
            self._state, self._reason = RecoveryState.QUARANTINED, "host_recovery_exhausted"
            self._save()
        return self._state

    def observe(
        self, *, frame_digest: str, observed_at: float, screen: str,
        account_id: str | None, online_required: bool = False,
        readable: bool = True, session_conflict: bool = False,
    ) -> RecoveryState:
        """Authorize the next action only from a current, identified frame."""
        if self._state is RecoveryState.QUARANTINED:
            return self._state
        if session_conflict:
            self._state, self._reason = RecoveryState.QUARANTINED, "session_conflict"
        elif self._device is None:
            self._state, self._reason = RecoveryState.BLOCKED, "device_unavailable"
        elif (not frame_digest or not math.isfinite(observed_at)
              or observed_at > self.clock() or self.clock() - observed_at > 5
              or (self._last_observed_at is not None and observed_at <= self._last_observed_at)):
            self._state, self._reason = RecoveryState.BLOCKED, "stale_frame"
        elif not readable:
            self._state, self._reason = RecoveryState.BLOCKED, "unreadable_frame"
        elif online_required:
            self._state, self._reason = RecoveryState.BLOCKED, "online_required"
        elif not screen or screen == "UNKNOWN":
            self._state, self._reason = RecoveryState.BLOCKED, "unknown_screen"
        elif account_id is None or self.expected_account is None:
            self._state, self._reason = RecoveryState.BLOCKED, "account_unverified"
        elif account_id != self.expected_account:
            self._state, self._reason = RecoveryState.QUARANTINED, "wrong_account"
        elif self._pending_digest == frame_digest:
            self._state, self._reason = RecoveryState.BLOCKED, "stale_frame"
        else:
            self._pending_digest = None
            self._state, self._reason = RecoveryState.READY, "fresh_evidence"
        if frame_digest and math.isfinite(observed_at):
            self._last_digest = frame_digest
            self._last_observed_at = observed_at
        self._save()
        return self._state

    def verify_account(self, account_id: str, *, observed_at: float) -> None:
        """Bind this live connection only to the account already expected."""
        if self._device is None or self._state is RecoveryState.QUARANTINED:
            raise RecoveryBlocked("device is not available for identity verification")
        if (self._connected_at is None or not math.isfinite(observed_at)
                or observed_at < self._connected_at or observed_at > self.clock()
                or self.clock() - observed_at > 5):
            raise RecoveryBlocked("fresh account evidence required")
        if not account_id.strip():
            raise ValueError("account id is required")
        if self.expected_account is not None and account_id != self.expected_account:
            self._state, self._reason = RecoveryState.QUARANTINED, "wrong_account"
            self._save()
            raise RecoveryBlocked("wrong account")
        self.expected_account = account_id
        self.current_account = account_id
        self._save()

    def invalidate_identity(self, reason: str) -> None:
        """Refuse actions if verified evidence could not be durably bound."""
        self.current_account = None
        if self._state is not RecoveryState.QUARANTINED:
            self._state, self._reason = RecoveryState.BLOCKED, reason
        self._save()

    def _action(self, perform: Callable[[], None]) -> None:
        """Checkpoint one input before sending it to the verified endpoint."""
        if (self._state is not RecoveryState.READY or self._device is None
                or self._pending_digest is not None or self._last_digest is None
                or self._last_observed_at is None
                or self.clock() - self._last_observed_at > 5):
            raise RecoveryBlocked(self._reason)
        self._pending_digest = self._last_digest
        self._state, self._reason = RecoveryState.BLOCKED, "action_unconfirmed"
        self._save()
        try:
            perform()
        except Exception as exc:  # noqa: BLE001 - the tap may have landed
            self.disconnected()
            raise RecoveryBlocked("action outcome unknown") from exc

    def tap(self, x: int, y: int) -> None:
        """Checkpoint the action before issuing exactly one ADB click."""
        self._action(lambda: self._device.click(x, y))

    def swipe(self, x: int, y: int, x2: int, y2: int, duration: float) -> None:
        """A panel scroll has the same evidence and replay guard as a tap."""
        self._action(lambda: self._device.swipe(x, y, x2, y2, duration))

    @property
    def device(self) -> Any | None:
        return self._device


class GuardedDevice:
    """Present the usual adbutils surface while input uses the supervisor."""

    def __init__(self, supervisor: DeviceSupervisor) -> None:
        self.supervisor = supervisor

    @property
    def serial(self) -> str | None:
        return self.supervisor.status().serial

    def screenshot(self, **kwargs: Any) -> Any:
        device = self.supervisor.device
        if device is None:
            raise RecoveryBlocked("device unavailable")
        try:
            return device.screenshot(**kwargs)
        except Exception as exc:  # noqa: BLE001 - transport may have died
            self.supervisor.disconnected()
            raise RecoveryBlocked("screencap failed") from exc

    def click(self, x: int, y: int) -> None:
        self.supervisor.tap(x, y)

    def swipe(self, x: int, y: int, x2: int, y2: int, duration: float) -> None:
        self.supervisor.swipe(x, y, x2, y2, duration)
