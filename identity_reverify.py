"""Bounded in-process account re-verification.

A worker verifies its account once at start. Later, reconciliation of a
pending purchase needs an identity observed within the last 30 s, and a
mid-run reconnect clears the verified account entirely. This pacer runs the
same observed-control walk (Home -> Settings -> Account -> Home) again, never
more often than ``min_spacing`` and with exponential backoff after failures,
so a blocked identity can neither freeze the worker nor tap in a loop.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)


class IdentityReverifier:
    def __init__(self, verify: Callable[[], None], *,
                 clock: Callable[[], float] = time.monotonic,
                 min_spacing: float = 60., max_backoff: float = 1800.) -> None:
        self._verify = verify
        self._clock = clock
        self.min_spacing = min_spacing
        self.max_backoff = max_backoff
        self._next_at: float | None = None
        self.failures = 0
        self.attempts = 0
        self.status = 'idle'
        self.last_error: str | None = None
        self.last_reason: str | None = None

    def due(self) -> bool:
        return self._next_at is None or self._clock() >= self._next_at

    def retry_in(self) -> float | None:
        if self._next_at is None:
            return None
        return max(0., self._next_at - self._clock())

    def attempt(self, reason: str) -> bool:
        """Run one bounded walk when due. True means the device was used."""
        if not self.due():
            return False
        self.attempts += 1
        self.last_reason = reason
        try:
            self._verify()
        except Exception as exc:  # noqa: BLE001 - every failure is paced, never fatal
            self.failures += 1
            self.last_error = str(exc)[:200] or type(exc).__name__
            self.status = 'failed'
            delay = min(self.max_backoff, self.min_spacing * 2 ** self.failures)
            self._next_at = self._clock() + delay
            logger.warning('account re-verification for %s failed (%s); retry in %.0fs',
                           reason, self.last_error, delay)
            return True
        self.failures = 0
        self.last_error = None
        self.status = 'verified'
        self._next_at = self._clock() + self.min_spacing
        return True

    def snapshot(self) -> dict[str, object]:
        return {'status': self.status, 'attempts': self.attempts, 'failures': self.failures,
                'last_error': self.last_error, 'last_reason': self.last_reason,
                'retry_in_seconds': self.retry_in()}
