"""Account-owned Cards overlays and bounded worker HTTP aggregation."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_runtime import BuildRouteRuntime
from fleet.build_route_store import RouteConflict
from fleet.setup import FleetSetupService
from tests.test_build_route_api import _registered_worker, _client


def service(root: Path) -> FleetSetupService:
    return FleetSetupService(root, qualification_root=root / 'qualifications')


def saved(svc: FleetSetupService) -> str:
    baseline = RouteDocument.compatibility().baseline.to_dict()
    baseline['cards'] = {'version': 1, 'gem_cap': 200, 'goals': [], 'loadouts': [
        {'id': 'farm', 'name': 'Farm', 'priority': ['cards.damage']}], 'selected_loadout_id': None}
    result = svc.strategy_library().save(expected_revision=0, name='Cards', source_template='opening', baseline=baseline)
    return result['strategies'][0]['id']


def assign(svc: FleetSetupService, strategy: str, revision: int = 0,
           targets: list[dict[str, str]] | None = None) -> dict[str, Any]:
    from fleet.cards import assign_card_program
    return assign_card_program(svc, expected_revision=revision, strategy_id=strategy,
        strategy_version=1, targets=targets or [{'account_id': 'ACCOUNT-A', 'worker': 'Air_38'}])


def test_offline_assignment_preserves_route_and_replays_identity(tmp_path: Path) -> None:
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    sid = saved(svc)
    before = svc.build_route_store().read()
    result = assign(svc, sid)
    assert result['revision'] == 1
    assert result['results'][0]['status'] == 'pending'
    route = svc.build_route_store().read()
    actual = resolve_route(route, 'Air_38', 'ACCOUNT-A')
    original = resolve_route(before, 'Air_38', 'ACCOUNT-A')
    assert replace(actual, revision=0, cards=None) == original
    assert actual.cards.gem_cap == 200
    assert resolve_route(route, 'Air_38', 'ACCOUNT-B').cards is None
    # Ownership follows the account, not a worker hint.
    assert resolve_route(route, 'Air_99', 'ACCOUNT-A').cards.gem_cap == 200
    replay = assign(svc, sid, 1)
    assert replay == result
    assert svc.build_route_store().read().revision == 1


def test_target_conflicts_are_partial_and_wrong_binding_is_not_published(tmp_path: Path) -> None:
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    _registered_worker(tmp_path, 'ACCOUNT-B', 'ACCOUNT-X', 'Air_39')
    svc = service(tmp_path)
    result = assign(svc, saved(svc), targets=[{'account_id': 'ACCOUNT-A'},
        {'account_id': 'ACCOUNT-B', 'worker': 'Air_39'}, {'account_id': 'UNKNOWN'}])
    assert [r['status'] for r in result['results']] == ['pending', 'conflict', 'unavailable']
    assert set(svc.build_route_store().read().card_assignments) == {'ACCOUNT-A'}


def test_stale_revision_publishes_none(tmp_path: Path) -> None:
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    sid = saved(svc)
    svc.build_route_store().publish(RouteDocument.compatibility(), 0, 'operator')
    with pytest.raises(RouteConflict):
        assign(svc, sid)
    assert svc.build_route_store().read().card_assignments == {}


def test_later_route_ack_proves_exact_overlay_and_replacement_cannot_ack(tmp_path: Path) -> None:
    from fleet.cards import acknowledge_card_assignment
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    sid = saved(svc)
    assignment = assign(svc, sid)['results'][0]['assignment']
    runtime = BuildRouteRuntime(tmp_path, 'Air_38', 'ACCOUNT-A')
    route = svc.build_route_store().read()
    svc.build_route_store().publish(route, 1, 'later-edit')
    runtime.acknowledge(2, 'ACCOUNT-A')
    ack = json.loads(runtime.ack_path.read_text())['cards']
    assert ack['published_revision'] == 1
    active = acknowledge_card_assignment(assignment, account_id='ACCOUNT-A', revision=1, overlay_id=ack['overlay_id'])
    assert active['status'] == 'active' and active['applied_revision'] == 1
    assert acknowledge_card_assignment(assignment, account_id='ACCOUNT-B', revision=1, overlay_id=ack['overlay_id']) == assignment
    assert acknowledge_card_assignment(assignment, account_id='ACCOUNT-A', revision=1, overlay_id='other') == assignment
    assert acknowledge_card_assignment(assignment, account_id='ACCOUNT-A', revision=2, overlay_id=ack['overlay_id']) == assignment
    replay = assign(svc, sid, 2)['results'][0]
    assert replay['status'] == 'accepted'
    assert replay['assignment']['status'] == 'active'


def test_explicit_loadout_edit_is_validated_and_does_not_enable(tmp_path: Path) -> None:
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    client = _client(tmp_path)
    svc = service(tmp_path)
    assign(svc, saved(svc))
    payload = {'expected_revision': 1, 'account_id': 'ACCOUNT-A', 'loadout_id': 'missing'}
    assert client.post('/api/fleet/cards/loadouts', json=payload).status_code == 422
    payload['loadout_id'] = 'farm'
    result = client.post('/api/fleet/cards/loadouts', json=payload)
    assert result.status_code == 200
    assert result.json()['revision'] == 2
    overlay = svc.build_route_store().read().card_assignments['ACCOUNT-A']
    assert overlay.program.selected_loadout_id == 'farm'
    assert 'enabled' not in overlay.to_dict()


def test_fleet_api_requires_explicit_targets_and_refresh_only(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.post('/api/fleet/cards/commands', json={'targets': []}).status_code == 422
    assert client.post('/api/fleet/cards/commands', json={'targets': [{'worker': 'Air_38', 'kind': 'buy'}]}).status_code == 422


def test_ordinary_route_edit_cannot_invent_overlay(tmp_path: Path) -> None:
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    assign(svc, saved(svc))
    route = svc.build_route_store().read().to_dict()
    route['card_assignments']['ACCOUNT-A']['program']['gem_cap'] = 900
    response = _client(tmp_path).put('/api/fleet/reroll/route', json={'expected_revision': 1, 'actor': 'operator', 'route': route})
    assert response.status_code == 422
    assert svc.build_route_store().read().card_assignments['ACCOUNT-A'].program.gem_cap == 200


def test_atomic_publish_race_applies_no_partial_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fleet.build_route_store import BuildRouteStore
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    sid = saved(svc)
    publish = BuildRouteStore.publish
    def raced(self: BuildRouteStore, draft: RouteDocument, expected_revision: int, actor: str) -> RouteDocument:
        publish(self, RouteDocument.compatibility(), 0, 'concurrent')
        return publish(self, draft, expected_revision, actor)
    monkeypatch.setattr(BuildRouteStore, 'publish', raced)
    with pytest.raises(RouteConflict):
        assign(svc, sid)
    assert svc.build_route_store().read().card_assignments == {}


def test_gem_references_conflict_per_receiver_preserves_other_whole_strategy(tmp_path: Path) -> None:
    from fleet.build_route import StrategyAssignment
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    _registered_worker(tmp_path, 'ACCOUNT-B', 'ACCOUNT-B', 'Air_39')
    svc = service(tmp_path)
    sid = saved(svc)
    baseline = RouteDocument.compatibility().baseline.to_dict()
    baseline['cards'] = {'version': 1, 'gem_cap': 100, 'goals': [{'kind': 'slots', 'id': 'slots', 'capacity': 5}]}
    baseline['gems'].update(mode='blocks', blocks=[{'type': 'unlock_lab_slot', 'id': 'lab2', 'slot': 2}, {'type': 'card_goal', 'id': 'goal', 'goal_id': 'slots'}])
    whole = StrategyAssignment.from_dict({'account_id': 'ACCOUNT-B', 'strategy_id': 'other', 'strategy_version': 1,
        'strategy_name': 'Other', 'baseline': baseline})
    route = replace(RouteDocument.compatibility(), assignments={'Air_39': whole})
    svc.build_route_store().publish(route, 0, 'operator')
    result = assign(svc, sid, 1, [{'account_id': 'ACCOUNT-A'}, {'account_id': 'ACCOUNT-B'}])
    assert [row['status'] for row in result['results']] == ['pending', 'conflict']
    after = svc.build_route_store().read()
    assert after.assignments == route.assignments
    assert after.baseline == route.baseline
    assert resolve_route(after, 'Air_39', 'ACCOUNT-B').cards.goals[0].id == 'slots'


def test_replacement_cannot_use_old_ack_or_history(tmp_path: Path) -> None:
    import db
    from fleet.cards import fleet_cards_snapshot
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    assign(svc, saved(svc))
    runtime = BuildRouteRuntime(tmp_path, 'Air_38', 'ACCOUNT-A')
    runtime.acknowledge(1, 'ACCOUNT-A')
    registration_path = tmp_path / 'workers/Air_38/fleet-registration.json'
    registration = json.loads(registration_path.read_text())
    binding_path = Path(registration['binding'])
    binding = json.loads(binding_path.read_text())
    registration['account_id'] = binding['account_id'] = 'ACCOUNT-B'
    registration_path.write_text(json.dumps(registration))
    binding_path.write_text(json.dumps(binding))
    with pytest.raises(ValueError, match='binding'):
        runtime.acknowledge(1, 'ACCOUNT-A')
    rows = fleet_cards_snapshot(svc)['accounts']
    old = next(row for row in rows if row['account_id'] == 'ACCOUNT-A')
    new = next(row for row in rows if row['account_id'] == 'ACCOUNT-B')
    assert old['assignment']['status'] == 'pending'
    assert old['cards']['snapshot'] is None and old['cards']['preconditions'] is None
    assert new['cards']['read_only_reason'] == 'route_account_binding_changed'
    assert new['cards']['snapshot'] is None


def test_aggregation_reads_concurrently_and_falls_back_bound_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from threading import Barrier
    from fleet.cards import fleet_cards_snapshot
    from web.cards_api import read_cards_projection
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    _registered_worker(tmp_path, 'ACCOUNT-B', 'ACCOUNT-B', 'Air_39')
    barrier = Barrier(2)
    def request(choice: Any, path: str, body: dict | None = None) -> dict:
        barrier.wait(timeout=2)
        if choice.account_id == 'ACCOUNT-B':
            raise TimeoutError()
        return read_cards_projection(path=choice.db_path, account_id=choice.account_id,
            context=None, read_only_reason='cards_runtime_unavailable').model_dump(mode='json')
    monkeypatch.setattr('fleet.cards._worker_request', request)
    result = fleet_cards_snapshot(service(tmp_path))
    assert [row['cards']['read_only_reason'] for row in result['accounts']] == ['cards_runtime_unavailable', 'worker_unavailable']
    assert all(row['cards']['scope'] is None and row['cards']['fresh'] is False for row in result['accounts'])


def test_aggregation_rejects_cross_account_worker_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fleet.cards import fleet_cards_snapshot
    from web.cards_api import read_cards_projection
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    wrong = read_cards_projection(path=None, account_id='ACCOUNT-B', context=None,
        read_only_reason='worker_unavailable').model_dump(mode='json')
    monkeypatch.setattr('fleet.cards._worker_request', lambda *args: wrong)
    cards = fleet_cards_snapshot(service(tmp_path))['accounts'][0]['cards']
    assert cards['account_id'] == 'ACCOUNT-A'
    assert cards['program'] is None and cards['preconditions'] is None


def test_offline_controls_unavailable_and_never_create_assignment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    def unavailable(*args: object) -> dict:
        raise TimeoutError()
    monkeypatch.setattr('fleet.cards._worker_request', unavailable)
    client = _client(tmp_path)
    body = {'targets': [{'worker': 'Air_38', 'expected_account_id': 'ACCOUNT-A',
        'expected_generation': 'generation', 'expected_epoch': 1,
        'expected_program_revision': 'revision', 'enabled': True}]}
    response = client.post('/api/fleet/cards/automation', json=body)
    assert response.status_code == 200
    assert response.json()['results'][0]['status'] == 'unavailable'
    assert service(tmp_path).build_route_store().read().revision == 0
    assert not (tmp_path / 'build-route.json').exists()


def test_old_overlay_ack_cannot_activate_reassigned_program(tmp_path: Path) -> None:
    from fleet.cards import fleet_cards_snapshot
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    sid = saved(svc)
    first = assign(svc, sid)['results'][0]['assignment']
    BuildRouteRuntime(tmp_path, 'Air_38', 'ACCOUNT-A').acknowledge(1, 'ACCOUNT-A')
    baseline = svc.strategy_library().version(sid, 1)['baseline']
    baseline['cards']['gem_cap'] = 400
    svc.strategy_library().save(expected_revision=1, strategy_id=sid, name='Cards', source_template='opening', baseline=baseline)
    from fleet.cards import assign_card_program
    response = assign_card_program(svc, expected_revision=1, strategy_id=sid, strategy_version=2,
        targets=[{'account_id': 'ACCOUNT-A'}])
    row = response['results'][0]
    assert row['assignment']['overlay_id'] != first['overlay_id']
    assert row['assignment']['published_revision'] == 2
    assert row['status'] == 'pending'
    assert row['assignment']['applied_revision'] is None


@pytest.mark.parametrize('mutation', ['unknown_field', 'wrong_key', 'bool_revision', 'bad_worker'])
def test_route_overlay_schema_rejects_ambiguous_ownership(tmp_path: Path, mutation: str) -> None:
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    assign(svc, saved(svc))
    raw = svc.build_route_store().read().to_dict()
    overlay = raw['card_assignments']['ACCOUNT-A']
    if mutation == 'unknown_field':
        overlay['enabled'] = True
    elif mutation == 'wrong_key':
        overlay['account_id'] = 'ACCOUNT-B'
    elif mutation == 'bool_revision':
        overlay['published_revision'] = True
    else:
        overlay['worker'] = '../escape'
    with pytest.raises(ValueError):
        RouteDocument.from_dict(raw)


def test_api_publishes_valid_subset_and_reports_revision_conflict(tmp_path: Path) -> None:
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    sid = saved(svc)
    body = {'expected_revision': 0, 'strategy_id': sid, 'strategy_version': 1,
        'targets': [{'account_id': 'ACCOUNT-A'}, {'account_id': 'UNKNOWN'}]}
    client = _client(tmp_path)
    result = client.post('/api/fleet/cards/assignments', json=body)
    assert result.status_code == 200
    assert [row['status'] for row in result.json()['results']] == ['pending', 'unavailable']
    conflict = client.post('/api/fleet/cards/assignments', json=body)
    assert conflict.status_code == 409
    assert conflict.json()['detail']['current_revision'] == 1


def test_ambiguous_account_registration_stays_visible_but_cannot_assign(tmp_path: Path) -> None:
    from fleet.cards import fleet_cards_snapshot
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A', 'Air_39')
    svc = service(tmp_path)
    result = assign(svc, saved(svc), targets=[{'account_id': 'ACCOUNT-A'}])
    assert result['results'][0]['status'] == 'conflict'
    rows = fleet_cards_snapshot(svc)['accounts']
    assert len(rows) == 1
    assert rows[0]['cards']['read_only_reason'] == 'account_identity_ambiguous'
    assert rows[0]['cards']['preconditions'] is None
    assert rows[0]['worker'] is None


def test_corrupt_worker_database_does_not_hide_other_accounts(tmp_path: Path) -> None:
    from fleet.cards import fleet_cards_snapshot
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    _registered_worker(tmp_path, 'ACCOUNT-B', 'ACCOUNT-B', 'Air_39')
    (tmp_path / 'workers/Air_39/tower_bot.db').write_bytes(b'not a database')
    rows = fleet_cards_snapshot(service(tmp_path))['accounts']
    assert {row['account_id'] for row in rows} == {'ACCOUNT-A', 'ACCOUNT-B'}
    bad = next(row for row in rows if row['account_id'] == 'ACCOUNT-B')
    assert bad['cards']['scope'] is None
    assert bad['cards']['snapshot'] is None


@pytest.mark.parametrize('historical_overlay', [False, True])
def test_generic_rollback_cannot_delete_or_restore_cards_overlay(tmp_path: Path, historical_overlay: bool) -> None:
    from fleet.cards import select_card_loadout
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    sid = saved(svc)
    svc.build_route_store().publish(RouteDocument.compatibility(), 0, 'operator')
    assign(svc, sid, 1)
    target_revision = 1
    if historical_overlay:
        BuildRouteRuntime(tmp_path, 'Air_38', 'ACCOUNT-A').acknowledge(2, 'ACCOUNT-A')
        select_card_loadout(svc, expected_revision=2, account_id='ACCOUNT-A', loadout_id='farm')
        target_revision = 2
    before = svc.build_route_store().read()
    response = _client(tmp_path).post('/api/fleet/reroll/route/rollback', json={
        'revision': target_revision, 'expected_revision': before.revision, 'actor': 'operator'})
    assert response.status_code == 422
    assert 'Cards' in response.json()['detail']
    assert svc.build_route_store().read() == before
    assert not (tmp_path / 'build-route-history' / f'{before.revision + 1}.json').exists()


def test_truncated_worker_read_keeps_healthy_account_and_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    from http.client import IncompleteRead
    from fleet.cards import fleet_cards_snapshot
    from web.cards_api import read_cards_projection
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    _registered_worker(tmp_path, 'ACCOUNT-B', 'ACCOUNT-B', 'Air_39')
    class Truncated(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise IncompleteRead(b'{"account_id":', 100)
    def fetch(request: Any, *, timeout: float) -> io.BytesIO:
        if request.headers['X-expected-account-id'] == 'ACCOUNT-B':
            return Truncated()
        payload = read_cards_projection(path=tmp_path / 'workers/Air_38/tower_bot.db',
            account_id='ACCOUNT-A', context=None, read_only_reason='cards_runtime_unavailable')
        return io.BytesIO(payload.model_dump_json().encode())
    monkeypatch.setattr('fleet.cards.urlopen', fetch)
    rows = fleet_cards_snapshot(service(tmp_path))['accounts']
    assert [row['account_id'] for row in rows] == ['ACCOUNT-A', 'ACCOUNT-B']
    assert [row['cards']['read_only_reason'] for row in rows] == ['cards_runtime_unavailable', 'worker_unavailable']
    assert rows[1]['cards']['scope'] is None and rows[1]['cards']['preconditions'] is None


@pytest.mark.parametrize('automation', [False, True])
@pytest.mark.parametrize('error_body', [False, True])
def test_truncated_worker_proxy_preserves_successful_sibling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        automation: bool, error_body: bool) -> None:
    import io
    from http.client import IncompleteRead
    from urllib.error import HTTPError
    from fleet.cards import proxy_card_targets
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    _registered_worker(tmp_path, 'ACCOUNT-B', 'ACCOUNT-B', 'Air_39')
    class Truncated(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise IncompleteRead(b'{', 100)
    def fetch(request: Any, *, timeout: float) -> io.BytesIO:
        if request.headers['X-expected-account-id'] == 'ACCOUNT-B':
            if error_body:
                raise HTTPError(request.full_url, 503, 'unavailable', {}, Truncated())
            return Truncated()
        return io.BytesIO(b'{"operation_id":"healthy-result"}')
    monkeypatch.setattr('fleet.cards.urlopen', fetch)
    targets = [{'worker': worker, 'expected_account_id': account,
        'expected_generation': 'generation', 'expected_epoch': 1, 'expected_program_revision': 'program',
        **({'enabled': True} if automation else {'kind': 'refresh', 'idempotency_key': 'refresh'})}
        for worker, account in [('Air_38', 'ACCOUNT-A'), ('Air_39', 'ACCOUNT-B')]]
    results = proxy_card_targets(service(tmp_path), targets=targets, automation=automation)['results']
    assert [row['status'] for row in results] == ['accepted', 'unavailable']
    assert results[0]['result']['operation_id'] == 'healthy-result'
    assert results[1]['reason'] == ('worker_request_failed' if error_body else 'worker_unavailable')


def test_rollback_publication_race_cannot_overwrite_new_cards_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fleet.build_route_store import BuildRouteStore
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    sid = saved(svc)
    store = svc.build_route_store()
    store.publish(RouteDocument.compatibility(), 0, 'operator')
    store.publish(store.read(), 1, 'operator')
    publish = BuildRouteStore.publish
    def raced(self: BuildRouteStore, draft: RouteDocument, expected_revision: int, actor: str) -> RouteDocument:
        monkeypatch.setattr(BuildRouteStore, 'publish', publish)
        assign(svc, sid, 2)
        return publish(self, draft, expected_revision, actor)
    monkeypatch.setattr(BuildRouteStore, 'publish', raced)
    response = _client(tmp_path).post('/api/fleet/reroll/route/rollback', json={
        'revision': 1, 'expected_revision': 2, 'actor': 'operator'})
    assert response.status_code == 409
    assert store.read().revision == 3
    assert store.read().card_assignments['ACCOUNT-A'].published_revision == 3

@pytest.mark.parametrize('change', ['operation_id', 'account_id', 'lease'])
def test_account_evidence_proxy_rejects_mismatched_response_or_scope(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    from fleet.cards import proxy_card_account
    from web.cards_api import read_cards_projection
    from card_models import CardCommand, CardOperation
    from evidence_scope import FactScope
    _registered_worker(tmp_path, 'ACCOUNT-A', 'ACCOUNT-A')
    svc = service(tmp_path)
    scope = FactScope('ACCOUNT-A', 'lease', 'g', 1)
    projection = read_cards_projection(path=None, account_id='ACCOUNT-A', context=None,
                                      read_only_reason=None).model_dump(mode='json')
    pre = {'expected_account_id': 'ACCOUNT-A', 'expected_generation': 'g', 'expected_epoch': 1,
           'expected_program_revision': 'revision'}
    projection.update(scope={'account_id': 'ACCOUNT-A', 'lease_id': 'lease', 'generation': 'g', 'epoch': 1},
                      preconditions=pre)
    command = CardCommand(idempotency_key='k', scope=scope, program_revision='revision', kind='refresh', quantity=1, source='manual')
    operation = CardOperation(operation_id='expected', command=command, status='queued', created_at=1, updated_at=1).model_dump(mode='json')
    if change == 'operation_id':
        operation['operation_id'] = 'wrong'
    if change == 'account_id':
        operation['command']['scope']['account_id'] = 'OTHER'
    reads = 0
    def request(choice: object, path: str, body: object = None) -> dict[str, Any]:
        nonlocal reads
        if path == '/api/cards':
            reads += 1
            result = json.loads(json.dumps(projection))
            if change == 'lease' and reads > 1:
                result['scope']['lease_id'] = 'new'
            return result
        return operation
    monkeypatch.setattr('fleet.cards._worker_request', request)
    result = proxy_card_account(svc, action='operation', target={**pre, 'worker': 'Air_38', 'operation_id': 'expected'})
    assert result['status'] == 'conflict'
    assert 'result' not in result

@pytest.mark.parametrize(('action', 'replacement_phase'), [
    ('operation', 'response'), ('operation', 'final_projection'),
    ('preview', 'response'), ('preview', 'final_projection'),
    ('command', 'response'), ('cycle', 'response'),
])
@pytest.mark.parametrize('changed_field', ['web_port', 'db_path'])
def test_account_proxy_rejects_same_account_registration_replacement_after_io(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, action: str, replacement_phase: str,
        changed_field: str) -> None:
    from card_models import CardCommand, CardOperation, CardProgram
    from evidence_scope import FactScope
    from fleet.cards import proxy_card_account
    from web.account_catalog import AccountChoice
    from web.cards_api import read_cards_projection
    original = AccountChoice('worker:Air_38', 'ACCOUNT-A', 'Air_38', tmp_path / 'old.db', 'worker', 8081)
    replacement = replace(original, **{changed_field: 8082 if changed_field == 'web_port' else tmp_path / 'new.db'})
    current = original
    monkeypatch.setattr('fleet.cards._choices', lambda service: [current])
    monkeypatch.setattr('fleet.cards.registered_worker', lambda path: current)
    monkeypatch.setattr('fleet.cards._bound_account', lambda path: 'ACCOUNT-A')
    pre = {'expected_account_id': 'ACCOUNT-A', 'expected_generation': 'g', 'expected_epoch': 1,
           'expected_program_revision': 'revision'}
    projection = read_cards_projection(path=None, account_id='ACCOUNT-A', context=None,
                                      read_only_reason=None).model_dump(mode='json')
    projection.update(scope={'account_id': 'ACCOUNT-A', 'lease_id': 'old-lease', 'generation': 'g', 'epoch': 1},
                      preconditions=pre)
    command = CardCommand(idempotency_key='k', scope=FactScope('ACCOUNT-A', 'old-lease', 'g', 1),
                          program_revision='revision', kind='refresh', quantity=1, source='manual')
    operation = CardOperation(operation_id='expected', command=command, status='queued',
                              created_at=1, updated_at=1).model_dump(mode='json')
    target: dict[str, Any] = {**pre, 'worker': 'Air_38'}
    if action == 'operation':
        target['operation_id'] = 'expected'
    elif action == 'preview':
        target['program'] = CardProgram(version=1, gem_cap=100).model_dump(mode='json')
    elif action == 'command':
        target.update(idempotency_key='k', kind='refresh')
    else:
        target.update(cycle_id='cycle', cap=100)
    budget = {'cycle_id': 'cycle', 'cap': 100, 'spent': 0, 'pending': 0}
    response = {'command': operation, 'operation': operation,
                'preview': {'fresh': False, 'goals': [], 'loadouts': []},
                'cycle': {'budget': budget, 'active': True, 'active_budget': budget}}[action]
    reads = 0
    def request(choice: AccountChoice, path: str, body: object = None) -> dict[str, Any]:
        nonlocal current, reads
        assert choice == original
        if path == '/api/cards':
            reads += 1
            if replacement_phase == 'final_projection' and reads == 2:
                current = replacement
            # The old endpoint remains internally consistent after replacement.
            return projection
        if replacement_phase == 'response':
            current = replacement
        return response
    monkeypatch.setattr('fleet.cards._worker_request', request)
    result = proxy_card_account(service(tmp_path), action=action, target=target)
    assert current == replacement
    assert result['status'] == 'conflict'
    assert result['reason'] == 'route_account_binding_changed'
    assert 'result' not in result
