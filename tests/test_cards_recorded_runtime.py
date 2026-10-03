"""Real capture adapter through HTTP/runtime/journal; device only records input."""
from pathlib import Path

import pytest
import screens
import db
from card_visit import CaptureCardAdapter
from tests.card_fixtures import frame, recorded, replaced
from tests.test_cards_story import Story
from card_models import CardLoadout, CardProgram


def tick(h: Story, name: str, *, boxes: tuple | None = None) -> None:
    h.now += 1.
    h.bot.tracker.state = screens.ScreenState.MAIN_MENU
    h.bot._screen = frame(name)
    h.bot._screen_fact_scope = h.account.verified_scope
    h.bot._screen_captured_at = h.now
    h.bot.card_runtime.visit.adapter = CaptureCardAdapter(h.bot.templates)
    h.bot._advance_cards(recorded(name) if boxes is None else boxes, opportunity=True)


def discover(h: Story, name: str = 'menu_cards_compact_empty') -> None:
    h.submit('refresh', key='discover')
    tick(h, name)
    tick(h, name)
    tick(h, 'cards_compact_home')
    h.device.taps.clear()


@pytest.mark.parametrize('before,reward,after,wallet', [
    ('menu_cards_compact_empty', 'card_reward_new', 'menu_cards_compact_damage', 284),
    ('menu_cards_compact_damage', 'card_reward_duplicate', 'menu_cards_compact_duplicate', 264),
])
def test_recorded_single_purchase_settles_once_claims_and_returns_home(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: str,
        reward: str, after: str, wallet: int) -> None:
    h = Story(tmp_path / 'account.db', monkeypatch)
    discover(h, before)
    submitted = h.submit('buy', quantity=1, budget_cycle_id='cycle')
    tick(h, before)
    assert len(h.device.taps) == 1
    tick(h, reward)
    op = h.store.operation(submitted['operation_id'])
    assert op.status == 'confirmed' and op.spent_gems == 20
    assert h.shopping.journal._require(op.transaction_key).reconciliation['wallet_after'] == wallet
    tick(h, reward)
    assert h.account.currencies.balance('gems', scope=h.scope, now=h.now).lower == wallet
    assert len(h.device.taps) == 2 and 1830 < h.device.taps[-1][1] < 1960
    tick(h, reward)
    assert len(h.device.taps) == 2
    tick(h, after)
    assert len(h.device.taps) == 3 and h.device.taps[-1][1] > 2200
    tick(h, 'cards_compact_home')
    assert h.bot.card_runtime.visit.home_observed and not h.bot.card_runtime.active
    assert h.store.budget('cycle').spent == 20
    assert len(h.ledger()) == 1 and h.ledger()[0]['delta'] == -20
    with db.reader(h.store.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 1


@pytest.mark.parametrize('restart_after_claim', [False, True])
def test_recorded_restart_never_repeats_paid_or_claim_input(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restart_after_claim: bool) -> None:
    h = Story(tmp_path / 'account.db', monkeypatch)
    discover(h)
    submitted = h.submit('buy', quantity=1, budget_cycle_id='cycle')
    tick(h, 'menu_cards_compact_empty')
    if restart_after_claim:
        tick(h, 'card_reward_new')
        tick(h, 'card_reward_new')
    h.restart()
    if restart_after_claim:
        tick(h, 'card_reward_new')
        tick(h, 'card_reward_new')
        assert len(h.device.taps) == 2
    else:
        tick(h, 'card_reward_early')
        assert len(h.device.taps) == 1
        assert h.store.budget('cycle').pending == 20
        tick(h, 'card_reward_new')
        tick(h, 'card_reward_new')
        assert len(h.device.taps) == 2
    tick(h, 'menu_cards_compact_damage')
    tick(h, 'cards_compact_home')
    assert h.store.operation(submitted['operation_id']).status == 'confirmed'
    assert len(h.ledger()) == 1 and h.ledger()[0]['delta'] == -20
    assert h.bot.card_runtime.visit.home_observed


@pytest.mark.parametrize('wallet_text', ['?', '290'])
def test_unresolved_recorded_wallet_keeps_reward_until_exact_settlement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wallet_text: str) -> None:
    h = Story(tmp_path / 'account.db', monkeypatch)
    discover(h)
    submitted = h.submit('buy', quantity=1, budget_cycle_id='cycle')
    tick(h, 'menu_cards_compact_empty')
    uncertain = replaced(recorded('card_reward_new'), '284', into=wallet_text)
    assert uncertain != recorded('card_reward_new')
    tick(h, 'card_reward_new', boxes=uncertain)
    tick(h, 'card_reward_new', boxes=uncertain)
    assert len(h.device.taps) == 1
    assert h.store.budget('cycle').pending == 20
    h.restart()
    tick(h, 'card_reward_new')
    tick(h, 'card_reward_new')
    assert h.store.operation(submitted['operation_id']).status == 'confirmed'
    assert len(h.device.taps) == 2
    assert len(h.ledger()) == 1 and h.ledger()[0]['delta'] == -20


def test_recorded_apply_then_clear_confirms_observed_equipment(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    program = CardProgram(version=1, gem_cap=100, loadouts=(
        CardLoadout(id='damage', name='Damage', priority=('cards.damage',)),))
    h = Story(tmp_path / 'account.db', monkeypatch, program=program)
    discover(h, 'menu_cards_compact_damage')
    applied = h.submit('apply', key='apply', loadout_id='damage')
    tick(h, 'menu_cards_compact_damage')
    assert len(h.device.taps) == 1
    tick(h, 'menu_cards_compact_equipped')
    assert h.store.operation(applied['operation_id']).status == 'confirmed'
    tick(h, 'menu_cards_compact_equipped')
    tick(h, 'cards_compact_home')
    h.device.taps.clear()
    cleared = h.submit('clear', key='clear')
    tick(h, 'menu_cards_compact_equipped')
    assert len(h.device.taps) == 1
    tick(h, 'menu_cards_compact_restored')
    assert h.store.operation(cleared['operation_id']).status == 'confirmed'
    tick(h, 'menu_cards_compact_restored')
    tick(h, 'cards_compact_home')
    assert h.bot.card_runtime.visit.home_observed
    assert h.store.budget('cycle').spent == 0
    assert len(h.ledger()) == 2
