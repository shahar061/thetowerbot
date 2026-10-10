"""Shared versioned plans and explicit account-bound assignments."""
from __future__ import annotations

from dataclasses import replace
from contextlib import ExitStack
import json
from pathlib import Path
from typing import Any, Callable

from fleet.build_route import RouteDocument, StrategyAssignment
from fleet.build_route_store import BuildRouteStore
from fleet.strategy_library import StrategyLibrary
from strategy_target import StrategyTargetContext, StrategyTargetError


def ack_matches(ack: dict[str, Any], assignment: StrategyAssignment,
                revision: int, target: StrategyTargetContext) -> bool:
    return all(ack.get(key) == value for key, value in {
        'account_id': target.account_id, 'target_id': target.target_id,
        'revision': revision, 'strategy_id': assignment.strategy_id,
        'strategy_version': assignment.strategy_version,
        'generation': target.scope.generation, 'epoch': target.scope.epoch,
    }.items())


class StrategyService:
    def __init__(self, *, library_root: Path, assignment_root: Path,
                 resolve_target: Callable[[str], StrategyTargetContext]) -> None:
        self.library_store = StrategyLibrary(library_root)
        self.assignment_store = BuildRouteStore(assignment_root)
        self.resolve_target = resolve_target

    def library(self) -> dict[str, Any]:
        return self.library_store.read()

    def save_version(self, **kwargs: Any) -> dict[str, Any]:
        return self.library_store.save(**kwargs)

    def assign(self, *, expected_revision: int, strategy_id: str,
               strategy_version: int, targets: list[StrategyTargetContext],
               actor: str = 'operator') -> dict[str, Any]:
        if not targets or len({t.target_id for t in targets}) != len(targets):
            raise ValueError('assignment requires distinct targets')
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('invalid expected_revision')
        saved = self.library_store.version(strategy_id, strategy_version)
        with self.assignment_store.locked(), ExitStack() as guards:
            for target in sorted(targets, key=lambda t: t.target_id):
                guards.enter_context(target.guard_scope())
            current = self.assignment_store.read()
            assignments, overrides = dict(current.assignments), dict(current.overrides)
            for target in targets:
                target.verify_current()
                assignments[target.target_id] = StrategyAssignment.from_dict({
                    'account_id': target.account_id, 'strategy_id': saved['id'],
                    'strategy_version': saved['version'], 'strategy_name': saved['name'],
                    'baseline': saved['baseline'], 'kind': saved.get('kind', 'native'),
                    'legacy_snapshot': saved.get('legacy_snapshot'),
                })
                overrides.pop(target.target_id, None)
            for target in targets:
                target.verify_current()
            published = self.assignment_store.publish_locked(
                replace(current, assignments=assignments, overrides=overrides),
                expected_revision, actor)
        return {'route': published.to_dict(), 'status': 'pending'}

    def status(self, target: StrategyTargetContext) -> dict[str, Any]:
        route = self.assignment_store.read()
        assigned = route.assignments.get(target.target_id)
        status: dict[str, Any] = {'target_id': target.target_id,
            'account_id': target.account_id, 'generation': target.scope.generation,
            'lease_id': target.scope.lease_id,
            'epoch': target.scope.epoch, 'revision': route.revision, 'state': 'legacy',
            'strategy_id': None, 'strategy_version': None, 'reason': None}
        if assigned is None:
            if (target.state_dir / "build-route-applied.json").exists():
                status.update(state="blocked", reason="strategy_assignment_missing")
            return status
        status.update(assigned.to_dict())
        if assigned.account_id != target.account_id:
            status.update(state='blocked', reason='strategy_account_changed')
            return status
        try:
            target.verify_current()
        except StrategyTargetError as exc:
            status.update(state='blocked', reason=str(exc))
            return status
        try:
            ack = json.loads((target.state_dir / 'build-route-applied.json').read_text())
        except (OSError, ValueError):
            ack = {}
        status['acknowledged'] = ack
        status['lane_sources'] = {lane: {'source': assigned.kind,
            'strategy_id': assigned.strategy_id, 'strategy_version': assigned.strategy_version}
            for lane in ('battle', 'workshop', 'gems', 'labs', 'cards', 'tournament')}
        if target.account_id in route.card_assignments:
            overlay = route.card_assignments[target.account_id]
            status['lane_sources']['cards'] = {'source': 'account_overlay',
                'overlay_id': overlay.overlay_id, 'revision': overlay.published_revision}
        status['state'] = 'applied' if ack_matches(ack, assigned, route.revision, target) else 'pending'
        return status

    def ledger(self, limit: int = 200) -> dict[str, Any]:
        from fleet.strategy_ledger import read_ledger
        return {'entries': read_ledger(self.library_store.root, limit)}
