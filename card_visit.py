"""One bounded Cards owner. SQLite/OCR/device work is synchronous; runtime offloads it.

Adapters expose controls from THIS frame only. The compact profile supports
measured x1 and assignment targets; unsupported mutation geometry is forbidden.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum, auto
import hashlib
import json
import logging
import math
import threading
from typing import Any, Callable, Protocol

import db
import ocr
from account_collection import ControlTaps, MATCH_THRESHOLD, locate_control
from account_screens import ControlTarget
from card_models import CardCapabilities, CardOperation, CardPlanContext, CardQuote, CardSnapshot
from card_plan import current_quote, plan_cards
from card_store import CardStore, TERMINAL
from card_transactions import (CardBudgetPrecondition, CardResultEvidence, assignment_target,
    pin_card_assignment, reconcile_card_operation, repair_card_ledger)
from device import Image
from evidence_scope import BalanceInterval, FactScope, ScopeContinuity
from transactions import Intent, TransactionJournal

log = logging.getLogger(__name__)
VISIT_SECONDS = 120.
TRANSITION_SECONDS = 10.
MAX_PASSES = 20


def equipment_changes(current: tuple[str, ...], desired: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (tuple(card for card in current if card not in desired),
            tuple(card for card in desired if card not in current))


@dataclass(frozen=True)
class CardCapture:
    scope: FactScope
    captured_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.scope, FactScope) or not math.isfinite(self.captured_at) or self.captured_at < 0:
            raise ValueError('invalid capture provenance')


@dataclass(frozen=True)
class CardView:
    page: str  # home/cards/intro/rewards/slot_confirmation/unknown
    capabilities: CardCapabilities
    snapshot: CardSnapshot | None = None
    quotes: tuple[CardQuote, ...] = ()
    controls: dict[str, ControlTarget] = field(default_factory=dict)
    result: CardResultEvidence | None = None
    balance: BalanceInterval | None = None


class CardAdapter(Protocol):
    def read(self, screen: Image, boxes: tuple[ocr.TextBox, ...], *, capture: CardCapture,
             visit_id: str, operation: CardOperation) -> CardView: ...


class CaptureCardAdapter:
    """Recorded controls and receipts; unsupported action paths remain disabled."""
    def __init__(self, templates: Any | None = None) -> None:
        import vision
        import config
        self.templates = templates if templates is not None else vision.TemplateCache(config.TEMPLATE_DIR)

    def read(self, screen: Image, boxes: tuple[ocr.TextBox, ...], *, capture: CardCapture,
             visit_id: str, operation: CardOperation) -> CardView:
        import card_screen
        import config
        import pages
        from cards_intro import _dismiss_target
        from account_screens import ScreenReadings
        from account_collection import at_home
        digest = hashlib.sha256(screen.tobytes()).hexdigest()
        rewards = card_screen.read_rewards(screen, boxes)
        if rewards is not None:
            capability = card_screen.capabilities(card_screen.REWARD_LAYOUT)
            if (operation.command.kind != 'buy' or operation.command.quantity != 1
                    or operation.transaction_key is None):
                return CardView(page='unknown', capabilities=capability)
            wallet = card_screen.exact_wallet(boxes, reward=True)
            claim = card_screen.measured_box(boxes, r'CLAIM', (400, 1830, 280, 110))
            result = CardResultEvidence(operation_id=operation.operation_id, action_sequence=1,
                scope=capture.scope, visit_id=visit_id, observed_at=capture.captured_at,
                evidence_ref=digest, frame_digest=digest, layout_id=card_screen.REWARD_LAYOUT,
                acquisition_observed=True, reward_flow_complete=True, rewards=rewards)
            return CardView(page='rewards', capabilities=capability, result=result,
                controls={'continue': card_screen.box_control('continue', claim)},
                balance=BalanceInterval('gems', wallet, wallet, capture.scope,
                    capture.captured_at, digest) if wallet is not None else None)
        snapshot = card_screen.read_snapshot(screen, boxes, scope=capture.scope,
            visit_id=visit_id, now=capture.captured_at)
        capability = card_screen.capabilities(snapshot.layout_id if snapshot else None)
        page = pages.classify_page(screen, self.templates).page
        controls: dict[str, ControlTarget] = {}
        if page == 'MAIN_MENU':
            readings = ScreenReadings()
            readings.scan(screen)
            if not at_home(page, readings.current_evidence()):
                return CardView(page='unknown', capabilities=capability)
        popup = _dismiss_target(screen, self.templates, MATCH_THRESHOLD)
        if popup is not None and page in ('CARDS', 'MAIN_MENU'):
            return CardView(page='intro', capabilities=capability, controls={'dismiss': popup})
        name = 'enter' if page == 'MAIN_MENU' else 'home'
        if page in ('MAIN_MENU', 'CARDS'):
            template = config.NAV_TARGETS['CARDS' if name == 'enter' else 'BATTLE_TAB']
            controls[name] = locate_control(screen, self.templates.get(template), name, MATCH_THRESHOLD)
        quotes: tuple[CardQuote, ...] = ()
        balance = None
        if snapshot is not None:
            price, mutations = card_screen.compact_controls(screen, boxes, snapshot)
            controls.update(mutations)
            wallet = card_screen.exact_wallet(boxes)
            if wallet is not None:
                balance = BalanceInterval('gems', wallet, wallet, capture.scope, capture.captured_at, digest)
            if price is not None:
                quotes = (CardQuote(kind='buy', quantity=1, price=price, scope=capture.scope,
                    visit_id=visit_id, observed_at=capture.captured_at, evidence_ref=digest,
                    layout_id=snapshot.layout_id),)
            assignment = (snapshot.layout_id == card_screen.COMPACT_LAYOUT
                          and snapshot.equipment_complete and snapshot.equipped is not None
                          and all(card in mutations for card in snapshot.equipped))
            reasons = {key: value for key, value in capability.reasons.items()
                       if not (key == 'buy_one' and quotes or key == 'assign' and assignment)}
            capability = capability.model_copy(update={'buy_one': bool(quotes),
                'assign': assignment, 'reasons': reasons})
        return CardView(page='home' if page == 'MAIN_MENU' else 'cards' if snapshot else 'unknown',
                        capabilities=capability, snapshot=snapshot, controls=controls,
                        quotes=quotes, balance=balance)


def assignment_steps(store: CardStore, operation_id: str) -> tuple[dict[str, Any], ...]:
    """Read-only durable claims; no schema creation on archive reads."""
    if not store.path.exists():
        return ()
    with db.reader(store.path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='card_assignment_steps'").fetchone():
            return ()
        if store._operation(conn, operation_id) is None:
            return ()
        return tuple(json.loads(row[0]) for row in conn.execute(
            'SELECT detail FROM card_assignment_steps WHERE operation_id=? ORDER BY sequence', (operation_id,)))


def visit_purchases(store: CardStore, visit_id: str) -> int:
    """Journal-confirmed acquisitions count once across operations, retries and processes."""
    if not store.path.exists():
        return 0
    with db.reader(store.path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='card_visit_flows'").fetchone():
            return 0
        rows = conn.execute('SELECT operation_id FROM card_visit_flows WHERE visit_id=?', (visit_id,)).fetchall()
        operations = (store._operation(conn, row[0]) for row in rows)
        return sum(op.command.quantity for op in operations
                   if op is not None and op.command.kind == 'buy' and op.spent_gems is not None and op.spent_gems > 0)


def latest_card_mutation(store: CardStore) -> float | None:
    """Claims invalidate prior observation authority even when input outcome is unknown."""
    if not store.path.exists():
        return None
    with db.reader(store.path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='card_assignment_steps'").fetchone():
            return None
        paid = conn.execute('SELECT MAX(t.acted_at) FROM transactions t JOIN card_operations o '
            'ON o.transaction_key=t.key WHERE o.account_id=?', (db.connection_account(conn),)).fetchone()[0]
        assignment = conn.execute("SELECT MAX(json_extract(s.detail,'$.claimed_at')) "
            'FROM card_assignment_steps s JOIN card_operations o USING(operation_id) WHERE o.account_id=?',
            (db.connection_account(conn),)).fetchone()[0]
        known = [value for value in (paid, assignment) if value is not None]
        return max(known) if known else None


def pending_home_operations(store: CardStore) -> tuple[str, ...]:
    """Runtime recovery also adopts finished operations whose visit has not reached home."""
    if not store.path.exists():
        return ()
    with db.reader(store.path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='card_visit_flows'").fetchone():
            return ()
        return tuple(row[0] for row in conn.execute(
            'SELECT f.operation_id FROM card_visit_flows f JOIN card_operations o USING(operation_id) '
            'WHERE f.home_observed=0 AND f.stopped_reason IS NULL AND o.account_id=? ORDER BY f.started_at',
            (db.connection_account(conn),)))


class Step(Enum):
    ENTER = auto()
    SCAN = auto()
    VERIFY = auto()
    RETURN = auto()
    STOPPED = auto()
    DONE = auto()


class _DispatchRefused(RuntimeError):
    pass


class _DispatchDevice:
    """ControlTaps applies jitter first, then this guard claims and delegates to GuardedDevice."""
    def __init__(self, device: Any, dispatch: Callable[[], None]) -> None:
        self.device = device
        self.dispatch = dispatch

    def click(self, x: int, y: int) -> None:
        self.dispatch()
        self.device.click(x, y)


class CardVisit(ControlTaps):
    def __init__(self, store: CardStore, journal: TransactionJournal, *,
                 adapter: CardAdapter | None = None,
                 observe: Callable[[CardSnapshot], Any] | None = None,
                 observe_balance: Callable[[BalanceInterval], bool] | None = None,
                 current_context: Callable[[], CardPlanContext | None] | None = None,
                 route_remaining: Callable[[], int | None] | None = None) -> None:
        if store.path.resolve() != journal.path.resolve():
            raise ValueError('Cards requires shared account storage')
        self.store, self.journal = store, journal
        self.adapter = adapter if adapter is not None else CaptureCardAdapter()
        self.observe = observe if observe is not None else store.observe
        self.observe_balance = observe_balance
        self.current_context = current_context
        self.route_remaining = route_remaining
        self.tuning: Any | None = None
        self._tuning = None
        self._threshold = MATCH_THRESHOLD
        self._lock = threading.RLock()
        self._step = Step.DONE
        self._operation_id: str | None = None
        self.visit_id: str | None = None
        self.home_observed = False
        self.reason: str | None = None
        self._started: float | None = None
        self._session_started: float | None = None
        self._now = 0.
        self._progress_at = 0.
        self._last_input: tuple[str, str, float] | None = None
        self._recovery_only = False

    @property
    def active(self) -> bool:
        return self._step not in (Step.DONE, Step.STOPPED)

    @property
    def operation_id(self) -> str | None:
        return self._operation_id

    @property
    def cards_bought(self) -> int:
        return visit_purchases(self.store, self.visit_id) if self.visit_id else 0

    def request(self, operation_id: str, *, visit_id: str | None = None) -> bool:
        """Adopt a durable queued/recovery command. Submission is a separate runtime boundary."""
        with self._lock:
            if self.active:
                return self._operation_id == operation_id
            op = self.store.operation(operation_id)
            if op is None:
                return False
            with db.reader(self.store.path) as conn:
                row = conn.execute('SELECT visit_id,started_at,home_observed,stopped_reason FROM card_visit_flows WHERE operation_id=?', (operation_id,)).fetchone()
            if row is None:
                # Adopt an already durable result flow; a recovery cannot invent a new visit label.
                flow: tuple[str, float] | None = None
                if op.transaction_key:
                    txn = self.journal._require(op.transaction_key)
                    if txn.before.get('visit_id'):
                        flow = (txn.before['visit_id'], txn.ts)
                elif op.command.kind in ('apply', 'clear'):
                    with db.reader(self.store.path) as conn:
                        assignment = conn.execute('SELECT detail FROM card_assignment_intents WHERE operation_id=?', (operation_id,)).fetchone()
                    if assignment:
                        pinned = json.loads(assignment[0])
                        flow = (pinned['visit_id'], pinned['pinned_at'])
                if flow:
                    with self.store._write() as conn:
                        conn.execute('INSERT OR IGNORE INTO card_visit_flows(operation_id,visit_id,started_at) VALUES (?,?,?)',
                                     (operation_id, *flow))
                        row = conn.execute('SELECT visit_id,started_at,home_observed,stopped_reason FROM card_visit_flows WHERE operation_id=?', (operation_id,)).fetchone()
            if (row and (row[2] or row[3] and op.status in TERMINAL)
                    or op.status in TERMINAL and row is None
                    or row and visit_id is not None and row[0] != visit_id):
                return False
            if visit_id is not None and (not isinstance(visit_id, str) or not visit_id.strip()):
                raise ValueError('invalid logical Cards visit')
            if any(other.operation_id != operation_id and other.status not in ('queued', 'preflight')
                   for other in self.store.unresolved()):
                return False
            repair_card_ledger(self.store, self.journal)
            if row and row[3] is not None:
                with self.store._write() as conn:
                    conn.execute('UPDATE card_visit_flows SET stopped_reason=NULL WHERE operation_id=?', (operation_id,))
            self._operation_id = operation_id
            self._step = Step.RETURN if op.status in TERMINAL else Step.ENTER
            self.home_observed = False
            self.reason = None
            self._session_started = None
            self._last_input = None
            self.visit_id = row[0] if row else visit_id or f'cards:{operation_id}'
            self._started = row[1] if row else None
            self._recovery_only = False
            return True

    def cancel(self, reason: str) -> None:
        with self._lock:
            if not self.active:
                return
            op = self.store.operation(self._operation_id)
            now = max(self._now, op.updated_at)
            if reason == 'cancel_requested':
                op = self.store.request_cancel(op.operation_id, now=now)
            if op.status not in TERMINAL:
                unresolved = bool(op.transaction_key or assignment_steps(self.store, op.operation_id)
                                  or op.status in ('dispatched', 'verifying', 'reconciliation_required'))
                status = 'reconciliation_required' if unresolved else 'canceled'
                if op.status != status:
                    self.store.transition(op.operation_id, expected_status=op.status, status=status, reason=reason, now=now)
            with self.store._write() as conn:
                conn.execute('UPDATE card_visit_flows SET stopped_reason=? WHERE operation_id=?', (reason, op.operation_id))
            self.reason, self._step = reason, Step.STOPPED
            log.info('Cards visit stopped', extra={'account_id': op.command.scope.account_id,
                'generation': op.command.scope.generation, 'operation_id': op.operation_id, 'reason': reason})

    def _start(self, context: CardPlanContext) -> None:
        if self._session_started is None:
            self._session_started = self._progress_at = context.now
            if self._started is None:
                with self.store._write() as conn:
                    self.store._current(conn, context.scope)
                    prior = conn.execute('SELECT MIN(started_at) FROM card_visit_flows WHERE visit_id=?', (self.visit_id,)).fetchone()[0]
                    conn.execute('INSERT OR IGNORE INTO card_visit_flows(operation_id,visit_id,started_at) VALUES (?,?,?)',
                                 (self._operation_id, self.visit_id, prior if prior is not None else context.now))
                    row = conn.execute('SELECT visit_id,started_at FROM card_visit_flows WHERE operation_id=?', (self._operation_id,)).fetchone()
                    self.visit_id, self._started = row
            self._recovery_only = context.now - self._started >= VISIT_SECONDS

    def _authority(self, op: CardOperation, context: CardPlanContext, capture: CardCapture,
                   *, continuity: ScopeContinuity | None = None) -> bool:
        same = op.command.scope == context.scope
        reconciles = (continuity is not None and continuity.original == op.command.scope
                      and continuity.current == context.scope and continuity.valid(now=context.now))
        with db.reader(self.store.path) as conn:
            current = self.journal.currencies.current_scope(conn)
        return (current == context.scope == capture.scope and (same or reconciles)
                and 0 <= context.now - capture.captured_at <= TRANSITION_SECONDS)

    def _context(self, context: CardPlanContext, view: CardView, op: CardOperation) -> CardPlanContext:
        unresolved = next((other for other in self.store.unresolved()
            if other.operation_id != op.operation_id and other.status != 'queued'), None)
        boundaries = [value for value in (context.evidence_after, latest_card_mutation(self.store)) if value is not None]
        return context.model_copy(update={'visit_id': self.visit_id, 'snapshot': view.snapshot,
            'evidence_after': max(boundaries) if boundaries else None,
            'capabilities': view.capabilities, 'quotes': view.quotes, 'command': op.command,
            'balance': view.balance if view.balance is not None else context.balance,
            'unresolved_operation': unresolved,
            'cards_bought_this_visit': max(context.cards_bought_this_visit, self.cards_bought),
            'budget': self.store.budget(op.command.budget_cycle_id) if op.command.budget_cycle_id else context.budget})

    def advance(self, *, screen: Image, boxes: tuple[ocr.TextBox, ...], device: Any,
                context: CardPlanContext, capture: CardCapture,
                continuity: ScopeContinuity | None = None) -> CardOperation | None:
        with self._lock:
            if not self.active:
                return self.store.operation(self._operation_id) if self._operation_id else None
            self._now = context.now
            op = self.store.operation(self._operation_id)
            if (op.cancel_requested and op.status in ('queued', 'preflight')
                    and not op.transaction_key and not assignment_steps(self.store, op.operation_id)):
                self.cancel('cancel_requested')
                return self.store.operation(op.operation_id)
            self._start(context)
            if (self._recovery_only and op.status in ('queued', 'preflight') and not op.transaction_key
                    or context.now - self._session_started >= VISIT_SECONDS
                    or not self._recovery_only and context.now - self._started >= VISIT_SECONDS
                    or context.now - self._progress_at >= TRANSITION_SECONDS):
                self.cancel('visit_timeout' if context.now - self._started >= VISIT_SECONDS else 'transition_timeout')
                return self.store.operation(op.operation_id)
            if not self._authority(op, context, capture, continuity=continuity):
                return op
            view = self.adapter.read(screen, boxes, capture=capture, visit_id=self.visit_id, operation=op)
            digest = hashlib.sha256(screen.tobytes()).hexdigest()
            if view.balance is not None:
                balance = view.balance
                if (balance.scope != capture.scope or balance.observed_at != capture.captured_at
                        or balance.evidence_ref != digest or balance.currency != 'gems'
                        or balance.source != 'observed' or balance.lower != balance.upper
                        or self.observe_balance is None or not self.observe_balance(balance)):
                    self.cancel('wallet_provenance_mismatch')
                    return self.store.operation(op.operation_id)
            if view.snapshot is not None:
                snap = view.snapshot
                if (snap.scope != capture.scope or snap.observed_at != capture.captured_at
                        or snap.visit_id != self.visit_id or snap.frame_digest != digest):
                    self.cancel('capture_provenance_mismatch')
                    return self.store.operation(op.operation_id)
                self.observe(snap)
            if view.result is not None and (view.result.scope != capture.scope
                    or view.result.observed_at != capture.captured_at or view.result.visit_id != self.visit_id
                    or view.result.frame_digest != digest or view.result.layout_id != view.capabilities.layout_id):
                self.cancel('result_provenance_mismatch')
                return self.store.operation(op.operation_id)
            ctx = self._context(context, view, op)
            if self._last_input and self._last_input[0] == 'scan' and digest == self._last_input[1]:
                self.cancel('scan_no_progress')
                return self.store.operation(op.operation_id)
            if self._last_input and (capture.captured_at <= self._last_input[2]
                                    or digest == self._last_input[1]):
                return op
            if self._last_input:
                expected_pages = {'enter': ('cards', 'intro'), 'home': ('home', 'intro'),
                                  'slot_open': ('slot_confirmation',)}.get(self._last_input[0])
                if expected_pages is not None and view.page not in expected_pages:
                    return op
            if op.status in TERMINAL or self._step == Step.RETURN:
                self._step = Step.RETURN
                if view.page == 'home':
                    with self.store._write() as conn:
                        self.store._current(conn, capture.scope)
                        completed = self.store._operation(conn, op.operation_id)
                        returned_at = conn.execute("SELECT MAX(claimed_at) FROM card_visit_inputs WHERE operation_id=? AND kind='home'", (op.operation_id,)).fetchone()[0]
                        boundary = max(completed.updated_at, returned_at if returned_at is not None else 0.)
                        if completed.status not in TERMINAL or capture.captured_at <= boundary:
                            return completed
                        conn.execute('UPDATE card_visit_flows SET home_observed=1,stopped_reason=NULL WHERE operation_id=?', (op.operation_id,))
                    self.home_observed, self._step = True, Step.DONE
                elif view.page in ('cards', 'rewards', 'intro'):
                    control = 'dismiss' if view.page == 'intro' else 'continue' if view.page == 'rewards' else 'home'
                    self._tap_view(control, view, screen, boxes,
                                   device, capture, ctx, op)
                return self.store.operation(op.operation_id)
            if op.transaction_key:
                # An existing journal intent NEVER reaches paid dispatch, including prepared-only crashes.
                if view.result is not None:
                    op = reconcile_card_operation(self.store, self.journal, op.operation_id,
                        snapshot=view.snapshot, result=view.result, wallet=ctx.balance,
                        continuity=continuity, now=context.now)
                    if op.status in TERMINAL:
                        self._step, self._progress_at = Step.RETURN, context.now
                return op
            pinned = assignment_target(self.store, op.operation_id)
            if pinned is not None:
                self._assignment(op, pinned, view, screen, boxes, device, capture, ctx, continuity)
                return self.store.operation(op.operation_id)
            if context.paused or context.in_run or not context.armed:
                return op
            if view.page in ('home', 'intro'):
                self._tap_view('enter' if view.page == 'home' else 'dismiss', view, screen, boxes,
                               device, capture, ctx, op)
                return self.store.operation(op.operation_id)
            if view.page not in ('cards', 'slot_confirmation'):
                return op
            if op.status == 'queued':
                op = self.store.transition(op.operation_id, expected_status='queued', status='preflight', reason=None, now=context.now)
            if op.command.kind == 'refresh' and view.snapshot is not None:
                self.store.record_evidence(op.operation_id, expected_status=op.status, snapshot_after=view.snapshot, now=context.now)
                self._complete(op, context.now)
                return self.store.operation(op.operation_id)
            decision = plan_cards(ctx)
            if decision.kind == 'done':
                if view.snapshot is not None:
                    self.store.record_evidence(op.operation_id, expected_status=op.status, snapshot_after=view.snapshot, now=context.now)
                self._complete(op, context.now)
            elif decision.kind == 'apply' and decision.desired is not None:
                pin_card_assignment(self.store, op.operation_id, desired=decision.desired, visit_id=self.visit_id, now=context.now)
                self._assignment(op, decision.desired, view, screen, boxes, device, capture, ctx, continuity)
            elif decision.kind in ('buy', 'slot'):
                name = 'slot_open' if decision.kind == 'slot' and 'slot_open' in view.controls else decision.kind
                self._tap_view(name, view, screen, boxes, device, capture, ctx, op,
                               paid=name != 'slot_open')
            elif decision.kind == 'refresh':
                self._scan(view, screen, boxes, device, capture, ctx, op, digest)
            else:
                self.store.transition(op.operation_id, expected_status=op.status, status='blocked',
                                      reason=decision.reason, now=context.now)
                self._step, self._progress_at = Step.RETURN, context.now
            return self.store.operation(op.operation_id)

    def _complete(self, op: CardOperation, now: float) -> None:
        self.store.transition(op.operation_id, expected_status=op.status,
            status='not_applied' if op.command.kind in ('buy', 'slot') else 'confirmed',
            reason=None, now=now)
        repair_card_ledger(self.store, self.journal)
        self._step, self._progress_at = Step.RETURN, now

    def _assignment(self, op: CardOperation, desired: tuple[str, ...], view: CardView, screen: Image,
                    boxes: tuple[ocr.TextBox, ...], device: Any, capture: CardCapture,
                    ctx: CardPlanContext, continuity: ScopeContinuity | None) -> None:
        snap = view.snapshot
        evidence = snap.equipment_evidence if snap else None
        if (snap is None or not snap.equipment_complete or snap.equipped is None or evidence is None
                or evidence.scope != capture.scope or evidence.visit_id != self.visit_id
                or evidence.observed_at != capture.captured_at or evidence.frame_digest != snap.frame_digest
                or evidence.layout_id != snap.layout_id or snap.confidence is None or snap.confidence < .9
                or evidence.confidence is None or evidence.confidence < .9):
            if not self.store.operation(op.operation_id).cancel_requested:
                self._scan(view, screen, boxes, device, capture, ctx, op, hashlib.sha256(screen.tobytes()).hexdigest())
            return
        steps = assignment_steps(self.store, op.operation_id)
        if steps:
            last = steps[-1]
            # A crash may have persisted verification without finalizing the operation.
            # Incoming proof must still follow the latest input and cannot predate saved proof.
            if (capture.captured_at <= last['claimed_at']
                    or last['verified_at'] is not None and capture.captured_at < last['verified_at']):
                return
        if steps and steps[-1]['verified_at'] is None:
            last = steps[-1]
            if (capture.captured_at <= last['claimed_at'] or set(snap.equipped) != set(last['after'])
                    or snap.frame_digest == last['frame_digest']):
                return
            with self.store._write() as conn:
                self.store._current(conn, capture.scope)
                last = dict(last, verified_at=capture.captured_at, verification=snap.model_dump(mode='json'))
                conn.execute('UPDATE card_assignment_steps SET verified_at=?,detail=? WHERE operation_id=? AND sequence=? AND verified_at IS NULL',
                    (capture.captured_at, json.dumps(last), op.operation_id, last['sequence']))
            self._progress_at = ctx.now
        if self.store.operation(op.operation_id).cancel_requested:
            # Only the claimed input was authorized before cancellation. Its
            # observed effect is now known; retain the requested target and the
            # truthful partial set without completing or undoing the loadout.
            if op.snapshot_after is None:
                op = self.store.record_evidence(op.operation_id, expected_status=op.status,
                    snapshot_after=snap, now=ctx.now)
            self.store.transition(op.operation_id, expected_status=op.status,
                status='canceled', reason='cancel_requested', now=ctx.now)
            repair_card_ledger(self.store, self.journal)
            self._step, self._progress_at = Step.RETURN, ctx.now
            return
        if set(snap.equipped) == set(desired):
            if not steps:
                self.store.record_evidence(op.operation_id, expected_status=op.status, snapshot_after=snap, now=ctx.now)
                self._complete(op, ctx.now)
            else:
                result = reconcile_card_operation(self.store, self.journal, op.operation_id,
                    snapshot=snap, continuity=continuity, now=ctx.now)
                if result.status == 'confirmed':
                    self._step, self._progress_at = Step.RETURN, ctx.now
            return
        if self._recovery_only or op.command.scope != capture.scope:
            return
        remove, add = equipment_changes(snap.equipped, desired)
        target = (remove or add)[0]
        self._tap_view(target, view, screen, boxes, device, capture, ctx, op, desired=desired)

    def _scan(self, view: CardView, screen: Image, boxes: tuple[ocr.TextBox, ...], device: Any,
              capture: CardCapture, ctx: CardPlanContext, op: CardOperation, digest: str) -> None:
        with db.reader(self.store.path) as conn:
            claims = conn.execute("SELECT frame_digest,claimed_at FROM card_visit_inputs WHERE visit_id=? AND kind='scan' ORDER BY sequence", (self.visit_id,)).fetchall()
        if any(row[0] == digest for row in claims) or len(claims) >= MAX_PASSES:
            self.cancel('scan_no_progress' if any(row[0] == digest for row in claims) else 'scan_limit')
            return
        if claims and capture.captured_at <= claims[-1][1]:
            return
        if 'scan' in view.controls:
            self._tap_view('scan', view, screen, boxes, device, capture, ctx, op)

    def _claim_visit_input(self, op: CardOperation, kind: str, capture: CardCapture,
                           digest: str, now: float) -> None:
        """Reserve scan allowance or return boundary before input, under the account lock."""
        with self.store._write() as conn:
            self.store._current(conn, capture.scope)
            latest = self.store._operation(conn, op.operation_id)
            flow = conn.execute('SELECT visit_id FROM card_visit_flows WHERE operation_id=?', (op.operation_id,)).fetchone()
            if latest.status != op.status or flow is None or flow[0] != self.visit_id:
                raise _DispatchRefused('visit owner changed')
            if kind == 'continue':
                claim = json.dumps({'claimed_at': now, 'capture': asdict(capture), 'frame_digest': digest})
                changed = conn.execute('UPDATE card_visit_flows SET continuation_claim=? '
                    'WHERE operation_id=? AND continuation_claim IS NULL', (claim, op.operation_id))
                if changed.rowcount != 1:
                    raise _DispatchRefused('continuation_outcome_unknown')
                return
            rows = conn.execute('SELECT frame_digest,claimed_at FROM card_visit_inputs WHERE visit_id=? AND kind=? ORDER BY sequence', (self.visit_id, kind)).fetchall()
            if kind == 'scan':
                if any(row[0] == digest for row in rows):
                    raise _DispatchRefused('scan_no_progress')
                if len(rows) >= MAX_PASSES:
                    raise _DispatchRefused('scan_limit')
                if rows and capture.captured_at <= rows[-1][1]:
                    raise _DispatchRefused('scan_capture_before_claim')
            conn.execute('INSERT INTO card_visit_inputs VALUES (?,?,?,?,?,?,?,?)',
                (self.visit_id, kind, len(rows) + 1, op.operation_id, now,
                 capture.captured_at, digest, json.dumps(asdict(capture.scope))))

    def _tap_view(self, name: str, view: CardView, screen: Image, boxes: tuple[ocr.TextBox, ...],
                  device: Any, capture: CardCapture, context: CardPlanContext, op: CardOperation,
                  *, paid: bool = False, desired: tuple[str, ...] | None = None) -> None:
        target = view.controls.get(name)
        if target is None or target.status != 'located' or target.point is None:
            return
        if self._recovery_only:
            return
        digest = hashlib.sha256(screen.tobytes()).hexdigest()

        def cancellation_blocks(latest: CardOperation) -> bool:
            # Cancellation permits only receipt recovery and terminal safe return.
            return latest.cancel_requested and not (
                name in ('home', 'dismiss') and latest.status in TERMINAL
                or name == 'continue' and latest.transaction_key is not None)

        def dispatch() -> None:
            live = self.current_context() if self.current_context else None
            latest = self.store.operation(op.operation_id)
            if (live is None or not self.active or live.paused or live.in_run or not live.armed
                    or latest.status != op.status or latest.command != op.command
                    or cancellation_blocks(latest)
                    or not self._authority(latest, live, capture)
                    or live.now - self._started >= VISIT_SECONDS):
                raise _DispatchRefused('runtime gate changed')
            # Re-recognize controls after reaction delay; never borrow target coordinates from another frame.
            current_view = self.adapter.read(screen, boxes, capture=capture, visit_id=self.visit_id, operation=latest)
            if (current_view.controls.get(name) != target or current_view.snapshot != view.snapshot
                    or current_view.quotes != view.quotes or current_view.capabilities != view.capabilities
                    or current_view.balance != view.balance):
                raise _DispatchRefused('capture reading changed')
            current = self._context(live, current_view, latest)
            with self.store._write() as conn:
                self.store._current(conn, capture.scope)
            if paid:
                decision = plan_cards(current)
                if decision.kind != latest.command.kind or decision.quantity != latest.command.quantity:
                    raise _DispatchRefused('planner changed')
                quote = current_quote(current, decision.kind, decision.quantity)
                wallet = current.balance
                if quote is None or wallet is None or wallet.lower is None or wallet.lower != wallet.upper:
                    raise _DispatchRefused('exact wallet/quote unavailable')
                if latest.snapshot_before is None and current.snapshot is not None:
                    self.store.record_evidence(latest.operation_id, expected_status=latest.status,
                        now=live.now, snapshot_before=current.snapshot)
                intent = Intent(item='Cards', category='CARDS', currency='gems', price=quote.price,
                    wallet_before=wallet.lower, ts=live.now,
                    operation='card_slot_buy' if decision.kind == 'slot' else 'card_buy',
                    before={'card_operation_id': latest.operation_id, 'action_sequence': 1,
                        'scope': asdict(capture.scope), 'visit_id': self.visit_id,
                        'quantity': latest.command.quantity, 'evidence_digest': digest,
                        'source_capacity': quote.capacity})
                txn = self.journal.prepare(intent, scope=capture.scope, balance=wallet,
                    reserve=current.policy.gem_floor,
                    cards=CardBudgetPrecondition(self.store, latest.operation_id,
                        program_cap=current.program.gem_cap,
                        route_remaining=self.route_remaining() if self.route_remaining else None))
                if txn is None:
                    raise _DispatchRefused('journal refused')
                if self.store.operation(op.operation_id).cancel_requested:
                    raise _DispatchRefused('cancel_requested')
                self.journal.record_action(txn.key, at=live.now)
                self.store.transition(latest.operation_id, expected_status='preflight', status='dispatched', reason=None, now=live.now)
            elif desired is not None:
                if (latest.cancel_requested or not current.capabilities.assign or current.unresolved_operation is not None
                        or latest.command.source == 'automatic' and not current.policy.enabled):
                    raise _DispatchRefused('assignment unavailable')
                if current.snapshot is None or current.snapshot.equipped is None:
                    raise _DispatchRefused('equipment unavailable')
                remove, add = equipment_changes(current.snapshot.equipped, desired)
                if name != (remove or add or (None,))[0]:
                    raise _DispatchRefused('equipment target changed')
                if latest.status == 'preflight':
                    if latest.snapshot_before is None:
                        self.store.record_evidence(latest.operation_id, expected_status=latest.status,
                            snapshot_before=current.snapshot, now=live.now)
                    self.store.transition(latest.operation_id, expected_status='preflight', status='dispatched', reason=None, now=live.now)
                self._claim_toggle(latest, name, desired, current.snapshot, capture, live.now)
            if name in ('scan', 'home', 'continue'):
                self._claim_visit_input(latest, name, capture, digest, live.now)
            final = self.current_context() if self.current_context else None
            if (final is None or not self.active or final.paused or final.in_run or not final.armed
                    or not self._authority(latest, final, capture)
                    or cancellation_blocks(self.store.operation(op.operation_id))):
                raise _DispatchRefused('runtime changed during durable claim')
            self._last_input = name, digest, live.now
            self._progress_at = live.now

        self._tuning = self.tuning
        try:
            self._tap_target(target, _DispatchDevice(device, dispatch), name, Step.VERIFY, context.now)
        except _DispatchRefused as exc:
            self.reason = str(exc)
            if str(exc) in ('scan_limit', 'scan_no_progress'):
                self.cancel(str(exc))
            latest = self.store.operation(op.operation_id)
            if latest.status == 'dispatched' and op.status == 'preflight':
                self.cancel('dispatch_refused_after_claim')
        except Exception:
            self.cancel('input_outcome_unknown')
            raise

    def _claim_toggle(self, op: CardOperation, target: str, desired: tuple[str, ...],
                      snap: CardSnapshot, capture: CardCapture, now: float) -> None:
        before = snap.equipped
        after = tuple(card for card in before if card != target) if target in before else before + (target,)
        with self.store._write() as conn:
            self.store._current(conn, capture.scope)
            latest = self.store._operation(conn, op.operation_id)
            if latest.cancel_requested or latest.status not in ('dispatched', 'verifying', 'reconciliation_required'):
                raise _DispatchRefused('assignment state changed')
            rows = conn.execute('SELECT sequence,verified_at,detail FROM card_assignment_steps WHERE operation_id=? ORDER BY sequence', (op.operation_id,)).fetchall()
            if rows and (rows[-1][1] is None or capture.captured_at < rows[-1][1]
                         or set(json.loads(rows[-1][2])['after']) != set(before)):
                raise _DispatchRefused('assignment effect unresolved')
            sequence = len(rows) + 1
            detail = {'operation_id': op.operation_id, 'sequence': sequence, 'target': target,
                'before': before, 'after': after, 'desired': desired, 'scope': asdict(capture.scope),
                'visit_id': self.visit_id, 'frame_digest': snap.frame_digest,
                'captured_at': capture.captured_at, 'claimed_at': now, 'verified_at': None,
                'verification': None}
            conn.execute('INSERT INTO card_assignment_steps VALUES (?,?,NULL,?)', (op.operation_id, sequence, json.dumps(detail)))

    def _enter(self, step: Step) -> None:
        self._step = step

    def _wait(self, reason: str, detail: str, moment: float) -> None:
        self.reason = reason

    def _finish(self, status: str, reason: str, detail: str, moment: float) -> None:
        self.cancel(reason)
