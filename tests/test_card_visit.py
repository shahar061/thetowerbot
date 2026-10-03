"""Deterministic capture adapters exercise the real executor/journal without live calibration."""
from dataclasses import replace
from pathlib import Path
import hashlib
from typing import Any

import numpy as np
import pytest

import db
from account_screens import ControlTarget
from card_models import CardCommand, CardFieldEvidence, CardItem, CardLoadout, CardProgram, CardQuote, CardSnapshot, RewardItem
from card_store import CardStore
from currencies import CurrencyRepository
from transactions import TransactionJournal
from tests.test_card_plan import SCOPE, context


class Device:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def click(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class Replay:
    """Trust boundary: each explicitly supplied scene is one calibrated observation."""
    def __init__(self) -> None:
        self.page = 'cards'
        self.equipped: tuple[str, ...] = ()
        self.controls = {'buy': (11, 12), 'slot': (21, 22), 'cards.damage': (31, 32),
                         'cards.health': (41, 42), 'home': (51, 52), 'enter': (61, 62),
                         'dismiss': (71, 72), 'scan': (81, 82)}
        self.result: Any = None
        self.capacity = 2
        self.complete = True

    def read(self, screen: Any, boxes: Any, *, capture: Any, visit_id: str, operation: Any) -> Any:
        from card_visit import CardView
        import hashlib
        digest = hashlib.sha256(screen.tobytes()).hexdigest()
        ev = CardFieldEvidence(scope=capture.scope, visit_id=visit_id, observed_at=capture.captured_at,
            evidence_ref=digest, frame_digest=digest, confidence=.99, layout_id='fixture')
        items = tuple(CardItem(card_id=cid, ownership='owned', equipped=cid in self.equipped,
            observed_at=capture.captured_at, evidence_ref=digest,
            field_evidence={'ownership': ev, 'equipped': ev}) for cid in ('cards.damage', 'cards.health'))
        snap = CardSnapshot(scope=capture.scope, visit_id=visit_id, observed_at=capture.captured_at,
            revision=0, items=items, equipped=self.equipped, capacity=self.capacity,
            equipment_complete=self.complete, equipment_evidence=ev, confidence=.99,
            collection_complete=False, frame_digest=digest, layout_id='fixture') if self.page in ('cards', 'slot_confirmation') else None
        quotes = tuple(CardQuote(kind=kind, quantity=q, price=20*q, scope=capture.scope,
            visit_id=visit_id, observed_at=capture.captured_at, evidence_ref=digest,
            layout_id='fixture', capacity=self.capacity if kind == 'slot' else None)
            for kind, q in (('buy', 1), ('buy', 10), ('slot', 1)))
        return CardView(page=self.page, snapshot=snap, capabilities=context().capabilities,
            quotes=quotes, controls={name: ControlTarget(name, point, 'located', 1.) for name, point in self.controls.items()},
            result=self.result)


class Harness:
    def __init__(self, path: Path, kind: str = 'apply', quantity: int = 1) -> None:
        from card_visit import CardVisit
        db.bind_account(path, 'a')
        CurrencyRepository(path).bind_scope(SCOPE)
        self.store = CardStore(path)
        self.store.start_budget_cycle(scope=SCOPE, cycle_id='cycle', cap=500, now=1.)
        command = CardCommand(idempotency_key='request', scope=SCOPE, kind=kind, quantity=quantity,
            program_revision='r1', source='manual', budget_cycle_id='cycle',
            loadout_id='loadout' if kind == 'apply' else None)
        self.op = self.store.submit(command, now=2.)
        self.journal = TransactionJournal(path)
        self.replay = Replay()
        self.device = Device()
        self.ctx = context(now=10., program=CardProgram(version=1, gem_cap=500,
            loadouts=(CardLoadout(id='loadout', name='Loadout', priority=('cards.damage', 'cards.health')),)))
        self.observed: list[CardSnapshot] = []
        self.visit = CardVisit(self.store, self.journal, adapter=self.replay,
            observe=self.observe, current_context=lambda: self.ctx)
        assert self.visit.request(self.op.operation_id)

    def observe(self, snapshot: CardSnapshot) -> None:
        self.observed.append(snapshot)
        self.store.observe(snapshot)

    def advance(self, at: float, *, pixel: int | None = None, scope: Any = SCOPE) -> Any:
        from card_visit import CardCapture
        from evidence_scope import BalanceInterval
        self.ctx = self.ctx.model_copy(update={'now': at, 'balance': BalanceInterval('gems', 500, 500, SCOPE, at, 'wallet')})
        return self.visit.advance(screen=np.full((2, 2, 3), int(at) if pixel is None else pixel, dtype=np.uint8),
            boxes=(), device=self.device, context=self.ctx, capture=CardCapture(scope, at))

    def restart(self) -> None:
        from card_visit import CardVisit
        self.visit = CardVisit(self.store, self.journal, adapter=self.replay,
            observe=self.observe, current_context=lambda: self.ctx)
        assert self.visit.request(self.op.operation_id)


def test_remaining_changes_after_partial_apply() -> None:
    from card_visit import equipment_changes
    assert equipment_changes(('cards.damage',), ('cards.damage', 'cards.health')) == ((), ('cards.health',))


def test_each_toggle_is_durable_and_verified_before_next_then_home(tmp_path: Path) -> None:
    from card_visit import assignment_steps
    h = Harness(tmp_path / 'a.db')
    h.advance(10.)
    assert h.device.taps == [(31, 32)]
    steps = assignment_steps(h.store, h.op.operation_id)
    assert len(steps) == 1 and steps[0]['verified_at'] is None
    h.advance(11.)  # no observed effect: never tap again
    assert h.device.taps == [(31, 32)]
    h.replay.equipped = ('cards.damage',)
    h.advance(12.)
    assert h.device.taps == [(31, 32), (41, 42)]
    h.replay.equipped = ('cards.damage', 'cards.health')
    h.advance(13.)
    assert h.store.operation(h.op.operation_id).status == 'confirmed'
    assert h.visit.active and not h.visit.home_observed
    h.advance(14.)
    assert h.device.taps[-1] == (51, 52)
    h.replay.page = 'home'
    h.advance(15.)
    assert not h.visit.active and h.visit.home_observed
    steps = assignment_steps(h.store, h.op.operation_id)
    assert [(s['sequence'], s['target'], s['verified_at']) for s in steps] == [(1, 'cards.damage', 12.), (2, 'cards.health', 13.)]
    assert steps[1]['before'] == ['cards.damage'] and steps[1]['after'] == ['cards.damage', 'cards.health']
    assert len(h.observed) >= 3


def test_restart_never_blindly_retaps_and_pins_target_across_edit(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db')
    h.advance(10.)
    h.restart()
    h.ctx = h.ctx.model_copy(update={'program': CardProgram(version=1, gem_cap=500)})
    h.advance(11.)
    assert h.device.taps == [(31, 32)]
    h.replay.equipped = ('cards.damage',)
    h.advance(12.)
    assert h.device.taps == [(31, 32), (41, 42)]
    h.restart()
    h.replay.equipped = ('cards.damage', 'cards.health')
    h.advance(13.)
    assert h.store.operation(h.op.operation_id).status == 'confirmed'
    assert len(h.device.taps) == 2


@pytest.mark.parametrize('gate', ['paused', 'armed', 'in_run'])
def test_dispatch_rechecks_live_gates_after_jitter(tmp_path: Path, monkeypatch: Any, gate: str) -> None:
    from strategy import Strategy, ActionRule
    import account_collection
    h = Harness(tmp_path / 'a.db')
    h.visit.tuning = Strategy(name='test', actions=(ActionRule(name='Damage', template='upgrade_damage.png', enabled=False),))
    monkeypatch.setattr(account_collection.jitter, 'pause', lambda *args: setattr(h, 'ctx', h.ctx.model_copy(update={gate: gate != 'armed'})))
    h.advance(10.)
    assert h.device.taps == []
    assert h.store.operation(h.op.operation_id).status == 'preflight'


def test_capture_scope_is_not_relabelled_and_timeout_does_not_claim_home(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db')
    h.advance(10., scope=replace(SCOPE, epoch=2))
    assert not h.observed and not h.device.taps
    h.advance(131.)
    assert not h.visit.active and not h.visit.home_observed


def test_disappearing_target_noop_and_clear(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db', kind='clear')
    h.replay.equipped = ('cards.damage',)
    del h.replay.controls['cards.damage']
    h.advance(10.)
    assert h.device.taps == []
    h.replay.controls['cards.damage'] = (31, 32)
    h.advance(11.)
    assert h.device.taps == [(31, 32)]
    h.replay.equipped = ()
    h.advance(12.)
    assert h.store.operation(h.op.operation_id).status == 'confirmed'
    other = Harness(tmp_path / 'b.db', kind='clear')
    other.advance(10.)
    assert other.device.taps == []
    assert other.store.operation(other.op.operation_id).status == 'confirmed'


@pytest.mark.parametrize('quantity', [1, 10])
def test_paid_replay_settles_once_and_never_redispatches(tmp_path: Path, quantity: int) -> None:
    from card_transactions import CardResultEvidence
    from evidence_scope import BalanceInterval
    from card_visit import CardCapture
    h = Harness(tmp_path / 'a.db', kind='buy', quantity=quantity)
    h.advance(10.)
    assert h.device.taps == [(11, 12)]
    assert h.store.budget('cycle').pending == 20 * quantity
    h.restart()
    h.advance(11.)
    assert h.device.taps == [(11, 12)]
    h.replay.result = CardResultEvidence(operation_id=h.op.operation_id, action_sequence=1,
        scope=SCOPE, visit_id=h.visit.visit_id, observed_at=12., evidence_ref='reward',
        frame_digest=hashlib.sha256(np.full((2, 2, 3), 12, dtype=np.uint8).tobytes()).hexdigest(), layout_id='fixture', acquisition_observed=True, reward_flow_complete=True,
        rewards=tuple(RewardItem(position=p, card_id='cards.damage', quantity=1) for p in range(quantity)))
    h.ctx = h.ctx.model_copy(update={'now': 12., 'balance': BalanceInterval('gems', 500-20*quantity, 500-20*quantity, SCOPE, 12., 'wallet')})
    h.visit.advance(screen=np.full((2, 2, 3), 12, dtype=np.uint8), boxes=(), device=h.device,
        context=h.ctx, capture=CardCapture(SCOPE, 12.))
    assert h.store.operation(h.op.operation_id).status == 'confirmed'
    assert h.store.budget('cycle').spent == 20 * quantity
    assert len(h.device.taps) == 1


def test_cancel_after_claim_retains_reconciliation_and_before_claim_sends_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db')
    h.advance(10.)
    h.visit.cancel('paused')
    assert h.store.operation(h.op.operation_id).status == 'reconciliation_required'
    h.advance(11.)
    assert h.device.taps == [(31, 32)]
    other = Harness(tmp_path / 'b.db')
    other.visit.cancel('canceled')
    other.advance(10.)
    assert not other.device.taps
    assert other.store.operation(other.op.operation_id).status == 'canceled'


def test_navigation_waits_for_page_transition_and_bounds_unknown_screen(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db', kind='refresh')
    h.replay.page = 'home'
    h.advance(10.)
    h.advance(11.)  # a new home frame is not evidence that enter succeeded
    assert h.device.taps == [(61, 62)]
    h.replay.page = 'intro'
    h.advance(12.)
    h.replay.page = 'cards'
    h.advance(13.)
    h.advance(14.)
    h.replay.page = 'home'
    h.advance(15.)
    assert h.device.taps == [(61, 62), (71, 72), (51, 52)]
    assert h.visit.home_observed
    other = Harness(tmp_path / 'b.db')
    other.replay.page = 'unknown'
    other.advance(10.)
    other.advance(20.)
    assert not other.device.taps and not other.visit.active and not other.visit.home_observed


def test_scan_stops_on_repeated_content_and_twenty_passes(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db')
    h.replay.complete = False
    h.advance(10., pixel=1)
    h.advance(11., pixel=1)
    assert not h.visit.active
    assert h.device.taps == [(81, 82)]
    other = Harness(tmp_path / 'b.db')
    other.replay.complete = False
    for i in range(24):
        other.advance(float(10+i))
    assert not other.visit.active and len(other.device.taps) <= 20


def test_restart_does_not_reset_execution_deadline(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db')
    h.advance(10.)
    h.restart()
    h.replay.equipped = ('cards.damage',)
    h.advance(131.)
    assert h.device.taps == [(31, 32)]
    h.advance(252.)
    assert not h.visit.active and not h.visit.home_observed


def test_slot_confirmation_final_control_and_scoped_growth(tmp_path: Path) -> None:
    import hashlib
    from card_visit import CardCapture
    from card_transactions import CardResultEvidence
    from evidence_scope import BalanceInterval
    h = Harness(tmp_path / 'a.db', kind='slot')
    h.replay.controls['slot_open'] = (25, 26)
    h.advance(10.)
    assert h.device.taps == [(25, 26)] and h.store.operation(h.op.operation_id).transaction_key is None
    del h.replay.controls['slot_open']
    h.advance(11.)
    # Wait for an actual confirmation screen before its final charged control.
    assert h.device.taps == [(25, 26)]
    h.replay.page = 'slot_confirmation'
    h.advance(12.)
    assert h.device.taps == [(25, 26), (21, 22)]
    h.replay.page, h.replay.capacity = 'cards', 3
    frame = np.full((2, 2, 3), 13, dtype=np.uint8)
    h.replay.result = CardResultEvidence(operation_id=h.op.operation_id, action_sequence=1,
        scope=SCOPE, visit_id=h.visit.visit_id, observed_at=13., evidence_ref='growth',
        frame_digest=hashlib.sha256(frame.tobytes()).hexdigest(), layout_id='fixture',
        acquisition_observed=True, reward_flow_complete=True)
    h.ctx = h.ctx.model_copy(update={'now': 13., 'balance': BalanceInterval('gems', 480, 480, SCOPE, 13., 'wallet')})
    h.visit.advance(screen=frame, boxes=(), device=h.device, context=h.ctx, capture=CardCapture(SCOPE, 13.))
    assert h.store.operation(h.op.operation_id).status == 'confirmed'
    assert h.store.budget('cycle').spent == 20


def test_missing_live_guard_never_uses_static_context_to_dispatch(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db')
    h.visit.current_context = None
    h.advance(10.)
    assert h.device.taps == []


def test_old_capture_time_and_foreign_equipment_proof_are_not_authority(tmp_path: Path) -> None:
    from card_visit import CardCapture
    h = Harness(tmp_path / 'a.db')
    h.ctx = h.ctx.model_copy(update={'now': 100.})
    h.visit.advance(screen=np.ones((2, 2, 3), dtype=np.uint8), boxes=(), device=h.device,
        context=h.ctx, capture=CardCapture(SCOPE, 10.))
    assert not h.observed and not h.device.taps


def test_real_capture_adapter_never_exposes_mutation_controls(tmp_path: Path) -> None:
    import cv2
    from card_visit import CardCapture, CaptureCardAdapter
    from tests.card_fixtures import recorded
    h = Harness(tmp_path / 'a.db')
    adapter = CaptureCardAdapter()
    for name in ('menu_cards', 'menu_cards_stocked'):
        screen = cv2.imread(str(Path(__file__).parent / 'fixtures' / f'{name}.png'))
        view = adapter.read(screen, recorded(name), capture=CardCapture(SCOPE, 10.),
                            visit_id='visit', operation=h.op)
        assert view.capabilities.inventory
        assert not any((view.capabilities.buy_one, view.capabilities.buy_ten, view.capabilities.buy_slot, view.capabilities.assign))
        assert set(view.controls) <= {'home', 'dismiss'}


def test_completed_operation_keeps_home_ownership_across_restart(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db', kind='clear')
    h.advance(10.)
    assert h.store.operation(h.op.operation_id).status == 'confirmed'
    h.restart()
    assert h.visit.active and not h.visit.home_observed
    h.advance(11.)
    assert h.device.taps == [(51, 52)]
    h.replay.page = 'home'
    h.advance(12.)
    assert h.visit.home_observed and not h.visit.active
    assert not h.visit.request(h.op.operation_id)


def test_new_capture_cannot_lend_its_digest_to_different_result(tmp_path: Path) -> None:
    from card_transactions import CardResultEvidence
    h = Harness(tmp_path / 'a.db', kind='buy')
    h.advance(10.)
    h.replay.result = CardResultEvidence(operation_id=h.op.operation_id, action_sequence=1,
        scope=SCOPE, visit_id=h.visit.visit_id, observed_at=11., evidence_ref='different-result',
        frame_digest='different-frame', layout_id='fixture', acquisition_observed=True,
        reward_flow_complete=True, rewards=(RewardItem(position=0, card_id='cards.damage', quantity=1),))
    h.advance(11.)
    assert not h.visit.active
    assert h.store.operation(h.op.operation_id).status == 'reconciliation_required'
    assert h.store.budget('cycle').pending == 20


@pytest.mark.parametrize('kind', ['buy', 'apply'])
def test_claim_is_durable_before_device_exception_and_restart_never_replays(tmp_path: Path, kind: str) -> None:
    from card_visit import assignment_steps
    h = Harness(tmp_path / 'a.db', kind=kind)
    class BrokenDevice:
        def click(self, x: int, y: int) -> None:
            op = h.store.operation(h.op.operation_id)
            assert op.status == 'dispatched'
            if kind == 'buy':
                assert h.journal._require(op.transaction_key).acted_at == 10.
            else:
                assert assignment_steps(h.store, op.operation_id)[-1]['claimed_at'] == 10.
            raise OSError('connection lost after sending')
    h.device = BrokenDevice()
    with pytest.raises(OSError):
        h.advance(10.)
    assert h.store.operation(h.op.operation_id).status == 'reconciliation_required'
    h.device = Device()
    h.restart()
    h.advance(11.)
    assert h.device.taps == []


def test_atomic_route_cap_and_shared_commitment_override_stale_advisory_context(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db', kind='buy')
    h.visit.route_remaining = lambda: 19
    h.advance(10.)
    assert h.device.taps == [] and h.store.budget('cycle').pending == 0
    h.visit.route_remaining = lambda: 500
    with h.store._write() as conn:
        conn.execute("INSERT INTO currency_commitments VALUES ('lab','gems',490)")
    h.advance(11.)
    assert not h.device.taps
    assert h.store.budget('cycle').pending == 0


def test_jitter_cancel_and_scope_change_block_all_claims(tmp_path: Path, monkeypatch: Any) -> None:
    from strategy import Strategy, ActionRule
    import account_collection
    h = Harness(tmp_path / 'a.db', kind='buy')
    h.visit.tuning = Strategy(name='test', actions=(ActionRule(name='Damage', template='upgrade_damage.png'),))
    monkeypatch.setattr(account_collection.jitter, 'pause', lambda *args: h.visit.cancel('operator'))
    h.advance(10.)
    assert h.device.taps == []
    assert h.store.operation(h.op.operation_id).status == 'canceled'
    assert not h.journal.open_transactions()


def test_assignment_final_proof_must_follow_latest_toggle_claim(tmp_path: Path) -> None:
    from card_visit import CardCapture
    h = Harness(tmp_path / 'a.db')
    h.advance(10.)
    h.replay.equipped = ('cards.damage',)
    h.advance(11.)
    h.replay.equipped = ('cards.damage', 'cards.health')
    h.ctx = h.ctx.model_copy(update={'now': 12.})
    h.visit.advance(screen=np.full((2, 2, 3), 12, dtype=np.uint8), boxes=(), device=h.device,
                    context=h.ctx, capture=CardCapture(SCOPE, 11.))
    assert h.store.operation(h.op.operation_id).status != 'confirmed'
    assert len(h.device.taps) == 2


def test_shared_logical_visit_cap_survives_separate_operations_and_restart(tmp_path: Path) -> None:
    from card_visit import CardCapture, CardVisit, visit_purchases
    from card_transactions import CardResultEvidence
    from evidence_scope import BalanceInterval
    from strategy import CardPolicy
    import hashlib
    h = Harness(tmp_path / 'a.db', kind='buy')
    h.ctx = h.ctx.model_copy(update={'policy': CardPolicy(enabled=True, max_per_visit=1)})
    h.advance(10.)
    frame = np.full((2, 2, 3), 11, dtype=np.uint8)
    h.replay.result = CardResultEvidence(operation_id=h.op.operation_id, action_sequence=1,
        scope=SCOPE, visit_id=h.visit.visit_id, observed_at=11., evidence_ref='result',
        frame_digest=hashlib.sha256(frame.tobytes()).hexdigest(), layout_id='fixture', acquisition_observed=True,
        reward_flow_complete=True, rewards=(RewardItem(position=0, card_id='cards.damage', quantity=1),))
    h.ctx = h.ctx.model_copy(update={'now': 11., 'balance': BalanceInterval('gems',480,480,SCOPE,11.,'wallet')})
    h.visit.advance(screen=frame, boxes=(), device=h.device, context=h.ctx, capture=CardCapture(SCOPE,11.))
    flow = h.visit.visit_id
    assert visit_purchases(h.store, flow) == 1
    h.replay.result = None
    h.replay.page = 'home'
    h.advance(12.)
    next_op = h.store.submit(h.op.command.model_copy(update={'idempotency_key': 'next'}), now=13.)
    h.visit = CardVisit(h.store, h.journal, adapter=h.replay, current_context=lambda: h.ctx)
    assert h.visit.request(next_op.operation_id, visit_id=flow)
    h.replay.page = 'cards'
    h.advance(14.)
    assert h.device.taps == [(11, 12)]
    assert h.store.operation(next_op.operation_id).reason == 'visit_limit'
    assert h.visit.cards_bought == 1


def test_retained_snapshot_cannot_hide_earlier_unverified_claims(tmp_path: Path) -> None:
    from card_visit import latest_card_mutation
    h = Harness(tmp_path / 'a.db')
    assert latest_card_mutation(h.store) is None
    h.advance(10.)
    assert latest_card_mutation(h.store) == 10.
    h.replay.equipped = ('cards.damage',)
    h.advance(11.)
    h.restart()
    assert latest_card_mutation(h.store) == 11.


def test_recovery_adopts_original_journal_flow_without_retagging(tmp_path: Path) -> None:
    from tests.test_card_transactions import setup, prepare
    from card_visit import CardVisit
    store, journal, op_id = setup(tmp_path)
    prepare(store, journal, op_id)
    visit = CardVisit(store, journal, adapter=Replay())
    assert visit.request(op_id)
    assert visit.visit_id == 'visit'


def test_automatic_assignment_stops_when_automation_changes_after_first_toggle(tmp_path: Path) -> None:
    from strategy import CardPolicy
    h = Harness(tmp_path / 'a.db')
    auto = h.store.submit(h.op.command.model_copy(update={'idempotency_key': 'auto', 'source': 'automatic'}), now=3.)
    h.visit.cancel('replace')
    assert h.visit.request(auto.operation_id)
    h.advance(10.)
    h.replay.equipped = ('cards.damage',)
    h.ctx = h.ctx.model_copy(update={'policy': CardPolicy(enabled=False)})
    h.advance(11.)
    assert h.device.taps == [(31, 32)]


def test_real_refresh_walk_uses_measured_tabs_and_observes_home(tmp_path: Path, monkeypatch: Any) -> None:
    from card_visit import CardCapture, CaptureCardAdapter
    from tests.card_fixtures import recorded, frame
    import ocr
    h = Harness(tmp_path / 'a.db', kind='refresh')
    h.visit.adapter = CaptureCardAdapter()
    # Account-panel crop examination finds no Settings/Stats overlay on these real frames.
    monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: ())
    for at, name in ((10., 'menu_main'), (11., 'menu_cards'), (12., 'menu_cards'), (13., 'menu_main')):
        h.ctx = h.ctx.model_copy(update={'now': at})
        h.visit.advance(screen=frame(name), boxes=recorded(name) if name == 'menu_cards' else (),
                        device=h.device, context=h.ctx, capture=CardCapture(SCOPE, at))
    assert h.device.taps == [(446, 2312), (84, 2312)]
    assert h.visit.home_observed and not h.visit.active


def test_resumed_recovery_restores_durable_home_ownership(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db', kind='clear')
    h.replay.equipped = ('cards.damage',)
    h.advance(10.)
    h.visit.cancel('pause')
    h.restart()
    h.replay.equipped = ()
    h.advance(11.)
    assert h.store.operation(h.op.operation_id).status == 'confirmed'
    h.restart()
    assert h.visit.active and not h.visit.home_observed


def test_changed_same_frame_equipment_reader_refuses_dispatch(tmp_path: Path, monkeypatch: Any) -> None:
    from strategy import Strategy, ActionRule
    import account_collection
    h = Harness(tmp_path / 'a.db')
    h.visit.tuning = Strategy(name='test', actions=(ActionRule(name='Damage', template='upgrade_damage.png'),))
    monkeypatch.setattr(account_collection.jitter, 'pause', lambda *args: setattr(h.replay, 'equipped', ('cards.health',)))
    h.advance(10.)
    assert not h.device.taps


def test_restart_rejects_home_proof_before_completion_and_return_claim(tmp_path: Path) -> None:
    from card_visit import CardCapture
    h = Harness(tmp_path / 'a.db', kind='clear')
    h.advance(10.)
    h.restart()
    h.replay.page = 'home'
    h.ctx = h.ctx.model_copy(update={'now': 11.})
    h.visit.advance(screen=np.full((2, 2, 3), 9, dtype=np.uint8), boxes=(), device=h.device,
                    context=h.ctx, capture=CardCapture(SCOPE, 9.))
    assert h.visit.active and not h.visit.home_observed
    assert not h.device.taps
    h.replay.page = 'cards'
    h.advance(12.)
    assert h.device.taps == [(51, 52)]
    h.restart()
    h.replay.page = 'home'
    h.ctx = h.ctx.model_copy(update={'now': 13.})
    h.visit.advance(screen=np.full((2, 2, 3), 11, dtype=np.uint8), boxes=(), device=h.device,
                    context=h.ctx, capture=CardCapture(SCOPE, 11.))
    assert h.visit.active and not h.visit.home_observed
    h.advance(14.)
    assert h.visit.home_observed and not h.visit.active


def test_restart_after_toggle_verification_rejects_older_final_proof(tmp_path: Path, monkeypatch: Any) -> None:
    import card_visit
    h = Harness(tmp_path / 'a.db')
    h.advance(10.)
    h.replay.equipped = ('cards.damage',)
    h.advance(11.)
    h.replay.equipped = ('cards.damage', 'cards.health')
    reconcile = card_visit.reconcile_card_operation
    def crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError('crash after verified step, before final reconciliation')
    monkeypatch.setattr(card_visit, 'reconcile_card_operation', crash)
    with pytest.raises(RuntimeError):
        h.advance(12.)
    assert card_visit.assignment_steps(h.store, h.op.operation_id)[-1]['verified_at'] == 12.
    monkeypatch.setattr(card_visit, 'reconcile_card_operation', reconcile)
    h.restart()
    h.ctx = h.ctx.model_copy(update={'now': 13.})
    h.visit.advance(screen=np.full((2, 2, 3), 15, dtype=np.uint8), boxes=(), device=h.device,
                    context=h.ctx, capture=card_visit.CardCapture(SCOPE, 10.5))
    assert h.store.operation(h.op.operation_id).status != 'confirmed'
    assert h.store.operation(h.op.operation_id).snapshot_after is None
    h.advance(14.)
    assert h.store.operation(h.op.operation_id).status == 'confirmed'
    assert h.store.operation(h.op.operation_id).snapshot_after.observed_at == 14.


@pytest.mark.parametrize('resume', ['restart', 'next_operation'])
def test_scan_allowance_is_shared_durably_across_requests(tmp_path: Path, resume: str) -> None:
    h = Harness(tmp_path / 'a.db')
    h.replay.complete = False
    for at in range(10, 20):
        h.advance(float(at))
    assert len(h.device.taps) == 10
    if resume == 'restart':
        h.restart()
    else:
        flow = h.visit.visit_id
        h.visit.cancel('next operation')
        op = h.store.submit(h.op.command.model_copy(update={'idempotency_key': 'next-scan'}), now=20.)
        assert h.visit.request(op.operation_id, visit_id=flow)
    for at in range(20, 32):
        h.advance(float(at))
    assert len(h.device.taps) == 20
    assert not h.visit.active and h.visit.reason == 'scan_limit'


def test_scan_claim_survives_device_failure_and_preserves_content_fence(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'a.db')
    h.replay.complete = False
    class BrokenDevice:
        def click(self, x: int, y: int) -> None:
            raise OSError('scan may have been sent')
    h.device = BrokenDevice()
    with pytest.raises(OSError):
        h.advance(10., pixel=1)
    # Scanning is free navigation: recover its stopped operation via a different request in the same flow.
    flow = h.visit.visit_id
    op = h.store.submit(h.op.command.model_copy(update={'idempotency_key': 'after-scan-crash'}), now=11.)
    h.device = Device()
    assert h.visit.request(op.operation_id, visit_id=flow)
    h.advance(12., pixel=1)
    assert not h.device.taps
    assert not h.visit.active and h.visit.reason == 'scan_no_progress'


def test_canceled_assignment_finalization_restart_keeps_first_partial_proof(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(tmp_path / 'account.db')
    h.advance(10.)
    h.visit.cancel('cancel_requested')
    h.restart()
    h.replay.equipped = ('cards.damage',)
    transition = h.store.transition
    def crash(operation_id: str, **kwargs: Any) -> Any:
        if kwargs.get('status') == 'canceled':
            raise RuntimeError('crash after partial evidence')
        return transition(operation_id, **kwargs)
    monkeypatch.setattr(h.store, 'transition', crash)
    with pytest.raises(RuntimeError, match='partial evidence'):
        h.advance(12.)
    partial = h.store.operation(h.op.operation_id).snapshot_after
    monkeypatch.setattr(h.store, 'transition', transition)
    h.restart()
    h.advance(13.)
    actual = h.store.operation(h.op.operation_id)
    assert actual.status == 'canceled' and actual.snapshot_after == partial
    assert h.device.taps == [(31, 32)]


def test_canceled_assignment_unknown_equipment_does_not_scan_or_toggle(tmp_path: Path) -> None:
    h = Harness(tmp_path / 'account.db')
    h.advance(10.)
    h.visit.cancel('cancel_requested')
    h.restart()
    h.replay.complete = False
    h.advance(12.)
    assert h.device.taps == [(31, 32)]
    assert h.store.operation(h.op.operation_id).status == 'reconciliation_required'
