"""Recorded production adapter calibration; no device or live account access."""
import pytest
import ocr

from card_models import CardCommand, CardOperation
from card_visit import CaptureCardAdapter, CardCapture, CardView
from evidence_scope import FactScope
from tests.card_fixtures import frame, recorded, replaced

SCOPE = FactScope('account-a', 'lease-a', 'generation-a', 1)


def operation(*, dispatched: bool = False, quantity: int = 1) -> CardOperation:
    return CardOperation(operation_id='operation-a', command=CardCommand(
        idempotency_key='command-a', scope=SCOPE, program_revision='revision-a',
        kind='buy', quantity=quantity, source='manual', budget_cycle_id='cycle-a'),
        status='dispatched' if dispatched else 'queued', created_at=1., updated_at=1.,
        transaction_key='transaction-a' if dispatched else None)


def view(name: str, *, dispatched: bool = False, quantity: int = 1,
         boxes: tuple[ocr.TextBox, ...] | None = None) -> CardView:
    return CaptureCardAdapter().read(frame(name), recorded(name) if boxes is None else boxes,
        capture=CardCapture(SCOPE, 10.), visit_id='visit-a',
        operation=operation(dispatched=dispatched, quantity=quantity))


def test_compact_quote_wallet_and_single_control_are_measured() -> None:
    result = view('menu_cards_compact_empty')
    assert result.capabilities.buy_one
    assert not result.capabilities.buy_ten and not result.capabilities.buy_slot
    assert [(q.kind, q.quantity, q.price) for q in result.quotes] == [('buy', 1, 20)]
    assert result.balance.lower == result.balance.upper == 304
    assert result.balance.scope == SCOPE and result.balance.observed_at == 10.
    assert result.controls['buy'].status == 'located'
    assert 375 < result.controls['buy'].point[0] < 715


@pytest.mark.parametrize('name,gems', [('card_reward_new', 284), ('card_reward_duplicate', 264)])
def test_settled_single_receipt_has_claim_only_and_exact_attributed_wallet(name: str, gems: int) -> None:
    result = view(name, dispatched=True)
    assert result.page == 'rewards'
    assert set(result.controls) == {'continue'}
    assert result.balance.lower == result.balance.upper == gems
    assert result.result.operation_id == 'operation-a'
    assert result.result.acquisition_observed and result.result.reward_flow_complete
    assert [(r.position, r.card_id, r.quantity) for r in result.result.rewards] == [(0, 'cards.damage', 1)]


@pytest.mark.parametrize('name', ['card_reward_early', 'card_reward_animating'])
def test_animation_is_not_a_receipt_or_a_purchase_target(name: str) -> None:
    result = view(name, dispatched=True)
    assert result.result is None and result.controls == {}


@pytest.mark.parametrize('dispatched,quantity', [(False, 1), (True, 10)])
def test_reward_screen_does_not_attribute_queued_or_batch_operations(dispatched: bool, quantity: int) -> None:
    result = view('card_reward_new', dispatched=dispatched, quantity=quantity)
    assert result.result is None and result.controls == {}


def test_uncertain_quote_or_receipt_anchor_refuses_action() -> None:
    result = view('menu_cards_compact_empty', boxes=replaced(recorded('menu_cards_compact_empty'),
        '20', into='20', confidence=.4))
    assert not result.capabilities.buy_one and 'buy' not in result.controls
    result = view('card_reward_new', dispatched=True,
        boxes=replaced(recorded('card_reward_new'), 'CLAIM', into='CLAIM', confidence=.4))
    assert result.result is None and result.controls == {}


def test_compact_equipment_is_complete_from_the_entire_active_strip() -> None:
    result = view('menu_cards_compact_equipped')
    assert result.snapshot.equipment_complete
    assert result.snapshot.equipped == ('cards.damage',)
    assert result.capabilities.assign
    assert result.controls['cards.damage'].status == 'located'
    empty = view('menu_cards_compact_restored')
    assert empty.snapshot.equipment_complete and empty.snapshot.equipped == ()


def test_detail_overlay_never_exposes_purchase_or_toggle_controls() -> None:
    result = view('card_detail_damage')
    assert 'buy' not in result.controls and 'cards.damage' not in result.controls
