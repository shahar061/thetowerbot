from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
import hashlib
import time

import cv2
import pytest

import config
import pages
import shopping
import vision
from account_state import AccountRepository, AccountState
from evidence_scope import FactScope
from fleet.identity import IdentityEvidence
from perception import Observation, ObservedUpgrade
from shopping import PendingCard, ShoppingSession
from strategy import Strategy
from transactions import TransactionJournal


def make_session(tmp_path: Path) -> ShoppingSession:
    session = ShoppingSession(vision.TemplateCache(config.TEMPLATE_DIR), Mock(), Mock(),
                              journal=TransactionJournal(tmp_path / 'bot.db'))
    session.account_state = AccountState(AccountRepository(session.journal.path))
    session.account_state.bind_scope(FactScope('acct', 'lease', 'generation', 0),
        identity=IdentityEvidence('acct', time.time(), 'identity'))
    session._reader.read.return_value = 20
    return session


def screen(name: str):
    return cv2.imread(str(Path('tests/fixtures') / (name + '.png')))


def test_pending_home_inspection_only_navigates_and_pause_holds(tmp_path: Path) -> None:
    sut = make_session(tmp_path)
    txn = sut._open_intent(item='Damage', category='ATTACK', currency='coins', price=30,
                           wallet_before=100, armed=True, before={"observed_at":time.time(),"frame_digest":"before"})
    sut._mark_acted(txn)
    device = Mock()
    home = screen('menu_main')
    assert home is not None
    assert sut.inspect(home, device, paused=True)
    device.click.assert_not_called()
    assert sut.inspect(home, device)
    assert device.click.call_count == 1
    assert sut.journal.open_transactions()[0].key == txn.key
    assert sut.currencies.committed('coins') == 30


def test_exact_card_debit_is_not_independent_semantics(tmp_path: Path) -> None:
    sut = make_session(tmp_path)
    txn = sut._open_intent(item='x1', category='CARDS', currency='gems', price=20,
                          wallet_before=50, armed=True, before={"observed_at":time.time(),"frame_digest":"before"})
    sut._mark_acted(txn)
    sut._pending_card = PendingCard('x1', 20, 50, key=txn.key)
    sut._confirm_card(30, Mock(), Strategy.from_config().shopping, screen('menu_cards'))
    assert sut.journal.open_transactions()[0].key == txn.key
    assert sut.currencies.committed('gems') == 20
    assert sut._bought == 0


def test_cards_capability_unavailable_before_new_spend(tmp_path: Path, monkeypatch) -> None:
    sut = make_session(tmp_path)
    policy = Strategy.from_config().shopping
    policy = replace(policy, armed=True, cards=replace(policy.cards, enabled=True))
    device = Mock()
    monkeypatch.setattr(shopping, 'header_numbers', lambda *args: (100, 100))
    sut._buy_cards(Mock(page='CARDS', top_left=(0, 0)), screen('menu_cards'), device, policy)
    device.click.assert_not_called()
    assert not sut.journal.open_transactions()


def test_real_restart_account_home_inspection_settles_original_once(tmp_path: Path, monkeypatch) -> None:
    import json
    import db
    from evidence_scope import BalanceInterval
    from fleet.account_creation import AccountFrame
    from fleet.restart_account import verify_restart_account
    from fleet.input_lease import InputLease
    from supervisor import GuardedDevice, RecoveryState
    from transactions import Intent

    clock = [90.]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    root = tmp_path / 'worker'
    root.mkdir()
    path = root / 'bot.db'
    db.bind_account(path, 'acct')
    old_scope = FactScope('acct','lease','a'*32,0)
    current = replace(old_scope,generation='b'*32)
    (root/'checkpoints').mkdir()
    binding = dict(worker_id='worker',account_id='acct',lease_id='lease',attempt_id='attempt',
                   endpoint='endpoint',created_at=1.,observed_at=2.,evidence_ref='original')
    for scope, previous in ((old_scope,None),(current,old_scope.generation)):
        (root/'checkpoints'/f'{scope.generation}.json').write_text(json.dumps({**binding,
            'generation':scope.generation,'predecessor_generation':previous}))
    (root/'fleet-registration.json').write_text(json.dumps(dict(account_id='acct',job_id='attempt',
        state='registered',instance='worker',endpoint='endpoint',lease_id='lease',
        binding=str(root/'checkpoints'/f'{current.generation}.json'))))
    page = screen('menu_workshop_attack')
    before = dict(upgrade_id='damage',value=9,price=30,status='available',confidence=.99,
        observed_at=90.,frame_digest='before',frame_width=page.shape[1],frame_height=page.shape[0])
    journal = TransactionJournal(path)
    journal.currencies.bind_scope(old_scope)
    txn = journal.prepare(Intent(item='Damage',category='ATTACK',currency='coins',price=30,
        wallet_before=100,ts=90.,before=before),scope=old_scope,
        balance=BalanceInterval('coins',100,100,old_scope,90.,'before'))
    journal.record_action(txn.key,at=91.)
    lease = InputLease(root/'input-lease.json')
    lease.grant(current.generation)
    clock[0] = 100.
    class Supervisor:
        def __init__(self):
            self.taps = []
        def verify_account(self, account_id, **kwargs):
            assert account_id == 'acct'
        def observe(self, **kwargs):
            return RecoveryState.READY
        def tap(self,x,y):
            lease.assert_current(current.generation)
            self.taps.append((x,y))
    supervisor = Supervisor()
    device = GuardedDevice(supervisor)
    def af(name,controls=None,account_id=None):
        return AccountFrame(name,account_id,'29.0','digest-'+name,100.,'capture://'+name,
            controls or {},None,'ACCOUNT' if name=='account' else None,'ID:' if name=='account' else None)
    frames = iter([af('home',{'settings':(1,2)}),af('settings',{'account':(3,4)}),
        af('account',{'close':(5,6)},'acct'),af('settings',{'close':(7,8)}),af('home')])
    identity = verify_restart_account(device=device,observe=lambda _:next(frames),
        supervisor=supervisor,expected_account='acct',clock=lambda:clock[0],sleep=lambda _:None)
    sut = ShoppingSession(vision.TemplateCache(config.TEMPLATE_DIR),Mock(),Mock(),journal=journal)
    sut.account_state = AccountState(AccountRepository(path))
    sut.account_state.bind_scope(current,identity=identity,runtime_root=root)
    assert sut.inspect(screen('menu_main'),device)
    assert len(supervisor.taps) == 5  # Four identity controls, one Workshop nav.
    assert journal.open_transactions()[0].key == txn.key
    monkeypatch.setattr(shopping,'_heading_names',lambda *args:True)
    monkeypatch.setattr(shopping,'header_numbers',lambda *args:(70,0))
    def observe(image,*args):
        clock[0] += .1
        row = ObservedUpgrade('damage','Damage','ATTACK','workshop',10.,55,'available',
            clock[0],config.Rect(0,300,400,200),(100,450),confidence=.99)
        return Observation('ATTACK',(row,),{},None,clock[0],270,context='workshop',
            frame_digest=hashlib.sha256(image.tobytes()).hexdigest(),
            frame_width=image.shape[1],frame_height=image.shape[0])
    monkeypatch.setattr(shopping,'observe_frame',observe)
    assert sut.inspect(page,device)
    assert sut.reconciliation_pending
    assert sut.inspect(page,device)
    assert not sut.reconciliation_pending
    assert sut.currencies.committed('coins') == 0
    assert sut._bought == 1 and sut._spent == 30
    assert len(supervisor.taps) == 5
    assert not sut.inspect(page,device)
    with db.reader(path) as conn:
        assert conn.execute("SELECT count(*) FROM ledger WHERE kind='WORKSHOP_BUY'").fetchone()[0] == 1


@pytest.mark.parametrize('fault',['stale_identity','wrong_account','invalidated_epoch'])
def test_pending_inspection_retains_reservation_without_identity(tmp_path, fault) -> None:
    sut = make_session(tmp_path)
    txn = sut._open_intent(item='Damage',category='ATTACK',currency='coins',price=30,
        wallet_before=100,armed=True,before={'observed_at':time.time(),'frame_digest':'before'})
    sut._mark_acted(txn)
    scope = sut.account_state.verified_scope
    if fault == 'invalidated_epoch':
        sut.account_state.invalidate_scope('manual play')
    else:
        account = 'other' if fault == 'wrong_account' else 'acct'
        sut.account_state.bind_scope(replace(scope,account_id=account),
            identity=IdentityEvidence(account,time.time()-100 if fault=='stale_identity' else time.time(),'id'))
    device = Mock()
    # Held reconciliation releases the scan (battles continue); the
    # reservation and the spending block stay.
    assert not sut.inspect(screen('menu_main'),device)
    assert sut._inspection.hold_reason in ('identity_stale', 'scope_continuity_unavailable')
    device.click.assert_not_called()
    assert sut.journal.open_transactions()[0].key == txn.key
    assert sut.currencies.committed('coins') == 30
    assert sut.reconciliation_pending


def test_unknown_goal_gets_read_only_observations_ahead_of_unrelated_cards(tmp_path, monkeypatch) -> None:
    import db
    from fleet.reroll_progress import RerollProgress
    from tests.test_perception import recorded
    from perception import parse_frame
    sut = make_session(tmp_path)
    db.bind_account(tmp_path/'tower_bot.db','acct')
    planner = RerollProgress(tmp_path,'acct',sut.account_state)
    sut.reroll_observe_prices = planner.observe_prices
    sut.inspection_resolved = planner.inspection_resolved
    policy = Strategy.from_config().shopping
    sut.begin(replace(policy,enabled=True,armed=True,workshop=(),
        cards=replace(policy.cards,enabled=True)),1)
    class Progress:
        def __init__(self):
            self.row = {'observation_due':True,'since_utc':1.}
        def snapshot(self):
            return {'capabilities':{'workshop':self.row}}
        def observe_capability(self,name,outcome,evidence,timeout):
            self.row['observation_due'] = outcome != 'progress'
    progress = Progress()
    device = Mock()
    assert sut.inspect(screen('menu_main'),device,progress=progress)
    for category in ('attack','defense','utility'):
        if not progress.row['observation_due']:
            break
        name = 'menu_workshop_'+category
        monkeypatch.setattr(shopping,'_heading_names',lambda image,expected,c=category:expected.lower()==c)
        monkeypatch.setattr(shopping,'observe_frame',lambda image,*args,n=name:
            parse_frame(image,recorded(n),'workshop'))
        monkeypatch.setattr(shopping,'header_numbers',lambda *args:(100,100))
        for _ in range(2):
            assert sut.inspect(screen(name),device,progress=progress)
    assert not progress.row['observation_due']
    assert planner.price_memory.entries['damage']['price'] == 30
    assert sut.account_state.snapshot()['revision'] is not None
    assert device.click.call_count == 1  # Navigation only, never unrelated Cards spending.
    assert sut.journal.open_transactions() == ()



def test_inspection_navigates_original_category_without_tapping_a_row(tmp_path, monkeypatch) -> None:
    sut = make_session(tmp_path)
    txn = sut._open_intent(item='Health',category='DEFENSE',currency='coins',price=30,
        wallet_before=100,armed=True,before={'observed_at':time.time(),'frame_digest':'before'})
    sut._mark_acted(txn)
    monkeypatch.setattr(shopping,'_heading_names',lambda *args:False)
    device = Mock()
    sut.inspect(screen('menu_workshop_attack'),device)
    assert device.click.call_count == 1
    assert device.click.call_args.args[1] > 1700  # Tab control below the upgrade rows.
    assert sut.journal.open_transactions()[0].key == txn.key
    assert sut.currencies.committed('coins') == 30


def test_inspection_reader_failure_does_not_send_escape_or_release_pending(tmp_path, monkeypatch) -> None:
    sut = make_session(tmp_path)
    txn = sut._open_intent(item='Damage',category='ATTACK',currency='coins',price=30,
        wallet_before=100,armed=True,before={'observed_at':time.time(),'frame_digest':'before'})
    sut._mark_acted(txn)
    monkeypatch.setattr(shopping,'_heading_names',lambda *args:True)
    def broken(*args):
        raise RuntimeError('reader unavailable')
    monkeypatch.setattr(shopping,'observe_frame',broken)
    device = Mock()
    assert sut.inspect(screen('menu_workshop_attack'),device)
    device.click.assert_not_called()
    assert sut.journal.open_transactions()[0].key == txn.key
    assert sut.currencies.committed('coins') == 30


class DueProgress:
    def __init__(self, reason: str = 'offscreen prerequisite') -> None:
        self.row = {'observation_due':True,'since_utc':1.,'evidence_ref':reason}

    def snapshot(self) -> dict:
        return {'capabilities':{'workshop':self.row}}

    def observe_capability(self, name: str, outcome: str, evidence: str, timeout: float) -> None:
        self.row['observation_due'] = outcome != 'progress'


def test_due_offscreen_prerequisite_is_found_by_guarded_read_only_sweep(tmp_path, monkeypatch) -> None:
    from supervisor import GuardedDevice
    from tests.test_perception import recorded
    from perception import parse_frame
    sut = make_session(tmp_path)
    progress = DueProgress()
    page = screen('menu_workshop_attack')
    base = parse_frame(page,recorded('menu_workshop_attack'),'workshop')
    found = next(r for r in base.rows if r.upgrade_id=='damage')
    viewport = {'lower':False,'now':time.time()}
    class Supervisor:
        def __init__(self) -> None:
            self.swipes = []
        def swipe(self,*args) -> None:
            self.swipes.append(args)
            viewport['lower'] = args[3] < args[1]
        def tap(self,*args) -> None:
            pytest.fail('inspection must not purchase or navigate from its current category')
    transport = Supervisor()
    device = GuardedDevice(transport)
    def observation(image,*args):
        viewport['now'] += .01
        # The requested Damage row starts below the current viewport.
        rows = (found,) if viewport['lower'] else tuple(r for r in base.rows if r.upgrade_id!='damage')
        return replace(base,observed_at=viewport['now'],rows=tuple(replace(r,
            observed_at=viewport['now']) for r in rows))
    monkeypatch.setattr(shopping,'_heading_names',lambda *args:True)
    monkeypatch.setattr(shopping,'observe_frame',observation)
    monkeypatch.setattr(shopping,'header_numbers',lambda *args:(100,0))
    sut.inspection_resolved = lambda:any(f.concept_id=='stats.damage' for f in sut.account_state.actionable_facts())
    for _ in range(12):
        if not progress.row['observation_due']:
            break
        sut.inspect(page,device,progress=progress)
    assert transport.swipes and not progress.row['observation_due']
    assert any(f.concept_id=='stats.damage' for f in sut.account_state.actionable_facts())
    assert sut.journal.open_transactions() == ()


def test_exhausted_due_inspection_backs_off_and_changed_request_restarts(tmp_path, monkeypatch) -> None:
    sut = make_session(tmp_path)
    progress = DueProgress('first missing target')
    clock = [time.time()]
    monkeypatch.setattr(time,'time',lambda:clock[0])
    sut.inspection_resolved = lambda:False
    monkeypatch.setattr(shopping,'_heading_names',lambda *args:False)
    device = Mock()
    page = screen('menu_workshop_attack')
    for _ in range(35):
        sut.inspect(page,device,progress=progress)
    count = device.click.call_count
    assert count <= 16
    assert not sut.inspect(page,device,progress=progress)
    assert not sut.inspection_requested(progress)
    assert device.click.call_count == count
    progress.row['evidence_ref'] = 'different target'
    assert sut.inspection_requested(progress)
    assert sut.inspect(page,device,progress=progress)
    assert device.click.call_count == count+1
    # A resolved request releases ownership even if an old helper key exists.
    progress.row['observation_due'] = False
    assert not sut.inspect(page,device,progress=progress)
    assert not sut.inspection_requested(progress)


def test_due_inspection_retries_after_backoff_without_request_timestamp_reset(tmp_path, monkeypatch) -> None:
    sut = make_session(tmp_path)
    progress = DueProgress()
    clock = [time.time()]
    monkeypatch.setattr(time,'time',lambda:clock[0])
    monkeypatch.setattr(shopping,'_heading_names',lambda *args:False)
    device = Mock()
    for _ in range(35):
        sut.inspect(screen('menu_workshop_attack'),device,progress=progress)
    assert not sut.inspection_requested(progress)
    clock[0] += 31
    assert sut.inspection_requested(progress)
    assert sut.inspect(screen('menu_workshop_attack'),device,progress=progress)
    assert progress.row['since_utc'] == 1.



def test_pending_spend_keeps_authority_after_exhaustion_or_resolved_due_request(tmp_path, monkeypatch) -> None:
    sut = make_session(tmp_path)
    txn = sut._open_intent(item='Damage',category='ATTACK',currency='coins',price=30,
        wallet_before=100,armed=True,before={'observed_at':time.time(),'frame_digest':'before'})
    sut._mark_acted(txn)
    progress = DueProgress()
    monkeypatch.setattr(shopping,'_heading_names',lambda *args:False)
    device = Mock()
    page = screen('menu_workshop_attack')
    owned = [sut.inspect(page,device,progress=progress) for _ in range(35)]
    # Sixteen bounded steps own the scan; then the search is held and the
    # scan is released while the reservation stays.
    assert owned[:16] == [True]*16 and not any(owned[16:])
    assert sut._inspection.hold_reason == 'held_pending'
    assert device.click.call_count == 16
    progress.row['observation_due'] = False
    assert sut.inspection_requested(progress)
    assert not sut.inspect(page,device,progress=progress)
    assert device.click.call_count == 16
    assert sut.journal.open_transactions()[0].key == txn.key
    assert sut.currencies.committed('coins') == 30


def _restart_fixture(tmp_path: Path, monkeypatch, *, invalidate: bool):
    """The real-restart Workshop reconciliation fixture (shared by C1/C2 regressions)."""
    import json
    import db
    from evidence_scope import BalanceInterval
    from fleet.account_creation import AccountFrame
    from fleet.restart_account import verify_restart_account
    from fleet.input_lease import InputLease
    from supervisor import GuardedDevice, RecoveryState
    from transactions import Intent

    clock = [90.]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    root = tmp_path / 'worker'
    root.mkdir()
    path = root / 'bot.db'
    db.bind_account(path, 'acct')
    old_scope = FactScope('acct', 'lease', 'a'*32, 0)
    current = replace(old_scope, generation='b'*32)
    (root/'checkpoints').mkdir()
    binding = dict(worker_id='worker', account_id='acct', lease_id='lease', attempt_id='attempt',
                   endpoint='endpoint', created_at=1., observed_at=2., evidence_ref='original')
    for scope, previous in ((old_scope, None), (current, old_scope.generation)):
        (root/'checkpoints'/f'{scope.generation}.json').write_text(json.dumps({**binding,
            'generation': scope.generation, 'predecessor_generation': previous}))
    (root/'fleet-registration.json').write_text(json.dumps(dict(account_id='acct', job_id='attempt',
        state='registered', instance='worker', endpoint='endpoint', lease_id='lease',
        binding=str(root/'checkpoints'/f'{current.generation}.json'))))
    page = screen('menu_workshop_attack')
    before = dict(upgrade_id='damage', value=9, price=30, status='available', confidence=.99,
        observed_at=90., frame_digest='before', frame_width=page.shape[1], frame_height=page.shape[0])
    journal = TransactionJournal(path)
    journal.currencies.bind_scope(old_scope)
    txn = journal.prepare(Intent(item='Damage', category='ATTACK', currency='coins', price=30,
        wallet_before=100, ts=90., before=before), scope=old_scope,
        balance=BalanceInterval('coins', 100, 100, old_scope, 90., 'before'))
    journal.record_action(txn.key, at=91.)
    if invalidate:
        # The uncertain-tap/disconnect path bumps the durable epoch to N+1.
        state = AccountState(AccountRepository(path))
        state.bind_scope(old_scope, identity=IdentityEvidence('acct', 90., 'id'))
        state.invalidate_scope('device_disconnected')
        current = replace(current, epoch=state.persisted_epoch)
        assert current.epoch == 1
    lease = InputLease(root/'input-lease.json')
    lease.grant(current.generation)
    clock[0] = 100.

    class Supervisor:
        def __init__(self):
            self.taps = []

        def verify_account(self, account_id, **kwargs):
            assert account_id == 'acct'

        def observe(self, **kwargs):
            return RecoveryState.READY

        def tap(self, x, y):
            lease.assert_current(current.generation)
            self.taps.append((x, y))

    supervisor = Supervisor()
    device = GuardedDevice(supervisor)

    def af(name, controls=None, account_id=None):
        return AccountFrame(name, account_id, '29.0', 'digest-'+name, clock[0], 'capture://'+name,
            controls or {}, None, 'ACCOUNT' if name == 'account' else None,
            'ID:' if name == 'account' else None)

    def walk():
        frames = iter([af('home', {'settings': (1, 2)}), af('settings', {'account': (3, 4)}),
            af('account', {'close': (5, 6)}, 'acct'), af('settings', {'close': (7, 8)}), af('home')])
        return verify_restart_account(device=device, observe=lambda _: next(frames),
            supervisor=supervisor, expected_account='acct', clock=lambda: clock[0],
            sleep=lambda _: None)

    sut = ShoppingSession(vision.TemplateCache(config.TEMPLATE_DIR), Mock(), Mock(), journal=journal)
    sut.account_state = AccountState(AccountRepository(path))
    sut.account_state.bind_scope(current, identity=walk(), runtime_root=root)
    monkeypatch.setattr(shopping, '_heading_names', lambda *args: True)
    monkeypatch.setattr(shopping, 'header_numbers', lambda *args: (70, 0))

    def observe(image, *args):
        clock[0] += .1
        row = ObservedUpgrade('damage', 'Damage', 'ATTACK', 'workshop', 10., 55, 'available',
            clock[0], config.Rect(0, 300, 400, 200), (100, 450), confidence=.99)
        return Observation('ATTACK', (row,), {}, None, clock[0], 270, context='workshop',
            frame_digest=hashlib.sha256(image.tobytes()).hexdigest(),
            frame_width=image.shape[1], frame_height=image.shape[0])

    monkeypatch.setattr(shopping, 'observe_frame', observe)

    def rebind():
        sut.account_state.bind_scope(current, identity=walk(), runtime_root=root)

    return sut, device, supervisor, page, clock, path, txn, rebind


def _assert_settled_once(sut, path) -> None:
    import db
    assert not sut.reconciliation_pending
    assert sut.currencies.committed('coins') == 0
    assert sut._bought == 1 and sut._spent == 30
    with db.reader(path) as conn:
        assert conn.execute("SELECT count(*) FROM ledger WHERE kind='WORKSHOP_BUY'").fetchone()[0] == 1


def test_epoch_bumped_pending_reconciles_read_only_after_reverified_identity(tmp_path, monkeypatch) -> None:
    """C2: a disconnect epoch bump no longer makes the intent unreconcilable."""
    sut, device, supervisor, page, clock, path, txn, _ = _restart_fixture(
        tmp_path, monkeypatch, invalidate=True)
    assert sut.inspect(screen('menu_main'), device)
    assert len(supervisor.taps) == 5  # Four identity controls, one Workshop nav.
    for _ in range(4):
        if not sut.reconciliation_pending:
            break
        assert sut.inspect(page, device)
    _assert_settled_once(sut, path)
    assert len(supervisor.taps) == 5  # Reconciliation stays read-only.
    assert not sut.inspect(page, device)


def test_epoch_advance_continuity_requires_same_account_and_lease(tmp_path, monkeypatch) -> None:
    from evidence_scope import verified_continuity
    sut, *_ = _restart_fixture(tmp_path, monkeypatch, invalidate=True)
    state = sut.account_state
    current, identity = state.verified_scope, state._identity
    original = replace(current, generation='a'*32, epoch=0)
    now = time.time()
    assert state.continuity(original, now=now).epoch_advance
    for forged in (replace(original, account_id='other'), replace(original, lease_id='other'),
                   replace(original, epoch=2)):
        assert state.continuity(forged, now=now) is None
    assert verified_continuity(state._runtime_root, original, current, identity=identity,
                               now=now) is None  # Strict mode never crosses an epoch.


def test_stale_identity_is_reverified_in_process_and_pending_settles(tmp_path, monkeypatch) -> None:
    """C1 restart variant: identity older than 30 s is re-proved, not held forever."""
    from identity_reverify import IdentityReverifier
    sut, device, supervisor, page, clock, path, txn, rebind = _restart_fixture(
        tmp_path, monkeypatch, invalidate=False)
    assert sut.inspect(screen('menu_main'), device)
    assert len(supervisor.taps) == 5
    clock[0] += 31  # A startup popup or a scroll search took longer than 30 s.
    assert not sut.inspect(page, device)  # Released: battles continue, spending held.
    assert sut._inspection.hold_reason == 'identity_stale'
    assert sut.reconciliation_pending
    sut.identity_reverifier = IdentityReverifier(rebind, clock=lambda: clock[0])
    assert sut.inspect(screen('menu_main'), device)  # Bounded identity walk at Home.
    assert len(supervisor.taps) == 9
    for _ in range(4):
        if not sut.reconciliation_pending:
            break
        assert sut.inspect(page, device)
    _assert_settled_once(sut, path)
    assert len(supervisor.taps) == 9


def test_live_unproven_in_long_running_process_navigates_after_reverification(tmp_path, monkeypatch) -> None:
    """C1 live variant: a scoped UNPROVEN purchase an hour into a run."""
    from identity_reverify import IdentityReverifier
    sut = make_session(tmp_path)
    real = time.time
    monkeypatch.setattr(time, 'time', lambda: real() + 3600)
    txn = sut._open_intent(item='Damage', category='ATTACK', currency='coins', price=30,
                           wallet_before=100, armed=True,
                           before={"observed_at": time.time(), "frame_digest": "before"})
    sut._mark_acted(txn)
    sut._close(txn.key, price=30, wallet_before=100, wallet_after=None, effect_changed=None)
    assert sut.reconciliation_pending
    device = Mock()
    # Without a re-verifier the scan is released, never frozen.
    assert not any(sut.inspect(screen('menu_main'), device) for _ in range(3))
    device.click.assert_not_called()
    assert sut.currencies.committed('coins') == 30
    scope = sut.account_state.verified_scope
    walks = []

    def verify():
        walks.append(time.time())
        sut.account_state.bind_scope(scope, identity=IdentityEvidence('acct', time.time(), 'again'))

    sut.identity_reverifier = IdentityReverifier(verify)
    assert sut.inspect(screen('menu_main'), device)
    assert sut.inspect(screen('menu_main'), device)
    assert len(walks) == 1
    assert device.click.call_count >= 1  # Navigates to the Workshop.


def test_identity_reverifier_is_paced_with_backoff() -> None:
    from identity_reverify import IdentityReverifier
    now = [0.]
    calls = []

    def verify():
        calls.append(now[0])
        raise RuntimeError('account navigation evidence unavailable')

    pacer = IdentityReverifier(verify, clock=lambda: now[0])
    assert pacer.attempt('x')
    assert not pacer.attempt('x')
    now[0] = 119.
    assert not pacer.attempt('x')
    now[0] = 120.
    assert pacer.attempt('x')
    now[0] = 120. + 239.
    assert not pacer.attempt('x')
    assert len(calls) == 2 and pacer.status == 'failed'
    now[0] = 10**6
    for _ in range(10):
        pacer.attempt('x')
        now[0] += pacer.max_backoff
    assert pacer.retry_in() <= pacer.max_backoff


def test_held_pending_search_releases_the_scan_and_retries_later(tmp_path, monkeypatch) -> None:
    sut = make_session(tmp_path)
    txn = sut._open_intent(item='Damage', category='ATTACK', currency='coins', price=30,
                           wallet_before=100, armed=True,
                           before={'observed_at': time.time(), 'frame_digest': 'before'})
    sut._mark_acted(txn)
    device = Mock()
    # A battle frame is never owned by a pending purchase.
    assert not sut.inspect(screen('in_run_early'), device)
    assert sut._inspection.hold_reason == 'waiting_for_menu'
    monkeypatch.setattr(shopping, '_heading_names', lambda *args: False)
    page = screen('menu_workshop_attack')
    owned = [sut.inspect(page, device) for _ in range(20)]
    assert owned[:16] == [True]*16 and not any(owned[16:])
    real = time.time
    monkeypatch.setattr(time, 'time', lambda: real() + 901)
    sut.account_state.bind_scope(sut.account_state.verified_scope,
        identity=IdentityEvidence('acct', time.time(), 'again'))
    assert sut.inspect(page, device)  # Bounded periodic retry.
    assert device.click.call_count == 17
    assert sut.currencies.committed('coins') == 30


def test_new_visit_and_spend_refused_while_a_purchase_is_unresolved(tmp_path) -> None:
    from shopping import PurchasePending
    from strategy import Shopping
    sut = make_session(tmp_path)
    txn = sut._open_intent(item='Damage', category='ATTACK', currency='coins', price=30,
                           wallet_before=100, armed=True,
                           before={'observed_at': time.time(), 'frame_digest': 'before'})
    sut._mark_acted(txn)
    sut._pending = None
    with pytest.raises(PurchasePending):
        sut._open_intent(item='Health', category='DEFENSE', currency='coins', price=1,
                         wallet_before=100, armed=True,
                         before={'observed_at': time.time(), 'frame_digest': 'b2'})
    assert sut._commitment_reason(PurchasePending('x')) == 'reconciliation_pending'


def test_unscoped_worker_skip_reason_is_account_scope_unavailable(tmp_path) -> None:
    from shopping import AccountScopeUnavailable
    session = ShoppingSession(vision.TemplateCache(config.TEMPLATE_DIR), Mock(), Mock(),
                              journal=TransactionJournal(tmp_path / 'bot.db'))
    session.account_state = AccountState(AccountRepository(session.journal.path))
    with pytest.raises(AccountScopeUnavailable) as raised:
        session._open_intent(item='Damage', category='ATTACK', currency='coins', price=30,
                             wallet_before=100, armed=True,
                             before={'observed_at': time.time(), 'frame_digest': 'before'})
    assert session._commitment_reason(raised.value) == 'account_scope_unavailable'
