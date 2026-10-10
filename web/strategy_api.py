"""Scope-explicit Strategy Studio API for one emulator."""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from fleet.build_route_store import RouteConflict, RouteUnavailable
from fleet.strategy_library import LibraryConflict
from strategy import StrategyStore
from strategy_service import StrategyService
from strategy_target import StrategyTargetContext, StrategyTargetError


class SaveVersion(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int
    name: str
    source_template: str
    baseline: dict[str, Any]
    strategy_id: str | None = None


class AssignmentInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int
    strategy_id: str
    strategy_version: int
    target_id: str
    account_id: str
    generation: str
    epoch: int


def register_strategy_routes(app: FastAPI, *, service: StrategyService,
        local_target: Callable[[], StrategyTargetContext | None],
        store: StrategyStore | None = None, runner: Any = None,
        remote: Callable[[Request, str, dict[str, Any] | None], dict[str, Any] | None] | None = None,
        settings_read: Callable[[], dict[str, Any]] | None = None,
        settings_save: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
    def guarded(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            with runner._lock if runner is not None else nullcontext():
                return action()
        except RouteConflict as exc:
            raise HTTPException(409, detail={'message': 'route_revision_changed',
                'current_revision': exc.current.revision}) from None
        except LibraryConflict as exc:
            raise HTTPException(409, detail={'message': 'library_revision_changed',
                'current_revision': exc.revision}) from None
        except StrategyTargetError as exc:
            raise HTTPException(409, str(exc)) from None
        except RouteUnavailable as exc:
            raise HTTPException(503, exc.reason) from None
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from None

    def target() -> StrategyTargetContext:
        result = local_target()
        if result is None:
            raise HTTPException(409, 'strategy_account_not_verified')
        result.verify_current()
        return result

    def forward(request: Request, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        return remote(request, path, body) if remote else None

    @app.get('/api/strategy-studio/library')
    async def library() -> dict[str, Any]:
        return await run_in_threadpool(guarded, service.library)

    @app.post('/api/strategy-studio/library')
    async def save(body: SaveVersion) -> dict[str, Any]:
        return await run_in_threadpool(guarded, lambda: service.save_version(**body.model_dump()))

    @app.get('/api/strategy-studio/labs')
    async def labs(request: Request) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            forwarded = forward(request, '/api/strategy-studio/labs')
            if forwarded is not None:
                return forwarded
            import time
            import lab_catalog
            from dataclasses import replace
            from fleet.resource_blocks import automated_list
            from fleet.lab_facts import persisted_lab_facts
            from fleet.labs_view import project_row, _history
            from fleet.build_route import resolve_route
            snapshot: dict[str, Any] = {'workers': [], 'automated': automated_list(),
                                      'reference': lab_catalog.reference()}
            context = local_target()
            if context is None:
                return snapshot
            context.verify_current()
            now = time.time()
            route = service.assignment_store.read()
            assigned = route.assignments.get(context.target_id)
            facts = replace(persisted_lab_facts(context.state_dir, context.account_id,
                now=now, coins=None, gems=None, db_path=context.db_path), worker=context.target_id)
            snapshot['workers'] = [project_row(context.target_id, context.account_id,
                assigned.strategy_name if assigned else 'Local profile',
                resolve_route(route, context.target_id, context.account_id), facts,
                None, None, None, _history(context.db_path))]
            context.verify_current()
            return snapshot
        return await run_in_threadpool(guarded, read)

    @app.get('/api/strategy-studio/route')
    async def route() -> dict[str, Any]:
        return await run_in_threadpool(guarded, lambda: service.assignment_store.read().to_dict())

    @app.get('/api/strategy-studio/status')
    async def status(request: Request) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            proxied = forward(request, '/api/strategy-studio/status')
            if proxied is not None:
                return proxied
            context = local_target()
            if context is None:
                return {'state': 'blocked', 'reason': 'strategy_account_not_verified',
                        'target_id': None, 'account_id': None}
            result = service.status(context)
            push = getattr(getattr(runner, "_bot", None), "push_runs", None)
            if push is not None:
                result["push_runs"] = {**push.snapshot(), "active": push.active}
            return result
        return await run_in_threadpool(guarded, read)

    @app.post('/api/strategy-studio/assign')
    async def assign(request: Request, body: AssignmentInput) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            proxied = forward(request, '/api/strategy-studio/assign', body.model_dump())
            if proxied is not None:
                return proxied
            context = target()
            if (body.target_id, body.account_id, body.generation, body.epoch) != (
                    context.target_id, context.account_id, context.scope.generation, context.scope.epoch):
                raise StrategyTargetError('strategy_target_scope_changed')
            return service.assign(expected_revision=body.expected_revision,
                strategy_id=body.strategy_id, strategy_version=body.strategy_version,
                targets=[context], actor='operator')
        return await run_in_threadpool(guarded, apply)

    @app.post('/api/strategy-studio/import')
    async def import_profile(request: Request) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            proxied = forward(request, '/api/strategy-studio/import', {})
            if proxied is not None:
                return proxied
            if store is None:
                raise HTTPException(503, 'local_profiles_unavailable')
            from strategy_compat import capture_legacy
            source = store.load(store.active_name())
            context = local_target()
            source_key = f'{context.target_id if context else store.directory.resolve()}:{source.name}'
            return service.library_store.import_legacy(capture_legacy(source, store.revision(source)),
                                                       source_key=source_key)
        return await run_in_threadpool(guarded, apply)

    @app.get('/api/strategy-studio/ledger')
    async def ledger() -> dict[str, Any]:
        return await run_in_threadpool(guarded, service.ledger)

    @app.post('/api/strategy-studio/preview')
    async def preview(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            proxied = forward(request, '/api/strategy-studio/preview', body)
            if proxied is not None:
                return proxied
            from strategy_execution import preview_target
            return preview_target(target(), body)
        return await run_in_threadpool(guarded, read)

    @app.get('/api/strategy-studio/settings')
    async def read_settings(request: Request) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            proxied = forward(request, '/api/strategy-studio/settings')
            if proxied is not None:
                return proxied
            if settings_read is None:
                raise HTTPException(503, 'bot_settings_unavailable')
            return settings_read()
        return await run_in_threadpool(guarded, read)

    @app.post('/api/strategy-studio/settings')
    async def save_settings(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            proxied = forward(request, '/api/strategy-studio/settings', body)
            if proxied is not None:
                return proxied
            if settings_save is None:
                raise HTTPException(503, 'bot_settings_unavailable')
            return settings_save(body)
        return await run_in_threadpool(guarded, apply)
