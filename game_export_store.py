"""Durable, read-only reporting projections from official game exports.

This store never writes worker events, actions or spending evidence. Its synchronous
SQLite methods should be called in a thread from async collectors and routes.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any

import db
from game_export import parse_export


STALE_AFTER_SECONDS = 660
ESTIMATE_HORIZON_SECONDS = 300
_SECTIONS = ('versions', 'wallet', 'labs', 'workshop', 'cards', 'current_run',
             'battle_history', 'completed_labs')
_SCHEMA = """
CREATE TABLE IF NOT EXISTS export_snapshots (
    id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, digest TEXT NOT NULL,
    endpoint TEXT NOT NULL, scope_key TEXT NOT NULL, exported_at REAL NOT NULL,
    captured_at REAL NOT NULL, raw TEXT NOT NULL, normalized TEXT NOT NULL,
    UNIQUE(account_id,digest));
CREATE INDEX IF NOT EXISTS export_snapshot_account ON export_snapshots(account_id,id);
CREATE TABLE IF NOT EXISTS export_reports (
    account_id TEXT NOT NULL, report_id TEXT NOT NULL, battle_at REAL,
    detail TEXT NOT NULL, PRIMARY KEY(account_id,report_id));
CREATE TABLE IF NOT EXISTS export_journal (
    account_id TEXT NOT NULL, event_id TEXT NOT NULL, observed_to REAL NOT NULL,
    detail TEXT NOT NULL, PRIMARY KEY(account_id,event_id));
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _number(value: Any) -> bool:
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def _identifier(account: str, kind: str, value: Any) -> str:
    return hashlib.sha256(_json([account, kind, value]).encode()).hexdigest()


def _report_identity(report: dict[str, Any]) -> Any:
    # The export has no native report id. These immutable battle coordinates are
    # independent of rolling-history position and of subsequently corrected loot.
    fields = ('battle_at', 'tier', 'wave', 'elapsed_seconds', 'game_elapsed_seconds')
    identity = {key: report.get(key) for key in fields}
    return identity if report.get('battle_at') is not None else report.get('raw', report)


def _meaningful(section: str, value: Any) -> Any:
    if section != 'labs' or not isinstance(value, dict):
        return value
    # Remaining time and percent tick every export; only actual research/queue
    # changes belong in the account journal.
    return {'research': [{key: row.get(key) for key in ('index', 'id', 'level')}
                         for row in value.get('research', [])],
            'slots': [{key: row.get(key) for key in ('slot', 'index', 'id', 'active', 'level')}
                      for row in value.get('slots', [])],
            'unlocked_raw': value.get('unlocked_raw')}


class ExportStore:
    """Account-partitioned export storage alongside an explicitly bound worker DB.

    Construction and reads never create or migrate either database. New trusted
    source generations may advance an account only with newer exporter evidence.
    """

    def __init__(self, db_path: Path | str, account_id: str) -> None:
        self.db_path = Path(db_path)
        self.path = self.db_path.parent / 'game-export.sqlite3'
        self.account_id = account_id

    def _bound(self) -> bool:
        try:
            return bool(self.account_id and db.bound_account(self.db_path) == self.account_id)
        except sqlite3.DatabaseError:
            return False

    def ingest(self, raw: bytes | str | dict[str, Any], *, endpoint: str,
               scope_key: str, captured_at: float) -> bool:
        """Accept a chronological snapshot; duplicate/rejected captures are no-ops."""
        if (not self._bound() or not isinstance(endpoint, str) or not endpoint.strip()
                or not isinstance(scope_key, str) or not scope_key.strip()
                or not _number(captured_at)):
            return False
        try:
            text = raw.decode('utf-8') if isinstance(raw, bytes) else raw if isinstance(raw, str) else _json(raw)
            normalized = parse_export(raw)
            exported_at = normalized.get('exported_at')
            if not _number(exported_at) or exported_at > captured_at + 30:
                return False
            encoded = _json(normalized)
            digest = hashlib.sha256(text.encode()).hexdigest()
        except (ValueError, TypeError, UnicodeError, KeyError):
            return False
        with closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT * FROM export_snapshots WHERE account_id=? ORDER BY id DESC LIMIT 1',
                                    (self.account_id,)).fetchone()
            if previous and (exported_at <= previous['exported_at'] or captured_at < previous['captured_at']):
                return False
            if conn.execute('SELECT 1 FROM export_snapshots WHERE account_id=? AND digest=?',
                            (self.account_id, digest)).fetchone():
                return False
            # Re-check explicit binding immediately before storing reporting data.
            if not self._bound():
                return False
            conn.execute('INSERT INTO export_snapshots(account_id,digest,endpoint,scope_key,exported_at,captured_at,raw,normalized) VALUES (?,?,?,?,?,?,?,?)',
                         (self.account_id, digest, endpoint, scope_key, exported_at, captured_at, text, encoded))
            for report in normalized.get('battle_history', []):
                report_id = _identifier(self.account_id, 'battle', _report_identity(report))
                detail = {**report, 'id': report_id, 'account_id': self.account_id,
                          'source': 'game_export', 'exported_at': exported_at,
                          'endpoint': endpoint, 'scope_key': scope_key, 'source_key': digest,
                          'report': report.get('raw', report)}
                conn.execute('INSERT INTO export_reports(account_id,report_id,battle_at,detail) VALUES (?,?,?,?) ON CONFLICT(account_id,report_id) DO UPDATE SET detail=excluded.detail',
                             (self.account_id, report_id, report.get('battle_at'), _json(detail)))
            if previous:
                before = json.loads(previous['normalized'])
                changes = {section: {'before': _meaningful(section, before.get(section)),
                                     'after': _meaningful(section, normalized.get(section))}
                           for section in ('wallet', 'labs', 'workshop', 'cards')
                           if _meaningful(section, before.get(section)) != _meaningful(section, normalized.get(section))}
                if changes:
                    self._journal(conn, _identifier(self.account_id, 'snapshot', digest),
                                  kind='snapshot_change', observed_from=previous['exported_at'],
                                  observed_to=exported_at, occurred_at=None, changes=changes,
                                  endpoint=endpoint, scope_key=scope_key, source_key=digest,
                                  summary='Account changes observed between game exports')
            for lab in normalized.get('completed_labs', []):
                completed_at = lab.get('completed_at')
                if not _number(completed_at) or completed_at > exported_at + 30:
                    continue
                identity = [lab.get('index'), lab.get('level'), completed_at]
                self._journal(conn, _identifier(self.account_id, 'lab', identity),
                              kind='lab_completed', observed_from=None, observed_to=exported_at,
                              occurred_at=completed_at, changes=lab,
                              endpoint=endpoint, scope_key=scope_key, source_key=digest,
                              summary=f"Lab completed: {lab.get('name') or lab.get('id') or lab.get('index')}")
        return True

    def _journal(self, conn: sqlite3.Connection, event_id: str, **values: Any) -> None:
        detail = {'id': event_id, 'account_id': self.account_id, 'source': 'game_export', **values}
        conn.execute('INSERT OR IGNORE INTO export_journal(account_id,event_id,observed_to,detail) VALUES (?,?,?,?)',
                     (self.account_id, event_id, values['observed_to'], _json(detail)))

    def _unavailable(self, warning: str | None = None) -> dict[str, Any]:
        return {'available': False, 'account_id': self.account_id, 'source': 'game_export',
                'exported_at': None, 'captured_at': None, 'age_seconds': None,
                'stale': True, 'warnings': [warning] if warning else [],
                'runs': [], 'journal': [], **{key: None for key in _SECTIONS}}

    def projection(self, now: float | None = None) -> dict[str, Any]:
        """Read reporting state without migration or refreshing evidence age."""
        if not self._bound():
            return self._unavailable('Worker account binding does not match export account')
        if not self.path.exists():
            return self._unavailable()
        now = time.time() if now is None else now
        if not _number(now):
            raise ValueError('Invalid projection timestamp')
        try:
            with db.reader(self.path) as conn:
                snapshots = conn.execute('SELECT * FROM export_snapshots WHERE account_id=? ORDER BY id DESC LIMIT 2',
                                         (self.account_id,)).fetchall()
                if not snapshots:
                    return self._unavailable()
                latest = snapshots[0]
                normalized = json.loads(latest['normalized'])
                age = max(0.0, now - latest['exported_at'])
                result = {**normalized, 'available': True, 'account_id': self.account_id,
                          'source': 'game_export', 'exported_at': latest['exported_at'],
                          'captured_at': latest['captured_at'], 'age_seconds': age,
                          'endpoint': latest['endpoint'], 'scope_key': latest['scope_key'],
                          'stale': age > STALE_AFTER_SECONDS,
                          'runs': [json.loads(row[0]) for row in conn.execute(
                              'SELECT detail FROM export_reports WHERE account_id=? ORDER BY battle_at DESC,report_id LIMIT 500', (self.account_id,))],
                          'journal': [json.loads(row[0]) for row in conn.execute(
                              "SELECT detail FROM export_journal WHERE account_id=? ORDER BY COALESCE(json_extract(detail,'$.occurred_at'),observed_to) DESC,event_id LIMIT 200", (self.account_id,))]}
                current = dict(normalized.get('current_run') or {})
                current.update(observed_wave=current.get('wave'), estimated_wave=None, estimate_label=None)
                if len(snapshots) == 2 and not result['stale']:
                    current['estimated_wave'] = _estimate(snapshots[1], latest, now)
                    if current['estimated_wave'] is not None:
                        current['estimate_label'] = 'Estimated'
                result['current_run'] = current
                return result
        except (sqlite3.DatabaseError, ValueError, TypeError, KeyError):
            return self._unavailable('Export reporting store is unavailable or incompatible')


def _estimate(previous: sqlite3.Row, latest: sqlite3.Row, now: float) -> int | None:
    if (previous['scope_key'] != latest['scope_key'] or previous['endpoint'] != latest['endpoint']
            or not 0 < now - latest['exported_at'] <= ESTIMATE_HORIZON_SECONDS):
        return None
    old = json.loads(previous['normalized']).get('current_run') or {}
    new = json.loads(latest['normalized']).get('current_run') or {}
    if old.get('active') is not True or new.get('active') is not True or old.get('tier') != new.get('tier') or new.get('tier') is None:
        return None
    keys = ('wave', 'elapsed_seconds', 'game_elapsed_seconds')
    if any(not _number(sample.get(key)) for sample in (old, new) for key in keys):
        return None
    interval = latest['exported_at'] - previous['exported_at']
    elapsed = new['elapsed_seconds'] - old['elapsed_seconds']
    if (not 0 < interval <= STALE_AFTER_SECONDS or elapsed <= 0 or abs(elapsed - interval) > 30
            or new['game_elapsed_seconds'] <= old['game_elapsed_seconds']
            or new['wave'] <= old['wave']):
        return None
    return int(new['wave'] + (new['wave'] - old['wave']) / interval * (now - latest['exported_at']))


def read_export(db_path: Path | str, account_id: str, now: float | None = None) -> dict[str, Any]:
    return ExportStore(db_path, account_id).projection(now=now)
