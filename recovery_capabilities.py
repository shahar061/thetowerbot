"""Operator-established, auditable recovery calibration. Empty until explicitly activated.

This is local operator authority, not proof that an arbitrary JSON claim is true.
Registration validates the supported contract; activation records the operator's
review of replay and real hardware evidence. Tests use isolated temporary stores.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import sqlite3
from contextlib import closing
from typing import Literal, Self

from recovery_status import bump_config_epoch
from pydantic import BaseModel, ConfigDict, Field, model_validator

HOST_CONTRACT = 'towerbot-dismiss-v1'


class CalibrationEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, allow_inf_nan=False)
    schema_version: Literal[1]
    host_contract: Literal['towerbot-dismiss-v1']
    action: Literal['close_overlay']
    model: str = Field(min_length=3, max_length=160)
    worker: str = Field(min_length=1, max_length=160)
    account_id: str = Field(min_length=1, max_length=160)
    screen: Literal['STALL_ESCAPE']
    target: Literal['CLOSE', 'OK', 'NO THANKS']
    replay_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    canary_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    canary_kind: Literal['recorded_hardware']
    replay_passed: Literal[True]
    semantic_postcondition: Literal['overlay_absent']
    canary_postcondition_confirmed: Literal[True]
    recorded_at: float = Field(ge=0)
    expires_at: float = Field(ge=0)
    operator: str = Field(min_length=1, max_length=160)

    @model_validator(mode='after')
    def lifetime(self) -> Self:
        if not self.recorded_at < self.expires_at <= self.recorded_at + 30 * 86400:
            raise ValueError('calibration_lifetime_invalid')
        return self

    def allows(self, *, worker: str, account_id: str, model: str, screen: str,
               target: str, action: str, now: float) -> bool:
        return (self.recorded_at <= now < self.expires_at
                and (worker, account_id, model, screen, target, action) ==
                (self.worker, self.account_id, self.model, self.screen, self.target, self.action))


class CapabilityRegistry:
    """SQLite I/O: use only from background storage or explicit operator tooling."""
    def __init__(self, root: Path) -> None:
        self.path = Path(root) / 'recovery-capabilities.sqlite3'

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, document TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS audit (id TEXT NOT NULL, operation TEXT NOT NULL, operator TEXT NOT NULL, at REAL NOT NULL)')
        return db

    @staticmethod
    def _time(now: float) -> None:
        if not math.isfinite(now) or now < 0:
            raise ValueError('invalid_calibration_time')

    def register(self, evidence: CalibrationEvidence, *, now: float) -> str:
        self._time(now)
        if not evidence.recorded_at <= now < evidence.expires_at:
            raise ValueError('calibration_expired_or_future')
        raw = evidence.model_dump_json()
        identifier = hashlib.sha256(raw.encode()).hexdigest()
        with closing(self._connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO evidence(id,document) VALUES (?,?)', (identifier, raw))
            db.execute('INSERT INTO audit VALUES (?,?,?,?)', (identifier, 'register', evidence.operator, now))
        return identifier

    def activate(self, identifier: str, *, operator: str, now: float) -> None:
        self._change(identifier, operator=operator, now=now, active=True)

    def revoke(self, identifier: str, *, operator: str, now: float) -> None:
        self._change(identifier, operator=operator, now=now, active=False)

    def _change(self, identifier: str, *, operator: str, now: float, active: bool) -> None:
        self._time(now)
        if not isinstance(operator, str) or not operator.strip() or len(operator) > 160:
            raise ValueError('operator_required')
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT document FROM evidence WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise ValueError('unknown_calibration')
            evidence = CalibrationEvidence.model_validate_json(row[0])
            if active and not evidence.recorded_at <= now < evidence.expires_at:
                raise ValueError('calibration_expired_or_future')
            db.execute('UPDATE evidence SET active=? WHERE id=?', (int(active), identifier))
            db.execute('INSERT INTO audit VALUES (?,?,?,?)',
                       (identifier, 'activate' if active else 'revoke', operator, now))
        bump_config_epoch(self.path.parent)  # After commit; see recovery_status.

    def active(self, *, now: float) -> tuple[CalibrationEvidence, ...]:
        self._time(now)
        if not self.path.exists():
            return ()
        with closing(self._connect()) as db:
            # The latest audit operation must also be an activation, so a
            # direct column edit without an audit trail never grants.
            rows = db.execute('''SELECT e.id, e.document FROM evidence e WHERE e.active=1
                AND (SELECT a.operation FROM audit a WHERE a.id = e.id
                     ORDER BY a.rowid DESC LIMIT 1) = 'activate' ''').fetchall()
        result = []
        for identifier, document in rows:
            try:
                item = CalibrationEvidence.model_validate_json(document)
            except ValueError:
                continue  # Incompatible/older contract or tampered row: never a grant.
            if (hashlib.sha256(item.model_dump_json().encode()).hexdigest() == identifier
                    and item.recorded_at <= now < item.expires_at):
                result.append(item)
        return tuple(result)

    def allowed_actions(self, *, model: str, now: float) -> tuple[str, ...]:
        """Action classes with active validated evidence for ``model``."""
        return tuple(sorted({g.action for g in self.active(now=now) if g.model == model}))

    def audit(self) -> tuple[tuple[str, str, str, float], ...]:
        if not self.path.exists():
            return ()
        with closing(self._connect()) as db:
            return tuple(db.execute('SELECT id, operation, operator, at FROM audit ORDER BY rowid'))
