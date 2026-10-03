"""HTTP boundaries use real account storage and runner queueing, never live input."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
from typing import Any

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from card_models import CardLoadout, CardProgram, SlotGoal
from sinks.sse import SseSink
from sinks.state import BotState
from web.account_catalog import AccountChoice
from web.app import create_app
from tests.test_card_runtime import CardsRuntimeHarness


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, CardsRuntimeHarness]:
    harness = CardsRuntimeHarness(tmp_path / 'account.db', monkeypatch)
    monkeypatch.setattr(harness.runner, 'verified_account', lambda: 'a')
    choices = [AccountChoice('worker:local', 'a', 'local', harness.store.path, 'worker')]
    monkeypatch.setattr('web.app.account_choices', lambda *_: choices)
    app = create_app(state=BotState(), sse=SseSink(), bus=harness.bus,
                     runner=harness.runner, controls=harness.controls, db_path=harness.store.path)
    client = TestClient(app, headers={'x-account-scope': 'worker:local',
                                     'x-expected-account-id': 'a'})
    return client, harness


def payload(harness: CardsRuntimeHarness, **changes: Any) -> dict[str, Any]:
    context = harness.runner.cards_context()
    result = dict(idempotency_key='request-1', expected_account_id='a',
                  expected_generation=context.scope.generation, expected_epoch=context.scope.epoch,
                  expected_program_revision=context.program_revision, kind='refresh')
    result.update(changes)
    return result


def test_command_kind_is_closed() -> None:
    from web.cards_api import CardCommandRequest
    with pytest.raises(ValidationError):
        CardCommandRequest.model_validate({'kind': 'tap_arbitrary_coordinates'})


def test_read_projection_and_catalog_are_typed(api: tuple[TestClient, CardsRuntimeHarness]) -> None:
    client, harness = api
    response = client.get('/api/cards')
    assert response.status_code == 200
    result = response.json()
    assert result['account_id'] == 'a'
    assert result['program']['gem_cap'] == 100
    assert result['program_revision'] == harness.runner.cards_context().program_revision
    assert result['active_budget']['cycle_id'] == 'cycle'
    assert result['preconditions']['expected_epoch'] == 1
    assert result['scope']['lease_id'] == 'lease'
    assert result['policy']['enabled'] is False
    assert result['capabilities']['inventory'] is True
    assert result['capabilities']['buy_one'] is False
    assert result['recent_operations'] == [] and result['read_only_reason'] is None
    assert result['decision']['kind'] in {'wait', 'refresh', 'done'}
    catalog = client.get('/api/cards/catalog').json()
    assert catalog['max_gem_slots'] == 21
    damage = next(item for item in catalog['cards'] if item['card_id'] == 'cards.damage')
    assert damage['name'] == 'Damage' and damage['max_level'] == 7
    assert catalog['level_source_url'].startswith('https://')
    assert '/api/cards/settings' not in {getattr(route, 'path', None) for route in client.app.routes}


@pytest.mark.parametrize(('field', 'value'), [
    ('expected_account_id', 'other'), ('expected_generation', 'old'),
    ('expected_epoch', 99), ('expected_program_revision', 'old'),
])
def test_stale_command_and_cycle_preconditions_conflict(api: tuple[TestClient, CardsRuntimeHarness],
                                                       field: str, value: Any) -> None:
    client, harness = api
    body = payload(harness, **{field: value})
    assert client.post('/api/cards/commands', json=body).status_code == 409
    for key in ('kind', 'idempotency_key'):
        body.pop(key)
    assert client.post('/api/cards/budget-cycles', json={**body, 'cycle_id': 'next', 'cap': 10}).status_code == 409
    assert harness.store.recent_operations() == ()


def test_header_account_conflict_and_standalone_selection(api: tuple[TestClient, CardsRuntimeHarness]) -> None:
    client, harness = api
    assert client.post('/api/cards/commands', json=payload(harness),
                       headers={'x-expected-account-id': 'other'}).status_code == 409
    client.headers.clear()
    assert client.post('/api/cards/commands', json=payload(harness)).status_code == 202
    assert client.get('/api/cards').json()['account_id'] == 'a'


@pytest.mark.parametrize('changes', [
    {'kind': 'buy', 'quantity': 2, 'budget_cycle_id': 'cycle'},
    {'kind': 'buy', 'quantity': 1}, {'kind': 'slot', 'quantity': 10, 'budget_cycle_id': 'cycle'},
    {'kind': 'apply'}, {'kind': 'cancel'}, {'quantity': True}, {'quantity': '1'},
    {'expected_epoch': True}, {'quantity': 11}, {'loadout_id': 'extra'},
    {'target_operation_id': 'extra'}, {'kind': 'tap'}, {'lease_id': 'caller-lease'},
    {'source': 'automatic'}, {'idempotency_key': ' '}, {'budget_cycle_id': 'bad token'},
])
def test_strict_command_validation(api: tuple[TestClient, CardsRuntimeHarness], changes: dict[str, Any]) -> None:
    client, harness = api
    assert client.post('/api/cards/commands', json=payload(harness, **changes)).status_code == 422


@pytest.mark.parametrize('kind', ['buy', 'slot', 'apply', 'clear'])
def test_unsupported_mutations_refused_but_refresh_is_available(api: tuple[TestClient, CardsRuntimeHarness],
                                                              kind: str) -> None:
    client, harness = api
    changes = {'kind': kind}
    if kind in {'buy', 'slot'}:
        changes['budget_cycle_id'] = 'cycle'
    if kind == 'apply':
        changes['loadout_id'] = 'loadout'
    result = client.post('/api/cards/commands', json=payload(harness, **changes))
    assert result.status_code == 503 and result.json()['detail']
    assert harness.store.recent_operations() == ()
    assert client.post('/api/cards/commands', json=payload(harness)).status_code == 202


@pytest.mark.parametrize('paused', [False, True])
def test_battle_or_pause_queues_without_device_input(api: tuple[TestClient, CardsRuntimeHarness], paused: bool) -> None:
    import screens
    client, harness = api
    harness.set_paused(paused)
    harness.bot.tracker.state = screens.ScreenState.IN_RUN
    result = client.post('/api/cards/commands', json=payload(harness))
    assert result.status_code == 202
    operation = result.json()
    assert operation['status'] == 'queued' and operation['spent_gems'] is None
    assert operation['command']['source'] == 'manual'
    assert operation['command']['scope']['lease_id'] == 'lease'
    assert client.get('/api/cards/operations/' + operation['operation_id']).json() == operation
    assert harness.inputs == []


def test_duplicate_returns_original_and_conflicting_payload_is_409(api: tuple[TestClient, CardsRuntimeHarness]) -> None:
    client, harness = api
    first = client.post('/api/cards/commands', json=payload(harness))
    assert first.status_code == 202
    assert client.post('/api/cards/commands', json=payload(harness)).json() == first.json()
    result = client.post('/api/cards/commands', json=payload(harness, kind='clear'))
    assert result.status_code == 409
    assert len(harness.store.recent_operations()) == 1


def test_exact_buy_ten_and_no_downgrade(api: tuple[TestClient, CardsRuntimeHarness], monkeypatch: pytest.MonkeyPatch) -> None:
    client, harness = api
    original = harness.runner.cards_context
    monkeypatch.setattr(harness.runner, 'cards_context', lambda: original().model_copy(update={
        'capabilities': original().capabilities.model_copy(update={'buy_one': True, 'buy_ten': True})}))
    response = client.post('/api/cards/commands', json=payload(harness, kind='buy', quantity=10, budget_cycle_id='cycle'))
    assert response.status_code == 202
    assert response.json()['command']['quantity'] == 10
    assert harness.inputs == []


def test_budget_cycle_replay_preserves_closed_status_and_controls(api: tuple[TestClient, CardsRuntimeHarness]) -> None:
    client, harness = api
    body = payload(harness)
    for key in ('kind', 'idempotency_key'):
        body.pop(key)
    before = harness.controls.snapshot()
    first = client.post('/api/cards/budget-cycles', json={**body, 'cycle_id': 'next', 'cap': 25})
    assert first.status_code == 200 and first.json()['active'] is True
    old = client.post('/api/cards/budget-cycles', json={**body, 'cycle_id': 'cycle', 'cap': 100})
    assert old.status_code == 200 and old.json()['active'] is False
    assert old.json()['budget']['cap'] == 100
    assert old.json()['active_budget']['cycle_id'] == 'next'
    assert client.post('/api/cards/budget-cycles', json={**body, 'cycle_id': 'cycle', 'cap': 101}).status_code == 409
    for cap in (-1, True, '10', 1.5, 2**63):
        assert client.post('/api/cards/budget-cycles', json={**body, 'cycle_id': 'bad', 'cap': cap}).status_code == 422
    assert harness.controls.snapshot() == before


def test_archived_database_reads_do_not_migrate_or_leak_other_account(api: tuple[TestClient, CardsRuntimeHarness],
                                                                   tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import db
    client, harness = api
    operation = client.post('/api/cards/commands', json=payload(harness)).json()
    old_path = tmp_path / 'old.db'
    with sqlite3.connect(old_path) as conn:
        conn.execute('CREATE TABLE account_identity (id INTEGER PRIMARY KEY, account_id TEXT NOT NULL)')
        conn.execute("INSERT INTO account_identity VALUES (1, 'archive')")
    assert db.bound_account(old_path) == 'archive'
    before = old_path.read_bytes()
    monkeypatch.setattr('web.app.account_choices', lambda *_: [
        AccountChoice('worker:archive', 'archive', 'archive', old_path, 'worker')])
    client.headers.update({'x-account-scope': 'worker:archive', 'x-expected-account-id': 'archive'})
    read = client.get('/api/cards')
    assert read.status_code == 200
    assert read.json()['snapshot'] is None and read.json()['active_budget'] is None
    assert read.json()['recent_operations'] == [] and read.json()['preconditions'] is None
    assert read.json()['read_only_reason'] == 'selected_account_not_running'
    assert client.get('/api/cards/operations/' + operation['operation_id']).status_code == 404
    assert client.post('/api/cards/commands', json=payload(harness, expected_account_id='archive')).status_code == 503
    assert old_path.read_bytes() == before


def test_offline_and_unverified_runtime_refuse_mutation(api: tuple[TestClient, CardsRuntimeHarness], monkeypatch: pytest.MonkeyPatch) -> None:
    client, harness = api
    monkeypatch.setattr(harness.runner, '_running_locked', lambda: False)
    assert client.post('/api/cards/commands', json=payload(harness)).status_code == 503


def test_database_and_runner_adapters_are_offloaded(api: tuple[TestClient, CardsRuntimeHarness], monkeypatch: pytest.MonkeyPatch) -> None:
    client, harness = api
    original = harness.runner.cards_context
    def off_loop() -> Any:
        with pytest.raises(RuntimeError, match='no running event loop'):
            asyncio.get_running_loop()
        return original()
    monkeypatch.setattr(harness.runner, 'cards_context', off_loop)
    assert client.get('/api/cards').status_code == 200
    assert client.post('/api/cards/commands', json=payload(harness)).status_code == 202


@pytest.mark.parametrize('change', ['unverified', 'scope_mismatch', 'db_mismatch'])
def test_standalone_requires_verified_identity_and_exact_database(api: tuple[TestClient, CardsRuntimeHarness],
                                                                monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str) -> None:
    from dataclasses import replace
    from account_state import AccountState
    client, harness = api
    client.headers.clear()
    body = payload(harness)
    if change == 'unverified':
        monkeypatch.setattr(AccountState, 'verified_scope', property(lambda _: None))
    elif change == 'scope_mismatch':
        context = harness.runner.cards_context()
        monkeypatch.setattr(harness.runner, 'cards_context', lambda: context.model_copy(
            update={'scope': replace(context.scope, lease_id='different')}))
    else:
        monkeypatch.setattr(harness.account.currencies, 'path', tmp_path / 'other.db')
    assert client.post('/api/cards/commands', json=body).status_code == 503
    read = client.get('/api/cards').json()
    assert read['preconditions'] is None and read['read_only_reason'] == 'cards_identity_unverified'
    assert harness.store.recent_operations() == ()


def test_explicit_unattributed_selection_never_becomes_live(api: tuple[TestClient, CardsRuntimeHarness],
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    client, harness = api
    monkeypatch.setattr('web.app.account_choices', lambda *_: [
        AccountChoice('unattributed', None, None, harness.store.path, 'unattributed')])
    client.headers.clear()
    client.headers['x-account-scope'] = 'unattributed'
    read = client.get('/api/cards').json()
    assert read['account_id'] is None and read['preconditions'] is None
    assert client.post('/api/cards/commands', json=payload(harness)).status_code == 409


def test_archived_snapshot_is_history_and_never_scope_authority(api: tuple[TestClient, CardsRuntimeHarness],
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_card_plan import snapshot
    client, harness = api
    harness.store.observe(snapshot())
    monkeypatch.setattr(harness.runner, '_running_locked', lambda: False)
    read = client.get('/api/cards').json()
    assert read['snapshot']['scope']['account_id'] == 'a'
    assert read['scope'] is None and read['preconditions'] is None and not read['fresh']
    assert read['program'] is None and read['program_revision'] is None
    assert not read['capabilities']['inventory']


def test_missing_archive_remains_absent(api: tuple[TestClient, CardsRuntimeHarness],
                                     monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, _ = api
    absent = tmp_path / 'missing.db'
    monkeypatch.setattr('web.app.account_choices', lambda *_: [
        AccountChoice('worker:local', 'a', 'local', absent, 'worker')])
    assert client.get('/api/cards').json()['active_budget'] is None
    assert client.get('/api/cards/operations/missing').status_code == 404
    assert not absent.exists()


def test_x10_refusal_does_not_fall_back_to_supported_x1(api: tuple[TestClient, CardsRuntimeHarness],
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    client, harness = api
    original = harness.runner.cards_context
    monkeypatch.setattr(harness.runner, 'cards_context', lambda: original().model_copy(update={
        'capabilities': original().capabilities.model_copy(update={'buy_one': True, 'buy_ten': False})}))
    response = client.post('/api/cards/commands', json=payload(harness, kind='buy', quantity=10, budget_cycle_id='cycle'))
    assert response.status_code == 503 and harness.store.recent_operations() == ()


def test_replay_remains_original_after_capability_loss_and_over_50_commands(api: tuple[TestClient, CardsRuntimeHarness],
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    client, harness = api
    body = payload(harness)
    first = client.post('/api/cards/commands', json=body).json()
    command = harness.store.operation(first['operation_id']).command
    for index in range(55):
        harness.store.submit(command.model_copy(update={'idempotency_key': f'new-{index}'}), now=101. + index)
    original = harness.runner.cards_context
    monkeypatch.setattr(harness.runner, 'cards_context', lambda: original().model_copy(update={
        'capabilities': original().capabilities.model_copy(update={'inventory': False})}))
    retry = client.post('/api/cards/commands', json=body)
    assert retry.status_code == 202 and retry.json() == first


def test_cancel_validates_target_and_returns_actual_runner_result(api: tuple[TestClient, CardsRuntimeHarness]) -> None:
    client, harness = api
    missing = payload(harness, kind='cancel', target_operation_id='missing')
    assert client.post('/api/cards/commands', json=missing).status_code == 409
    first = client.post('/api/cards/commands', json=payload(harness)).json()
    canceled = client.post('/api/cards/commands', json=payload(harness,
        idempotency_key='cancel-1', kind='cancel', target_operation_id=first['operation_id']))
    assert canceled.status_code == 202
    assert canceled.json()['status'] == 'confirmed' and canceled.json()['reason'] == 'cancel_requested'
    assert harness.store.operation(first['operation_id']).status == 'canceled'


def test_apply_validates_current_program_loadout(api: tuple[TestClient, CardsRuntimeHarness],
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    client, harness = api
    harness.controls.replace(replace(harness.controls.snapshot().strategy, cards=CardProgram(
        version=1, gem_cap=100, loadouts=(CardLoadout(id='real', name='Real'),))))
    original = harness.runner.cards_context
    monkeypatch.setattr(harness.runner, 'cards_context', lambda: original().model_copy(update={
        'capabilities': original().capabilities.model_copy(update={'assign': True})}))
    assert client.post('/api/cards/commands', json=payload(harness, kind='apply', loadout_id='absent')).status_code == 422
    response = client.post('/api/cards/commands', json=payload(harness, kind='apply', loadout_id='real'))
    assert response.status_code == 202 and response.json()['status'] == 'queued'


def test_read_without_cycle_never_reports_inactive_placeholder_as_budget(api: tuple[TestClient, CardsRuntimeHarness]) -> None:
    client, harness = api
    with sqlite3.connect(harness.store.path) as conn:
        conn.execute('UPDATE card_budget_cycles SET closed_at=99')
    assert harness.runner.cards_context().budget.cycle_id == 'inactive'
    assert client.get('/api/cards').json()['active_budget'] is None


def _route_goals(harness: CardsRuntimeHarness, monkeypatch: pytest.MonkeyPatch, kind: str) -> list[Any]:
    from dataclasses import replace
    from card_visit import CardCapture
    from fleet.build_route import GemRoute
    from tests.conftest import _frame
    from tests.test_card_plan import context as calibrated_context, goal
    from tests.test_resource_blocks import template_route
    first = goal(id='g') if kind == 'buy' else SlotGoal(id='g', kind='slots', capacity=3)
    later = goal('cards.health', id='later') if kind == 'buy' else SlotGoal(id='later', kind='slots', capacity=4)
    template = template_route()
    route = replace(template, cards=CardProgram(version=1, gem_cap=100, goals=(first, later)),
        gems=GemRoute(mode='blocks', blocks=({'id': 'first', 'type': 'card_goal', 'goal_id': 'g'},
                                           {'id': 'later', 'type': 'card_goal', 'goal_id': 'later'})),
        rules=replace(template.rules, gems=replace(template.rules.gems, keep=0, spend_limit_pct=100)))
    routes = [route]
    monkeypatch.setattr(harness.bot.card_runtime, 'effective_route', lambda: routes[0])
    monkeypatch.setattr('card_runtime.capabilities', lambda _: calibrated_context().capabilities)
    original_read = harness.replay.read
    def read(*args: Any, **kwargs: Any) -> Any:
        view = original_read(*args, **kwargs)
        if view.snapshot:
            view = replace(view, snapshot=view.snapshot.model_copy(update={
                'items': tuple(item.model_copy(update={'ownership': 'unowned'}) for item in view.snapshot.items)}))
        return view
    monkeypatch.setattr(harness.replay, 'read', read)
    context = harness.runner.cards_context()
    snapshot = read(_frame('menu_cards_stocked'), (), capture=CardCapture(context.scope, harness.now),
                    visit_id=context.visit_id, operation=None).snapshot
    harness.account.observe_cards(snapshot)
    assert harness.runner.cards_context().eligible_goal_ids == ('g',)
    return routes


@pytest.mark.parametrize('kind', ['buy', 'slot'])
@pytest.mark.parametrize('explicit', [False, True])
def test_route_manual_goal_reaches_actual_scheduler(api: tuple[TestClient, CardsRuntimeHarness],
                                                  monkeypatch: pytest.MonkeyPatch, kind: str, explicit: bool) -> None:
    client, harness = api
    _route_goals(harness, monkeypatch, kind)
    changes = {'goal_id': 'g'} if explicit else {}
    response = client.post('/api/cards/commands', json=payload(harness, kind=kind, budget_cycle_id='cycle', **changes))
    assert response.status_code == 202
    assert response.json()['command']['goal_id'] == 'g'
    if kind == 'slot':
        harness.replay.page = 'slot_confirmation'
    harness.tick()
    operation = harness.store.operation(response.json()['operation_id'])
    assert operation.status == 'dispatched', operation.reason
    assert harness.inputs == [harness.replay.controls[kind]]


@pytest.mark.parametrize('kind', ['buy', 'slot'])
def test_derived_goal_retry_keeps_pinned_goal_after_route_advances(api: tuple[TestClient, CardsRuntimeHarness],
                                                                 monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    client, harness = api
    _route_goals(harness, monkeypatch, kind)
    body = payload(harness, kind=kind, budget_cycle_id='cycle')
    first = client.post('/api/cards/commands', json=body)
    assert first.status_code == 202 and first.json()['command']['goal_id'] == 'g'
    harness.now += 1.
    snapshot = harness.store.snapshot()
    snapshot = snapshot.model_copy(update={'observed_at': harness.now, 'capacity': 3, 'items': tuple(
        item.model_copy(update={'ownership': 'owned', 'observed_at': harness.now, 'field_evidence': {
            **item.field_evidence, 'ownership': item.field_evidence['ownership'].model_copy(update={
                'observed_at': harness.now, 'evidence_ref': 'new-owned-observation'})}}) if item.card_id == 'cards.damage' else item
        for item in snapshot.items)})
    harness.account.observe_cards(snapshot)
    assert harness.runner.cards_context().eligible_goal_ids == ('later',)
    retry = client.post('/api/cards/commands', json=body)
    assert retry.status_code == 202 and retry.json() == first.json()
    assert client.post('/api/cards/commands', json={**body, 'goal_id': 'later'}).status_code == 409
    next_command = client.post('/api/cards/commands', json={**body, 'idempotency_key': 'next'})
    assert next_command.status_code == 202 and next_command.json()['command']['goal_id'] == 'later'


@pytest.mark.parametrize('kind', ['buy', 'slot'])
def test_explicit_future_goal_queues_under_current_program(api: tuple[TestClient, CardsRuntimeHarness],
                                                         monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    from card_plan import plan_cards
    client, harness = api
    _route_goals(harness, monkeypatch, kind)
    response = client.post('/api/cards/commands', json=payload(harness, kind=kind, budget_cycle_id='cycle', goal_id='later'))
    assert response.status_code == 202
    operation = harness.store.operation(response.json()['operation_id'])
    assert operation.command.goal_id == 'later'
    decision = plan_cards(harness.runner.cards_context().model_copy(update={'command': operation.command}))
    assert decision.reason == 'route_waiting'


@pytest.mark.parametrize('eligible', [(), ('unknown',), ('g', 'later')])
def test_untargeted_route_requires_one_compatible_goal(api: tuple[TestClient, CardsRuntimeHarness],
                                                    monkeypatch: pytest.MonkeyPatch, eligible: tuple[str, ...]) -> None:
    client, harness = api
    _route_goals(harness, monkeypatch, 'buy')
    original = harness.runner.cards_context
    monkeypatch.setattr(harness.runner, 'cards_context', lambda: original().model_copy(update={'eligible_goal_ids': eligible}))
    response = client.post('/api/cards/commands', json=payload(harness, kind='buy', budget_cycle_id='cycle'))
    assert response.status_code == 409 and response.json()['detail'] == 'route_waiting'
    assert harness.store.recent_operations() == ()


@pytest.mark.parametrize('changes', [
    {'kind': 'refresh', 'goal_id': 'g'},
    {'kind': 'buy', 'budget_cycle_id': 'cycle', 'goal_id': 'unknown'},
    {'kind': 'slot', 'budget_cycle_id': 'cycle', 'goal_id': 'g'},
    {'kind': 'buy', 'budget_cycle_id': 'cycle', 'goal_id': ''},
])
def test_goal_id_is_strict_and_matches_effective_program_kind(api: tuple[TestClient, CardsRuntimeHarness],
                                                            monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]) -> None:
    client, harness = api
    _route_goals(harness, monkeypatch, 'buy')
    assert client.post('/api/cards/commands', json=payload(harness, **changes)).status_code == 422


def test_incompatible_current_route_goal_does_not_queue(api: tuple[TestClient, CardsRuntimeHarness],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    client, harness = api
    _route_goals(harness, monkeypatch, 'buy')
    response = client.post('/api/cards/commands', json=payload(harness, kind='slot', budget_cycle_id='cycle'))
    assert response.status_code == 409 and response.json()['detail'] == 'route_waiting'


def test_route_manual_buy_ten_reaches_scheduler_unchanged(api: tuple[TestClient, CardsRuntimeHarness],
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    client, harness = api
    routes = _route_goals(harness, monkeypatch, 'buy')
    routes[0] = replace(routes[0], cards=routes[0].cards.model_copy(update={'gem_cap': 500}))
    strategy = harness.controls.snapshot().strategy
    harness.controls.replace(replace(strategy, shopping=replace(strategy.shopping,
        cards=replace(strategy.shopping.cards, max_per_visit=10))))
    context = harness.runner.cards_context()
    harness.runner.start_cards_cycle(scope=context.scope, program_revision=context.program_revision,
        cycle_id='ten', cap=500)
    response = client.post('/api/cards/commands', json=payload(harness, kind='buy', quantity=10, budget_cycle_id='ten'))
    assert response.status_code == 202
    assert response.json()['command']['quantity'] == 10 and response.json()['command']['goal_id'] == 'g'
    harness.tick()
    operation = harness.store.operation(response.json()['operation_id'])
    assert operation.status == 'dispatched', operation.reason
    assert operation.command.quantity == 10 and harness.inputs == [(11, 12)]
    assert harness.store.active_budget().pending == 200


@pytest.mark.parametrize('kind', ['buy', 'slot'])
def test_future_goal_waits_across_ticks_then_same_operation_is_admitted(api: tuple[TestClient, CardsRuntimeHarness],
                                                                     monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    from dataclasses import replace
    client, harness = api
    routes = _route_goals(harness, monkeypatch, kind)
    body = payload(harness, kind=kind, budget_cycle_id='cycle', goal_id='later')
    response = client.post('/api/cards/commands', json=body)
    operation_id = response.json()['operation_id']
    for _ in range(3):
        harness.tick()
        assert harness.store.operation(operation_id).status == 'queued'
        assert not harness.bot.card_runtime.active
        assert not harness.bot.card_runtime.needs_home()
    assert harness.inputs == []
    # Only route ordering changes; the Cards program/revision is identical.
    routes[0] = replace(routes[0], gems=replace(routes[0].gems, blocks=(routes[0].gems.blocks[1],)))
    assert harness.runner.cards_context().eligible_goal_ids == ('later',)
    assert client.post('/api/cards/commands', json=body).json()['operation_id'] == operation_id
    if kind == 'slot':
        harness.replay.page = 'slot_confirmation'
    harness.tick()
    operation = harness.store.operation(operation_id)
    assert operation.status == 'dispatched', operation.reason
    assert operation.command.idempotency_key == body['idempotency_key']
    assert harness.inputs == [harness.replay.controls[kind]]


def test_future_goal_does_not_prevent_eligible_manual_work(api: tuple[TestClient, CardsRuntimeHarness],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    client, harness = api
    _route_goals(harness, monkeypatch, 'buy')
    future = client.post('/api/cards/commands', json=payload(harness,
        kind='buy', budget_cycle_id='cycle', goal_id='later')).json()
    harness.now += 1.
    current = client.post('/api/cards/commands', json=payload(harness,
        idempotency_key='current', kind='buy', budget_cycle_id='cycle', goal_id='g')).json()
    harness.tick()
    assert harness.store.operation(future['operation_id']).status == 'queued'
    assert harness.store.operation(current['operation_id']).status == 'dispatched'
    assert harness.inputs == [(11, 12)]


def test_guarded_automation_changes_only_cards_enablement(api: tuple[TestClient, CardsRuntimeHarness]) -> None:
    client, harness = api
    body = payload(harness)
    for key in ('kind', 'idempotency_key'):
        body.pop(key)
    before = harness.controls.snapshot().strategy
    response = client.post('/api/cards/automation', json={**body, 'enabled': True})
    assert response.status_code == 200
    after = harness.controls.snapshot().strategy
    assert after.shopping.cards.enabled is True
    from dataclasses import replace
    assert replace(after, shopping=replace(after.shopping, cards=before.shopping.cards)) == before
    assert harness.store.active_budget().cap == 100
    assert harness.store.recent_operations() == ()
    assert client.post('/api/cards/automation', json={**body, 'enabled': False}).status_code == 200
    assert harness.controls.snapshot().strategy.shopping.cards.enabled is False


@pytest.mark.parametrize('drift', ['account', 'revision', 'stopped'])
def test_automation_rechecks_preconditions_under_runner_lock(api: tuple[TestClient, CardsRuntimeHarness],
        monkeypatch: pytest.MonkeyPatch, drift: str) -> None:
    client, harness = api
    body = payload(harness)
    for key in ('kind', 'idempotency_key'):
        body.pop(key)
    from fleet.identity import IdentityEvidence
    original = harness.runner.update_cards_automation
    def race(**kwargs: Any) -> dict:
        from dataclasses import replace
        if drift == 'account':
            harness.account.bind_scope(replace(harness.account.verified_scope, epoch=2),
                identity=IdentityEvidence('a', 1., 'identity'))
        elif drift == 'revision':
            strategy = harness.controls.snapshot().strategy
            harness.controls.replace(replace(strategy, cards=CardProgram(version=1, gem_cap=99)))
        else:
            monkeypatch.setattr(harness.runner, '_running_locked', lambda: False)
        return original(**kwargs)
    monkeypatch.setattr(harness.runner, 'update_cards_automation', race)
    result = client.post('/api/cards/automation', json={**body, 'enabled': True})
    assert result.status_code in {409, 503}
    assert harness.controls.snapshot().strategy.shopping.cards.enabled is False


def test_fleet_proxy_reaches_real_worker_queue_and_controls(api: tuple[TestClient, CardsRuntimeHarness],
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import json
    from fleet.setup import FleetSetupService
    from fleet.cards import fleet_cards_snapshot, proxy_card_targets
    client, harness = api
    choice = AccountChoice('worker:local', 'a', 'local', harness.store.path, 'worker', 8081)
    monkeypatch.setattr('fleet.cards._choices', lambda service: [choice])
    monkeypatch.setattr('fleet.cards.registered_worker', lambda path: choice)
    def transport(request: Any, *, timeout: float) -> io.BytesIO:
        assert timeout == .2
        assert request.headers['X-expected-account-id'] == 'a'
        assert request.headers['X-account-scope'] == 'worker:local'
        path = request.full_url.split(':8081')[1]
        response = client.request(request.get_method(), path, content=request.data, headers=dict(request.headers))
        assert response.status_code in {200, 202}, response.text
        return io.BytesIO(response.content)
    monkeypatch.setattr('fleet.cards.urlopen', transport)
    svc = FleetSetupService(tmp_path / 'fleet', qualification_root=tmp_path / 'q')
    row = fleet_cards_snapshot(svc)['accounts'][0]
    assert row['cards']['scope']['account_id'] == 'a'
    assert row['cards']['preconditions']['expected_program_revision'] == harness.runner.cards_context().program_revision
    request = {**payload(harness), 'worker': 'local'}
    result = proxy_card_targets(svc, targets=[request])['results'][0]
    assert result['status'] == 'accepted'
    assert result['result']['status'] == 'queued'
    operation = harness.store.operation(result['result']['operation_id'])
    assert operation.command.idempotency_key == 'request-1'
    assert operation.command.source == 'manual'
    auto = {**row['cards']['preconditions'], 'worker': 'local', 'enabled': True}
    assert proxy_card_targets(svc, targets=[auto], automation=True)['results'][0]['status'] == 'accepted'
    assert harness.controls.snapshot().strategy.shopping.cards.enabled is True
    assert len(harness.store.recent_operations()) == 1


@pytest.mark.parametrize('concurrent_change', ['strategy', 'identity', 'overlay'])
def test_automation_serializes_with_concurrent_authority_changes(api: tuple[TestClient, CardsRuntimeHarness],
        tmp_path: Path, concurrent_change: str) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace
    from dataclasses import replace
    from fleet.build_route import RouteDocument
    from fleet.build_route_runtime import BuildRouteRuntime
    from fleet.build_route_store import BuildRouteStore
    _, harness = api
    route_store = BuildRouteStore(tmp_path / 'fleet')
    if concurrent_change == 'overlay':
        route_store.publish(RouteDocument.compatibility(), 0, 'operator')
        harness.bot.reroll_progress = SimpleNamespace(root=tmp_path / 'fleet/workers/local',
            route_runtime=BuildRouteRuntime(tmp_path / 'fleet', 'local', 'a'))
    context = harness.runner.cards_context()
    entered, attempted, finished = Event(), Event(), Event()
    def competing() -> None:
        assert entered.wait(2)
        attempted.set()
        if concurrent_change == 'strategy':
            harness.controls.replace(replace(harness.controls.snapshot().strategy,
                cards=CardProgram(version=1, gem_cap=500)))
        elif concurrent_change == 'identity':
            harness.account.invalidate_scope('replacement')
        else:
            route_store.publish(route_store.read(), 1, 'concurrent-overlay')
        finished.set()
    def apply() -> dict:
        entered.set()
        assert attempted.wait(2)
        assert not finished.wait(.1), 'authority changed while Cards mutation held its guard'
        shopping = harness.controls.snapshot().strategy.shopping.to_dict()
        shopping['cards']['enabled'] = True
        harness.controls.apply({'shopping': shopping})
        return {'enabled': True}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(competing)
        result = harness.runner.update_cards_automation(scope=context.scope,
            program_revision=context.program_revision, apply=apply)
        future.result(timeout=3)
    assert result == {'enabled': True}
    assert finished.is_set()


def test_single_account_fleet_transport_reaches_worker_and_fences_reads(api: tuple[TestClient, CardsRuntimeHarness],
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import json
    from urllib.error import HTTPError
    from fleet.setup import FleetSetupService
    from web.cards_api import register_fleet_cards_routes
    from fastapi import FastAPI
    worker, harness = api
    choice = AccountChoice('worker:local', 'a', 'local', harness.store.path, 'worker', 8081)
    monkeypatch.setattr('fleet.cards._choices', lambda service: [choice])
    monkeypatch.setattr('fleet.cards.registered_worker', lambda path: choice)
    calls: list[tuple[str, object]] = []
    def transport(request: Any, *, timeout: float) -> io.BytesIO:
        assert request.headers['X-expected-account-id'] == 'a'
        path = request.full_url.split(':8081')[1]
        calls.append((path, json.loads(request.data) if request.data else None))
        response = worker.request(request.get_method(), path, content=request.data, headers=dict(request.headers))
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, 'worker', {}, io.BytesIO(response.content))
        return io.BytesIO(response.content)
    monkeypatch.setattr('fleet.cards.urlopen', transport)
    app = FastAPI()
    register_fleet_cards_routes(app, service=FleetSetupService(tmp_path / 'fleet', qualification_root=tmp_path / 'q'))
    client = TestClient(app)
    command = {**payload(harness), 'worker': 'local'}
    response = client.post('/api/fleet/cards/account-command', json=command)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'accepted'
    assert client.post('/api/fleet/cards/account-command', json=command).json() == result
    assert len(harness.store.recent_operations()) == 1
    pre = worker.get('/api/cards').json()['preconditions']
    read = {**pre, 'worker': 'local', 'operation_id': result['result']['operation_id']}
    assert client.post('/api/fleet/cards/account-operation', json=read).json()['result'] == result['result']
    preview = {**pre, 'worker': 'local', 'program': worker.get('/api/cards').json()['program']}
    assert client.post('/api/fleet/cards/account-preview', json=preview).json()['result']['goals'] == []
    stale = client.post('/api/fleet/cards/account-operation', json={**read, 'expected_epoch': 999}).json()
    assert stale['status'] == 'conflict' and 'result' not in stale
    cycle = {**pre, 'worker': 'local', 'cycle_id': 'explicit', 'cap': 25}
    assert client.post('/api/fleet/cards/account-cycle', json=cycle).json()['result']['budget']['cap'] == 25
    assert harness.controls.snapshot().strategy.shopping.cards.enabled is False
    assert client.post('/api/fleet/cards/account-command', json={'targets': [command]}).status_code == 422
    assert client.post('/api/fleet/cards/account-command', json={**command, 'path': '/api/control'}).status_code == 422
    missing = client.post('/api/fleet/cards/account-operation', json={**read, 'operation_id': 'missing'}).json()
    assert missing['status'] == 'conflict' and missing['http_status'] == 404
    paid = {**command, 'kind': 'buy', 'quantity': 10, 'budget_cycle_id': 'explicit', 'idempotency_key': 'paid-ten'}
    unsupported = client.post('/api/fleet/cards/account-command', json=paid).json()
    assert unsupported['status'] == 'unavailable' and unsupported['http_status'] == 503
    original = harness.runner.cards_context
    monkeypatch.setattr(harness.runner, 'cards_context', lambda: original().model_copy(update={
        'capabilities': original().capabilities.model_copy(update={'buy_ten': True})}))
    purchased = client.post('/api/fleet/cards/account-command', json=paid).json()
    assert purchased['status'] == 'accepted'
    assert purchased['result']['command']['quantity'] == 10
    assert client.post('/api/fleet/cards/account-command', json=paid).json() == purchased
    assert len(harness.store.recent_operations()) == 2
    assert harness.inputs == []
    assert all(path.startswith('/api/cards') for path, _ in calls)
