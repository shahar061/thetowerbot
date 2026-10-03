"""Account-owned Cards publication and bounded, read-only fleet aggregation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.client import HTTPException as HTTPProtocolError
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import db
from fleet.build_route import CardAssignment, RouteBaseline, resolve_route
from fleet.build_route_store import RouteConflict
from fleet.state_view import MAX_WORKERS, STATUS_TIMEOUT
from web.account_catalog import AccountChoice, registered_worker

if TYPE_CHECKING:
    from fleet.setup import FleetSetupService

logger = logging.getLogger(__name__)


def acknowledge_card_assignment(assignment: dict[str, object], *, account_id: str,
                                revision: int, overlay_id: str | None = None) -> dict[str, object]:
    """Only exact immutable overlay proof can activate a published assignment."""
    if (assignment.get('account_id') != account_id or assignment.get('published_revision') != revision
            or not overlay_id or assignment.get('overlay_id') != overlay_id):
        return dict(assignment)
    return {**assignment, 'applied_revision': revision, 'status': 'active'}


def _choices(service: FleetSetupService) -> list[AccountChoice]:
    workers = service.root / 'workers'
    if not workers.is_dir():
        return []
    return [choice for path in sorted(workers.iterdir()) if path.is_dir() and not path.is_symlink()
            if (choice := registered_worker(path)) is not None]


def _bound_account(path: Path) -> str | None:
    try:
        return db.bound_account(path)
    except (OSError, sqlite3.Error):
        return None


def _target(service: FleetSetupService, account_id: str, worker: str | None,
            choices: list[AccountChoice]) -> tuple[AccountChoice | None, str | None]:
    matches = [choice for choice in choices if choice.account_id == account_id]
    if len(matches) > 1:
        return None, 'account_identity_ambiguous'
    if worker is not None:
        registration = registered_worker(service.root / 'workers' / worker)
        if registration is None or registration.account_id != account_id:
            return None, 'route_account_binding_changed'
        matches = [choice for choice in matches if choice.instance == worker]
    if len(matches) != 1:
        return None, 'account_unavailable'
    choice = matches[0]
    if _bound_account(choice.db_path) != account_id:
        return None, 'route_account_binding_changed'
    return choice, None


def _assignment_state(service: FleetSetupService, overlay: CardAssignment,
                      choices: list[AccountChoice]) -> dict[str, object]:
    result = {**overlay.to_dict(), 'applied_revision': None, 'status': 'pending'}
    choice, reason = _target(service, overlay.account_id, None, choices)
    if reason or choice is None:
        return result
    try:
        ack = json.loads((choice.db_path.parent / 'build-route-applied.json').read_text())
        proof = ack.get('cards') or {}
        return acknowledge_card_assignment(result, account_id=ack.get('account_id'),
            revision=proof.get('published_revision'), overlay_id=proof.get('overlay_id'))
    except (OSError, ValueError, TypeError, AttributeError):
        return result


def assign_card_program(service: FleetSetupService, *, expected_revision: int,
                        strategy_id: str, strategy_version: int,
                        targets: list[dict[str, str]]) -> dict[str, object]:
    """One optimistic publication for valid accounts; no worker control mutations."""
    store = service.build_route_store()
    current = store.read()
    if current.revision != expected_revision:
        raise RouteConflict(current)
    if not targets or len({target.get('account_id') for target in targets}) != len(targets):
        raise ValueError('Cards assignment requires distinct explicit accounts')
    saved = service.strategy_library().version(strategy_id, strategy_version)
    program = RouteBaseline.from_dict(saved['baseline']).cards
    if program is None:
        raise ValueError('saved strategy has no Cards program')
    choices = _choices(service)
    overlays = dict(current.card_assignments)
    results: list[dict[str, Any]] = []
    for target in targets:
        account_id, worker = target['account_id'], target.get('worker')
        choice, reason = _target(service, account_id, worker, choices)
        previous = overlays.get(account_id)
        # Published account-owned intent remains known after its worker leaves.
        if reason == 'account_unavailable' and previous is not None and worker is None:
            reason = None
        if reason:
            results.append({**target, 'status': 'conflict' if reason in {'route_account_binding_changed', 'account_identity_ambiguous'} else 'unavailable', 'reason': reason})
            continue
        overlay = CardAssignment(account_id, saved['id'], saved['version'],
            choice.instance if choice else None, uuid4().hex, expected_revision + 1, program)
        if previous is not None and (previous.strategy_id, previous.strategy_version, previous.program) == (saved['id'], saved['version'], program):
            overlay = previous
        candidate = replace(current, card_assignments={**overlays, account_id: overlay})
        receiver = choice.instance if choice else next((name for name, value in current.assignments.items()
            if value.account_id == account_id), '')
        try:
            resolve_route(candidate, receiver or '', account_id)
        except ValueError as exc:
            results.append({**target, 'status': 'conflict', 'reason': str(exc)})
            continue
        overlays[account_id] = overlay
        results.append({**target, 'status': 'pending', 'assignment': overlay})
    if overlays != current.card_assignments:
        current = store.publish(replace(current, card_assignments=overlays), expected_revision, 'operator:cards')
        logger.info('Cards assignments published', extra={'route_revision': current.revision,
                    'account_ids': [row['account_id'] for row in results if 'assignment' in row]})
    for row in results:
        if 'assignment' in row:
            row['assignment'] = _assignment_state(service, row['assignment'], choices)
            row['status'] = 'accepted' if row['assignment']['status'] == 'active' else 'pending'
    return {'revision': current.revision, 'results': results}


def select_card_loadout(service: FleetSetupService, *, expected_revision: int,
                        account_id: str, loadout_id: str | None) -> dict[str, object]:
    store = service.build_route_store()
    route = store.read()
    if route.revision != expected_revision:
        raise RouteConflict(route)
    overlay = route.card_assignments.get(account_id)
    if overlay is None:
        raise ValueError('Cards assignment not found')
    if loadout_id is not None and loadout_id not in {item.id for item in overlay.program.loadouts}:
        raise ValueError('card_loadout_not_found')
    if overlay.program.selected_loadout_id != loadout_id:
        program = overlay.program.model_copy(update={'selected_loadout_id': loadout_id})
        overlay = replace(overlay, program=program, overlay_id=uuid4().hex, published_revision=expected_revision + 1)
        route = store.publish(replace(route, card_assignments={**route.card_assignments, account_id: overlay}),
                              expected_revision, 'operator:cards-loadout')
    return {'revision': route.revision, 'assignment': _assignment_state(service, overlay, _choices(service))}


def _worker_request(choice: AccountChoice, path: str, body: dict[str, object] | None = None) -> dict[str, Any]:
    request = Request(f'http://127.0.0.1:{choice.web_port}{path}',
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json', 'x-account-scope': choice.key,
                 'x-expected-account-id': choice.account_id})
    with urlopen(request, timeout=STATUS_TIMEOUT) as response:
        return json.load(response)


def fleet_cards_snapshot(service: FleetSetupService) -> dict[str, object]:
    from web.cards_api import CardsProjection, read_cards_projection
    route = service.build_route_store().read()
    choices = _choices(service)

    def read(choice: AccountChoice) -> dict[str, object]:
        reason = 'worker_unavailable'
        projection = None
        if _bound_account(choice.db_path) == choice.account_id:
            try:
                live = CardsProjection.model_validate_json(json.dumps(_worker_request(choice, '/api/cards')))
                if live.account_id != choice.account_id or (live.scope and live.scope.account_id != choice.account_id):
                    raise ValueError('worker_account_changed')
                # Recheck registration/binding after HTTP; replacements cannot lend authority.
                current, error = _target(service, choice.account_id, choice.instance, _choices(service))
                if error or current != choice:
                    raise ValueError('worker_account_changed')
                projection = live
            except (OSError, HTTPProtocolError, ValueError, TypeError):
                reason = 'worker_unavailable'
        else:
            reason = 'route_account_binding_changed'
        if projection is None:
            try:
                projection = read_cards_projection(path=choice.db_path if _bound_account(choice.db_path) == choice.account_id else None,
                    account_id=choice.account_id, context=None, read_only_reason=reason)
            except (OSError, ValueError, sqlite3.Error):
                projection = read_cards_projection(path=None, account_id=choice.account_id,
                    context=None, read_only_reason='cards_history_unavailable')
        overlay = route.card_assignments.get(choice.account_id)
        return {'account_id': choice.account_id, 'worker': choice.instance,
                'cards': projection.model_dump(mode='json'),
                'assignment': _assignment_state(service, overlay, choices) if overlay else None}

    counts = {choice.account_id: sum(other.account_id == choice.account_id for other in choices) for choice in choices}
    unique = [choice for choice in choices if counts[choice.account_id] == 1]
    with ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(unique)))) as pool:
        rows = list(pool.map(read, unique))
    for account_id, count in counts.items():
        if count > 1:
            overlay = route.card_assignments.get(account_id)
            rows.append({'account_id': account_id, 'worker': None,
                'cards': read_cards_projection(path=None, account_id=account_id, context=None,
                    read_only_reason='account_identity_ambiguous').model_dump(mode='json'),
                'assignment': _assignment_state(service, overlay, choices) if overlay else None})
    present = {row['account_id'] for row in rows}
    for account_id, overlay in route.card_assignments.items():
        if account_id not in present:
            rows.append({'account_id': account_id, 'worker': None,
                'cards': read_cards_projection(path=None, account_id=account_id, context=None,
                    read_only_reason='worker_unavailable').model_dump(mode='json'),
                'assignment': _assignment_state(service, overlay, choices)})
    return {'revision': route.revision, 'accounts': rows}


def proxy_card_targets(service: FleetSetupService, *, targets: list[dict[str, Any]],
                       automation: bool = False) -> dict[str, object]:
    """Proxy explicit worker requests; only worker runtime owns queue/control writes."""
    choices = _choices(service)

    def submit(target: dict[str, Any]) -> dict[str, object]:
        account_id, worker = target['expected_account_id'], target['worker']
        choice, reason = _target(service, account_id, worker, choices)
        base = {'account_id': account_id, 'worker': worker}
        if reason or choice is None:
            return {**base, 'status': 'conflict' if reason in {'route_account_binding_changed', 'account_identity_ambiguous'} else 'unavailable', 'reason': reason}
        body = {key: value for key, value in target.items() if key != 'worker'}
        try:
            result = _worker_request(choice, '/api/cards/automation' if automation else '/api/cards/commands', body)
            return {**base, 'status': 'accepted', 'result': result}
        except HTTPError as exc:
            try:
                detail = json.load(exc).get('detail')
            except (ValueError, OSError, HTTPProtocolError):
                detail = 'worker_request_failed'
            return {**base, 'status': 'conflict' if exc.code in {409, 422} else 'unavailable', 'reason': detail}
        except (OSError, HTTPProtocolError, ValueError, TypeError):
            return {**base, 'status': 'unavailable', 'reason': 'worker_unavailable'}

    with ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(targets)))) as pool:
        return {'results': list(pool.map(submit, targets))}


def proxy_card_account(service: FleetSetupService, *, action: str,
                       target: dict[str, Any]) -> dict[str, object]:
    """Closed, single-account transport. Reads are fenced before and after I/O."""
    from urllib.parse import quote
    from web.cards_api import CardsProjection, CardPreview, CardBudgetCycleResponse
    from card_models import CardOperation
    if action not in {'command', 'cycle', 'preview', 'operation'}:
        raise ValueError('unsupported Cards transport')
    account_id, worker = target['expected_account_id'], target['worker']
    base = {'account_id': account_id, 'worker': worker}
    choice, reason = _target(service, account_id, worker, _choices(service))
    if reason or choice is None:
        return {**base, 'status': 'conflict' if reason in {'route_account_binding_changed', 'account_identity_ambiguous'} else 'unavailable', 'reason': reason}
    body = {key: value for key, value in target.items() if key != 'worker'}
    try:
        before = None
        if action in {'preview', 'operation'}:
            before = CardsProjection.model_validate_json(json.dumps(_worker_request(choice, '/api/cards')))
            pre = before.preconditions.model_dump() if before.preconditions else None
            if before.account_id != account_id or pre != {key: target[key] for key in (
                    'expected_account_id', 'expected_generation', 'expected_epoch', 'expected_program_revision')}:
                return {**base, 'status': 'conflict', 'reason': 'cards_authority_changed'}
        path = {'command': '/api/cards/commands', 'cycle': '/api/cards/budget-cycles',
                'preview': '/api/cards/preview',
                'operation': '/api/cards/operations/' + quote(target.get('operation_id', ''), safe='')}[action]
        wire = {'program': target['program']} if action == 'preview' else None if action == 'operation' else body
        result = _worker_request(choice, path, wire)
        model = CardOperation if action in {'command', 'operation'} else CardPreview if action == 'preview' else CardBudgetCycleResponse
        parsed = model.model_validate_json(json.dumps(result))
        if isinstance(parsed, CardOperation) and parsed.command.scope.account_id != account_id:
            return {**base, 'status': 'conflict', 'reason': 'cards_response_identity_changed'}
        if action == 'operation' and parsed.operation_id != target['operation_id']:
            return {**base, 'status': 'conflict', 'reason': 'cards_response_identity_changed'}
        if action == 'command' and (parsed.command.idempotency_key != target['idempotency_key']
                or parsed.command.kind != target['kind']
                or parsed.command.scope.generation != target['expected_generation']
                or parsed.command.scope.epoch != target['expected_epoch']
                or parsed.command.program_revision != target['expected_program_revision']):
            return {**base, 'status': 'conflict', 'reason': 'cards_response_identity_changed'}
        if before is not None:
            after = CardsProjection.model_validate_json(json.dumps(_worker_request(choice, '/api/cards')))
            if after.account_id != account_id or before.scope != after.scope or before.preconditions != after.preconditions:
                return {**base, 'status': 'conflict', 'reason': 'cards_authority_changed'}
        # Account/worker names alone do not pin a registration: the same account
        # can be moved to another endpoint or database while the old endpoint
        # continues returning internally consistent evidence. Check the complete
        # target after the last worker I/O, including the final projection read.
        current_choice, binding_error = _target(service, account_id, worker, _choices(service))
        if binding_error or current_choice != choice:
            return {**base, 'status': 'conflict',
                    'reason': binding_error or 'route_account_binding_changed'}
        logger.info('Fleet Cards account request', extra={'account_id': account_id, 'worker': worker,
                    'card_action': action, 'operation_id': getattr(parsed, 'operation_id', None)})
        return {**base, 'status': 'accepted', 'result': parsed.model_dump(mode='json')}
    except HTTPError as exc:
        try:
            detail = json.load(exc).get('detail')
        except (ValueError, OSError, HTTPProtocolError):
            detail = 'worker_request_failed'
        return {**base, 'status': 'conflict' if 400 <= exc.code < 500 else 'unavailable',
                'reason': detail, 'http_status': exc.code}
    except (OSError, HTTPProtocolError, ValueError, TypeError):
        return {**base, 'status': 'unavailable', 'reason': 'worker_unavailable'}
