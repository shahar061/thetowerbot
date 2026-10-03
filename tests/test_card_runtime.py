"""Real runner/scheduler/store integration; only the device and capture reader are fake."""
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import db
import config
import screens
import vision
from account_state import AccountState
from card_models import CardCommand, CardProgram
from card_store import CardStore
from control import Controls
from events import EventBus
from evidence_scope import BalanceInterval
from fleet.identity import IdentityEvidence
from runner import BotRunner, RunnerError
from shopping import ShoppingSession
from sinks.state import BotState
from strategy import ActionRule, CardPolicy, Shopping, Strategy
from tower_bot import TowerBot
from transactions import TransactionJournal
from tests.test_card_visit import Device, Replay
from tests.test_card_plan import SCOPE
from tests.conftest import _frame


class CardsRuntimeHarness:
    def __init__(self, path: Path, monkeypatch: Any) -> None:
        self.now = 100.
        self.gems = 500
        monkeypatch.setattr('time.time', lambda: self.now)
        self.inputs: list[tuple[int, int]] = []
        self.device = Device()
        self.device.taps = self.inputs
        db.bind_account(path, 'a')
        self.account = AccountState()
        self.account.attach_safety_storage(path)
        self.account.bind_scope(SCOPE, identity=IdentityEvidence('a', 1., 'identity'))
        self.controls = Controls(Strategy(name='cards', tap_jitter_px=0., timing_jitter=0., tap_delay=0., actions=(ActionRule(name='Damage',
            template='upgrade_damage.png', enabled=False),), cards=CardProgram(version=1, gem_cap=100),
            shopping=Shopping(enabled=True, armed=True, cards=CardPolicy(enabled=False, gem_floor=0))))
        self.bus = EventBus()
        self.templates = vision.TemplateCache(config.TEMPLATE_DIR)
        self.shopping = ShoppingSession(self.templates, self.bus, None)
        self.shopping.journal = TransactionJournal(path)
        self.bot = TowerBot(device=self.device, templates=self.templates, bus=self.bus,
            controls=self.controls, shopping=self.shopping, account_state=self.account)
        self.runner = BotRunner(bus=self.bus, controls=self.controls, state=BotState(),
            templates=self.templates, device_factory=lambda: self.device, checks={},
            shopping=self.shopping, account_state=self.account)
        self.runner._bot = self.bot
        monkeypatch.setattr(self.runner, '_running_locked', lambda: True)
        self.store = CardStore(path)
        self.store.start_budget_cycle(scope=SCOPE, cycle_id='cycle', cap=100, now=2.)
        self.replay = Replay()

    @property
    def buy_command(self) -> CardCommand:
        context = self.runner.cards_context()
        return CardCommand(idempotency_key='manual-buy', scope=SCOPE,
            program_revision=context.program_revision, kind='buy', quantity=1,
            source='manual', budget_cycle_id='cycle')

    def set_paused(self, value: bool) -> None:
        self.controls.apply({'paused': value})

    def tick(self, frame_name: str = 'menu_cards_stocked', *, in_run: bool = False) -> bool:
        self.now += 1.
        self.bot.tracker.state = screens.ScreenState.IN_RUN if in_run else screens.ScreenState.MAIN_MENU
        self.bot._screen = _frame(frame_name)
        self.bot._screen_fact_scope = self.account.verified_scope
        self.bot._screen_captured_at = self.now
        self.account.observe_balance(BalanceInterval('gems', self.gems, self.gems, SCOPE, self.now, 'wallet'))
        runtime = self.bot.card_runtime
        runtime.visit.adapter = self.replay
        return self.bot._advance_cards((), opportunity=not in_run)


@pytest.fixture
def cards_runtime(tmp_path: Path, monkeypatch: Any) -> CardsRuntimeHarness:
    return CardsRuntimeHarness(tmp_path / 'account.db', monkeypatch)


def test_paused_runner_preserves_queued_buy(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    h.set_paused(True)
    op = h.runner.request_cards(h.buy_command)
    h.tick()
    h.tick()
    assert h.inputs == []
    assert h.store.operation(op.operation_id).status == 'queued'


def test_battle_queues_then_repeated_ticks_dispatch_once(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command)
    h.tick(in_run=True)
    assert h.inputs == [] and h.store.operation(op.operation_id).status == 'queued'
    h.tick()
    h.tick()
    assert h.inputs == [(11, 12)]
    assert h.store.operation(op.operation_id).transaction_key is not None


def test_context_no_cycle_no_implicit_budget_and_exact_preconditions(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    assert h.runner.cards_context().budget.cap == 100
    for field, value in [('scope', replace(SCOPE, epoch=2)), ('program_revision', 'stale')]:
        with pytest.raises(RunnerError):
            h.runner.request_cards(h.buy_command.model_copy(update={field: value}))
    h.controls.replace(replace(h.controls.snapshot().strategy, cards=CardProgram(version=1, gem_cap=10)))
    context = h.runner.cards_context()
    assert context.program.gem_cap == 10 and context.budget.cap == 100


def test_store_active_budget_and_bounded_history_are_archive_safe(tmp_path: Path) -> None:
    store = CardStore(tmp_path / 'absent.db')
    assert store.active_budget() is None
    assert store.recent_operations(limit=5) == ()
    assert not store.path.exists()


def test_restart_recovers_old_generation_before_new_manual_work(cards_runtime: CardsRuntimeHarness) -> None:
    from card_runtime import CardRuntime
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command)
    h.tick()
    old_visit = h.bot.card_runtime.visit.visit_id
    new_scope = replace(SCOPE, generation='new', epoch=2)
    h.account.bind_scope(new_scope, identity=IdentityEvidence('a', h.now, 'new-identity'))
    h.bot.card_runtime = CardRuntime(h.bot)
    h.bot.card_runtime.visit.adapter = h.replay
    h.tick()
    assert h.inputs == [(11, 12)]
    assert h.bot.card_runtime.active
    assert h.bot.card_runtime.visit.operation_id == op.operation_id
    assert h.bot.card_runtime.visit.visit_id == old_visit
    assert h.store.operation(op.operation_id).transaction_key is not None


def test_scope_change_cancels_undispatched_without_old_input(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command)
    h.account.bind_scope(replace(SCOPE, epoch=2), identity=IdentityEvidence('a', h.now, 'new'))
    h.tick()
    assert h.inputs == []
    assert h.store.operation(op.operation_id).status == 'canceled'


def test_plan_edit_cancels_only_undispatched_automatic(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    automatic = h.runner.request_cards(h.buy_command.model_copy(update={
        'idempotency_key': 'automatic', 'source': 'automatic'}))
    manual = h.runner.request_cards(h.buy_command)
    h.set_paused(True)
    h.controls.replace(replace(h.controls.snapshot().strategy, cards=CardProgram(version=1, gem_cap=10)))
    h.tick()
    assert h.store.operation(automatic.operation_id).status == 'canceled'
    assert h.store.operation(manual.operation_id).status == 'queued'


def test_cycle_replay_is_closed_and_edited_cap_blocks_buy(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    context = h.runner.cards_context()
    result = h.runner.start_cards_cycle(scope=SCOPE, program_revision=context.program_revision,
        cycle_id='new', cap=80)
    assert result['active'] and result['budget'].cap == 80
    old = h.runner.start_cards_cycle(scope=SCOPE, program_revision=context.program_revision,
        cycle_id='cycle', cap=100)
    assert not old['active'] and old['active_budget'].cycle_id == 'new'
    h.controls.replace(replace(h.controls.snapshot().strategy, cards=CardProgram(version=1, gem_cap=10)))
    op = h.runner.request_cards(h.buy_command.model_copy(update={'budget_cycle_id': 'new'}))
    h.tick()
    assert h.inputs == []
    assert h.store.operation(op.operation_id).reason == 'budget_reached'


def test_lab_or_shopping_visit_excludes_fresh_cards(cards_runtime: CardsRuntimeHarness, monkeypatch: Any) -> None:
    from types import SimpleNamespace
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command)
    h.bot.lab_visit = SimpleNamespace(active=True)
    h.tick()
    assert h.inputs == [] and h.store.operation(op.operation_id).status == 'queued'
    h.bot.lab_visit = None
    monkeypatch.setattr(type(h.shopping), 'visit_in_progress', property(lambda _: True))
    h.tick()
    assert h.inputs == [] and h.store.operation(op.operation_id).status == 'queued'


def test_shared_intro_and_legacy_shopping_never_enable_manual_buy(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    assert h.shopping.queue_cards is not None
    assert not h.shopping.queue_cards('legacy', CardPolicy(enabled=True))
    assert h.store.recent_operations() == ()
    assert h.bot.cards_intro.request()
    assert not h.bot.cards_intro.active
    operation, = h.store.recent_operations()
    assert operation.command.kind == 'refresh'


def test_account_switch_rejects_old_database_context(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    command = h.buy_command
    h.account._scope = replace(SCOPE, account_id='b')
    h.account._identity = IdentityEvidence('b', h.now, 'new-account')
    assert h.runner.cards_context() is None
    with pytest.raises(RunnerError):
        h.runner.request_cards(command)


def test_run_once_cards_owns_before_generic_recovery_and_labs(cards_runtime: CardsRuntimeHarness, monkeypatch: Any) -> None:
    h = cards_runtime
    h.runner.request_cards(h.buy_command)
    h.tick()
    monkeypatch.setattr(h.bot, 'refresh_screen', lambda: h.bot._screen)
    monkeypatch.setattr(h.shopping, 'inspect', lambda *a, **k: pytest.fail('generic shopping entered Cards frame'))
    monkeypatch.setattr(h.bot, '_bind_lab_runtime', lambda: pytest.fail('Labs entered Cards frame'))
    h.bot.run_once()
    assert h.inputs == [(11, 12)]


def test_automatic_unknown_inventory_starts_refresh_without_creating_cycle(cards_runtime: CardsRuntimeHarness) -> None:
    from tests.test_card_plan import goal
    h = cards_runtime
    current = h.controls.snapshot().strategy
    h.controls.replace(replace(current, cards=CardProgram(version=1, gem_cap=100, goals=(goal(),)),
        shopping=replace(current.shopping, cards=CardPolicy(enabled=True, gem_floor=0))))
    h.tick()
    operation, = h.store.recent_operations()
    assert operation.command.source == 'automatic' and operation.command.kind == 'refresh'
    assert operation.status == 'confirmed'
    assert h.store.active_budget().cycle_id == 'cycle'


def test_pending_home_is_recovered_after_restart_before_new_work(cards_runtime: CardsRuntimeHarness) -> None:
    from card_runtime import CardRuntime
    h = cards_runtime
    refresh = h.buy_command.model_copy(update={'kind': 'refresh', 'budget_cycle_id': ''})
    op = h.runner.request_cards(refresh)
    h.tick()
    assert h.store.operation(op.operation_id).status == 'confirmed'
    h.bot.card_runtime = CardRuntime(h.bot)
    h.tick('menu_cards')
    assert h.bot.card_runtime.visit.operation_id == op.operation_id
    assert h.bot.card_runtime.active and h.inputs == [(51, 52)]
    h.replay.page = 'home'
    h.tick('menu_cards_stocked')
    assert not h.bot.card_runtime.active and h.bot.card_runtime.visit.home_observed


def test_route_context_uses_one_program_reserve_and_percentage(cards_runtime: CardsRuntimeHarness, monkeypatch: Any) -> None:
    from fleet.build_route import resolve_route
    from tests.test_resource_blocks import template_route
    from tests.test_card_plan import goal
    h = cards_runtime
    h.tick(in_run=True)
    route = template_route()
    program = CardProgram(version=1, gem_cap=50, goals=(goal(),))
    route = replace(route, cards=program, rules=replace(route.rules,
        gems=replace(route.rules.gems, keep=100, spend_limit_pct=25)))
    monkeypatch.setattr(h.bot.card_runtime, 'effective_route', lambda: route)
    h.account.currencies.reserve('lab:test', 'gems', 40, wallet=500)
    context = h.runner.cards_context()
    assert context.program == program
    assert context.policy.gem_floor == 100
    assert context.committed_gems == 40
    assert context.eligible_goal_ids == ()
    assert h.bot.card_runtime.route_remaining() == 90


def _reward(h: CardsRuntimeHarness, operation_id: str, frame_name: str) -> None:
    import hashlib
    from card_models import RewardItem
    from card_transactions import CardResultEvidence
    h.gems -= 20
    h.replay.page = 'rewards'
    h.replay.result = CardResultEvidence(operation_id=operation_id, action_sequence=1,
        scope=SCOPE, visit_id=h.bot.card_runtime.visit.visit_id, observed_at=h.now + 1.,
        evidence_ref='reward', frame_digest=hashlib.sha256(_frame(frame_name).tobytes()).hexdigest(),
        layout_id='fixture', acquisition_observed=True, reward_flow_complete=True,
        rewards=(RewardItem(position=0, card_id='cards.damage', quantity=1),))
    h.tick(frame_name)
    h.replay.result = None
    assert h.store.operation(operation_id).status == 'confirmed'


def test_route_percentage_allowance_is_shared_across_operations(cards_runtime: CardsRuntimeHarness, monkeypatch: Any) -> None:
    from tests.test_resource_blocks import template_route
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command)
    h.tick()
    _reward(h, op.operation_id, 'menu_cards')
    h.replay.page = 'home'
    h.tick('menu_main_bluestacks_1920')
    route = template_route()
    route = replace(route, rules=replace(route.rules, gems=replace(route.rules.gems, spend_limit_pct=10, keep=0)))
    monkeypatch.setattr(h.bot.card_runtime, 'effective_route', lambda: route)
    assert h.bot.card_runtime.route_remaining() == 30


def test_disabled_parent_shopping_does_not_schedule_automatic(cards_runtime: CardsRuntimeHarness) -> None:
    from tests.test_card_plan import goal
    h = cards_runtime
    strategy = h.controls.snapshot().strategy
    h.controls.replace(replace(strategy, cards=CardProgram(version=1, gem_cap=100, goals=(goal(),)),
        shopping=replace(strategy.shopping, enabled=False, cards=CardPolicy(enabled=True))))
    h.tick()
    assert h.store.recent_operations() == ()


def test_multiple_automatic_x1_recheck_shared_count_after_each_home(cards_runtime: CardsRuntimeHarness) -> None:
    from tests.test_card_plan import goal
    h = cards_runtime
    strategy = h.controls.snapshot().strategy
    h.controls.replace(replace(strategy, cards=CardProgram(version=1, gem_cap=100, goals=(goal(),)),
        shopping=replace(strategy.shopping, cards=CardPolicy(enabled=True, gem_floor=0, max_per_visit=2))))
    original_read = h.replay.read
    def read(*args: Any, **kwargs: Any) -> Any:
        view = original_read(*args, **kwargs)
        if view.snapshot is not None:
            view = replace(view, snapshot=view.snapshot.model_copy(update={'items': tuple(
                item.model_copy(update={'ownership': 'unowned'}) for item in view.snapshot.items)}))
        return view
    h.replay.read = read
    h.tick()  # inventory refresh
    visit_id = h.bot.card_runtime.visit.visit_id
    h.replay.page = 'home'
    h.tick('menu_cards')
    for index in range(2):
        h.replay.page = 'cards'
        h.tick('menu_cards_stocked')
        op = h.store.operation(h.bot.card_runtime.visit.operation_id)
        # After a mutation, a new fresh Cards observation is required first.
        if op.command.kind == 'refresh':
            h.replay.page = 'home'
            h.tick('menu_cards')
            h.replay.page = 'cards'
            h.tick('menu_cards_stocked')
            op = h.store.operation(h.bot.card_runtime.visit.operation_id)
        from card_plan import plan_cards
        assert op.status == 'dispatched', (index, h.bot.card_runtime.reason, plan_cards(h.runner.cards_context()), [(o.command.kind, o.status, o.reason) for o in h.store.recent_operations()])
        assert op.command.kind == 'buy' and op.command.quantity == 1
        _reward(h, op.operation_id, 'menu_cards')
        h.replay.page = 'home'
        h.tick('menu_main_bluestacks_1920')
        assert h.bot.card_runtime.visit.home_observed
        assert h.bot.card_runtime.visit.visit_id == visit_id
        assert h.runner.cards_context().cards_bought_this_visit == index + 1
    h.replay.page = 'cards'
    for _ in range(3):
        h.tick('menu_cards')
    buys = [op for op in h.store.recent_operations() if op.command.kind == 'buy']
    assert len(buys) == 2
    assert h.inputs.count((11, 12)) == 2
    assert h.store.active_budget().spent == 40


def test_actual_run_once_prioritizes_queued_cards_before_shopping_or_run_start(cards_runtime: CardsRuntimeHarness, monkeypatch: Any) -> None:
    h = cards_runtime
    h.runner.request_cards(h.buy_command)
    h.replay.page = 'home'
    h.bot.card_runtime.visit.adapter = h.replay
    h.bot._screen = _frame('menu_main_bluestacks_1920')
    h.bot._screen_fact_scope = SCOPE
    h.bot._screen_captured_at = h.now
    h.bot.tracker.state = screens.ScreenState.MAIN_MENU
    h.bot.tracker._confirmed = True
    monkeypatch.setattr(h.bot, 'refresh_screen', lambda: h.bot._screen)
    monkeypatch.setattr(h.shopping, 'begin', lambda *a, **k: pytest.fail('shopping began before queued Cards'))
    h.bot.run_once()
    assert h.inputs == [(61, 62)]
    assert h.bot.card_runtime.active


@pytest.mark.parametrize('supervised', [False, True])
@pytest.mark.parametrize('late_reward', [False, True])
def test_ad_recovery_precedes_recovering_cards(
        bot_with_frames: Any, tmp_path: Path, monkeypatch: Any,
        supervised: bool, late_reward: bool) -> None:
    from types import SimpleNamespace
    from tests.test_battle_menu_loop import _supervised
    from tests.test_in_game_ad import frame

    bot = bot_with_frames(['in_run_early'], battle_menu_opt_in=True)
    images = iter(frame(name) for name in ('battle_available', 'end_card', 'reward'))

    def refresh() -> Any:
        bot._screen = next(images)
        return bot._screen

    monkeypatch.setattr(bot, 'refresh_screen', refresh)
    monkeypatch.setattr(bot.gem, 'observe', lambda **kwargs: False)
    hardware = _supervised(bot, tmp_path) if supervised else bot.device
    assert bot.run_once()
    assert bot.in_game_ad.active
    bot.card_runtime = SimpleNamespace(active=True)
    monkeypatch.setattr(bot, '_advance_cards', lambda *args, **kwargs:
        pytest.fail('Cards advanced before active ad or late reward recovery'))
    if late_reward:
        bot.in_game_ad._started -= config.BATTLE_MENU_AD_TIMEOUT + 1
        bot.in_game_ad._closes = 3
    else:
        bot.in_game_ad._started -= 31
    assert bot.run_once()
    assert bot.in_game_ad.active is not late_reward
    assert bot.run_once()
    assert len(hardware.taps) == (2 if late_reward else 3)


def test_progress_reports_cards_with_and_without_observations(tmp_path: Path) -> None:
    from tests.test_runtime_progress import recorder, Clock
    progress = recorder(tmp_path / 'runtime.json', Clock())
    progress.observe_cards({'active': False})
    assert progress.snapshot()['capabilities']['cards']['outcome'] == 'waiting'
    progress.observe_cards({'active': True, 'operation_id': 'op'})
    assert progress.snapshot()['capabilities']['cards']['outcome'] == 'actionable_unknown'


def test_cancel_before_and_after_dispatch_uses_same_durable_queue(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command)
    canceled = h.runner.request_cards(h.buy_command.model_copy(update={'kind': 'cancel',
        'target_operation_id': op.operation_id, 'idempotency_key': 'cancel-before', 'budget_cycle_id': ''}))
    assert canceled.status == 'confirmed' and h.store.operation(op.operation_id).status == 'canceled'
    second = h.runner.request_cards(h.buy_command.model_copy(update={'idempotency_key': 'second'}))
    h.tick()
    h.runner.request_cards(h.buy_command.model_copy(update={'kind': 'cancel',
        'target_operation_id': second.operation_id, 'idempotency_key': 'cancel-after', 'budget_cycle_id': ''}))
    assert h.store.operation(second.operation_id).status == 'reconciliation_required'
    h.tick()
    assert h.inputs == [(11, 12)]


def test_stale_capture_does_not_gain_current_provenance(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command)
    h.bot._screen = _frame('menu_cards_stocked')
    h.bot._screen_fact_scope = replace(SCOPE, epoch=0)
    h.bot._screen_captured_at = h.now
    h.bot.card_runtime.visit.adapter = h.replay
    h.bot._advance_cards((), opportunity=True)
    assert h.inputs == [] and h.store.operation(op.operation_id).status == 'queued'


def test_queued_cards_detours_gameover_home(cards_runtime: CardsRuntimeHarness, monkeypatch: Any) -> None:
    h = cards_runtime
    h.runner.request_cards(h.buy_command)
    assert h.bot.card_runtime.needs_home()
    h.set_paused(True)
    assert not h.bot.card_runtime.needs_home()


def test_runtime_no_cycle_is_distinct_from_zero_planner_allowance(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    with h.store._write() as conn:
        conn.execute('UPDATE card_budget_cycles SET closed_at=99')
    context = h.runner.cards_context()
    assert context.budget.cap == 0 and h.store.active_budget() is None
    h.tick()
    assert h.store.active_budget() is None
    assert h.runner.status()['cards']['active_budget'] is None


def test_legacy_handoff_retains_stricter_policy_for_executor(cards_runtime: CardsRuntimeHarness) -> None:
    from tests.test_card_plan import goal
    h = cards_runtime
    strategy = h.controls.snapshot().strategy
    h.controls.replace(replace(strategy, cards=CardProgram(version=1, gem_cap=100, goals=(goal(),)),
        shopping=replace(strategy.shopping, cards=CardPolicy(enabled=True, gem_floor=0, max_per_visit=2))))
    assert h.shopping.queue_cards('legacy', CardPolicy(enabled=True, gem_floor=200, max_per_visit=1, batch='x10'))
    context = h.runner.cards_context()
    assert context.policy.gem_floor == 200 and context.policy.max_per_visit == 1
    op, = h.store.recent_operations()
    assert op.command.source == 'automatic' and op.command.quantity == 1
    from card_runtime import CardRuntime
    h.bot.card_runtime = CardRuntime(h.bot)
    restored = h.runner.cards_context().policy
    assert restored.gem_floor == 200 and restored.max_per_visit == 1


def test_bad_cancel_does_not_leave_a_queued_command(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    with pytest.raises(RunnerError):
        h.runner.request_cards(h.buy_command.model_copy(update={'kind': 'cancel',
            'target_operation_id': 'missing', 'idempotency_key': 'cancel', 'budget_cycle_id': ''}))
    assert h.store.recent_operations() == ()


def test_actual_gameover_navigation_detours_for_manual_cards(cards_runtime: CardsRuntimeHarness, monkeypatch: Any) -> None:
    h = cards_runtime
    h.runner.request_cards(h.buy_command)
    h.controls.replace(replace(h.controls.snapshot().strategy, auto_navigate=True, shopping=replace(h.controls.snapshot().strategy.shopping, enabled=False)))
    h.bot._screen = _frame('game_over')
    h.bot._screen_fact_scope = SCOPE
    h.bot._screen_captured_at = h.now
    h.bot.tracker.state = screens.ScreenState.GAME_OVER
    h.bot.tracker._confirmed = True
    monkeypatch.setattr(h.bot, 'refresh_screen', lambda: h.bot._screen)
    seen: list[bool] = []
    monkeypatch.setattr(h.bot.navigator, 'maybe_navigate', lambda *a, **k: seen.append(k['go_home']))
    h.bot.run_once()
    assert seen == [True]
    assert h.inputs == []


def test_next_lab_unlock_receives_current_card_context(cards_runtime: CardsRuntimeHarness, monkeypatch: Any) -> None:
    from types import SimpleNamespace
    from fleet.reroll_progress import RerollProgress
    from fleet.build_route import GemRoute
    h = cards_runtime
    progress = RerollProgress.__new__(RerollProgress)
    progress.card_context = h.bot.card_runtime.context
    progress._effective_gems = lambda: GemRoute()
    progress.lab_cadence = SimpleNamespace(slot_records=lambda: {})
    seen: list[Any] = []
    monkeypatch.setattr('fleet.reroll_progress.next_unlock_slot', lambda *a, **k: seen.append(k['card_context']))
    progress.next_unlock_slot()
    assert seen[0].scope == SCOPE
    assert seen[0].program_revision == h.runner.cards_context().program_revision
    assert seen[0].evidence_after == h.runner.cards_context().evidence_after


def test_runtime_status_distinguishes_historical_cards_from_fresh(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    command = h.buy_command.model_copy(update={'kind': 'refresh', 'budget_cycle_id': ''})
    h.runner.request_cards(command)
    h.tick()
    assert h.runner.status()['cards']['fresh']
    h.now += 31.
    assert not h.runner.status()['cards']['fresh']
    assert h.runner.status()['cards']['observed_at'] is not None


def test_restart_after_home_preserves_visit_count_deadline_policy_and_route(cards_runtime: CardsRuntimeHarness, monkeypatch: Any) -> None:
    from card_runtime import CardRuntime
    from tests.test_resource_blocks import template_route
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command)
    h.tick()
    _reward(h, op.operation_id, 'menu_cards')
    h.replay.page = 'home'
    h.tick('menu_main_bluestacks_1920')
    original = h.runner.cards_context().visit_id
    route = template_route()
    route = replace(route, rules=replace(route.rules, gems=replace(route.rules.gems, keep=0, spend_limit_pct=10)))
    monkeypatch.setattr(h.bot.card_runtime, 'effective_route', lambda: route)
    assert h.bot.card_runtime.route_remaining() == 30
    h.bot.card_runtime = CardRuntime(h.bot)
    monkeypatch.setattr(h.bot.card_runtime, 'effective_route', lambda: route)
    assert h.runner.cards_context().visit_id == original
    assert h.runner.cards_context().cards_bought_this_visit == 1
    assert h.bot.card_runtime.route_remaining() == 30
    h.now = 240.
    refresh = h.runner.request_cards(h.buy_command.model_copy(update={'idempotency_key': 'late-refresh',
        'kind': 'refresh', 'budget_cycle_id': ''}))
    h.replay.page = 'cards'
    h.tick()
    assert h.store.operation(refresh.operation_id).status == 'canceled'
    assert h.store.operation(refresh.operation_id).reason == 'visit_timeout'
    assert h.inputs == [(11, 12)]
    # An actually observed next run, persisted across another restart, creates one boundary.
    h.tick(in_run=True)
    next_visit = h.runner.cards_context().visit_id
    assert next_visit != original
    h.bot.card_runtime = CardRuntime(h.bot)
    monkeypatch.setattr(h.bot.card_runtime, 'effective_route', lambda: route)
    h.tick(in_run=True)
    assert h.runner.cards_context().visit_id == next_visit
    h.tick()
    assert h.runner.cards_context().cards_bought_this_visit == 0
    assert h.bot.card_runtime.route_remaining() == 48


def test_completed_handoff_policy_survives_restart_until_next_run(cards_runtime: CardsRuntimeHarness) -> None:
    from card_runtime import CardRuntime
    from tests.test_card_plan import goal
    h = cards_runtime
    strategy = h.controls.snapshot().strategy
    h.controls.replace(replace(strategy, cards=CardProgram(version=1, gem_cap=100, goals=(goal(),)),
        shopping=replace(strategy.shopping, cards=CardPolicy(enabled=True, gem_floor=0))))
    assert h.shopping.queue_cards('handoff-bound', CardPolicy(enabled=True, gem_floor=200, max_per_visit=1))
    h.tick()
    h.replay.page = 'home'
    h.tick('menu_cards')
    h.account.bind_scope(replace(SCOPE, generation='restarted', epoch=2),
        identity=IdentityEvidence('a', h.now, 'new-generation'))
    h.bot.card_runtime = CardRuntime(h.bot)
    assert h.runner.cards_context().policy.gem_floor == 200
    h.tick(in_run=True)
    assert h.runner.cards_context().policy.gem_floor == 0
    assert h.runner.cards_context().policy.max_per_visit == 2


@pytest.mark.parametrize('restart', [False, True])
def test_two_automatic_slots_with_restart_and_repeated_ticks(cards_runtime: CardsRuntimeHarness, monkeypatch: Any, restart: bool) -> None:
    import hashlib
    from card_runtime import CardRuntime
    from card_models import SlotGoal
    from card_transactions import CardResultEvidence
    from tests.test_card_plan import context as calibrated_context
    h = cards_runtime
    # The fake reader is calibrated; production reader gates remain unchanged.
    monkeypatch.setattr('card_runtime.capabilities', lambda _: calibrated_context().capabilities)
    strategy = h.controls.snapshot().strategy
    h.controls.replace(replace(strategy, cards=CardProgram(version=1, gem_cap=100,
        goals=(SlotGoal(id='slots', kind='slots', capacity=4),)),
        shopping=replace(strategy.shopping, cards=CardPolicy(enabled=True, gem_floor=0))))
    h.tick()
    h.replay.page = 'home'
    h.tick('menu_main_bluestacks_1920')
    visit_id = h.runner.cards_context().visit_id
    for capacity in (3, 4):
        h.replay.page = 'slot_confirmation'
        h.tick('menu_cards_stocked')
        op = h.store.operation(h.bot.card_runtime.visit.operation_id)
        if op.command.kind == 'refresh':
            h.replay.page = 'home'
            h.tick('menu_main_bluestacks_1920')
            h.replay.page = 'slot_confirmation'
            h.tick('menu_cards_stocked')
            op = h.store.operation(h.bot.card_runtime.visit.operation_id)
        assert op.command.kind == 'slot' and op.status == 'dispatched'
        h.tick('menu_cards_stocked')
        assert h.inputs.count((21, 22)) == capacity - 2
        h.gems -= 20
        h.replay.capacity = capacity
        h.replay.result = CardResultEvidence(operation_id=op.operation_id, action_sequence=1,
            scope=SCOPE, visit_id=visit_id, observed_at=h.now + 1., evidence_ref='slot-growth',
            frame_digest=hashlib.sha256(_frame('menu_cards').tobytes()).hexdigest(), layout_id='fixture',
            acquisition_observed=True, reward_flow_complete=True)
        h.tick('menu_cards')
        assert h.store.operation(op.operation_id).status == 'confirmed', (capacity, h.store.operation(op.operation_id).reason)
        h.replay.result = None
        h.replay.page = 'home'
        h.tick('menu_main_bluestacks_1920')
        assert h.bot.card_runtime.visit.home_observed
        if capacity == 3 and restart:
            h.bot.card_runtime = CardRuntime(h.bot)
    for _ in range(3):
        h.tick('menu_main_bluestacks_1920')
    assert h.runner.cards_context().visit_id == visit_id
    assert h.store.active_budget().spent == 40
    assert len([op for op in h.store.recent_operations() if op.command.kind == 'slot']) == 2
    assert h.inputs.count((21, 22)) == 2


def test_unknown_frame_cannot_create_another_run_boundary(cards_runtime: CardsRuntimeHarness) -> None:
    h = cards_runtime
    h.tick(in_run=True)
    visit = h.runner.cards_context().visit_id
    h.bot._card_observed_state = 'UNKNOWN'
    h.tick()  # an overlay is not evidence that the current battle ended
    h.bot._card_observed_state = 'IN_RUN'
    h.tick(in_run=True)
    assert h.runner.cards_context().visit_id == visit


def test_assignment_progress_allows_successor_after_explicit_refresh(cards_runtime: CardsRuntimeHarness) -> None:
    from card_models import CardLoadout
    h = cards_runtime
    strategy = h.controls.snapshot().strategy
    h.controls.replace(replace(strategy, cards=CardProgram(version=1, gem_cap=100,
        loadouts=(CardLoadout(id='desired', name='Desired', priority=('cards.damage',)),),
        selected_loadout_id='desired'),
        shopping=replace(strategy.shopping, cards=CardPolicy(enabled=True, gem_floor=0))))
    h.tick()
    h.replay.page = 'home'
    h.tick('menu_main_bluestacks_1920')
    for number in (1, 2):
        h.replay.page = 'cards'
        h.tick('menu_cards_stocked')
        operation = h.store.operation(h.bot.card_runtime.visit.operation_id)
        assert operation.command.kind == 'apply' and operation.status == 'dispatched'
        h.replay.equipped = ('cards.damage',)
        h.tick('menu_cards')
        assert h.store.operation(operation.operation_id).status == 'confirmed'
        h.replay.page = 'home'
        h.tick('menu_main_bluestacks_1920')
        assert h.store.visit_progress(h.runner.cards_context().visit_id) == number
        if number == 1:
            h.replay.equipped = ()
            h.runner.request_cards(h.buy_command.model_copy(update={'kind': 'refresh',
                'budget_cycle_id': '', 'idempotency_key': 'observe-equipment-change'}))
            h.replay.page = 'cards'
            h.tick('menu_cards_stocked')
            h.replay.page = 'home'
            h.tick('menu_main_bluestacks_1920')
    assert h.inputs.count((31, 32)) == 2


def test_actual_run_capture_rotates_durable_visit_once_while_paused(cards_runtime: CardsRuntimeHarness, monkeypatch: Any) -> None:
    h = cards_runtime
    h.runner.request_cards(h.buy_command)
    h.tick()
    original = h.runner.cards_context().visit_id
    # A new battle may be started by the user; pause blocks inputs, not observation.
    h.set_paused(True)
    h.now += 1.
    h.bot._screen = _frame('in_run_early')
    h.bot._screen_fact_scope = SCOPE
    h.bot._screen_captured_at = h.now
    monkeypatch.setattr(h.bot, 'refresh_screen', lambda: h.bot._screen)
    h.bot.run_once()
    persisted = h.store.current_visit()
    assert persisted != original
    h.bot.run_once()
    assert h.store.current_visit() == persisted
    # The outstanding operation still reconciles on its original flow.
    assert h.runner.cards_context().visit_id == original
    assert h.inputs == [(11, 12)]


@pytest.mark.parametrize('scope_change', [
    {'generation': 'reconnected'}, {'epoch': 2},
    {'generation': 'reconnected', 'epoch': 2}, {'lease_id': 'renewed-lease'},
])
def test_new_scope_automatic_refresh_preserves_visit_without_key_conflict(cards_runtime: CardsRuntimeHarness, monkeypatch: Any, scope_change: dict[str, Any]) -> None:
    from card_runtime import CardRuntime
    from tests.test_card_plan import goal, context as calibrated_context
    h = cards_runtime
    monkeypatch.setattr('card_runtime.capabilities', lambda _: calibrated_context().capabilities)
    strategy = h.controls.snapshot().strategy
    h.controls.replace(replace(strategy, cards=CardProgram(version=1, gem_cap=100, goals=(goal(),)),
        shopping=replace(strategy.shopping, cards=CardPolicy(enabled=True, gem_floor=0))))
    h.tick()
    old, = h.store.recent_operations()
    h.replay.page = 'home'
    h.tick('menu_main_bluestacks_1920')
    visit_id = h.runner.cards_context().visit_id
    new_scope = replace(SCOPE, **scope_change)
    h.account.bind_scope(new_scope, identity=IdentityEvidence('a', h.now, 'reverified'))
    h.bot.card_runtime = CardRuntime(h.bot)
    h.replay.page = 'cards'
    h.tick()
    operations = h.store.recent_operations()
    assert len(operations) == 2
    fresh = next(op for op in operations if op.operation_id != old.operation_id)
    assert fresh.command.kind == 'refresh' and fresh.status == 'confirmed'
    assert fresh.command.scope == new_scope
    assert fresh.command.idempotency_key != old.command.idempotency_key
    assert h.store.operation(old.operation_id) == old
    assert h.runner.cards_context().visit_id == visit_id
    with db.reader(h.store.path) as conn:
        starts = conn.execute('SELECT DISTINCT started_at FROM card_visit_flows WHERE visit_id=?', (visit_id,)).fetchall()
    assert len(starts) == 1
    h.replay.page = 'home'
    for _ in range(3):
        h.tick('menu_main_bluestacks_1920')
    assert len(h.store.recent_operations()) == 2
    assert h.inputs == []


@pytest.mark.parametrize('entry', ['shopping', 'intro'])
def test_internal_handoff_refresh_keys_follow_scope(cards_runtime: CardsRuntimeHarness, monkeypatch: Any, entry: str) -> None:
    from card_runtime import CardRuntime
    from tests.test_card_plan import goal, context as calibrated_context
    h = cards_runtime
    monkeypatch.setattr('card_runtime.capabilities', lambda _: calibrated_context().capabilities)
    strategy = h.controls.snapshot().strategy
    h.controls.replace(replace(strategy, cards=CardProgram(version=1, gem_cap=100, goals=(goal(),)),
        shopping=replace(strategy.shopping, cards=CardPolicy(enabled=True, gem_floor=0))))
    def queue() -> bool:
        runtime = h.bot.card_runtime
        return (runtime.queue_refresh(h.now) if entry == 'intro' else
                runtime.queue_automatic('one-opportunity', CardPolicy(enabled=True, gem_floor=0)))
    assert queue()
    h.tick()
    original, = h.store.recent_operations()
    h.replay.page = 'home'
    h.tick('menu_main_bluestacks_1920')
    h.account.bind_scope(replace(SCOPE, generation='next', epoch=2),
        identity=IdentityEvidence('a', h.now, 'reverified'))
    h.bot.card_runtime = CardRuntime(h.bot)
    assert queue()
    assert queue()
    assert len(h.store.recent_operations()) == 2
    assert h.store.operation(original.operation_id) == original


@pytest.mark.parametrize('kind', ['refresh', 'buy'])
def test_unarmed_manual_queue_waits_without_visit_and_resumes_same_operation(
        cards_runtime: CardsRuntimeHarness, kind: str) -> None:
    from card_plan import plan_cards
    h = cards_runtime
    h.controls.replace(replace(h.controls.snapshot().strategy,
        shopping=replace(h.controls.snapshot().strategy.shopping, armed=False)))
    command = h.buy_command.model_copy(update={'kind': kind})
    op = h.runner.request_cards(command)
    h.replay.page = 'home'
    for _ in range(15):
        h.tick()
        assert not h.bot.card_runtime.active
        assert h.store.operation(op.operation_id).status == 'queued'
        assert h.bot.card_runtime.reason == 'not_armed'
        assert not h.bot.card_runtime.needs_home()
        assert plan_cards(h.runner.cards_context().model_copy(update={'command': command})).reason == 'not_armed'
    assert h.inputs == []
    h.controls.replace(replace(h.controls.snapshot().strategy,
        shopping=replace(h.controls.snapshot().strategy.shopping, armed=True)))
    h.tick()
    assert h.inputs == [(61, 62)]
    assert h.bot.card_runtime.visit.operation_id == op.operation_id
    h.replay.page = 'cards'
    h.tick('menu_workshop')
    assert h.store.operation(op.operation_id).status == ('confirmed' if kind == 'refresh' else 'dispatched')


@pytest.mark.parametrize('source', ['manual', 'automatic'])
@pytest.mark.parametrize('cancel_after_restart', [False, True])
@pytest.mark.parametrize('observed_effect', [False, True])
def test_cancel_assignment_runtime_restart_stops_after_claimed_effect(
        cards_runtime: CardsRuntimeHarness, monkeypatch: Any, source: str,
        cancel_after_restart: bool, observed_effect: bool) -> None:
    import json
    import numpy as np
    from card_models import CardLoadout
    from card_runtime import CardRuntime
    from card_transactions import assignment_target, repair_card_ledger
    from card_visit import assignment_steps
    h = cards_runtime
    monkeypatch.setattr('tests.test_card_runtime._frame',
                        lambda _: np.full((2, 2, 3), int(h.now), dtype=np.uint8))
    desired = ('cards.damage', 'cards.health')
    strategy = h.controls.snapshot().strategy
    h.controls.replace(replace(strategy, cards=CardProgram(version=1, gem_cap=100,
        loadouts=(CardLoadout(id='loadout', name='Two cards', priority=desired),)),
        shopping=replace(strategy.shopping, cards=replace(strategy.shopping.cards, enabled=True))))
    context = h.runner.cards_context()
    op = h.runner.request_cards(CardCommand(idempotency_key='apply', scope=SCOPE,
        program_revision=context.program_revision, kind='apply', quantity=1, source=source, loadout_id='loadout'))
    h.tick()
    assert h.inputs == [(31, 32)]
    if cancel_after_restart:
        h.bot.card_runtime = CardRuntime(h.bot)
    cancel = CardCommand(idempotency_key='cancel', scope=SCOPE,
        program_revision=context.program_revision, kind='cancel', quantity=1, source='manual', target_operation_id=op.operation_id)
    assert h.runner.request_cards(cancel).status == 'confirmed'
    h.bot.card_runtime = CardRuntime(h.bot)
    h.replay.equipped = ('cards.damage',) if observed_effect else ()
    h.tick()
    actual = h.store.operation(op.operation_id)
    assert len(assignment_steps(h.store, op.operation_id)) == 1
    assert h.inputs == [(31, 32)]
    assert actual.cancel_requested
    assert assignment_target(h.store, op.operation_id) == desired
    assert actual.status == ('canceled' if observed_effect else 'reconciliation_required')
    if not observed_effect:
        h.now += 11.
        h.tick()
        h.bot.card_runtime = CardRuntime(h.bot)
        h.tick()
        assert h.inputs == [(31, 32)]
        assert h.store.operation(op.operation_id).status == 'reconciliation_required'
        assert h.store.operation(op.operation_id).cancel_requested
        return
    assert actual.snapshot_after.equipped == ('cards.damage',)
    h.tick()
    assert h.inputs == [(31, 32), (51, 52)]
    h.replay.page = 'home'
    h.tick()
    assert not h.bot.card_runtime.active
    # Ordinary scheduler ticks must not revive the canceled operation, including
    # one originally submitted by automation. Existing policy remains enabled.
    for _ in range(3):
        h.tick()
        assert h.store.operation(op.operation_id).status == 'canceled'
        assert len(assignment_steps(h.store, op.operation_id)) == 1
    h.bot.card_runtime = CardRuntime(h.bot)
    assert not h.bot.card_runtime.visit.request(op.operation_id)
    assert h.inputs == [(31, 32), (51, 52)]
    assert h.controls.snapshot().strategy.shopping.cards.enabled
    with db.reader(h.store.path) as conn:
        first = conn.execute("SELECT * FROM ledger WHERE kind='CARD_ASSIGN'").fetchone()
    assert first is not None and first['currency'] is None and first['delta'] == 0
    detail = json.loads(first['detail'])
    assert detail['result'] == 'canceled'
    assert detail['loadout_id'] == 'loadout'
    assert detail['requested_equipped'] == list(desired)
    assert detail['snapshot_after']['equipped'] == ['cards.damage']
    with db.connect(h.store.path) as conn:
        conn.execute('DELETE FROM events')
    assert repair_card_ledger(h.store, h.shopping.journal) == 0
    with db.reader(h.store.path) as conn:
        replayed = conn.execute("SELECT * FROM ledger WHERE kind='CARD_ASSIGN'").fetchall()
    assert len(replayed) == 1 and dict(replayed[0]) == dict(first)


@pytest.mark.parametrize('gate', ['paused', 'unarmed'])
def test_dispatched_recovery_keeps_ownership_when_fresh_work_is_gated(
        cards_runtime: CardsRuntimeHarness, gate: str) -> None:
    from card_runtime import CardRuntime
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command)
    h.tick()
    assert h.inputs == [(11, 12)]
    queued = h.runner.request_cards(h.buy_command.model_copy(update={'idempotency_key': 'next'}))
    if gate == 'paused':
        h.set_paused(True)
    else:
        strategy = h.controls.snapshot().strategy
        h.controls.replace(replace(strategy, shopping=replace(strategy.shopping, armed=False)))
    h.bot.card_runtime = CardRuntime(h.bot)
    h.tick()
    assert h.bot.card_runtime.active
    assert h.bot.card_runtime.visit.operation_id == op.operation_id
    assert h.inputs == [(11, 12)]
    assert h.store.operation(queued.operation_id).status == 'queued'


@pytest.mark.parametrize('kind', ['buy', 'refresh'])
@pytest.mark.parametrize('status', ['queued', 'preflight'])
def test_cancel_acceptance_crash_prevents_undispatched_runtime_input(
        cards_runtime: CardsRuntimeHarness, kind: str, status: str) -> None:
    from card_runtime import CardRuntime
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command.model_copy(update={'kind': kind}))
    if status == 'preflight':
        h.store.transition(op.operation_id, expected_status='queued', status=status, reason=None, now=h.now)
    h.now += 1.
    cancel = h.buy_command.model_copy(update={'kind': 'cancel', 'idempotency_key': 'accepted-cancel',
        'target_operation_id': op.operation_id, 'budget_cycle_id': ''})
    accepted = h.store.submit(cancel, now=h.now)  # crash before runtime lifecycle/acknowledgment
    assert h.store.operation(op.operation_id).cancel_requested
    h.bot.card_runtime = CardRuntime(h.bot)
    h.replay.page = 'home' if kind == 'refresh' else 'cards'
    for _ in range(3):
        h.tick()
        assert h.inputs == []
        assert h.store.operation(op.operation_id).status == 'canceled'
        assert h.store.operation(op.operation_id).transaction_key is None
        assert h.store.operation(accepted.operation_id).status == 'confirmed'
        assert not h.bot.card_runtime.active
    assert h.store.budget('cycle').pending == 0


@pytest.mark.parametrize('kind', ['buy', 'slot', 'refresh'])
def test_cancel_accepted_during_jitter_blocks_fresh_runtime_dispatch(
        cards_runtime: CardsRuntimeHarness, monkeypatch: Any, kind: str) -> None:
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command.model_copy(update={'kind': kind}))
    cancel = h.buy_command.model_copy(update={'kind': 'cancel', 'idempotency_key': 'jitter-cancel',
        'target_operation_id': op.operation_id, 'budget_cycle_id': ''})
    monkeypatch.setattr('account_collection.jitter.pause',
                        lambda *args: h.store.submit(cancel, now=h.now))
    h.replay.page = 'home' if kind == 'refresh' else 'cards'
    h.tick()
    assert h.inputs == []
    assert h.store.operation(op.operation_id).transaction_key is None
    h.tick()
    assert h.inputs == [] and h.store.operation(op.operation_id).status == 'canceled'


def test_cancel_acceptance_crash_retains_dispatched_paid_recovery(
        cards_runtime: CardsRuntimeHarness, monkeypatch: Any) -> None:
    import hashlib
    import numpy as np
    from card_models import RewardItem
    from card_runtime import CardRuntime
    from card_transactions import CardResultEvidence
    h = cards_runtime
    monkeypatch.setattr('tests.test_card_runtime._frame',
                        lambda _: np.full((2, 2, 3), int(h.now), dtype=np.uint8))
    op = h.runner.request_cards(h.buy_command)
    h.tick()
    key = h.store.operation(op.operation_id).transaction_key
    assert h.inputs == [(11, 12)] and key is not None
    h.now += 1.
    accepted = h.store.submit(h.buy_command.model_copy(update={'kind': 'cancel',
        'idempotency_key': 'accepted-cancel', 'target_operation_id': op.operation_id,
        'budget_cycle_id': ''}), now=h.now)
    h.bot.card_runtime = CardRuntime(h.bot)
    h.tick()
    assert h.store.operation(op.operation_id).status == 'reconciliation_required'
    assert h.store.operation(accepted.operation_id).status == 'confirmed'
    assert h.bot.card_runtime.active
    assert h.store.budget('cycle').pending == 20
    assert h.inputs == [(11, 12)]
    at = h.now + 1.
    h.replay.result = CardResultEvidence(operation_id=op.operation_id, action_sequence=1,
        scope=SCOPE, visit_id=h.bot.card_runtime.visit.visit_id, observed_at=at,
        evidence_ref='reward', frame_digest=hashlib.sha256(np.full((2, 2, 3), int(at), dtype=np.uint8).tobytes()).hexdigest(),
        layout_id='fixture', acquisition_observed=True, reward_flow_complete=True,
        rewards=(RewardItem(position=0, card_id='cards.damage', quantity=1),))
    h.gems = 480
    h.tick()
    actual = h.store.operation(op.operation_id)
    assert actual.status == 'confirmed' and actual.cancel_requested
    assert actual.transaction_key == key and actual.spent_gems == 20
    assert h.inputs == [(11, 12)]


@pytest.mark.parametrize('kind', ['buy', 'slot'])
@pytest.mark.parametrize('boundary', ['prepare', 'claim'])
def test_cancel_during_paid_boundary_retains_receipt_without_input(
        cards_runtime: CardsRuntimeHarness, monkeypatch: Any, kind: str, boundary: str) -> None:
    from card_runtime import CardRuntime
    h = cards_runtime
    op = h.runner.request_cards(h.buy_command.model_copy(update={'kind': kind}))
    cancel = h.buy_command.model_copy(update={'kind': 'cancel', 'idempotency_key': 'boundary-cancel',
        'target_operation_id': op.operation_id, 'budget_cycle_id': ''})
    name = 'prepare' if boundary == 'prepare' else 'record_action'
    original = getattr(h.shopping.journal, name)
    def accept_cancel(*args: Any, **kwargs: Any) -> Any:
        receipt = original(*args, **kwargs)
        h.store.submit(cancel, now=h.now)
        return receipt
    monkeypatch.setattr(h.shopping.journal, name, accept_cancel)
    h.tick()
    actual = h.store.operation(op.operation_id)
    assert actual.transaction_key is not None and actual.cancel_requested
    receipt = h.shopping.journal._require(actual.transaction_key)
    assert (receipt.acted_at is None) == (boundary == 'prepare')
    assert h.inputs == [] and h.store.budget('cycle').pending == 20
    h.bot.card_runtime = CardRuntime(h.bot)
    h.tick()
    assert h.store.operation(op.operation_id).status == 'reconciliation_required'
    assert h.bot.card_runtime.active
    assert h.store.budget('cycle').pending == 20 and h.inputs == []
