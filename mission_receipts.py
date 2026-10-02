"""Import fsynced mission receipts into the existing ledger, independently of EventBus."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Callable

from account_state import AccountState
import db
from evidence_scope import historical_scope_matches
import events
import ledger


def ingest_receipts(root: Path, state: AccountState, *, now: float,
                    cancelled: Callable[[], bool] | None = None) -> set[str]:
    """Commit receipt lines and durable acknowledgments together; never prune source intents.

    The ack table is a monotonic merge keyed by immutable receipt identity. A future
    source compactor may use it, but must independently retain unresolved intents.
    """
    current = state.scope
    if current is None or state.safety_path is None:
        return set()
    if db.bound_account(state.safety_path) != current.account_id:
        return set()
    with closing(db.connect(state.safety_path)) as conn, conn:
        conn.execute('CREATE TABLE IF NOT EXISTS mission_receipt_ack '
                     '(key TEXT PRIMARY KEY, scope TEXT NOT NULL, confirmed_at REAL NOT NULL, fingerprint TEXT)')
        if 'fingerprint' not in {row[1] for row in conn.execute('PRAGMA table_info(mission_receipt_ack)')}:
            conn.execute('ALTER TABLE mission_receipt_ack ADD COLUMN fingerprint TEXT')
    acknowledged: set[str] = set()
    paths = [root / 'mission-notification-state.json',
             *sorted(root.glob('mission-notification-state-archive-*.json'))]
    for path in paths:
        if cancelled is not None and cancelled():
            break
        try:
            row = json.loads(path.read_text())
            raw_scope = row['scope']
            if not isinstance(raw_scope, dict):
                continue
            if 'fact_epoch' in raw_scope and (type(raw_scope['fact_epoch']) is not int or raw_scope['fact_epoch'] < 0):
                continue
            if (not historical_scope_matches(root, raw_scope, current, destination_account=current.account_id)
                    or not isinstance(raw_scope.get('attempt_id'), str) or not raw_scope['attempt_id']):
                continue
            receipts = row['receipts']
            if not isinstance(receipts, list):
                continue
        except (OSError, ValueError, TypeError, KeyError):
            continue
        for receipt in receipts:
            if cancelled is not None and cancelled():
                return acknowledged
            if not _valid_receipt(receipt, now=now):
                continue
            if (not historical_scope_matches(root, raw_scope, current, destination_account=current.account_id,
                                             observed_at=receipt['confirmed_at'])
                    or not historical_scope_matches(root, receipt['intent_scope'], current,
                        destination_account=current.account_id, observed_at=receipt['prepared_at'])):
                continue
            key = receipt['key']
            fingerprint = hashlib.sha256(json.dumps({'scope':raw_scope,'receipt':receipt},
                sort_keys=True,separators=(',', ':'),allow_nan=False).encode()).hexdigest()
            # BEGIN IMMEDIATE serializes this ingestion with every other spender/ingestor.
            with closing(db.connect(state.safety_path)) as conn, conn:
                conn.execute('PRAGMA synchronous=FULL')
                conn.execute('BEGIN IMMEDIATE')
                if state.currencies.current_scope(conn) != current:
                    return acknowledged
                saved = conn.execute('SELECT scope,fingerprint FROM mission_receipt_ack WHERE key=?', (key,)).fetchone()
                scope_json = json.dumps(raw_scope, sort_keys=True)
                if saved:
                    if saved[0] == scope_json and saved[1] == fingerprint:
                        acknowledged.add(key)
                    else:
                        acknowledged.discard(key)
                    continue
                if receipt.get('kind') == 'weekly_chest':
                    event: events.Event = events.WeeklyChestClaimed(
                        threshold=receipt['threshold'],
                        rewards=tuple(tuple(reward) for reward in receipt['rewards']),
                        unreadable_rewards=receipt['unreadable_rewards'],
                        reward_text=receipt['reward_text'],
                        confirmation='chest_marked_claimed',
                        receipt_key=key, ts=receipt['confirmed_at'])
                else:
                    event = events.MissionClaimed(
                        mission=receipt['mission'], mission_id=receipt.get('mission_id'),
                        coins=receipt.get('coins'), gems=receipt.get('gems'),
                        completed_before=receipt['completed_before'], completed_after=receipt['completed_after'],
                        receipt_key=key, ts=receipt['confirmed_at'])
                for line in ledger.LedgerWriter(conn).lines_for(event):
                    detail = {**line.detail, 'receipt_scope': raw_scope,
                              'confirmed_at': receipt['confirmed_at'], 'receipt_fingerprint': fingerprint}
                    # Historical credits never establish an actionable current balance.
                    db.insert_ledger(conn, replace(line, seq=None, detail=detail,
                        balance_after=None, observed=None).as_row(), commit=False)
                for expected in ledger.classify(event):
                    currency = expected.currency
                    saved_line = conn.execute("SELECT delta,detail FROM ledger WHERE "
                        "json_extract(detail,'$.receipt_key')=? AND currency IS ?",
                        (key,currency)).fetchone()
                    if saved_line is None or saved_line['delta'] != expected.delta:
                        raise ValueError('receipt conflicts with ledger')
                    detail = json.loads(saved_line['detail'] or '{}')
                    if detail.get('receipt_scope', raw_scope) != raw_scope:
                        raise ValueError('receipt scope conflicts with ledger')
                    if detail.get('receipt_fingerprint', fingerprint) != fingerprint:
                        raise ValueError('receipt content conflicts with ledger')
                    conn.execute("UPDATE ledger SET detail=json_set(detail,'$.receipt_scope',json(?),"
                                 "'$.receipt_fingerprint',?) "
                                 "WHERE json_extract(detail,'$.receipt_key')=? AND currency IS ?",
                                 (scope_json,fingerprint,key,currency))
                conn.execute('INSERT INTO mission_receipt_ack (key,scope,confirmed_at,fingerprint) VALUES (?,?,?,?)',
                             (key, scope_json, receipt['confirmed_at'],fingerprint))
            acknowledged.add(key)
    return acknowledged


def _valid_receipt(value: Any, *, now: float) -> bool:
    valid = (isinstance(value, dict) and type(value.get('schema_version')) is int and value['schema_version'] == 1
            and all(isinstance(value.get(k),str) and re.fullmatch(r'[0-9a-f]{32}',value[k]) is not None
                    for k in ('intent_id','receipt_token'))
            and isinstance(value.get('key'), str) and re.fullmatch(r'[0-9a-f]{64}', value['key']) is not None
            and isinstance(value.get('intent_scope'),dict)
            and all(isinstance(value['intent_scope'].get(k),str) and value['intent_scope'][k]
                    for k in ('account_id','lease_id','attempt_id','generation'))
            and ('fact_epoch' not in value['intent_scope']
                 or type(value['intent_scope']['fact_epoch']) is int and value['intent_scope']['fact_epoch'] >= 0)
            and type(value.get('prepared_at')) in (int,float) and math.isfinite(value['prepared_at'])
            and type(value.get('confirmed_at')) in (int,float)
            and math.isfinite(value['confirmed_at']) and 0 < value['prepared_at'] <= value['confirmed_at'] <= now)
    if not valid:
        return False
    if value.get('kind') == 'weekly_chest':
        rewards = value.get('rewards')
        if (type(value.get('threshold')) is not int or value['threshold'] not in range(5, 36, 5)
                or not isinstance(rewards, list)
                or type(value.get('unreadable_rewards')) is not int
                or not 0 <= value['unreadable_rewards'] <= 8
                or len(rewards) + value['unreadable_rewards'] not in range(1, 9)
                or value.get('reward_text') is not None
                and not isinstance(value['reward_text'], str)
                or any(not isinstance(reward, list) or len(reward) != 2
                       or reward[0] not in {'coins', 'gems', 'medals', 'stones'}
                       or type(reward[1]) is not int or reward[1] < 0
                       for reward in rewards)):
            return False
        source = [value['receipt_token'], 'weekly_chest', value['threshold']]
        return value['key'] == hashlib.sha256(
            json.dumps(source, separators=(',', ':')).encode()).hexdigest()
    if (value.get('kind') is not None or not {'mission_id','coins','gems'} <= value.keys()
            or not isinstance(value.get('mission'), str) or not value['mission']
            or value.get('mission_id') is not None
            and (not isinstance(value['mission_id'], str) or not value['mission_id'])
            or type(value.get('completed_before')) is not int
            or type(value.get('completed_after')) is not int
            or value['completed_before'] < 0
            or value['completed_after'] != value['completed_before'] + 1
            or any(value.get(k) is not None
                   and (type(value[k]) is not int or value[k] < 0)
                   for k in ('coins', 'gems'))):
        return False
    source = [value['receipt_token'],value.get('mission_id'),value['mission'],
              value['completed_before'],value['completed_after']]
    return value['key'] == hashlib.sha256(json.dumps(source,separators=(',', ':')).encode()).hexdigest()


class MissionReceiptReconciler:
    """A non-input worker; receipt durability never depends on the scan/EventBus thread."""
    def __init__(self, root: Path, state: AccountState, *, interval: float = 5.) -> None:
        import threading
        self.root, self.state, self.interval = root, state, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='mission-receipt-ledger', daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=6.)
        if self._thread.is_alive():
            raise RuntimeError('receipt reconciliation shutdown incomplete')

    def _run(self) -> None:
        import logging
        import time
        while not self._stop.is_set():
            try:
                ingest_receipts(self.root, self.state, now=time.time(), cancelled=self._stop.is_set)
            except Exception:
                logging.getLogger(__name__).exception('Durable mission receipt reconciliation failed; source retained')
            self._stop.wait(self.interval)
