"""Integrated HTTP/runtime stories using synthetic semantic input, never live actions.

The tiny generated frames and Replay adapter deliberately grant test-only mutation
capabilities. They are not recorded calibration evidence. The legacy-layout capture
story verifies those observation-only profiles. Compact real-adapter acceptance
lives separately in test_cards_recorded_runtime.py.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import numpy as np
import pytest

import config
import db
import screens
import vision
from account_state import AccountState
from card_models import CardLoadout, CardProgram, SlotGoal, RewardItem
from card_runtime import CardRuntime
from card_store import CardStore
from card_transactions import CardResultEvidence, repair_card_ledger
from control import Controls
from events import EventBus
from evidence_scope import BalanceInterval, FactScope
from fleet.identity import IdentityEvidence
from runner import BotRunner
from shopping import ShoppingSession
from sinks.sse import SseSink
from sinks.state import BotState
from strategy import ActionRule, CardPolicy, Shopping, Strategy
from tower_bot import TowerBot
from transactions import TransactionJournal
from web.app import create_app
from tests.test_card_plan import goal
from tests.test_card_visit import Device, Replay


class SyntheticScene(Replay):
    """Only transport/semantic capture are fake; ownership changes are explicit."""
    def __init__(self) -> None:
        super().__init__()
        self.owned = {'cards.health'}

    def read(self, *args: Any, **kwargs: Any) -> Any:
        view = super().read(*args, **kwargs)
        if view.snapshot is not None:
            items = tuple(item.model_copy(update={
                'ownership': 'owned' if item.card_id in self.owned else 'unowned',
            }) for item in view.snapshot.items)
            view = replace(view, snapshot=view.snapshot.model_copy(update={'items': items}))
        return view


class Story:
    def __init__(self, path: Path, monkeypatch: pytest.MonkeyPatch, *, account_id: str = 'a',
                 program: CardProgram | None = None, automatic: bool = False) -> None:
        self.now, self.gems = 100., 500
        self.scope = FactScope(account_id, 'lease-' + account_id, 'generation-' + account_id, 1)
        monkeypatch.setattr('time.time', lambda: self.now)
        db.bind_account(path, account_id)
        self.account = AccountState()
        self.account.attach_safety_storage(path)
        self.account.bind_scope(self.scope, identity=IdentityEvidence(account_id, 1., 'synthetic-identity'))
        self.controls = Controls(Strategy(name='cards', tap_jitter_px=0., timing_jitter=0., tap_delay=0.,
            actions=(ActionRule(name='Damage', template='upgrade_damage.png', enabled=False),),
            cards=program or CardProgram(version=1, gem_cap=100),
            shopping=Shopping(enabled=True, armed=True,
                cards=CardPolicy(enabled=automatic, gem_floor=0, max_per_visit=10))))
        self.bus, self.device, self.scene = EventBus(), Device(), SyntheticScene()
        templates = vision.TemplateCache(config.TEMPLATE_DIR)
        self.shopping = ShoppingSession(templates, self.bus, None)
        self.shopping.journal = TransactionJournal(path)
        self.bot = TowerBot(device=self.device, templates=templates, bus=self.bus,
            controls=self.controls, shopping=self.shopping, account_state=self.account)
        self.runner = BotRunner(bus=self.bus, controls=self.controls, state=BotState(), templates=templates,
            device_factory=lambda: self.device, checks={}, shopping=self.shopping, account_state=self.account)
        self.runner._bot = self.bot
        monkeypatch.setattr(self.runner, '_running_locked', lambda: True)
        monkeypatch.setattr(self.runner, 'verified_account', lambda: self.account.verified_scope.account_id)
        self.store = CardStore(path)
        self.client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=self.bus,
            runner=self.runner, controls=self.controls, db_path=path))
        response = self.client.post('/api/cards/budget-cycles', json={
            **self.preconditions(), 'cycle_id': 'cycle', 'cap': 100})
        assert response.status_code == 200, response.text

    def preconditions(self) -> dict[str, Any]:
        context = self.runner.cards_context()
        return dict(expected_account_id=self.scope.account_id, expected_generation=self.scope.generation,
                    expected_epoch=self.scope.epoch, expected_program_revision=context.program_revision)

    def submit(self, kind: str, key: str = 'request', **kwargs: Any) -> dict[str, Any]:
        response = self.client.post('/api/cards/commands', json={**self.preconditions(),
            'idempotency_key': key, 'kind': kind, **kwargs})
        assert response.status_code == 202, response.text
        return response.json()

    def tick(self, *, in_run: bool = False) -> None:
        self.now += 1.
        self.bot.tracker.state = screens.ScreenState.IN_RUN if in_run else screens.ScreenState.MAIN_MENU
        self.bot._screen = self.frame(self.now)
        self.bot._screen_fact_scope = self.account.verified_scope
        self.bot._screen_captured_at = self.now
        self.account.observe_balance(BalanceInterval('gems', self.gems, self.gems,
            self.account.verified_scope, self.now, 'synthetic-wallet'))
        self.bot.card_runtime.visit.adapter = self.scene
        self.bot._advance_cards((), opportunity=not in_run)

    @staticmethod
    def frame(at: float) -> np.ndarray:
        return np.full((2, 2, 3), int(at) % 255, dtype=np.uint8)

    def home(self) -> None:
        self.scene.page = 'home'
        self.tick()
        assert self.bot.card_runtime.visit.home_observed
        assert not self.bot.card_runtime.active
        self.scene.page = 'cards'

    def next_dispatched(self, kind: str) -> Any:
        for _ in range(8):
            self.tick()
            operation = self.store.operation(self.bot.card_runtime.visit.operation_id)
            if operation and operation.command.kind == kind and operation.status == 'dispatched':
                return operation
            if operation and operation.status == 'confirmed' and self.bot.card_runtime.active:
                self.home()
        pytest.fail(f'{kind} did not dispatch: {self.store.recent_operations()}')

    def reward(self, operation_id: str, card_id: str | None = None) -> None:
        self.gems -= 20
        self.scene.page = 'rewards' if card_id else 'cards'
        if card_id:
            self.scene.owned.add(card_id)
        self.scene.result = CardResultEvidence(operation_id=operation_id, action_sequence=1,
            scope=self.scope, visit_id=self.bot.card_runtime.visit.visit_id or next(
                txn.before['visit_id'] for txn in self.shopping.journal.open_transactions()
                if txn.before.get('card_operation_id') == operation_id), observed_at=self.now + 1.,
            evidence_ref='synthetic-result', frame_digest=hashlib.sha256(self.frame(self.now + 1.).tobytes()).hexdigest(),
            layout_id='fixture', acquisition_observed=True, reward_flow_complete=True,
            rewards=(RewardItem(position=0, card_id=card_id, quantity=1),) if card_id else ())
        self.tick()
        self.scene.result = None
        assert self.store.operation(operation_id).status == 'confirmed'

    def restart(self) -> None:
        self.store = CardStore(self.store.path)
        self.shopping.journal = TransactionJournal(self.store.path)
        self.bot.card_runtime = CardRuntime(self.bot)

    def ledger(self) -> list[dict[str, Any]]:
        with db.reader(self.store.path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM ledger WHERE kind IN ('CARD_BUY','CARD_SLOT_BUY','CARD_ASSIGN')")]

    def assert_debit(self, operation_id: str, amount: int = 20) -> None:
        matching = [row for row in self.ledger() if json.loads(row['detail']).get('card_operation_id') == operation_id]
        assert len(matching) == 1
        assert matching[0]['delta'] == -amount
        operation = self.store.operation(operation_id)
        assert operation.transaction_key is not None
        with db.reader(self.store.path) as conn:
            receipt = conn.execute('SELECT stage, spent FROM transactions WHERE key=?',
                                   (operation.transaction_key,)).fetchone()
        assert receipt['stage'] == 'resolved' and receipt['spent'] == amount
        assert json.loads(matching[0]['detail'])['rewards'] == [reward.model_dump(mode='json') for reward in operation.rewards]


def test_battle_queue_off_target_then_target_stops_before_visit_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Story(tmp_path / 'a.db', monkeypatch, program=CardProgram(version=1, gem_cap=100, goals=(goal(),)), automatic=True)
    h.bot.tracker.state = screens.ScreenState.IN_RUN
    queued = h.submit('refresh')
    h.tick(in_run=True)
    assert not h.device.taps and h.store.operation(queued['operation_id']).status == 'queued'
    for reward in ('cards.health', 'cards.damage'):
        op = h.next_dispatched('buy')
        assert op.command.quantity == 1 and op.command.source == 'automatic'
        h.reward(op.operation_id, reward)
        h.assert_debit(op.operation_id)
        h.home()
    for _ in range(4):
        h.tick()
        if h.bot.card_runtime.active and h.store.operation(h.bot.card_runtime.visit.operation_id).status == 'confirmed':
            h.home()
    projection = h.client.get('/api/cards').json()
    assert projection['active_budget']['spent'] == 40 and projection['active_budget']['pending'] == 0
    assert projection['preview']['goals'][0]['met'] is True
    assert h.device.taps.count((11, 12)) == 2
    buys = [op for op in h.store.recent_operations() if op.command.kind == 'buy']
    assert len(buys) == 2 and all(op.status == 'confirmed' for op in buys)
    assert {item.card_id for item in h.store.snapshot().items if item.ownership == 'owned'} == {'cards.damage', 'cards.health'}
    assert [op.rewards[0].card_id for op in reversed(buys)] == ['cards.health', 'cards.damage']


def test_slot_growth_then_fallback_loadout_applies_between_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program = CardProgram(version=1, gem_cap=100, goals=(SlotGoal(id='slot', kind='slots', capacity=2),),
        loadouts=(CardLoadout(id='fallback', name='Fallback', priority=('cards.damage', 'cards.health')),),
        selected_loadout_id='fallback')
    h = Story(tmp_path / 'a.db', monkeypatch, program=program, automatic=True)
    h.scene.capacity = 1
    h.scene.page = 'slot_confirmation'
    paid = h.next_dispatched('slot')
    h.scene.capacity = 2
    h.reward(paid.operation_id)
    h.home()
    applied = h.next_dispatched('apply')
    assert h.device.taps.count((41, 42)) == 1 and (31, 32) not in h.device.taps
    h.scene.equipped = ('cards.health',)
    h.tick()
    assert h.store.operation(applied.operation_id).status == 'confirmed'
    h.home()
    projection = h.client.get('/api/cards').json()
    assert projection['snapshot']['capacity'] == 2
    assert projection['snapshot']['equipped'] == ['cards.health']
    h.assert_debit(paid.operation_id)
    assignments = [row for row in h.ledger() if row['kind'] == 'CARD_ASSIGN']
    assert len(assignments) == 1 and assignments[0]['delta'] == 0
    assert projection['active_budget']['spent'] == 20


@pytest.mark.parametrize('kind', ['buy', 'apply'])
def test_interrupted_reward_or_equipment_recovers_without_repeating_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    program = CardProgram(version=1, gem_cap=100,
        loadouts=(CardLoadout(id='fallback', name='Fallback', priority=('cards.health',)),))
    h = Story(tmp_path / 'a.db', monkeypatch, program=program)
    h.submit('refresh')
    h.tick()
    h.home()
    operation = h.submit(kind, 'mutation', **({'budget_cycle_id': 'cycle'} if kind == 'buy' else {'loadout_id': 'fallback'}))
    op = h.next_dispatched(kind)
    taps = list(h.device.taps)
    h.restart()
    if kind == 'buy':
        h.reward(op.operation_id, 'cards.damage')
    else:
        h.scene.equipped = ('cards.health',)
        h.tick()
        assert h.store.operation(op.operation_id).status == 'confirmed'
    assert h.device.taps[:len(taps)] == taps
    assert h.device.taps.count((11, 12) if kind == 'buy' else (41, 42)) == 1
    h.home()
    repair_card_ledger(h.store, h.shopping.journal)
    repair_card_ledger(h.store, h.shopping.journal)
    response = h.client.get('/api/cards/operations/' + operation['operation_id']).json()
    assert response['status'] == 'confirmed'
    assert len(h.ledger()) == 1
    if kind == 'buy':
        h.assert_debit(op.operation_id)
        assert response['rewards'][0]['card_id'] == 'cards.damage'
    else:
        assert response['snapshot_after']['equipped'] == ['cards.health']
        assert h.ledger()[0]['delta'] == 0


def test_same_program_two_bound_accounts_keep_separate_budgets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program = CardProgram(version=1, gem_cap=100)
    first = Story(tmp_path / 'a.db', monkeypatch, program=program)
    first.submit('refresh')
    first.tick()
    first.home()
    first.submit('buy', 'buy', budget_cycle_id='cycle')
    op = first.next_dispatched('buy')
    first.reward(op.operation_id, 'cards.damage')
    first.home()
    second = Story(tmp_path / 'b.db', monkeypatch, account_id='b', program=program)
    assert db.bound_account(first.store.path) == 'a' and db.bound_account(second.store.path) == 'b'
    assert first.client.get('/api/cards').json()['active_budget']['spent'] == 20
    assert second.client.get('/api/cards').json()['active_budget']['spent'] == 0
    second.submit('refresh')
    second.tick()
    second.home()
    second.submit('buy', 'buy', budget_cycle_id='cycle')
    other = second.next_dispatched('buy')
    second.reward(other.operation_id, 'cards.health')
    second.home()
    first.assert_debit(op.operation_id)
    second.assert_debit(other.operation_id)
    assert first.store.operation(other.operation_id) is None and second.store.operation(op.operation_id) is None
    assert first.store.active_budget().spent == second.store.active_budget().spent == 20


def test_replaced_input_lease_fences_pending_http_request_at_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fleet.input_lease import InputLease
    from supervisor import DeviceSupervisor, GuardedDevice, RecoveryState, RecoveryPreflightBlocked
    h = Story(tmp_path / 'a.db', monkeypatch)
    h.submit('refresh')
    h.tick()
    h.home()
    request = h.submit('buy', 'pending', budget_cycle_id='cycle')
    h.bot.tracker.state = screens.ScreenState.IN_RUN
    h.tick(in_run=True)
    before = list(h.device.taps)
    h.device.serial = 'synthetic:5555'
    lease = InputLease(tmp_path / 'lease.json')
    lease.grant(h.scope.generation)
    supervisor = DeviceSupervisor(path=tmp_path / 'supervisor.json', endpoint='synthetic:5555',
        connect=lambda: h.device, expected_account='a', clock=lambda: h.now,
        sleep=lambda _: None, input_lease=lease, input_generation=h.scope.generation)
    supervisor.recover()
    assert supervisor.observe(frame_digest='synthetic-before', observed_at=h.now,
        screen='MAIN_MENU', account_id='a', online_required=False) is RecoveryState.READY
    h.bot.device = GuardedDevice(supervisor)
    # Replacement wins after the HTTP queue and just before the physical input.
    # Keep the stale runner context deliberately, proving the independent lease fence.
    monkeypatch.setattr('account_collection.jitter.pause',
        lambda *_: lease.grant('replacement-generation'))
    with pytest.raises(RecoveryPreflightBlocked, match='input_generation_revoked'):
        h.tick()
    op = h.store.operation(request['operation_id'])
    assert h.device.taps == before
    assert op.status == 'reconciliation_required'
    assert h.store.active_budget().pending == 20 and h.store.active_budget().spent == 0
    assert h.ledger() == []
    replacement = Story(tmp_path / 'replacement.db', monkeypatch, account_id='replacement')
    stale = replacement.client.post('/api/cards/commands', json={
        'expected_account_id': 'a', 'expected_generation': h.scope.generation,
        'expected_epoch': 1, 'expected_program_revision': op.command.program_revision,
        'idempotency_key': 'late', 'kind': 'refresh'})
    assert stale.status_code == 409
    assert h.device.taps == before


@pytest.mark.parametrize('name', ['menu_cards', 'menu_cards_stocked'])
def test_recorded_passive_refresh_persists_without_advertising_live_mutations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from card_visit import CaptureCardAdapter
    from tests.card_fixtures import frame, recorded
    h = Story(tmp_path / 'a.db', monkeypatch)
    request = h.submit('refresh')
    h.now += 1.
    h.bot._screen = frame(name)
    h.bot._screen_fact_scope = h.scope
    h.bot._screen_captured_at = h.now
    h.bot.tracker.state = screens.ScreenState.MAIN_MENU
    h.bot.card_runtime.visit.adapter = CaptureCardAdapter(h.bot.templates)
    h.bot._advance_cards(recorded(name), opportunity=True)
    projection = h.client.get('/api/cards').json()
    assert projection['snapshot'] is not None
    assert projection['capabilities']['inventory']
    assert all(projection['capabilities'][cap] is False for cap in ('buy_one', 'buy_ten', 'buy_slot', 'assign'))
    assert h.store.snapshot() is not None and not h.store.snapshot().collection_complete
    assert h.device.taps == [] and h.ledger() == []
    assert h.store.operation(request['operation_id']).command.kind == 'refresh'
