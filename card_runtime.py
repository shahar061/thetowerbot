"""One account-bound Cards scheduler and live policy boundary.

Synchronous like the scan loop and SQLite journal. HTTP callers offload these
methods. Capture provenance and the existing guarded device remain authoritative.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import threading
import time
from typing import Any
from uuid import uuid4

import db

from card_models import CardBudget, CardCommand, CardOperation, CardPlanContext
from card_plan import current_snapshot, plan_cards
from card_screen import capabilities
from card_store import CardStore, TERMINAL
from card_visit import (CardCapture, CardVisit, CaptureCardAdapter, latest_card_mutation,
                        pending_home_operations, visit_purchases)
from evidence_scope import FactScope


def _generated_key(scope: FactScope, *identity: Any) -> str:
    """Scope changes create new commands, never a new spending opportunity."""
    payload = json.dumps((asdict(scope), *identity), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


class CardRuntime:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.account = bot.account_state
        self.store = CardStore(self.account.safety_path)
        self._lock = threading.RLock()
        self._visit_id = self.store.current_visit() or 'cards:' + uuid4().hex
        self._view: Any | None = None
        self.reason: str | None = None
        self.visit = CardVisit(self.store, bot.shopping.journal,
            adapter=CaptureCardAdapter(bot.templates), observe=self.account.observe_cards,
            observe_balance=self.account.observe_balance,
            current_context=self.context, route_remaining=self.route_remaining)
        self._close_home_receipts()

    def _close_home_receipts(self) -> None:
        """Cards owns linked receipts until home, including crash repair after home."""
        receipts = self.bot.shopping.journal.recovered_visit(operations={'card_buy', 'card_slot_buy'})
        keys: set[str] = set()
        if receipts:
            with db.reader(self.store.path) as conn:
                for transaction, _ in receipts:
                    operation_id = transaction.before.get('card_operation_id')
                    if operation_id is None:
                        continue
                    row = conn.execute('SELECT home_observed FROM card_visit_flows WHERE operation_id=?',
                                       (operation_id,)).fetchone()
                    if row and row[0]:
                        keys.add(transaction.key)
        if keys:
            self.bot.shopping.journal.finish_recovered_visit(keys)

    @property
    def active(self) -> bool:
        return self.visit.active

    def effective_route(self) -> Any | None:
        progress = self.bot.reroll_progress
        runtime = getattr(progress, 'route_runtime', None)
        scope = self.account.verified_scope
        if runtime is None or scope is None:
            return None
        from fleet.build_route import resolve_route
        return resolve_route(runtime.current(), progress.root.name, scope.account_id)

    def context(self, *, route_gates: bool = True) -> CardPlanContext | None:
        """Authoritative current scope/policy/program, shared wallet and original cycle."""
        scope = self.account.verified_scope
        if scope is None or db.bound_account(self.store.path) != scope.account_id:
            return None
        live = self.bot.controls.snapshot()
        now = time.time()
        route_error = False
        try:
            route = self.effective_route()
        except (ValueError, KeyError, RuntimeError):
            route, route_error = None, True
        program = route.cards if route is not None and route.cards is not None else live.strategy.cards
        revision = hashlib.sha256((program.model_dump_json() if program else 'null').encode()).hexdigest()
        policy = replace(live.strategy.shopping.cards,
                         enabled=live.strategy.shopping.enabled and live.strategy.shopping.cards.enabled)
        policy_visit_id = self.visit.visit_id if self.active and self.visit.visit_id else self._visit_id
        for original in self.store.queue_policies(scope, policy_visit_id):
            policy = replace(policy, enabled=policy.enabled and original.enabled,
                gem_floor=max(policy.gem_floor, original.gem_floor),
                max_per_visit=min(policy.max_per_visit, original.max_per_visit), batch=original.batch)
        if route is not None:
            policy = replace(policy, gem_floor=max(policy.gem_floor, route.rules.gems.keep))
        budget = self.store.active_budget() or CardBudget(cycle_id='inactive', cap=0, spent=0, pending=0)
        unresolved = next((op for op in self.store.unresolved()
            if (not self.active or op.operation_id != self.visit.operation_id) and self._progressed(op)), None)
        visit_id = self.visit.visit_id if self.active and self.visit.visit_id else self._visit_id
        snapshot = self.store.snapshot()
        observed_capabilities = (self._view.capabilities if self._view and (self._view.capabilities.inventory
                                 or self._view.page not in ('home', 'intro')) else
                                 capabilities(snapshot.layout_id if snapshot else None))
        # Inventory discovery is implemented, even before this account has visited Cards.
        # This advertises navigation/reading only; mutation gates remain reader-owned.
        if self._view is None and snapshot is None:
            observed_capabilities = observed_capabilities.model_copy(update={'inventory': True})
        context = CardPlanContext(scope=scope, visit_id=visit_id, now=now,
            program_revision=revision, program=program, policy=policy, snapshot=snapshot,
            config_owner="unavailable" if route_error else "fleet" if route is not None and route.cards is not None else "local",
            budget=budget, balance=self.account.currencies.balance('gems', scope=scope, now=now),
            committed_gems=self.account.currencies.committed('gems'), eligible_goal_ids=() if route_error else None,
            capabilities=observed_capabilities,
            quotes=self._view.quotes if self._view else (), armed=live.strategy.shopping.armed,
            paused=live.paused, in_run=getattr(self.bot, '_card_in_run', self.bot.screen_state.value == 'IN_RUN'),
            unresolved_operation=unresolved, cards_bought_this_visit=visit_purchases(self.store, visit_id),
            evidence_after=latest_card_mutation(self.store))
        if route is not None and route_gates:
            from fleet.resource_blocks import LabFacts, _gem_plan, gem_lane_blocks
            cadence = getattr(self.bot.reroll_progress, 'lab_cadence', None)
            facts = LabFacts(now=now, wallet_gems=context.balance.lower if context.balance else None,
                slot_ownership=cadence.slot_records() if cadence else {}, card_context=context)
            lane = _gem_plan(gem_lane_blocks(route.gems), facts, route.rules)
            context = context.model_copy(update={'eligible_goal_ids': lane.eligible_card_goal_ids})
        return context

    def route_remaining(self) -> int | None:
        context = self.context(route_gates=False)
        if context is None:
            return 0
        try:
            route = self.effective_route()
        except (ValueError, KeyError, RuntimeError):
            return 0
        if route is None:
            return None
        if context.balance is None or context.balance.lower is None:
            return 0
        # The first journal wallet anchors the shared visit allowance. Each later
        # operation consumes it; a restart or a new x1 cannot reset the percentage.
        with db.reader(self.store.path) as conn:
            rows = conn.execute('SELECT t.wallet_before,t.price,t.spent,t.stage FROM transactions t '
                'JOIN card_operations o ON o.transaction_key=t.key '
                'JOIN card_visit_flows f USING(operation_id) WHERE f.visit_id=? ORDER BY t.ts,t.key',
                (context.visit_id,)).fetchall()
        consumed = sum(row['spent'] if row['spent'] is not None else row['price'] or 0 for row in rows)
        pending = sum(row['price'] or 0 for row in rows if row['spent'] is None)
        others = max(0, context.committed_gems - pending)
        anchor = rows[0]['wallet_before'] if rows else context.balance.lower
        if anchor is None:
            return 0
        allowance = max(0, anchor - others - context.policy.gem_floor) * route.rules.gems.spend_limit_pct // 100
        current = max(0, context.balance.lower - context.committed_gems - context.policy.gem_floor)
        return min(current, max(0, allowance - consumed))

    @staticmethod
    def _progressed(op: CardOperation) -> bool:
        return bool(op.transaction_key or op.status in ('dispatched', 'verifying', 'reconciliation_required'))

    def validate(self, scope: FactScope, program_revision: str) -> CardPlanContext:
        context = self.context()
        if context is None or scope != context.scope or program_revision != context.program_revision:
            raise ValueError('Cards account, generation, epoch or program revision changed')
        return context

    def request(self, command: CardCommand) -> CardOperation:
        with self._lock:
            self.validate(command.scope, command.program_revision)
            self._visit_id = self.store.observe_visit(command.scope, self._visit_id)
            if command.kind == 'cancel':
                target = self.store.operation(command.target_operation_id)
                if target is None or target.command.scope.account_id != command.scope.account_id:
                    raise ValueError('Cards cancel target does not exist in this account')
            operation = self.store.submit(command, now=time.time())
            if command.kind == 'cancel' and operation.status in ('queued', 'preflight'):
                operation = self._finish_cancel(operation, now=time.time())
            return operation

    def _finish_cancel(self, operation: CardOperation, *, now: float) -> CardOperation:
        """Resume accepted cancellation before admitting any of its target's work."""
        target = self.store.operation(operation.command.target_operation_id)
        if target.status not in TERMINAL:
            if self.visit.operation_id == target.operation_id and self.active:
                self.visit.cancel('cancel_requested')
            elif self._progressed(target):
                # A linked receipt or claimed input retains recovery ownership.
                if target.status != 'reconciliation_required':
                    self.store.transition(target.operation_id, expected_status=target.status,
                        status='reconciliation_required', reason='cancel_requested', now=now)
            else:
                self.store.transition(target.operation_id, expected_status=target.status,
                    status='canceled', reason='cancel_requested', now=now)
        if operation.status == 'queued':
            operation = self.store.transition(operation.operation_id, expected_status='queued',
                status='preflight', reason=None, now=now)
        return self.store.transition(operation.operation_id, expected_status='preflight',
            status='confirmed', reason='cancel_requested', now=now)

    def start_cycle(self, *, scope: FactScope, program_revision: str, cycle_id: str, cap: int) -> dict[str, Any]:
        with self._lock:
            self.validate(scope, program_revision)
            budget = self.store.start_budget_cycle(scope=scope, cycle_id=cycle_id, cap=cap, now=time.time())
            current = self.store.active_budget()
            return {'budget': budget, 'active': current is not None and current.cycle_id == cycle_id,
                    'active_budget': current}

    def queue_refresh(self, now: float) -> bool:
        context = self.context()
        if context is None:
            return False
        self.request(CardCommand(idempotency_key=_generated_key(context.scope, 'intro', self._visit_id, context.program_revision),
            scope=context.scope, program_revision=context.program_revision,
            kind='refresh', quantity=1, source='manual'))
        return True

    def queue_automatic(self, key: str, policy: Any) -> bool:
        # Legacy shopping is an automatic opportunity. Never manufacture manual authority.
        context = self.context()
        if context is None or not policy.enabled:
            return False
        combined = replace(context.policy, enabled=context.policy.enabled and policy.enabled,
            gem_floor=max(context.policy.gem_floor, policy.gem_floor),
            max_per_visit=min(context.policy.max_per_visit, policy.max_per_visit))
        return self._queue_decision(context.model_copy(update={'policy': combined}), key, handoff_policy=policy) is not None

    def _queue_decision(self, context: CardPlanContext, key: str | None = None,
                        *, handoff_policy: Any | None = None) -> CardOperation | None:
        decision = plan_cards(context)
        self.reason = decision.reason
        if decision.kind not in ('refresh', 'buy', 'slot', 'apply'):
            return None
        if key is None:
            key = _generated_key(context.scope, 'automatic', context.visit_id,
                context.program_revision, context.budget.cycle_id, decision.kind,
                decision.goal_id, decision.loadout_id, self.store.visit_progress(context.visit_id))
        else:
            key = _generated_key(context.scope, 'shopping', key, context.program_revision)
        command = CardCommand(idempotency_key=key, scope=context.scope,
            program_revision=context.program_revision, kind=decision.kind, quantity=1,
            source='automatic', goal_id=decision.goal_id, loadout_id=decision.loadout_id,
            budget_cycle_id=context.budget.cycle_id if decision.kind in ('buy', 'slot') else '')
        if handoff_policy is not None:
            self.store.record_queue_policy(command, handoff_policy)
        return self.request(command)

    def _cancel_stale(self, context: CardPlanContext) -> None:
        for op in self.store.unresolved():
            if self._progressed(op):
                continue
            stale = op.command.scope != context.scope or (op.command.source == 'automatic' and
                (op.command.program_revision != context.program_revision or not context.policy.enabled))
            if stale or op.cancel_requested:
                reason = 'cancel_requested' if op.cancel_requested else 'scope_or_program_changed'
                if self.active and self.visit.operation_id == op.operation_id:
                    self.visit.cancel(reason)
                else:
                    self.store.transition(op.operation_id, expected_status=op.status, status='canceled',
                        reason=reason, now=context.now)

    def _admissible_pending(self, context: CardPlanContext) -> tuple[CardOperation, ...]:
        """Keep future manual route goals queued without taking menu ownership."""
        return tuple(op for op in self.store.pending(context.scope) if not (
            op.status == 'queued' and op.command.source == 'manual'
            and op.command.kind in {'buy', 'slot'} and op.command.goal_id is not None
            and op.command.program_revision == context.program_revision
            and context.eligible_goal_ids is not None
            and op.command.goal_id not in context.eligible_goal_ids))

    def advance(self, boxes: tuple[Any, ...], *, opportunity: bool = False) -> bool:
        """True means Cards owns this scan, including unresolved recovery holds."""
        with self._lock:
            context = self.context()
            if context is None:
                if self.active:
                    self.visit.cancel('scope_unverified')
                return self.active
            capture_scope = getattr(self.bot, '_screen_fact_scope', None)
            captured = getattr(self.bot, '_screen_captured_at', None)
            observed_state = getattr(self.bot, '_card_observed_state', self.bot.screen_state.value)
            if (observed_state in ('IN_RUN', 'MAIN_MENU', 'GAME_OVER')
                    and capture_scope == context.scope and captured is not None
                    and self.account.accepts_capture(capture_scope, captured)
                    and 0 <= context.now - captured <= 10):
                current = self.store.observe_visit(context.scope, self._visit_id,
                    in_run=observed_state == 'IN_RUN', captured_at=captured)
                if current != self._visit_id:
                    self._visit_id, self._view = current, None
                    context = self.context()
            for pending in self.store.pending(context.scope):
                if pending.command.kind == 'cancel' and pending.status in ('queued', 'preflight'):
                    self._finish_cancel(pending, now=context.now)
            self._cancel_stale(context)
            homes = pending_home_operations(self.store)
            progressed = tuple(op for op in self.store.unresolved() if self._progressed(op))
            if not self.active and (homes or progressed):
                if not self.visit.request(homes[0] if homes else progressed[0].operation_id):
                    self.reason = 'reconciliation_required'
                    return True
            if context.paused or context.in_run:
                return self.active
            if not self.active:
                if not context.armed:
                    self.reason = 'not_armed'
                    return False
                if not opportunity:
                    return False
                pending = self._admissible_pending(context)
                op = next((op for op in pending if op.command.source == 'manual'), None)
                op = op or next(iter(pending), None)
                if op is None:
                    op = self._queue_decision(context)
                if op is None or op.status in TERMINAL:
                    return False
                if not self.visit.request(op.operation_id, visit_id=self._visit_id):
                    self.reason = 'reconciliation_required'
                    return True
            context = self.context()
            capture_scope = getattr(self.bot, '_screen_fact_scope', None)
            captured = getattr(self.bot, '_screen_captured_at', None)
            if capture_scope is None or captured is None:
                self.reason = 'capture_provenance_unavailable'
                return True
            capture = CardCapture(capture_scope, captured)
            op = self.store.operation(self.visit.operation_id)
            self.visit.tuning = self.bot.controls.snapshot().strategy
            self._view = self.visit.adapter.read(self.bot.screen, boxes, capture=capture,
                visit_id=self.visit.visit_id, operation=op)
            continuity = self.account.continuity(op.command.scope, now=context.now)
            self.visit.advance(screen=self.bot.screen, boxes=boxes, device=self.bot.device,
                context=context, capture=capture, continuity=continuity)
            self.reason = self.visit.reason
            if self.visit.home_observed:
                self._close_home_receipts()
            return True

    def needs_home(self) -> bool:
        """A queued command must get a menu opportunity instead of an endless RETRY loop."""
        context = self.context()
        if context is None or context.paused or not context.armed:
            return False
        if self._admissible_pending(context):
            return True
        return plan_cards(context.model_copy(update={'in_run': False})).kind in ('refresh', 'buy', 'slot', 'apply')

    def status(self) -> dict[str, Any]:
        context = self.context()
        return {'active': self.active, 'operation_id': self.visit.operation_id,
                'visit_id': context.visit_id if context else self._visit_id, 'reason': self.reason,
                'home_observed': self.visit.home_observed,
                'program_revision': context.program_revision if context else None,
                'fresh': context is not None and current_snapshot(context) is not None,
                'observed_at': context.snapshot.observed_at if context and context.snapshot else None,
                'evidence_after': context.evidence_after if context else None,
                'active_budget': (budget.model_dump(mode='json') if (budget := self.store.active_budget()) else None)}
