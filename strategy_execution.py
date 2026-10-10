"""Resolve one immutable spending authority per decision, independently of pool lifecycle."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable
from pathlib import Path

from account_state import AccountState
from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_eval import evaluate_workshop
from fleet.build_route_store import BuildRouteStore, RouteUnavailable
from fleet.build_route_runtime import BuildRouteRuntime
from fleet.reroll_progress import RerollProgress
from strategy import Strategy, Shopping
from policy import AutopilotPolicy
from strategy_compat import restore_legacy, captured_build
from strategy_target import StrategyTargetContext


@dataclass(frozen=True)
class PlanResolution:
    source: str
    strategy: Strategy
    revision: int | None
    reason: str | None = None


class StrategyExecution:
    def __init__(self, target: Callable[[], StrategyTargetContext | None],
                 account_state: AccountState, *, root: Path | None = None,
                 fleet_progress: Any = None) -> None:
        self.target = target
        self.account_state = account_state
        self.root = root
        self.fleet_progress = fleet_progress
        self.progress: RerollProgress | None = None
        self.runtime: BuildRouteRuntime | None = None
        self.source = 'legacy_local'
        self.revision: int | None = None
        self.reason: str | None = None
        self._pin: tuple[Any, ...] | None = None
        self._resolved: Strategy | None = None
        self.build_recipe: Any = None

    @property
    def owns_plan(self) -> bool:
        return self.source != 'legacy_local'

    def authority_current(self) -> bool:
        """Recheck the pinned assignment before a purchase uses cached policy."""
        if not self.owns_plan:
            return True
        if self.source == 'blocked' or self.runtime is None:
            return False
        try:
            self.runtime.current()
            return True
        except (OSError, ValueError, TypeError, KeyError, RouteUnavailable):
            return False

    def blocked(self, local: Strategy, reason: str) -> PlanResolution:
        self.progress = None
        self._pin = None
        self.source, self.reason = 'blocked', reason
        # Observe-only autopilot suppresses the legacy template-click branch too.
        disabled = replace(local, autopilot=replace(local.autopilot, enabled=True, observe_only=True, rules=()),
                           shopping=replace(local.shopping, enabled=False),
                           tournament=replace(local.tournament, enabled=False))
        self._resolved = disabled
        return PlanResolution('blocked', disabled, self.revision, reason)

    def resolve(self, local: Strategy, *, pending_verification: bool,
                run_id: int | None = None, wave: int | None = None) -> PlanResolution:
        try:
            context = self.target()
            if context is None:
                if self.owns_plan:
                    return self.blocked(local, "strategy_account_not_verified")
                if self.root is not None and BuildRouteStore(self.root).read().assignments:
                    return self.blocked(local, 'strategy_account_not_verified')
                self.source = 'legacy_local'
                return PlanResolution(self.source, local, None)
            context.verify_current()
            route = BuildRouteStore(context.assignment_root).read()
            assigned = route.assignments.get(context.target_id)
            if assigned is None and (self.owns_plan or (context.state_dir / "build-route-applied.json").exists()):
                return self.blocked(local, "strategy_assignment_missing")
            if assigned is None:
                self.source, self.progress = 'legacy_local', None
                return PlanResolution(self.source, local, None)
            if assigned.account_id != context.account_id:
                return self.blocked(local, 'strategy_account_changed')
            pin = (context.target_id, context.account_id, context.scope.generation,
                   context.scope.epoch, route.revision)
            if pending_verification and pin != self._pin:
                return self.blocked(local, 'pending_purchase_verification')
            if pin != self._pin:
                runtime = BuildRouteRuntime.for_target(context)
                runtime.expected_revision = route.revision
                if assigned.kind == 'native':
                    progress = self.fleet_progress or RerollProgress(context.state_dir,
                        context.account_id, self.account_state, target_context=context,
                        reroll_mode=False)
                    progress.target_context = context
                    progress.route_runtime = runtime
                    self.progress = progress
                else:
                    self.progress = None
                self.runtime, self._pin = runtime, pin
            self.revision = route.revision
            if assigned.kind == 'legacy':
                resolved = restore_legacy(local, assigned.legacy_snapshot or {})
                self.build_recipe = captured_build(assigned.legacy_snapshot or {})
                self.source = 'studio_legacy'
            else:
                self.source = 'studio_native'
                self.build_recipe = None
                effective = resolve_route(route, context.target_id, context.account_id)
                shopping = Shopping(enabled=True, armed=True)
                resolved = replace(local,
                    autopilot=AutopilotPolicy(enabled=True, preset='manual'),
                    shopping=replace(shopping, cards=replace(shopping.cards,
                                     enabled=effective.cards is not None)),
                    cards=effective.cards, tournament=effective.tournament, tier_promotion=assigned.baseline.tier_promotion, build=None, _build_recipe=None)
            if not pending_verification:
                context.verify_current()
                self.runtime.acknowledge(route.revision, context.account_id)
            self._resolved, self.reason = resolved, None
            return PlanResolution(self.source, resolved, route.revision)
        except (OSError, ValueError, TypeError, KeyError, RouteUnavailable) as exc:
            return self.blocked(local, str(exc))


def preview_target(context: StrategyTargetContext, body: dict[str, Any]) -> dict[str, Any]:
    context.verify_current()
    route = RouteDocument.from_dict(body['route'])
    saved = BuildRouteStore(context.assignment_root).read()
    if body.get('expected_revision') != saved.revision:
        from fleet.build_route_store import RouteConflict
        raise RouteConflict(saved)
    progress = RerollProgress(context.state_dir, context.account_id, AccountState(),
        read_only=True, target_context=context, reroll_mode=False)
    progress.route_runtime = BuildRouteRuntime.for_target(context)
    facts = progress.route_facts()
    from dataclasses import asdict
    evaluation = evaluate_workshop(resolve_route(route, context.target_id, context.account_id), facts)
    return {'saved_revision': saved.revision, 'proposed_revision': saved.revision + 1,
            'members': [{'worker': context.target_id, 'account_id': context.account_id,
                         'current': asdict(evaluate_workshop(resolve_route(saved, context.target_id, context.account_id), facts)),
                         'proposed': asdict(evaluation)}]}
