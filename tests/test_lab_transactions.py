"""Real journal and recorded Lab frames across the irreversible input boundary."""
from dataclasses import replace
from pathlib import Path

import pytest

from account_state import AccountState, AccountRepository
import db
from evidence_scope import BalanceInterval, FactScope
from fleet.identity import IdentityEvidence
import events
import ledger
from transactions import Intent, RecoveryEvidence, Stage, TransactionJournal, Transaction, Verdict
from lab_runtime import LabRuntime
from lab_visit import LabVisit
from lab_plan import LabVisitOptions
from tests.test_lab_visit import frame, boxes, Device
import vision


class LabHarness:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.account, self.journal, self.scope = authority(root)
        self.time = 10.
        self.events = []
        self.crash = False
        self.restart()
        monkeypatch.setattr('lab_visit.tap', self.tap)

    def restart(self) -> None:
        self.journal = TransactionJournal(self.root / 'bot.db')
        self.runtime = LabRuntime(self.root, 'account-a', lease_id='lease', generation='generation')
        self.device = Device()
        self.visit = LabVisit(vision.TemplateCache(Path('templates')), journal=self.journal,
            account_state=self.account, runtime=self.runtime, wall_clock=lambda: self.time,
            event_sink=self.events.append)
        self.visit.request(LabVisitOptions())

    def tap(self, device: Device, x: int, y: int) -> None:
        device.taps.append((x, y))
        if self.crash:
            assert self.journal.open_transactions()[0].stage.value == 'acted'
            raise RuntimeError('crashed after dispatch')

    def scan(self, name: str, *, same_capture: bool = False, step: float = 1.) -> None:
        if not same_capture:
            self.time += step
        self.visit.advance(frame(name), boxes(name), self.device, self.time,
                           observed_at=self.time, capture_scope=self.scope)

    def confirmation(self) -> None:
        for name in ('menu_labs_slot1_affordable', 'menu_labs_slot1_affordable',
                     'menu_labs_game_speed_affordable', 'menu_labs_game_speed_affordable',
                     'menu_labs_game_speed_confirmation'):
            self.scan(name)


@pytest.fixture
def lab_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LabHarness:
    return LabHarness(tmp_path, monkeypatch)


def test_lab_crash_cannot_spend_twice(lab_harness: LabHarness) -> None:
    h = lab_harness
    h.confirmation()
    assert h.journal.open_transactions() == ()  # navigation is not a spend
    h.crash = True
    with pytest.raises(RuntimeError, match='crashed after dispatch'):
        h.scan('menu_labs_game_speed_confirmation')
    key = h.journal.open_transactions()[0].key
    h.crash = False
    h.restart()
    h.scan('menu_labs_game_speed_confirmation')  # cancel only, never research
    assert len(h.device.taps) <= 1
    h.device.taps.clear()
    h.scan('menu_labs_game_speed_running')
    h.scan('menu_labs_game_speed_running', same_capture=True)
    assert h.journal.open_transactions()
    h.scan('menu_labs_game_speed_running')
    assert h.journal.open_transactions() == ()
    assert h.runtime.snapshot().slots[0].transaction_id == key
    with db.reader(h.journal.path) as conn:
        assert [row[0] for row in conn.execute("SELECT delta FROM ledger WHERE kind='LAB'")] == [-300]
    assert len([e for e in h.events if isinstance(e, events.LabResearchStarted)]) == 1


def test_repeated_confirmation_capture_cannot_spend(lab_harness: LabHarness) -> None:
    h = lab_harness
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation', same_capture=True)
    assert h.journal.open_transactions() == ()


def test_lab_without_shared_authority_never_confirms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    visit = LabVisit(vision.TemplateCache(Path('templates')))
    device = Device()
    taps = []
    monkeypatch.setattr('lab_visit.tap', lambda _device, x, y: taps.append((x, y)))
    visit.request(LabVisitOptions())
    for index, name in enumerate(('menu_labs_slot1_affordable', 'menu_labs_slot1_affordable',
            'menu_labs_game_speed_affordable', 'menu_labs_game_speed_affordable',
            'menu_labs_game_speed_confirmation', 'menu_labs_game_speed_confirmation')):
        visit.advance(frame(name), boxes(name), device, 10.+index)
    assert visit.last_tap is None or visit.last_tap[0] != 'confirm_game_speed'


def test_production_constructor_wires_account_runtime_and_original_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tower_bot import TowerBot
    from shopping import ShoppingSession
    from tests.conftest import _RecordingBus
    import digits
    account, journal, scope = authority(tmp_path)
    bus = _RecordingBus()
    templates = vision.TemplateCache(Path('templates'))
    shopping = ShoppingSession(templates, bus, digits.NumberReader(), journal=journal)
    bot = TowerBot(Device(), templates, bus, account_state=account, shopping=shopping)
    monkeypatch.setattr('lab_visit.tap', lambda device, x, y: device.taps.append((x, y)))
    assert bot.lab_visit is not None
    monkeypatch.setattr('tower_bot.time.time', lambda: 20.)
    bot._screen = frame('menu_labs_active')
    bot._screen_fact_scope = scope
    bot._screen_captured_at = 19.
    bot._bind_lab_runtime()
    bot.lab_visit.request(LabVisitOptions(start_research=False))
    for stamp in (19., 19., 20.):
        bot._screen_captured_at = stamp
        bot._observe_labs_capture(boxes('menu_labs_active'))
        bot.lab_visit.advance(bot.screen, boxes('menu_labs_active'), bot.device, stamp,
                              observed_at=stamp, capture_scope=scope)
        if stamp == 19.:
            assert not bot.lab_runtime.snapshot().slots[0].confirmed
    jobs = bot.lab_runtime.snapshot().slots
    assert jobs[0].confirmed and jobs[4].confirmed
    assert all(job.observed_at == 20. for job in jobs)
    restored = LabRuntime(tmp_path, scope.account_id, lease_id=scope.lease_id, generation=scope.generation)
    assert restored.snapshot().slots[0].research_id == jobs[0].research_id
    assert not restored.snapshot().slots[0].confirmed


def test_real_bot_restart_inspects_home_without_second_spend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tower_bot import TowerBot
    from shopping import ShoppingSession
    from supervisor import GuardedDevice
    from tests.conftest import _RecordingBus
    import digits
    import ocr
    account, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope)
    current = ['menu_main_labs_unlocked', 20.]
    taps = []

    class InputGuard:
        def tap(self, x: int, y: int) -> None:
            assert account.verified_scope == scope
            assert journal.open_transactions()[0].key == txn.key
            assert (x, y) != (753, 1653)  # irreversible Research control
            taps.append((x, y))

    bus = _RecordingBus()
    templates = vision.TemplateCache(Path('templates'))
    monkeypatch.setattr('tower_bot.capture_screen', lambda _device: frame(current[0]))
    monkeypatch.setattr('tower_bot.time.time', lambda: current[1])
    monkeypatch.setattr(ocr.FrameReads, 'full', lambda _reads: boxes(current[0]))
    bot = TowerBot(GuardedDevice(InputGuard()), templates, bus, account_state=account,
        shopping=ShoppingSession(templates, bus, digits.NumberReader(), journal=TransactionJournal(journal.path)))
    bot.lab_visit.wall_clock = lambda: current[1]
    bot.run_once()
    assert len(taps) == 1
    for stamp in (21., 22.):
        current[:] = ['menu_labs_game_speed_running', stamp]
        bot.run_once()
    assert journal.open_transactions() == ()
    assert bot.lab_runtime.snapshot().slots[0].transaction_id == txn.key
    assert len(taps) == 1
    with db.reader(journal.path) as conn:
        assert [row[0] for row in conn.execute("SELECT delta FROM ledger WHERE kind='LAB'")] == [-300]


@pytest.mark.parametrize('home_frame', ['main_menu', 'menu_main_labs_unlocked'])
@pytest.mark.parametrize('settle_first', [False, True])
@pytest.mark.parametrize('shopping_accepts', [False, True])
def test_recovery_only_bot_resumes_normal_home_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home_frame: str,
    settle_first: bool, shopping_accepts: bool,
) -> None:
    from unittest.mock import Mock
    from control import Controls
    from strategy import Strategy
    from tower_bot import TowerBot
    from shopping import ShoppingSession
    from supervisor import GuardedDevice
    from tests.conftest import _RecordingBus, _RecordingSnapshotWriter
    import digits
    import ocr

    account, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope) if settle_first else None
    current = ['menu_main_labs_unlocked' if settle_first else home_frame, 20.]
    taps = []

    class InputGuard:
        def tap(self, x: int, y: int) -> None:
            assert account.verified_scope == scope
            assert (x, y) != (753, 1653)
            taps.append((x, y))

    bus = _RecordingBus()
    templates = vision.TemplateCache(Path('templates'))
    monkeypatch.setattr('tower_bot.capture_screen', lambda _device: frame(current[0]))
    monkeypatch.setattr('tower_bot.time.time', lambda: current[1])
    monkeypatch.setattr('tower_bot.header_numbers', lambda *_args: (613, 65))
    monkeypatch.setattr(ocr.FrameReads, 'full', lambda _reads:
        boxes(current[0]) if current[0] == 'menu_labs_game_speed_running' else ())
    bot = TowerBot(GuardedDevice(InputGuard()), templates, bus, account_state=account,
        shopping=ShoppingSession(templates, bus, digits.NumberReader(), journal=journal),
        controls=Controls(strategy=replace(Strategy.from_config(), auto_navigate=False)))
    bot.snapshots = _RecordingSnapshotWriter()
    assert bot.reroll_progress is None and bot.lab_visit is not None
    bot.lab_visit.wall_clock = lambda: current[1]
    monkeypatch.setattr(bot._notifications, 'verification_due', lambda _now: False)
    monkeypatch.setattr(bot, '_offer_cards_intro', lambda: False)
    claim = Mock(return_value=None)
    begin = Mock(return_value=shopping_accepts)
    monkeypatch.setattr(bot, '_offer_claim', claim)
    monkeypatch.setattr(bot.shopping, 'begin', begin)

    if settle_first:
        bot.run_once()
        for stamp in (21., 22.):
            current[:] = ['menu_labs_game_speed_running', stamp]
            bot.run_once()
        assert journal.open_transactions() == ()
        assert bot.lab_runtime.snapshot().slots[0].transaction_id == txn.key
        assert len(taps) == 1
        current[:] = [home_frame, 23.]
        bot.run_once()  # finish the recovery visit before ordinary maintenance

    assert not bot.lab_visit.active
    request = Mock(wraps=bot.lab_visit.request)
    monkeypatch.setattr(bot.lab_visit, 'request', request)
    for stamp in (24., 25., 26.):
        current[:] = [home_frame, stamp]
        bot.run_once()
    assert begin.call_count > 0
    assert claim.call_count == (0 if shopping_accepts else begin.call_count)
    request.assert_not_called()
    assert not bot.lab_visit.active
    assert len(taps) == (1 if settle_first else 0)


@pytest.mark.parametrize('block', ['reservation', 'stale', 'scope', 'policy'])
def test_lab_rechecks_shared_funds_scope_time_and_policy_at_confirmation(lab_harness: LabHarness, block: str) -> None:
    h = lab_harness
    h.confirmation()
    if block == 'reservation':
        h.account.currencies.reserve('workshop-plan', 'coins', 400, wallet=613)
    elif block == 'stale':
        h.time += 31
    elif block == 'scope':
        h.account.invalidate_scope('manual-account-change')
    else:
        h.visit.authorize = lambda *_args: False
    before = len(h.device.taps)
    h.scan('menu_labs_game_speed_confirmation')
    assert len(h.device.taps) == before
    assert h.journal.open_transactions() == ()


def test_recorded_picker_target_flows_to_completed_history_and_next_plan(tmp_path: Path) -> None:
    from lab_screen import read_slots, read_picker
    from lab_runtime import _catalog_revision
    from labs import LabsState
    from fleet.resource_blocks import evaluate_lab_plan
    from tests.test_resource_blocks import template_route
    account, journal, scope = authority(tmp_path)
    runtime = LabRuntime(tmp_path, 'account-a', lease_id='lease', generation='generation')
    labs = LabsState(account)
    picker = read_picker(frame('menu_labs_game_speed_affordable'), boxes('menu_labs_game_speed_affordable'))
    for stamp in (10., 11.):
        observation = read_slots(frame('menu_labs_slot1_affordable'), boxes('menu_labs_slot1_affordable'), observed_at=stamp)
        runtime.observe(observation)
        labs.observe(replace(observation, entries=(picker.game_speed,)), scope=scope,
                     catalog_revision=_catalog_revision())
    account.observe_balance(BalanceInterval.from_reading('coins', 613, scope, 11., 'wallet'))
    facts = account.lab_facts(runtime.snapshot(), now=12.)
    assert facts.completed_levels['labs.game-speed'] == 0
    assert account.snapshot()['revision']['lab_levels'][0]['value'] == 1  # raw target retained
    plan = evaluate_lab_plan(template_route(), facts)
    assert plan.slots[0].next.level == 1


def test_job_binding_recovers_if_process_dies_after_atomic_debit(lab_harness: LabHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    h = lab_harness
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation')
    key = h.journal.open_transactions()[0].key
    h.scan('menu_labs_game_speed_running')
    monkeypatch.setattr(h.runtime, 'bind_transaction', lambda *args: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError, match='disk'):
        h.scan('menu_labs_game_speed_running')
    assert h.journal.open_transactions() == ()
    h.restart()
    h.scan('menu_labs_game_speed_running')
    h.scan('menu_labs_game_speed_running')
    assert h.runtime.snapshot().slots[0].transaction_id == key
    with db.reader(h.journal.path) as conn:
        assert conn.execute("SELECT count(*) FROM ledger WHERE kind='LAB'").fetchone()[0] == 1


def test_plan_is_rechecked_against_current_slot_age_and_reservations(tmp_path: Path) -> None:
    from fleet.resource_blocks import LabPlan, SlotPlan, SlotNow, SlotNext
    from lab_runtime import LabRuntimeSnapshot, LabJobRecord, LabScope
    account, journal, scope = authority(tmp_path)
    lab_scope = LabScope(scope.account_id, scope.lease_id, scope.generation, scope.epoch)
    runtime = LabRuntimeSnapshot(lab_scope, (LabJobRecord(lab_scope, 1, state='idle',
        confirmed=True, observed_at=10., frame_digest='idle'),))
    plan = LabPlan(613, 0, (SlotPlan(1, SlotNow('idle', read_at=10.),
        SlotNext('labs.game-speed', 'Game Speed', 1, 300, 600), True, True, (), None,
        {'observe': True, 'plan': True, 'execute': True}),), gems=None,
        strategy_revision=4, account_id=scope.account_id, scope=lab_scope, evaluated_at=20.)
    account.observe_balance(BalanceInterval.from_reading('coins', 613, scope, 40., 'wallet'))
    assert account.lab_action(plan, runtime, revision=4, now=41.) is None
    assert account.lab_action(plan, runtime, revision=5, now=40.) is None
    txn = prepared(journal, scope)
    account.observe_balance(BalanceInterval.from_reading('coins', 1000, scope, 12., 'wallet-new'))
    assert account.lab_facts(runtime, now=12.).reserved_research == frozenset({'labs.game-speed'})
    assert account.lab_action(replace(plan, evaluated_at=12.), runtime, revision=4, now=12.) is None


@pytest.mark.parametrize('bad', [dict(slot=None), dict(slot=True), dict(slot=6),
    dict(research_id=None), dict(target_level=None), dict(target_level=True),
    dict(source_level=8), dict(evidence_digest='')])
def test_journal_refuses_incomplete_lab_operation_metadata(tmp_path: Path, bad: dict[str, object]) -> None:
    _, journal, scope = authority(tmp_path)
    before = dict(slot=1, research_id='labs.game-speed', source_level=0, target_level=1, evidence_digest='frame')
    before.update(bad)
    intent = Intent(item='Game Speed', category='LABS', currency='coins', price=300,
        wallet_before=613, ts=10., operation='lab_start', before=before)
    balance = BalanceInterval.from_reading('coins', 613, scope, 10., 'frame')
    assert journal.prepare(intent, scope=scope, balance=balance) is None
    assert journal.open_transactions() == ()


def test_pending_lab_cannot_navigate_another_account(lab_harness: LabHarness) -> None:
    h = lab_harness
    prepared(h.journal, h.scope)
    h.scope = FactScope('other-account', 'other-lease', 'other-generation', 0)
    h.account.bind_scope(h.scope, identity=IdentityEvidence('other-account', 10., 'other-id'))
    h.visit.runtime = LabRuntime(h.root, 'other-account', lease_id='other-lease', generation='other-generation')
    h.time = 11.
    h.visit.advance(frame('menu_main_labs_unlocked'), (), h.device, 11.,
                    observed_at=11., capture_scope=h.scope)
    assert h.device.taps == []
    assert h.journal.open_transactions()


def authority(root: Path) -> tuple[AccountState, TransactionJournal, FactScope]:
    journal = TransactionJournal(root / 'bot.db')
    account = AccountState(AccountRepository(journal.path))
    scope = FactScope('account-a', 'lease', 'generation', 0)
    account.bind_scope(scope, identity=IdentityEvidence('account-a', 1., 'identity'))
    return account, journal, scope


def prepared(journal: TransactionJournal, scope: FactScope, *, operation: str = 'lab_start') -> Transaction:
    unlock = operation == 'lab_unlock'
    intent = Intent(item='Lab 2' if unlock else 'Game Speed', category='LABS',
        currency='gems' if unlock else 'coins', price=100 if unlock else 300,
        wallet_before=613, ts=10., operation=operation,
        before={'slot': 2 if unlock else 1, 'research_id': None if unlock else 'labs.game-speed',
                'source_level': None if unlock else 0, 'target_level': None if unlock else 1,
                'evidence_digest': 'before'})
    balance = BalanceInterval.from_reading(intent.currency, 613, scope, 10., 'before')
    txn = journal.prepare(intent, scope=scope, balance=balance)
    assert txn is not None
    journal.record_action(txn.key, at=11.)
    return txn


@pytest.mark.parametrize('operation', ['lab_start', 'lab_unlock'])
def test_lab_settlement_keeps_metadata_and_one_correct_durable_debit(tmp_path: Path, operation: str) -> None:
    _, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope, operation=operation)
    proof = RecoveryEvidence(category='LABS', currency=txn.currency,
        wallet_after=613-txn.price, effect_changed=True, observed_at=12., frame_digest='after',
        scope=scope, operation=operation, slot=txn.before['slot'],
        research_id=txn.before['research_id'], target_level=txn.before['target_level'])
    outcome = journal.reconcile(txn.key, proof, now=12.)
    assert outcome.verdict == Verdict.BOUGHT
    assert journal.reconcile(txn.key, proof, now=13.) == outcome
    assert journal.recovered_visit() == ()  # shopping cannot claim Lab receipts
    restored, _ = journal.recovered_visit(operations={'lab_start', 'lab_unlock'})[0]
    assert restored.before == txn.before
    assert restored.operation == operation
    event = journal.recovery_event(restored, outcome)
    assert isinstance(event, events.LabSlotUnlocked if operation == 'lab_unlock' else events.LabResearchStarted)
    assert event.transaction_key == txn.key
    with db.reader(journal.path) as conn:
        rows = conn.execute("SELECT kind, delta FROM ledger WHERE delta<0").fetchall()
        assert [tuple(row) for row in rows] == [('LAB', -txn.price)]
        assert ledger.LedgerWriter(conn).lines_for(event) == []
    other_account = AccountState()
    other_scope = FactScope('account-b', 'lease-b', 'generation-b', 0)
    other_account.bind_scope(other_scope, identity=IdentityEvidence('account-b', 13., 'identity-b'))
    visit = LabVisit(vision.TemplateCache(Path('templates')), journal=journal, account_state=other_account)
    assert not visit.has_recovery_receipts  # settled history cannot arm a different account's visit


@pytest.mark.parametrize('change', [dict(operation='workshop_buy'), dict(slot=2),
    dict(research_id='labs.attack-speed'), dict(target_level=2), dict(wallet_after=613),
    dict(effect_changed=None)])
def test_wrong_lab_semantics_or_wallet_retains_shared_reservation(tmp_path: Path, change: dict[str, object]) -> None:
    _, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope)
    proof = RecoveryEvidence(category='LABS', currency='coins', wallet_after=313,
        effect_changed=True, observed_at=12., frame_digest='after', scope=scope,
        operation='lab_start', slot=1, research_id='labs.game-speed', target_level=1)
    assert journal.reconcile(txn.key, replace(proof, **change), now=12.).verdict == Verdict.UNPROVEN
    assert journal.open_transactions()[0].key == txn.key
    with db.reader(journal.path) as conn:
        assert conn.execute('SELECT amount FROM currency_commitments').fetchone()[0] == 300


def test_lab_recovery_timeout_uses_earlier_inspection_after_research_finishes(
        tmp_path: Path) -> None:
    account, journal, scope = authority(tmp_path)
    intent = Intent(item='Cash Bonus', category='LABS', currency='coins', price=81,
        wallet_before=14510, ts=10., operation='lab_start',
        before={'slot': 2, 'research_id': 'labs.cash-bonus', 'source_level': 1,
                'target_level': 2, 'evidence_digest': 'before'})
    balance = BalanceInterval.from_reading('coins', 14510, scope, 10., 'before')
    txn = journal.prepare(intent, scope=scope, balance=balance)
    assert txn is not None
    journal.record_action(txn.key, at=11.)
    # A 110 drop misses the 81 price by more than two "14.xxK" readings can
    # hide, so this inspection stays inconclusive.
    proof = RecoveryEvidence(category='LABS', currency='coins', wallet_after=14400,
        effect_changed=True, observed_at=12., frame_digest='after', scope=scope,
        operation='lab_start', slot=2, research_id='labs.cash-bonus', target_level=2)
    assert journal.reconcile(txn.key, proof, now=12.).verdict == Verdict.UNPROVEN
    finished = replace(proof, effect_changed=False, observed_at=13., frame_digest='finished')
    assert journal.reconcile(txn.key, finished, now=13.).verdict == Verdict.UNPROVEN
    assert journal._require(txn.key).reconciliation['effect_changed'] is False

    restarted_scope = FactScope('account-a', 'lease', 'next-generation', 0)
    account.bind_scope(restarted_scope,
                       identity=IdentityEvidence('account-a', 14., 'restart-identity'))
    runtime = LabRuntime(tmp_path, 'account-a', lease_id='lease', generation='next-generation')
    visit = LabVisit(vision.TemplateCache(Path('templates')), journal=journal,
        account_state=account, runtime=runtime, wall_clock=lambda: 102.)
    assert visit.request(LabVisitOptions(start_research=False))
    visit._started_at = 11.
    visit.advance(frame('menu_labs_active'), boxes('menu_labs_active'), Device(), 102.,
                  observed_at=102., capture_scope=restarted_scope)

    assert journal.open_transactions() == ()
    assert journal._require(txn.key).stage == Stage.RESOLVED
    with db.reader(journal.path) as conn:
        assert conn.execute(
            "SELECT json_extract(detail, '$.inspected_at') FROM transactions WHERE key=?",
            (txn.key,)).fetchone()[0] == 12.
        assert tuple(conn.execute(
            'SELECT outcome, spent FROM transactions WHERE key=?', (txn.key,)).fetchone()) == (
                Verdict.UNPROVEN.value, None)
        assert conn.execute("SELECT COUNT(*) FROM ledger WHERE kind='LAB'").fetchone()[0] == 0


def test_lab_recovery_timeout_keeps_uninspected_spend_reserved(tmp_path: Path) -> None:
    account, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope)
    runtime = LabRuntime(tmp_path, 'account-a', lease_id='lease', generation='generation')
    visit = LabVisit(vision.TemplateCache(Path('templates')), journal=journal,
        account_state=account, runtime=runtime, wall_clock=lambda: 102.)
    assert visit.request(LabVisitOptions(start_research=False))
    visit._started_at = 11.

    visit.advance(frame('menu_labs_active'), boxes('menu_labs_active'), Device(), 102.,
                  observed_at=102., capture_scope=scope)

    assert journal.open_transactions()[0].key == txn.key
    assert journal.currencies.committed('coins') == 300


def _unlanded(scope: FactScope, **overrides: object) -> RecoveryEvidence:
    return RecoveryEvidence(**{"category": "LABS", "currency": "gems", "wallet_after": 613,
        "effect_changed": False, "observed_at": 13., "frame_digest": "after", "scope": scope,
        "operation": "lab_unlock", "slot": 2, **overrides})


def test_an_unlanded_unlock_tap_is_settled_as_not_charged(tmp_path: Path) -> None:
    _, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope, operation='lab_unlock')  # acted at 11
    outcome = journal.refute_unlanded_unlock(txn.key, _unlanded(scope), now=13.)
    assert (outcome.verdict, outcome.spent) == (Verdict.REFUTED, 0)
    assert journal.open_transactions() == ()
    assert journal.currencies.committed('gems') == 0
    with db.reader(journal.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM currency_observations WHERE currency='gems'").fetchone()[0] == 0
    assert journal.refute_unlanded_unlock(txn.key, _unlanded(scope), now=14.) == outcome


_OTHER_SCOPE = FactScope('account-b', 'lease-b', 'generation-b', 0)


@pytest.mark.parametrize("overrides,now", [
    ({"observed_at": 12.5}, 13.),     # too soon after the tap
    ({"wallet_after": 513}, 13.),     # the gems moved
    ({"effect_changed": None}, 13.),  # the slot was not read
    ({"slot": 3}, 13.),               # another slot
    ({"frame_digest": ""}, 13.),      # no frame behind the read
    ({"observed_at": 13.5}, 13.),     # the read claims to be after "now"
    ({}, 44.),                        # the read is more than 30s stale by "now"
    ({"scope": _OTHER_SCOPE}, 13.),   # evidence from a different account/scope
    ({"category": "CARDS"}, 13.),     # category does not match the transaction
])
def test_an_unlanded_unlock_needs_complete_proof(tmp_path: Path, overrides: dict, now: float) -> None:
    _, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope, operation='lab_unlock')
    evidence = _unlanded(**{"scope": scope, **overrides})
    outcome = journal.refute_unlanded_unlock(txn.key, evidence, now=now)
    assert outcome.verdict == Verdict.UNPROVEN and outcome.spent is None
    assert journal.open_transactions()[0].key == txn.key


def test_an_unlanded_unlock_needs_an_acted_tap(tmp_path: Path) -> None:
    """A prepared-but-not-yet-dispatched row can't be settled through this path:

    no device action was ever claimed, so there is no tap to judge unlanded.
    """
    _, journal, scope = authority(tmp_path)
    intent = Intent(item='Lab 2', category='LABS', currency='gems', price=100,
        wallet_before=613, ts=10., operation='lab_unlock',
        before={'slot': 2, 'research_id': None, 'source_level': None, 'target_level': None,
                'evidence_digest': 'before'})
    balance = BalanceInterval.from_reading('gems', 613, scope, 10., 'before')
    txn = journal.prepare(intent, scope=scope, balance=balance)
    assert txn is not None and txn.stage == Stage.INTENDED  # never record_action'd
    outcome = journal.refute_unlanded_unlock(txn.key, _unlanded(scope), now=13.)
    assert outcome.verdict == Verdict.UNPROVEN and outcome.spent is None
    assert journal.open_transactions()[0].key == txn.key


def test_only_a_lab_unlock_can_be_refuted_as_unlanded(tmp_path: Path) -> None:
    """A gems row whose operation is not `lab_unlock` must not be refutable here.

    Currency, category and slot all match the (missing) evidence so this can
    only fail on the operation guard - proving the guard, not the currency
    check, is what blocks a non-unlock row.
    """
    _, journal, scope = authority(tmp_path)
    intent = Intent(item='Gem Pack', category='CARDS', currency='gems', price=100,
        wallet_before=613, ts=10., operation='card_buy')
    balance = BalanceInterval.from_reading('gems', 613, scope, 10., 'before')
    txn = journal.prepare(intent, scope=scope, balance=balance)
    assert txn is not None
    journal.record_action(txn.key, at=11.)
    evidence = _unlanded(scope, category='CARDS', slot=txn.before.get('slot'))
    outcome = journal.refute_unlanded_unlock(txn.key, evidence, now=13.)
    assert outcome.verdict == Verdict.UNPROVEN and outcome.spent is None
    assert journal.open_transactions()[0].key == txn.key


def test_production_constructor_wires_the_unlock_rollout(tmp_path: Path) -> None:
    from tower_bot import TowerBot
    from shopping import ShoppingSession
    from tests.conftest import _RecordingBus
    from fleet.reroll_progress import RerollProgress
    import digits
    root = tmp_path / 'workers' / 'Air_1'
    root.mkdir(parents=True)
    db.bind_account(root / 'tower_bot.db', 'account-a')
    account, journal, _ = authority(root)
    bus = _RecordingBus()
    templates = vision.TemplateCache(Path('templates'))
    shopping = ShoppingSession(templates, bus, digits.NumberReader(), journal=journal)
    bot = TowerBot(Device(), templates, bus, account_state=account, shopping=shopping,
                   reroll_progress=RerollProgress(root, 'account-a', account),
                   unknown_dir=root / 'evidence')
    assert bot.lab_visit.rollout.path == tmp_path / 'lab-unlock-rollout.json'
    assert bot.lab_visit.worker == 'Air_1'
    assert bot.lab_visit.evidence_dir == root / 'evidence'


@pytest.mark.parametrize("method", ["refute_unlanded_start", "refute_unlanded_unlock"])
def test_refuting_an_unlanded_tap_fences_recovery_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        method: str) -> None:
    """Like every recovery mutation, a refutation bumps the fence so a concurrent read retries."""
    import transactions
    _, journal, scope = authority(tmp_path)
    seen = []

    def refute(self, key, evidence, *, now, operation, currency, reason):
        seen.append(transactions._recovery_fences[self._recovery_key]['active'])
        return transactions.Outcome(key=key, verdict=transactions.Verdict.UNPROVEN, spent=0, reason='')

    monkeypatch.setattr(transactions.TransactionJournal, '_refute_unlanded', refute)
    getattr(journal, method)('k', None, now=1.)
    assert seen == [1]


@pytest.mark.parametrize('operation', ['lab_start', 'lab_unlock'])
def test_a_lab_effect_with_an_unreadable_wallet_stays_unproven(tmp_path: Path, operation: str) -> None:
    """The Workshop's book-the-read-price rule never reaches Labs."""
    _, journal, scope = authority(tmp_path)
    txn = prepared(journal, scope, operation=operation)
    unlock = operation == 'lab_unlock'
    proof = RecoveryEvidence(category='LABS', currency='gems' if unlock else 'coins', wallet_after=None,
        effect_changed=True, observed_at=12., frame_digest='after', scope=scope,
        operation=operation, slot=2 if unlock else 1,
        research_id=None if unlock else 'labs.game-speed', target_level=None if unlock else 1)

    outcome = journal.reconcile(txn.key, proof, now=12.)

    assert (outcome.verdict, outcome.spent) == (Verdict.UNPROVEN, None)
    assert journal.open_transactions()[0].key == txn.key
