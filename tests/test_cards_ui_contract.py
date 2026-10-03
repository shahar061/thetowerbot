from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from strategy import ControlError, Strategy, StrategyStore
from card_models import CardProgram, CardLoadout
from tests.test_strategy_store import store
from tests.test_cards_api import api
from tests.test_card_runtime import CardsRuntimeHarness


def test_strategy_cas_preserves_concurrent_fields(store: StrategyStore) -> None:
    original = Strategy.from_config('mine')
    store.save(original)
    revision = store.revision(original)
    winner = replace(original, interval=3)
    store.save(winner, expected_revision=revision)
    with pytest.raises(ControlError, match='revision'):
        store.save(replace(original, menu_interval=4), expected_revision=revision)
    assert store.load('mine') == winner


def test_cards_preview_is_read_only_and_owner_is_explicit(api: tuple[TestClient, CardsRuntimeHarness]) -> None:
    client, harness = api
    before = harness.store.recent_operations()
    current = client.get('/api/cards').json()
    assert current['config_owner'] == 'local'
    program = current['program']
    program['goals'] = [{'id': 'damage', 'kind': 'acquire', 'targets': [{'card_id': 'cards.damage'}]}]
    result = client.post('/api/cards/preview', json={'program': program})
    assert result.status_code == 200
    assert result.json()['goals'] == [{'id': 'damage', 'met': None}]
    assert harness.store.recent_operations() == before
    assert harness.store.active_budget().cycle_id == 'cycle'
    assert harness.runner.cards_context().program.goals != tuple(program['goals'])


def test_route_failure_never_advertises_editable_local(api: tuple[TestClient, CardsRuntimeHarness], monkeypatch: pytest.MonkeyPatch) -> None:
    client, harness = api
    monkeypatch.setattr(harness.bot.card_runtime, 'effective_route', lambda: (_ for _ in ()).throw(ValueError('unavailable')))
    assert client.get('/api/cards').json()['config_owner'] == 'unavailable'


def test_http_strategy_etag_conflict_changes_neither_file_nor_controls(store: StrategyStore) -> None:
    from control import Controls
    from events import EventBus
    from sinks.sse import SseSink
    from sinks.state import BotState
    from web.app import create_app
    original = Strategy.from_config('mine')
    store.save(original)
    store.set_active('mine')
    controls = Controls(original)
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(), controls=controls, store=store))
    first = client.get('/api/strategies/mine')
    body = first.json()
    assert first.headers['etag'] == store.revision(original)
    body['interval'] = 3
    winner = client.put('/api/strategies/mine', json=body, headers={'If-Match': first.headers['etag']})
    assert winner.status_code == 200
    body['interval'] = 4
    stale = client.put('/api/strategies/mine', json=body, headers={'If-Match': first.headers['etag']})
    assert stale.status_code == 409
    assert store.load('mine').interval == controls.strategy.interval == 3
    assert client.put('/api/strategies/mine', json=body).json()['interval'] == 4


def test_concurrent_strategy_cas_has_one_winner(store: StrategyStore) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    original = Strategy.from_config('mine')
    store.save(original)
    revision = store.revision(original)
    barrier = Barrier(2)

    def write(interval: int) -> str:
        other_store = StrategyStore(store.directory)
        barrier.wait(timeout=5)
        try:
            other_store.save(replace(original, interval=interval), expected_revision=revision)
            return 'saved'
        except ControlError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [3, 4]))
    assert sorted(results) == ['conflict', 'saved']
    assert store.load('mine').interval in (3, 4)


def test_server_preview_uses_current_goal_and_priority_fallback_proof() -> None:
    from card_models import AcquireGoal, CardTarget
    from tests.test_card_plan import context, snapshot, item
    from web.cards_api import preview_program
    program = CardProgram(version=1, gem_cap=100,
        goals=(AcquireGoal(id='health', kind='acquire', targets=(CardTarget(card_id='cards.health', min_level=3),)),),
        loadouts=(CardLoadout(id='farm', name='Farm', priority=('cards.damage', 'cards.health')),))
    current = context(program=program, snapshot=snapshot(item('cards.damage', ownership='unowned'), item('cards.health', ownership='owned', level=4)))
    preview = preview_program(program, current)
    assert preview.fresh and preview.goals[0].met is True
    assert preview.loadouts[0].resolution.desired == ('cards.health',)
    assert preview.loadouts[0].resolution.missing == ('cards.damage',)
    historical = preview_program(program, current.model_copy(update={'now': 1000.}))
    assert not historical.fresh and historical.goals[0].met is None
    assert historical.loadouts[0].resolution.desired is None


def test_config_owner_uses_route_selection_not_program_hash(api: tuple[TestClient, CardsRuntimeHarness], monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    _, harness = api
    runtime = harness.bot.card_runtime
    original = runtime.context(route_gates=False)
    route = SimpleNamespace(cards=original.program, rules=SimpleNamespace(gems=SimpleNamespace(keep=0)))
    monkeypatch.setattr(runtime, 'effective_route', lambda: route)
    effective = runtime.context(route_gates=False)
    assert effective.config_owner == 'fleet'
    assert effective.program_revision == original.program_revision


@pytest.mark.parametrize('body', [
    {'program': {'version': 1, 'gem_cap': True}},
    {'program': {'version': 1, 'gem_cap': 100}, 'enable': True},
    {'program': {'version': 1, 'gem_cap': 100, 'goals': [{'id': 'bad', 'kind': 'acquire', 'targets': [{'card_id': 'fake'}]}]}},
])
def test_draft_preview_strict_no_side_effects(api: tuple[TestClient, CardsRuntimeHarness], body: dict) -> None:
    client, harness = api
    before = harness.store.active_budget()
    assert client.post('/api/cards/preview', json=body).status_code == 422
    assert harness.store.active_budget() == before
    assert harness.store.recent_operations() == ()


@pytest.mark.parametrize('action', ['put', 'activate'])
def test_live_strategy_writes_hold_controls_then_file_through_replace(store: StrategyStore, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager
    from control import Controls
    from events import EventBus
    from sinks.sse import SseSink
    from sinks.state import BotState
    from web.app import create_app
    original = Strategy.from_config('mine')
    store.save(original)
    controls = Controls(original)
    app = create_app(state=BotState(), sse=SseSink(), bus=EventBus(), controls=controls, store=store)
    original_lock = store.locked
    original_replace = controls.replace
    held: list[bool] = []
    replaced: list[bool] = []

    @contextmanager
    def checked_lock() -> Iterator[None]:
        assert controls._lock._is_owned()
        with original_lock():
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    def checked_replace(strategy: Strategy) -> dict:
        assert held and controls._lock._is_owned()
        replaced.append(True)
        return original_replace(strategy)

    monkeypatch.setattr(store, 'locked', checked_lock)
    monkeypatch.setattr(controls, 'replace', checked_replace)
    client = TestClient(app)
    response = client.put('/api/strategies/mine', json=original.to_dict()) if action == 'put' else client.post('/api/strategies/mine/activate')
    assert response.status_code == 200 and replaced == [True]


@pytest.mark.parametrize('takeover', [False, True])
def test_guarded_cards_save_rechecks_ownership_before_persistence(api: tuple[TestClient, CardsRuntimeHarness], store: StrategyStore, monkeypatch: pytest.MonkeyPatch, takeover: bool) -> None:
    import json
    from sinks.sse import SseSink
    from sinks.state import BotState
    from web.app import create_app
    _, harness = api
    original = harness.controls.strategy
    store.save(original)
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=harness.bus,
        runner=harness.runner, controls=harness.controls, store=store, db_path=harness.store.path))
    read = client.get('/api/cards').json()
    document = client.get('/api/strategies/cards')
    incoming = document.json()
    incoming['cards']['gem_cap'] = 60
    authority = harness.runner.with_cards_authority
    runtime = harness.bot.card_runtime
    context = runtime.context

    def raced_authority(**kwargs: object) -> dict:
        if takeover:
            # Same effective program hash, but owner changes at the boundary
            # after HTTP read and before the guarded callback can persist.
            monkeypatch.setattr(runtime, 'context', lambda **kw: context(**kw).model_copy(update={'config_owner': 'fleet'}))
        return authority(**kwargs)

    monkeypatch.setattr(harness.runner, 'with_cards_authority', raced_authority)
    response = client.put('/api/strategies/cards', json=incoming,
        headers={'If-Match': document.headers['etag'], 'X-Cards-Preconditions': json.dumps(read['preconditions'])})
    assert response.status_code == (409 if takeover else 200)
    if takeover:
        assert store.load('cards') == original
        assert harness.controls.strategy == original
    else:
        assert harness.controls.strategy.cards.gem_cap == store.load('cards').cards.gem_cap == 60
    assert harness.store.active_budget().cap == 100
    assert harness.controls.strategy.shopping.cards.enabled is False


def test_loadout_preview_reports_capacity_and_equipment_delta_without_inventing_unknowns() -> None:
    from tests.test_card_plan import context, snapshot, item
    from web.cards_api import preview_program
    program = CardProgram(version=1, gem_cap=100,
        loadouts=(CardLoadout(id='farm', name='Farm', priority=('cards.damage', 'cards.health')),))
    observed = snapshot(item('cards.damage', ownership='owned'), item('cards.health', ownership='owned'), capacity=1, equipped=('cards.health',))
    result = preview_program(program, context(snapshot=observed)).loadouts[0]
    assert result.capacity == 1 and result.capacity_limited is True
    assert result.capacity_excluded == ('cards.health',)
    assert result.additions == ('cards.damage',) and result.removals == ('cards.health',)
    unknown = preview_program(program, context(snapshot=observed.model_copy(update={'equipment_complete': False}))).loadouts[0]
    assert unknown.additions is None and unknown.removals is None
