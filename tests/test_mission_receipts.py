from dataclasses import replace
import json

import db
import events
import ledger
import pytest


def test_receipt_index_migration_preserves_unkeyed_legacy_rows(tmp_path):
    path = tmp_path / 'legacy.db'
    conn = db.connect(path)
    conn.execute('DROP INDEX ledger_receipt_currency_idx')
    event = events.MissionClaimed(mission='Damage', coins=None, gems=3)
    for _ in range(2):
        for line in ledger.LedgerWriter(conn).lines_for(event):
            db.insert_ledger(conn,replace(line,seq=None).as_row())
    before = [tuple(row) for row in conn.execute('SELECT * FROM ledger ORDER BY id')]
    conn.close()
    migrated = db.connect(path)
    assert [tuple(row) for row in migrated.execute('SELECT * FROM ledger ORDER BY id')] == before
    assert len(before) == 4
    assert migrated.execute("SELECT 1 FROM sqlite_master WHERE name='ledger_receipt_currency_idx'").fetchone()
    migrated.close()


def test_receipt_currency_lines_deduplicate_across_independent_writers(tmp_path):
    path = tmp_path / 'bot.db'
    first, second = db.connect(path), db.connect(path)
    event = events.MissionClaimed(mission='Damage', coins=None, gems=3, receipt_key='receipt')
    rows1 = ledger.LedgerWriter(first).lines_for(event)
    rows2 = ledger.LedgerWriter(second).lines_for(event)
    for connection, rows in ((first, rows1), (second, rows2)):
        for row in rows:
            db.insert_ledger(connection, replace(row, seq=None).as_row())
    rows = first.execute("SELECT currency, delta FROM ledger WHERE kind='MISSION_CLAIM'").fetchall()
    assert [(r['currency'], r['delta']) for r in rows] == [('coins', None), ('gems', 3)]
    assert ledger.LedgerWriter(second).lines_for(event) == []
    first.close()
    second.close()


def receipt_source(tmp_path, *, chest=False, unknown=False):
    from account_state import AccountRepository, AccountState
    from evidence_scope import FactScope
    from fleet.identity import IdentityEvidence
    from notification_state import NotificationState
    root = tmp_path / 'worker'
    root.mkdir()
    path = root / 'bot.db'
    db.bind_account(path, 'acct')
    state = AccountState(AccountRepository(path))
    state.bind_scope(FactScope('acct','lease','a' * 32,0), identity=IdentityEvidence('acct',10.,'id'),runtime_root=root)
    (root / 'checkpoints').mkdir()
    binding = root / 'checkpoints' / f'{"a" * 32}.json'
    binding.write_text(json.dumps(dict(worker_id='worker', account_id='acct', lease_id='lease',
        attempt_id='attempt', generation='a' * 32, endpoint='endpoint', created_at=1.,observed_at=2.,evidence_ref='identity')))
    (root / 'fleet-registration.json').write_text(json.dumps(dict(account_id='acct',job_id='attempt',
        state='registered',instance='worker',endpoint='endpoint',lease_id='lease',binding=str(binding))))
    notifications = NotificationState(root / 'mission-notification-state.json',
                                     scope=dict(account_id='acct',lease_id='lease',generation='a' * 32,attempt_id='attempt',fact_epoch=0))
    notifications.begin('missions', 10.)
    if chest:
        notifications.prepare_chest(threshold=5, completed_before=7, now=10.)
        if unknown:
            notifications.observe_chest_reward(index=1, total=1, currency=None, amount=None,
                                               reward_text=None, final_tapped=True, now=10.5)
            claim = events.WeeklyChestClaimed(
                threshold=5, rewards=(), unreadable_rewards=1,
                reward_text=None, confirmation='chest_marked_claimed')
        else:
            notifications.observe_chest_reward(index=1, total=2, currency='coins', amount=300,
                                               reward_text='300 COINS', final_tapped=False, now=10.5)
            notifications.observe_chest_reward(index=2, total=2, currency='gems', amount=10,
                                               reward_text='10 GEMS', final_tapped=True, now=10.7)
            claim = events.WeeklyChestClaimed(
                threshold=5, rewards=(('coins', 300), ('gems', 10)),
                reward_text='300 COINS, 10 GEMS', confirmation='chest_marked_claimed')
        event = notifications.record_chest_receipt(claim, 11.)
        assert event is not None
        return root, path, state, event, event.receipt_key
    notifications.prepare_claim(mission='Damage',mission_id='d',coins=None,gems=3,
                                completed_before=1,completed_target=5,visible_before=[['d','Damage']],now=10.)
    event = events.MissionClaimed(mission='Damage',mission_id='d',coins=None,gems=3,completed_before=1,completed_after=2)
    assert notifications.record_receipt(event,11.)
    key = notifications.snapshot()['receipts'][0]['key']
    return root, path, state, event, key


def test_weekly_chest_receipt_replays_after_lost_event_without_duplicate(tmp_path):
    from mission_receipts import ingest_receipts
    root, path, state, event, key = receipt_source(tmp_path, chest=True)

    assert ingest_receipts(root, state, now=12.) == {key}
    assert ingest_receipts(root, state, now=13.) == {key}
    with db.connect(path) as conn:
        rows = conn.execute("SELECT currency,delta FROM ledger WHERE kind='WEEKLY_CHEST_CLAIM' "
                            "ORDER BY currency").fetchall()
        assert [(row['currency'], row['delta']) for row in rows] == [
            ('coins', 300), ('gems', 10)]
        assert ledger.LedgerWriter(conn).lines_for(event) == []


def test_unreadable_weekly_chest_receipt_replays_once_with_unknown_amount(tmp_path):
    from mission_receipts import ingest_receipts
    root, path, state, event, key = receipt_source(tmp_path, chest=True, unknown=True)

    assert ingest_receipts(root, state, now=12.) == {key}
    assert ingest_receipts(root, state, now=13.) == {key}
    with db.connect(path) as conn:
        rows = conn.execute("SELECT currency,delta,reason FROM ledger WHERE "
                            "kind='WEEKLY_CHEST_CLAIM'").fetchall()
        assert [(row['currency'], row['delta'], row['reason']) for row in rows] == [
            (None, None, '1 reward unreadable')]
        assert ledger.LedgerWriter(conn).lines_for(event) == []


def test_durable_receipt_ingestion_survives_lost_event_and_acks_after_commit(tmp_path, monkeypatch):
    from mission_receipts import ingest_receipts
    root, path, state, event, key = receipt_source(tmp_path)
    insert = db.insert_ledger
    def fail(*args, **kwargs):
        insert(*args, **kwargs)
        raise RuntimeError('interrupt')
    monkeypatch.setattr(db,'insert_ledger',fail)
    import pytest
    with pytest.raises(RuntimeError):
        ingest_receipts(root, state, now=12.)
    with db.reader(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM ledger').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM mission_receipt_ack').fetchone()[0] == 0
    monkeypatch.setattr(db,'insert_ledger',insert)
    assert ingest_receipts(root,state,now=12.) == {key}
    state.invalidate_scope('manual_play')
    assert state.verified_scope is None
    assert ingest_receipts(root,state,now=1000.) == {key}
    with db.connect(path) as conn:
        assert len(conn.execute('SELECT * FROM ledger').fetchall()) == 2
        assert ledger.LedgerWriter(conn).lines_for(replace(event,receipt_key=key)) == []
    # Sources, including unrelated unresolved intent archives, are never removed.
    assert (root / 'mission-notification-state.json').exists()


@pytest.mark.parametrize('mutation', [
    {'intent_id': None}, {'mission_id': 42}, {'mission': ''},
    {'completed_before': -2, 'completed_after': -1}, {'confirmed_at': 1.},
    {'prepared_at': .5, 'confirmed_at': 1.},
    {'prepared_at': 1.}, {'receipt_token': 'wrong'}, {'intent_scope': {}},
])
def test_malformed_or_pre_generation_receipt_cannot_credit(tmp_path, mutation):
    from mission_receipts import ingest_receipts
    root, path, state, _, _ = receipt_source(tmp_path)
    source = root / 'mission-notification-state.json'
    row = json.loads(source.read_text())
    row['receipts'][0].update(mutation)
    source.write_text(json.dumps(row))
    assert ingest_receipts(root,state,now=12.) == set()
    with db.reader(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM ledger').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM mission_receipt_ack').fetchone()[0] == 0


@pytest.mark.parametrize('changed', ['reward', 'intent', 'scope'])
def test_ack_rejects_same_key_with_changed_content_or_provenance(tmp_path, changed):
    from mission_receipts import ingest_receipts
    root, path, state, _, key = receipt_source(tmp_path)
    source = root / 'mission-notification-state.json'
    assert ingest_receipts(root,state,now=12.) == {key}
    row = json.loads(source.read_text())
    if changed == 'reward':
        row['receipts'][0]['gems'] = 4
    elif changed == 'intent':
        row['receipts'][0]['intent_id'] = 'f' * 32
    else:
        row['scope']['fact_epoch'] = 9
    source.unlink()
    (root / 'mission-notification-state-archive-conflict.json').write_text(json.dumps(row))
    assert ingest_receipts(root,state,now=13.) == set()
    with db.reader(path) as conn:
        assert [r[0] for r in conn.execute('SELECT delta FROM ledger ORDER BY id')] == [None, 3]


def test_background_reconciler_is_independent_and_stops(tmp_path, monkeypatch):
    import threading
    import mission_receipts
    from account_state import AccountState
    entered = threading.Event()
    threads = []
    def ingest(*args, **kwargs):
        threads.append(threading.current_thread().name)
        entered.set()
        return set()
    monkeypatch.setattr(mission_receipts, 'ingest_receipts', ingest)
    worker = mission_receipts.MissionReceiptReconciler(tmp_path, AccountState(), interval=100.)
    worker.start()
    assert entered.wait(1.)
    worker.stop()
    assert threads == ['mission-receipt-ledger']
    assert not worker._thread.is_alive()
