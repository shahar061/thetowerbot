"""Account-scoped Cards HTTP projections and durable runner commands."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from pathlib import Path
from typing import Literal, Self

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

import card_catalog
import db
from account_state import AccountState
from card_models import (CardBudget, CardCapabilities, CardCommand, CardDecision,
                         CardOperation, CardPlanContext, CardProgram, CardSnapshot, Id, LoadoutResolution, CardLoadout)
from card_plan import current_snapshot, plan_cards, goal_met, resolve_loadout
from card_store import CardStore
from concepts import REGISTRY
from evidence_scope import FactScope
from runner import BotRunner, RunnerError
from web.account_catalog import AccountChoice

logger = logging.getLogger(__name__)


class _Envelope(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, allow_inf_nan=False)


class CardPreconditions(_Envelope):
    expected_account_id: Id
    expected_generation: Id
    expected_epoch: int = Field(ge=0)
    expected_program_revision: Id


class CardCommandRequest(CardPreconditions):
    idempotency_key: Id
    kind: Literal['refresh', 'buy', 'slot', 'apply', 'clear', 'cancel']
    quantity: int = Field(default=1, ge=1, le=10)
    loadout_id: Id | None = None
    target_operation_id: Id | None = None
    goal_id: Id | None = None
    budget_cycle_id: str = Field(default='', max_length=100)

    @model_validator(mode='after')
    def action_fields(self) -> Self:
        if self.quantity not in ((1, 10) if self.kind == 'buy' else (1,)):
            raise ValueError('quantity is unsupported for this action')
        if self.kind in {'buy', 'slot'} and not self.budget_cycle_id:
            raise ValueError('budget_cycle_id is required for paid actions')
        if any(char.isspace() for char in self.budget_cycle_id):
            raise ValueError('budget_cycle_id must be a token')
        if (self.kind == 'apply') != (self.loadout_id is not None):
            raise ValueError('loadout_id is required only for apply')
        if (self.kind == 'cancel') != (self.target_operation_id is not None):
            raise ValueError('target_operation_id is required only for cancel')
        if self.goal_id is not None and self.kind not in {'buy', 'slot'}:
            raise ValueError('goal_id is supported only for paid actions')
        return self


class CardBudgetCycleRequest(CardPreconditions):
    cycle_id: Id
    cap: int = Field(ge=0, le=2**63 - 1)


class CardBudgetCycleResponse(_Envelope):
    budget: CardBudget
    active: bool
    active_budget: CardBudget | None


class CardPolicyProjection(_Envelope):
    enabled: bool
    gem_floor: int
    max_per_visit: int
    batch: str


class CardGoalPreview(_Envelope):
    id: str
    met: bool | None


class CardLoadoutPreview(_Envelope):
    id: str
    resolution: LoadoutResolution
    capacity: int | None = None
    capacity_limited: bool | None = None
    capacity_excluded: tuple[str, ...] | None = None
    observed_equipped: tuple[str, ...] | None = None
    additions: tuple[str, ...] | None = None
    removals: tuple[str, ...] | None = None


class CardPreview(_Envelope):
    fresh: bool
    goals: tuple[CardGoalPreview, ...]
    loadouts: tuple[CardLoadoutPreview, ...]


class CardPreviewRequest(_Envelope):
    program: CardProgram


def preview_loadout(loadout: CardLoadout, snapshot: CardSnapshot | None) -> CardLoadoutPreview:
    if snapshot is None:
        return CardLoadoutPreview(id=loadout.id, resolution=LoadoutResolution(reason='inventory_unknown'))
    resolution = resolve_loadout(loadout, snapshot)
    # The same Python compiler evaluates the hypothetical all-priority capacity;
    # unknown lower-priority ownership keeps the capacity explanation unknown.
    expanded = resolve_loadout(loadout, snapshot.model_copy(update={
        'capacity': max(len(loadout.priority), snapshot.capacity or 0)})) if snapshot.capacity is not None else None
    excluded = (tuple(card for card in expanded.desired[snapshot.capacity:])
        if expanded is not None and expanded.desired is not None else () if expanded is not None and expanded.reason == "no_usable_cards" else None)
    equipped = snapshot.equipped if snapshot.equipment_complete else None
    comparable = equipped is not None and resolution.desired is not None
    return CardLoadoutPreview(id=loadout.id, resolution=resolution, capacity=snapshot.capacity,
        capacity_limited=bool(excluded) if excluded is not None else None, capacity_excluded=excluded,
        observed_equipped=equipped,
        additions=tuple(card for card in resolution.desired if card not in equipped) if comparable else None,
        removals=tuple(card for card in equipped if card not in resolution.desired) if comparable else None)


def preview_program(program: CardProgram | None, context: CardPlanContext | None) -> CardPreview:
    snapshot = current_snapshot(context) if context else None
    return CardPreview(fresh=snapshot is not None,
        goals=tuple(CardGoalPreview(id=goal.id, met=goal_met(goal, snapshot) if snapshot else None)
                    for goal in program.goals) if program else (),
        loadouts=tuple(preview_loadout(loadout, snapshot) for loadout in program.loadouts) if program else ())


class CardsProjection(_Envelope):
    account_id: str | None
    config_owner: Literal["local", "fleet", "unavailable"] = "unavailable"
    preview: CardPreview | None = None
    snapshot: CardSnapshot | None
    fresh: bool
    program: CardProgram | None
    program_revision: str | None
    policy: CardPolicyProjection | None
    active_budget: CardBudget | None
    scope: FactScope | None
    preconditions: CardPreconditions | None
    decision: CardDecision
    capabilities: CardCapabilities
    recent_operations: tuple[CardOperation, ...]
    read_only_reason: str | None


class CardCatalogItem(_Envelope):
    card_id: str
    name: str
    max_level: int | None


class CardCatalogResponse(_Envelope):
    cards: tuple[CardCatalogItem, ...]
    max_gem_slots: int
    level_source_url: str
    slot_source_url: str


def read_cards_projection(*, path: Path | None, account_id: str | None,
                          context: CardPlanContext | None,
                          read_only_reason: str | None) -> CardsProjection:
    """Reusable synchronous read adapter; async callers must offload it.

    A recorded DB binding authorizes history only. Only a caller-verified live
    context supplies current plan, policy and mutation preconditions.
    """
    store = CardStore(path) if path is not None and account_id is not None and db.bound_account(path) == account_id else None
    snapshot = store.snapshot() if store else None
    budget = store.active_budget() if store else None
    operations = store.recent_operations() if store else ()
    if context is not None and context.scope.account_id != account_id:
        context = None
        read_only_reason = 'selected_account_changed'
    reason = read_only_reason or ('cards_runtime_unavailable' if context is None else None)
    unavailable = CardCapabilities(inventory=False, buy_one=False, buy_ten=False,
        buy_slot=False, assign=False, reasons={key: reason or 'cards_runtime_unavailable'
            for key in ('inventory', 'buy_one', 'buy_ten', 'buy_slot', 'assign')})
    return CardsProjection(account_id=account_id, snapshot=snapshot,
        config_owner=context.config_owner if context else "unavailable",
        preview=preview_program(context.program if context else None, context),
        fresh=context is not None and current_snapshot(context) is not None,
        program=context.program if context else None,
        program_revision=context.program_revision if context else None,
        policy=CardPolicyProjection(**context.policy.to_dict()) if context else None,
        active_budget=budget, scope=context.scope if context else None,
        preconditions=_preconditions(context) if context and reason is None else None,
        decision=plan_cards(context) if context else CardDecision(kind='wait', reason=reason),
        capabilities=context.capabilities if context else unavailable,
        recent_operations=operations, read_only_reason=reason)


def _preconditions(context: CardPlanContext) -> CardPreconditions:
    return CardPreconditions(expected_account_id=context.scope.account_id,
        expected_generation=context.scope.generation, expected_epoch=context.scope.epoch,
        expected_program_revision=context.program_revision)


def _replay(store: CardStore, command: CardCommand) -> CardOperation | None:
    # Read the account-wide key, including operations older than bounded UI history.
    # This must precede capability gates so a retry returns the original result.
    with db.reader(store.path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='card_operations'").fetchone():
            return None
        row = conn.execute('SELECT operation_id FROM card_operations WHERE account_id=? AND idempotency_key=?',
                           (command.scope.account_id, command.idempotency_key)).fetchone()
    operation = store.operation(row[0]) if row else None
    if operation is not None and command.goal_id is None and command.kind in {'buy', 'slot'}:
        # Omission delegates goal selection to the server. For an existing key,
        # that choice is already pinned; current route progress cannot replace it.
        command = command.model_copy(update={'goal_id': operation.command.goal_id})
    if operation is not None and operation.command != command:
        raise HTTPException(409, 'card_idempotency_conflict')
    return operation


def _paid_goal(context: CardPlanContext, command: CardCommand) -> str | None:
    if command.kind not in {'buy', 'slot'}:
        return None
    goals = context.program.goals if context.program is not None else ()
    compatible_kind = 'acquire' if command.kind == 'buy' else 'slots'
    if command.goal_id is not None:
        goal = next((goal for goal in goals if goal.id == command.goal_id), None)
        if goal is None or goal.kind != compatible_kind:
            raise HTTPException(422, 'card_goal_not_found_or_incompatible')
        # A valid explicit future goal may wait in the queue. Dispatch still
        # rechecks the live route allow-list before any spending.
        return goal.id
    if context.eligible_goal_ids is None:
        return None  # Standalone manual purchases can be untargeted.
    eligible = [goal.id for goal in goals if goal.kind == compatible_kind
                and goal.id in context.eligible_goal_ids]
    if len(eligible) != 1:
        raise HTTPException(409, 'route_waiting')
    return eligible[0]


def register_cards_routes(app: FastAPI, *, selected: Callable[[Request], AccountChoice | None],
                          history_path: Callable[[Request], Path | None],
                          running_account: Callable[[AccountChoice], bool],
                          runner: BotRunner | None, accounts: AccountState,
                          db_path: Path | None,
                          patch_automation: Callable[[bool], dict] | None = None) -> None:
    """Register async endpoints using existing selection and runner authority."""
    def selection(request: Request) -> tuple[Path | None, str | None, CardPlanContext | None, str | None]:
        choice = selected(request)
        path = history_path(request)
        bound = db.bound_account(path) if path else None
        account_id = choice.account_id if choice else bound
        local = path is not None and db_path is not None and path.resolve() == db_path.resolve()
        if not local or runner is None or (choice is not None and choice.kind != 'worker'):
            return path, account_id, None, 'selected_account_not_running'
        scope = accounts.verified_scope
        if scope is None or scope.account_id != account_id or bound != account_id:
            return path, account_id, None, 'cards_identity_unverified'
        # The runtime's account safety DB must be the selected history DB, too.
        safety = getattr(accounts, 'currencies', None)
        if safety is None or Path(safety.path).resolve() != path.resolve():
            return path, account_id, None, 'cards_identity_unverified'
        live = running_account(choice) if choice is not None else runner.status().get('running', False)
        if not live:
            return path, account_id, None, 'selected_account_not_running'
        context = runner.cards_context()
        if context is None:
            return path, account_id, None, 'cards_runtime_unavailable'
        if context.scope != scope:
            return path, account_id, None, 'cards_identity_unverified'
        return path, account_id, context, None

    def mutation(request: Request, body: CardPreconditions) -> tuple[Path, CardPlanContext]:
        path, account_id, context, reason = selection(request)
        if account_id != body.expected_account_id:
            raise HTTPException(409, 'selected_account_changed')
        if reason is not None or context is None or path is None:
            raise HTTPException(503, reason or 'cards_runtime_unavailable')
        expected = _preconditions(context)
        if any(getattr(body, key) != value for key, value in expected.model_dump().items()):
            raise HTTPException(409, 'cards_preconditions_changed')
        return path, context

    @app.post('/api/cards/automation')
    async def automation(request: Request, body: CardAutomationRequest) -> dict:
        def apply() -> dict:
            _, context = mutation(request, body)
            if patch_automation is None:
                raise HTTPException(503, 'cards_controls_unavailable')
            try:
                result = runner.update_cards_automation(scope=context.scope,
                    program_revision=context.program_revision,
                    apply=lambda: patch_automation(body.enabled))
            except RunnerError as exc:
                raise HTTPException(exc.status_code, str(exc)) from None
            if result is None:
                raise HTTPException(503, 'cards_controls_unavailable')
            return result
        return await asyncio.to_thread(apply)

    @app.get('/api/cards', response_model=CardsProjection)
    async def cards(request: Request) -> CardsProjection:
        def read() -> CardsProjection:
            path, account_id, context, reason = selection(request)
            return read_cards_projection(path=path, account_id=account_id, context=context, read_only_reason=reason)
        return await asyncio.to_thread(read)

    @app.post('/api/cards/preview', response_model=CardPreview)
    async def preview(request: Request, body: CardPreviewRequest) -> CardPreview:
        def read() -> CardPreview:
            _, _, context, _ = selection(request)
            return preview_program(body.program, context)
        return await asyncio.to_thread(read)

    @app.get('/api/cards/catalog', response_model=CardCatalogResponse)
    async def catalog() -> CardCatalogResponse:
        return CardCatalogResponse(cards=tuple(CardCatalogItem(card_id=card_id,
            name=REGISTRY.by_id(card_id).name, max_level=card_catalog.max_level(card_id))
            for card_id in sorted(card_catalog.card_ids())), max_gem_slots=card_catalog.max_card_slots(),
            level_source_url=card_catalog.LEVEL_SOURCE_URL, slot_source_url=card_catalog.SLOT_SOURCE_URL)

    @app.get('/api/cards/operations/{operation_id}', response_model=CardOperation)
    async def operation(operation_id: str, request: Request) -> CardOperation:
        def read() -> CardOperation:
            choice = selected(request)
            path = history_path(request)
            account_id = choice.account_id if choice else db.bound_account(path) if path else None
            found = CardStore(path).operation(operation_id) if path and account_id and db.bound_account(path) == account_id else None
            if found is None or found.command.scope.account_id != account_id:
                raise HTTPException(404, 'card_operation_not_found')
            return found
        return await asyncio.to_thread(read)

    @app.post('/api/cards/commands', status_code=202, response_model=CardOperation)
    async def command(request: Request, body: CardCommandRequest) -> CardOperation:
        def submit() -> CardOperation:
            path, context = mutation(request, body)
            command = CardCommand(idempotency_key=body.idempotency_key, scope=context.scope,
                program_revision=context.program_revision, kind=body.kind, quantity=body.quantity,
                loadout_id=body.loadout_id, target_operation_id=body.target_operation_id,
                source='manual', goal_id=body.goal_id, budget_cycle_id=body.budget_cycle_id)
            previous = _replay(CardStore(path), command)
            if previous is not None:
                return previous
            command = command.model_copy(update={'goal_id': _paid_goal(context, command)})
            capability = {'refresh': 'inventory', 'buy': 'buy_ten' if body.quantity == 10 else 'buy_one',
                          'slot': 'buy_slot', 'apply': 'assign', 'clear': 'assign'}.get(body.kind)
            if capability is not None and not getattr(context.capabilities, capability):
                raise HTTPException(503, context.capabilities.reasons.get(capability) or f'cards_{capability}_unsupported')
            if body.kind == 'apply' and (context.program is None or body.loadout_id not in {
                    loadout.id for loadout in context.program.loadouts}):
                raise HTTPException(422, 'card_loadout_not_found')
            try:
                result = runner.request_cards(command)
            except RunnerError as exc:
                raise HTTPException(exc.status_code, str(exc)) from None
            logger.info('Cards API command accepted', extra={'account_id': context.scope.account_id,
                        'operation_id': result.operation_id, 'card_kind': body.kind})
            return result
        return await asyncio.to_thread(submit)

    @app.post('/api/cards/budget-cycles', response_model=CardBudgetCycleResponse)
    async def budget_cycle(request: Request, body: CardBudgetCycleRequest) -> CardBudgetCycleResponse:
        def start() -> CardBudgetCycleResponse:
            _, context = mutation(request, body)
            try:
                result = runner.start_cards_cycle(scope=context.scope, program_revision=context.program_revision,
                                                  cycle_id=body.cycle_id, cap=body.cap)
            except RunnerError as exc:
                raise HTTPException(exc.status_code, str(exc)) from None
            return CardBudgetCycleResponse(**result)
        return await asyncio.to_thread(start)


class CardAutomationRequest(CardPreconditions):
    enabled: bool


class FleetCardTarget(_Envelope):
    account_id: Id
    worker: str | None = Field(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


class FleetCardAssignmentRequest(_Envelope):
    expected_revision: int = Field(ge=0)
    strategy_id: Id
    strategy_version: int = Field(ge=1)
    targets: list[FleetCardTarget] = Field(min_length=1, max_length=100)


class FleetCardLoadoutRequest(_Envelope):
    expected_revision: int = Field(ge=0)
    account_id: Id
    loadout_id: Id | None


class FleetCardRefreshTarget(CardPreconditions):
    worker: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
    idempotency_key: Id
    kind: Literal['refresh'] = 'refresh'


class FleetCardRefreshRequest(_Envelope):
    targets: list[FleetCardRefreshTarget] = Field(min_length=1, max_length=100)


class FleetCardAutomationTarget(CardAutomationRequest):
    worker: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


class FleetCardAutomationRequest(_Envelope):
    targets: list[FleetCardAutomationTarget] = Field(min_length=1, max_length=100)


class FleetCardCommandRequest(CardCommandRequest):
    worker: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


class FleetCardCycleRequest(CardBudgetCycleRequest):
    worker: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


class FleetCardPreviewRequest(CardPreconditions):
    worker: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
    program: CardProgram


class FleetCardOperationRequest(CardPreconditions):
    worker: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
    operation_id: Id


def register_fleet_cards_routes(app: FastAPI, *, service: object | None) -> None:
    from fleet.cards import (assign_card_program, fleet_cards_snapshot,
                             proxy_card_targets, proxy_card_account, select_card_loadout)
    from fleet.build_route_store import RouteConflict, RouteUnavailable

    async def call(function: Callable, **kwargs: object) -> dict[str, object]:
        if service is None or not callable(getattr(service, 'build_route_store', None)):
            raise HTTPException(503, 'fleet_not_configured')
        try:
            return await asyncio.to_thread(function, service, **kwargs)
        except RouteConflict as exc:
            raise HTTPException(409, {'message': 'route_revision_changed', 'current_revision': exc.current.revision}) from None
        except RouteUnavailable as exc:
            raise HTTPException(503, exc.reason) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get('/api/fleet/cards')
    async def fleet_cards() -> dict[str, object]:
        return await call(fleet_cards_snapshot)

    @app.post('/api/fleet/cards/assignments')
    async def assignments(body: FleetCardAssignmentRequest) -> dict[str, object]:
        return await call(assign_card_program, **body.model_dump(exclude_none=True))

    @app.post('/api/fleet/cards/loadouts')
    async def loadout(body: FleetCardLoadoutRequest) -> dict[str, object]:
        return await call(select_card_loadout, **body.model_dump())

    @app.post('/api/fleet/cards/commands', status_code=202)
    async def refresh(body: FleetCardRefreshRequest) -> dict[str, object]:
        return await call(proxy_card_targets, **body.model_dump())

    @app.post('/api/fleet/cards/automation')
    async def automation(body: FleetCardAutomationRequest) -> dict[str, object]:
        return await call(proxy_card_targets, automation=True, **body.model_dump())

    @app.post('/api/fleet/cards/account-command')
    async def account_command(body: FleetCardCommandRequest) -> dict[str, object]:
        return await call(proxy_card_account, action='command', target=body.model_dump(mode='json'))

    @app.post('/api/fleet/cards/account-cycle')
    async def account_cycle(body: FleetCardCycleRequest) -> dict[str, object]:
        return await call(proxy_card_account, action='cycle', target=body.model_dump(mode='json'))

    @app.post('/api/fleet/cards/account-preview')
    async def account_preview(body: FleetCardPreviewRequest) -> dict[str, object]:
        return await call(proxy_card_account, action='preview', target=body.model_dump(mode='json'))

    @app.post('/api/fleet/cards/account-operation')
    async def account_operation(body: FleetCardOperationRequest) -> dict[str, object]:
        return await call(proxy_card_account, action='operation', target=body.model_dump(mode='json'))
