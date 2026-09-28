"""Bounded, route-gated Lab execution using the shared purchase authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
import time
from typing import Callable

from account_state import AccountState
from evidence_scope import BalanceInterval, FactScope
from lab_runtime import LabRuntime, LabScope, _catalog_revision
import events
import transactions

from device import AdbDevice, Image, tap
from lab_plan import LabDecision, LabVisitOptions, decide
from fleet.resource_blocks import LabAction
from lab_routes import research_gate, unlock_gate
from lab_screen import (LabConfirmationReading, LabHomeReading, LabPickerReading,
                        read_confirmation, read_home, read_picker, read_slots,
                        read_selected_home, read_selected_picker)
import ocr
import pages
import vision
from labs import LabJob, LabsReading

# Body lines unique to the first-visit LABS info popup, whitespace removed.
_INTRO_LINES = ("LABSGRANTYOU", "GEMRUSHEFFICIENCY")


@dataclass(frozen=True)
class LabVisitResult:
    status: str
    reason: str
    decision: LabDecision
    confirmed_job: LabJob | None = None
    observed_coin_spend: int = 0
    confirmed_readings: tuple[LabsReading, ...] = ()
    slot2_status: str = "unknown"
    gem_balance: int | None = None
    gems_before: int | None = None
    observed_gem_spend: int = 0
    transaction_key: str | None = None
    unlock_transaction_key: str | None = None


class LabVisit:
    """One tap per scan; a research tap never implies a completed purchase."""

    def __init__(
        self,
        templates: vision.TemplateCache,
        home_reader: Callable[[Image, tuple[ocr.TextBox, ...]], LabHomeReading] = read_home,
        picker_reader: Callable[[Image, tuple[ocr.TextBox, ...]], LabPickerReading] = read_picker,
        confirmation_reader: Callable[[Image, tuple[ocr.TextBox, ...]], LabConfirmationReading]
        = read_confirmation,
        *, slot_observer: Callable[[LabsReading], None] | None = None,
        wall_clock: Callable[[], float] = time.time,
        journal: transactions.TransactionJournal | None = None,
        account_state: AccountState | None = None,
        runtime: LabRuntime | None = None,
        authorize: Callable[[str, LabDecision | None, float], bool] | None = None,
        event_sink: Callable[[events.Event], object] | None = None,
    ) -> None:
        self.templates = templates
        self.home_reader = home_reader
        self.picker_reader = picker_reader
        self.confirmation_reader = confirmation_reader
        self.slot_observer = slot_observer
        self.wall_clock = wall_clock
        self.journal, self.account_state, self.runtime = journal, account_state, runtime
        self.authorize, self.event_sink = authorize, event_sink
        self._capture_at = 0.
        self._capture_scope: FactScope | None = None
        self._reading: LabsReading | None = None
        self._emitted: set[str] = set()
        self.recovery_status: str | None = None
        self._state = "idle"
        self._started_at = 0.
        self._scans = 0
        self._picker_signature: tuple[int, int, tuple[int, int], int | None, str] | None = None
        self._picker_reads = 0
        self._picker_name: str | None = None
        self._dialog_signature: tuple[str, int, int, tuple[int, int]] | None = None
        self._dialog_reads = 0
        self._slot: LabHomeReading | None = None
        self._purchase: LabDecision | None = None
        self._outcome: LabVisitResult | None = None
        self._slot2_home: LabHomeReading | None = None
        self._slot2_signature: tuple[int, int, tuple[int, int]] | None = None
        self._slot2_reads = 0
        self._slot2_tapped = False
        self._options = LabVisitOptions()
        self.selected_action: LabAction | None = None
        self.pending_action: LabAction | None = None
        self._stage_name = 'idle'
        self._stage_scans = 0
        self._stage_started = 0.
        self.last_tap: tuple[str, int, int] | None = None

    @property
    def active(self) -> bool:
        return self._state != "idle"

    def request(self, action: LabAction | LabVisitOptions | None = None, *,
                options: LabVisitOptions | None = None) -> bool:
        if self.active:
            return False
        if isinstance(action, LabVisitOptions):
            options, action = action, None
        if action is not None:
            if (action.operation != 'start' or type(action.slot) is not int
                    or action.slot not in range(1, 6) or type(action.target_level) is not int
                    or action.target_level < 1 or type(action.strategy_revision) is not int):
                self.recovery_status = 'invalid_lab_action'
                return False
            self.pending_action = action
            if not research_gate(action.slot, action.research).enabled:
                self.recovery_status = 'lab_route_calibration_required'
                return False
        self.selected_action = action
        self._options = options or LabVisitOptions()
        self._stage_name, self._stage_scans, self._stage_started = 'idle', 0, 0.
        self._state = "open"
        self._started_at = 0.
        self._scans = 0
        self._picker_signature = None
        self._picker_reads = 0
        self._picker_name = None
        self._dialog_signature = None
        self._dialog_reads = 0
        self._slot = None
        self._purchase = None
        self._outcome = None
        self._slot2_home = None
        self._slot2_signature = None
        self._slot2_reads = 0
        self._slot2_tapped = False
        self.last_tap = None
        self.recovery_status = None
        return True

    @property
    def pending_transaction(self) -> transactions.Transaction | None:
        if self.journal is None:
            return None
        return next((txn for txn in self.journal.open_transactions()
                     if txn.operation in {'lab_start', 'lab_unlock'}), None)

    @property
    def has_recovery_receipts(self) -> bool:
        scope = self.account_state.verified_scope if self.account_state else None
        return bool(scope is not None and self.journal and any(
            txn.scope == scope or txn.scope is not None and
            self.account_state.continuity(txn.scope, now=self.wall_clock()) is not None
            for txn, _ in self.journal.recovered_visit(operations={'lab_start', 'lab_unlock'})))

    def _scope(self) -> FactScope | None:
        scope = self.account_state.verified_scope if self.account_state else None
        if (scope is None or scope != self._capture_scope or self.runtime is None
                or self.runtime.scope != LabScope(scope.account_id, scope.lease_id, scope.generation, scope.epoch)
                or not self.account_state.accepts_capture(scope, self._capture_at)
                or not 0 <= self.wall_clock()-self._capture_at <= 30):
            return None
        return scope

    def _prepare(self, operation: str, wallet: int, price: int) -> transactions.Transaction | None:
        scope = self._scope()
        now = self.wall_clock()
        if (scope is None or self.journal is None or self._reading is None
                or self.account_state.safety_path != self.journal.path):
            return None
        purchase = self._purchase
        target = purchase.target_level if purchase is not None else None
        selected_slot = purchase.slot if purchase is not None else 1
        research_id = purchase.research_id if purchase is not None else 'labs.game-speed'
        if operation == 'lab_start':
            if not research_gate(selected_slot, research_id).enabled:
                return None
            slot = self.runtime.snapshot().slots[selected_slot-1]
            if (type(target) is not int or target < 1 or not slot.confirmed or slot.state != 'idle'
                    or slot.observed_at is None or not 0 <= now-slot.observed_at <= 30):
                return None
        elif (not unlock_gate(2).enabled or not self._reading.strip_read() or self._reading.slots_owned != 1
                or self.runtime.snapshot().slots_owned != 1):
            return None
        currency = 'coins' if operation == 'lab_start' else 'gems'
        balance = BalanceInterval.from_reading(currency, wallet, scope, self._capture_at,
                                               self._reading.frame_digest)
        self.account_state.observe_balance(balance)
        if self.authorize is not None and not self.authorize(operation, self._purchase, now):
            return None
        from concepts import REGISTRY
        intent = transactions.Intent(item=REGISTRY.by_id(research_id).name if operation == 'lab_start' else 'Lab 2',
            category='LABS', currency=currency, price=price, wallet_before=wallet,
            ts=now, operation=operation, before={
                'slot': selected_slot if operation == 'lab_start' else 2,
                'research_id': research_id if operation == 'lab_start' else None,
                'source_level': target-1 if operation == 'lab_start' else None,
                'target_level': target if operation == 'lab_start' else None,
                'evidence_digest': self._reading.frame_digest, 'catalog_revision': _catalog_revision()})
        balance = replace(balance, catalog_revision=_catalog_revision())
        try:
            txn = self.journal.prepare(intent, scope=scope, balance=balance,
                reserve=self._options.keep_gems if currency == 'gems' else 0)
        except transactions.TransactionInFlight:
            return None
        if txn is None or txn.stage != transactions.Stage.INTENDED:
            return None
        return self.journal.record_action(txn.key, at=now)

    def _restore_receipts(self) -> None:
        if self.journal is None or self._scope() is None:
            return
        for txn, outcome in self.journal.recovered_visit(operations={'lab_start', 'lab_unlock'}):
            if outcome.verdict not in (transactions.Verdict.BOUGHT, transactions.Verdict.FREE):
                self.journal.finish_recovered_visit({txn.key})
                continue
            if txn.scope != self._scope() and (txn.scope is None or
                    self.account_state.continuity(txn.scope, now=self.wall_clock()) is None):
                continue
            if txn.operation == 'lab_start' and not self.runtime.bind_transaction(
                    txn.before['slot'], txn.before['research_id'], txn.before['target_level'], txn.key):
                continue
            if txn.key not in self._emitted:
                if self.event_sink is not None:
                    self.event_sink(self.journal.recovery_event(txn, outcome))
                self._emitted.add(txn.key)
            self.journal.finish_recovered_visit({txn.key})

    def _recover(self, txn: transactions.Transaction, home: LabHomeReading,
                 picker: LabPickerReading, confirmation: LabConfirmationReading,
                 screen: Image, device: AdbDevice) -> None:
        """Only calibrated inspection navigation is allowed while a spend is unresolved."""
        scope = self._scope()
        self.recovery_status = 'awaiting_lab_semantic_and_wallet_proof'
        if scope is None or self._reading is None:
            self.recovery_status = 'lab_scope_unavailable'
            return
        if (txn.scope != scope and (txn.scope is None
                or self.account_state.continuity(txn.scope, now=self.wall_clock()) is None)):
            self.recovery_status = 'lab_scope_continuity_unavailable'
            return
        if home.page:
            record = self.runtime.snapshot().slots[txn.before['slot']-1]
            effect: bool | None = None
            boundary = txn.acted_at if txn.acted_at is not None else txn.ts
            current = (record.confirmed and record.observed_at == self._capture_at
                       and record.started_observed_at is not None and record.started_observed_at > boundary)
            if txn.operation == 'lab_start' and current:
                if (record.state == 'researching' and record.research_id == txn.before.get('research_id')
                        and record.target_level == txn.before.get('target_level')):
                    effect = True
                elif record.state == 'idle':
                    effect = False
            if txn.operation == 'lab_unlock' and current and self.runtime.snapshot().slots_owned is not None:
                if home.slot2_status == 'owned' and self.runtime.snapshot().slots_owned >= 2:
                    effect = True
            proof = transactions.RecoveryEvidence(category='LABS', currency=txn.currency,
                wallet_after=home.coin_balance if txn.currency == 'coins' else home.gem_balance,
                effect_changed=effect, observed_at=self._capture_at,
                frame_digest=self._reading.frame_digest, scope=scope,
                continuity=self.account_state.continuity(txn.scope, now=self.wall_clock()) if txn.scope else None,
                operation=txn.operation, slot=txn.before['slot'],
                research_id=txn.before.get('research_id') if effect is False else record.research_id,
                target_level=txn.before.get('target_level') if effect is False else record.target_level,
                completes_at=record.expected_finish)
            outcome = self.journal.reconcile(txn.key, proof, now=self.wall_clock())
            if outcome.verdict in (transactions.Verdict.BOUGHT, transactions.Verdict.FREE):
                self._restore_receipts()
                if txn.operation == 'lab_start':
                    self._purchase = LabDecision('start', txn.price, txn.wallet_before,
                                                game_speed_level=txn.before['target_level'],
                                                slot=txn.before['slot'], research_id=txn.before['research_id'])
                    job = next((j for j in self._reading.jobs if j.slot == txn.before['slot']), None)
                    self.pending_action = None
                    reason = 'game_speed_confirmed' if txn.before['research_id'] == 'labs.game-speed' else 'research_confirmed'
                    self._return(LabVisitResult('started', reason, self._purchase,
                        confirmed_job=job, observed_coin_spend=outcome.spent, transaction_key=txn.key))
                else:
                    self._return(replace(self._outcome or LabVisitResult('observed', 'slot_unlocked', LabDecision('unknown')),
                        slot2_status='owned', gem_balance=home.gem_balance, gems_before=txn.wallet_before,
                        observed_gem_spend=outcome.spent, unlock_transaction_key=txn.key))
                self.recovery_status = 'settled'
            elif outcome.verdict == transactions.Verdict.REFUTED:
                self._restore_receipts()
                self._return(LabVisitResult('observed', 'unclaimed_dispatch_refuted', LabDecision('unknown')))
            return
        if not self.account_state.identity_fresh(now=self.wall_clock()):
            self.recovery_status = 'lab_identity_verification_required'
            return
        if confirmation.page and confirmation.cancel_point is not None:
            self._tap(device, confirmation.cancel_point, 'cancel_confirmation')
        elif picker.page:
            point = self._match(screen, 'nav/labs_close.png')
            if point is not None:
                self._tap(device, point, 'close_picker')
        elif pages.classify_page(screen, self.templates).page == 'MAIN_MENU' and self.tab_unlocked(screen):
            point = self._match(screen, 'nav/tab_labs.png')
            if point is not None:
                self._tap(device, point, 'inspect_pending_lab')
        else:
            self.recovery_status = 'lab_reconciliation_route_unavailable'

    def tab_unlocked(self, screen: Image) -> bool:
        """Return whether the actionable Labs tab is visible on this frame."""
        return self.tab_status(screen) == "unlocked"

    def tab_status(self, screen: Image) -> str:
        """Read Labs as locked, unlocked, or unknown without tapping it."""
        unlocked = self._match(screen, "nav/tab_labs.png") is not None
        locked = self._locked_tab_match(screen)
        if unlocked == locked:
            return "unknown"
        return "unlocked" if unlocked else "locked"

    def _locked_tab_match(self, screen: Image) -> bool:
        """Check the locked flask slot only, not other locked menu tabs."""
        height, width = screen.shape[:2]
        template = self.templates.get("nav/tab_labs_locked.png")
        template_height, template_width = template.shape[:2]
        # This slot origin is measured from the portrait main-menu fixtures.
        x = round(width * (765 / 1080))
        y = round(height * (2264 / 2400))
        region = screen[y:y + template_height, x:x + template_width]
        if region.shape[:2] != (template_height, template_width):
            return False
        return vision.locate_template(region, template, .94) is not None

    def cancel(self, reason: str) -> None:
        self._state = "idle"
        self._outcome = LabVisitResult("cancelled", reason, LabDecision("unknown"))
        self.last_tap = None

    def _match(self, screen: Image, path: str) -> tuple[int, int] | None:
        match = vision.locate_template(screen, self.templates.get(path), .94)
        return match.center if match is not None else None

    def _tap(self, device: AdbDevice, point: tuple[int, int], action: str) -> None:
        tap(device, *point)
        self.last_tap = (action, *point)

    def _finish(self, outcome: LabVisitResult) -> LabVisitResult:
        self._state = "idle"
        self._outcome = outcome
        return outcome

    def _return(self, outcome: LabVisitResult) -> None:
        if self._slot2_home is not None and outcome.observed_gem_spend == 0:
            outcome = replace(outcome, slot2_status=self._slot2_home.slot2_status,
                              gem_balance=self._slot2_home.gem_balance)
        self._outcome = outcome
        self._state = "return"

    def _unlock_lab_two(self, home: LabHomeReading, device: AdbDevice) -> bool:
        """On the way out, buy Lab 2 once from two matching affordable reads."""
        if (not unlock_gate(2).enabled or 2 not in self._options.unlock_slots or self._slot2_tapped
                or home.slot2_status != "locked" or home.slot2_price != 100
                or home.gem_balance is None
                or home.gem_balance < home.slot2_price + self._options.keep_gems
                or home.slot2_point is None):
            return False
        self._slot2_home = home
        signature = (home.slot2_price, home.gem_balance, home.slot2_point)
        if signature != self._slot2_signature:
            self._slot2_signature = signature
            self._slot2_reads = 1
            return True
        self._slot2_reads += 1
        if self._slot2_reads < 2:
            return True
        if self._prepare('lab_unlock', home.gem_balance, home.slot2_price) is None:
            self.recovery_status = 'lab_preparation_refused'
            return False
        self._tap(device, home.slot2_point, "unlock_lab_two")
        self._slot2_tapped = True
        self._state = "confirm_slot2"
        return True

    def advance(
        self, screen: Image, boxes: tuple[ocr.TextBox, ...],
        device: AdbDevice, now: float,
        *, observed_at: float | None = None, capture_scope: FactScope | None = None,
    ) -> LabVisitResult | None:
        if not self.active:
            return None
        self.last_tap = None
        capture_at = self.wall_clock() if observed_at is None else observed_at
        if capture_at <= self._capture_at:
            return None
        self._capture_at, self._capture_scope = capture_at, capture_scope
        if self._started_at == 0.:
            self._started_at = now
        self._scans += 1
        if self._scans > 48 or now - self._started_at > 90:
            if self.pending_transaction is not None:
                self.recovery_status = 'lab_reconciliation_route_unavailable'
            return self._finish(self._outcome or LabVisitResult(
                "failed", "visit_timeout", LabDecision("unknown")))

        # The first Labs visit opens an info popup over a still-readable Lab 1
        # card; close it before any reader can act on the dimmed page.
        if any("".join(box.text.upper().split()).startswith(_INTRO_LINES) for box in boxes):
            pending = self.pending_transaction
            scope = self._scope()
            if pending is not None and (scope is None or not self.account_state.identity_fresh(now=self.wall_clock())
                    or pending.scope != scope and (pending.scope is None or
                        self.account_state.continuity(pending.scope, now=self.wall_clock()) is None)):
                self.recovery_status = 'lab_scope_continuity_unavailable'
                return None
            point = self._match(screen, "nav/labs_close.png")
            if point is not None:
                self._tap(device, point, "close_labs_intro")
            return None

        self._reading = read_slots(screen, boxes, observed_at=capture_at)
        if self.slot_observer is not None:
            self.slot_observer(self._reading)
        elif self.runtime is not None and self._scope() is not None:
            self.runtime.observe(self._reading)
        self._restore_receipts()
        selected = self.selected_action
        home = (read_selected_home(screen, boxes, slot=selected.slot, observed_at=capture_at)
                if selected is not None and selected.slot != 1 else self.home_reader(screen, boxes))
        picker = (read_selected_picker(screen, boxes, research_id=selected.research)
                  if selected is not None and selected.research != 'labs.game-speed'
                  else self.picker_reader(screen, boxes))
        confirmation = self.confirmation_reader(screen, boxes)
        pending = self.pending_transaction
        if pending is not None:
            self._recover(pending, home, picker, confirmation, screen, device)
            return None
        if self._stage_name != self._state:
            self._stage_name, self._stage_scans, self._stage_started = self._state, 0, now
        self._stage_scans += 1
        budget = 6 if self._state == 'return' else 8
        if self._stage_scans > budget or now - self._stage_started > 30:
            outcome = self._outcome or LabVisitResult('failed',
                f'{self._state}_stage_timeout', LabDecision('unknown'))
            if self._state == 'return' or not (home.page or picker.page or confirmation.page):
                return self._finish(outcome)
            self._return(outcome)
            return None
        if self._state == "open":
            if home.page:
                self._state = "home"
            elif pages.classify_page(screen, self.templates).page == "MAIN_MENU":
                point = self._match(screen, "nav/tab_labs.png")
                if point is not None:
                    self._tap(device, point, "open_labs")
                    self._state = "home"
                return None
            else:
                return self._finish(LabVisitResult("failed", "not_at_menu", LabDecision("unknown")))

        if self._state == "home":
            if not home.page:
                return None
            self._slot2_home = home
            decision = decide(home, None)
            if not self._options.start_research and self.runtime is not None:
                snapshot = self.runtime.snapshot()
                if (not self._reading.strip_read() or snapshot.slots_owned is None
                        or not all(record.confirmed and record.observed_at == capture_at
                                   for record in snapshot.slots[:snapshot.slots_owned])):
                    return None
            if decision.kind == "inspect" and not self._options.start_research:
                # Auto-start is off: read the slot, never open the picker.
                self._return(LabVisitResult("observed", "auto_start_off", decision,
                                             confirmed_job=home.job))
            elif decision.kind == "inspect" and home.slot_point is not None:
                if self.runtime is not None and not self.runtime.snapshot().slots[(selected.slot if selected else 1)-1].confirmed:
                    return None
                self._slot = home
                self._tap(device, home.slot_point, "open_lab_one" if selected is None or selected.slot == 1 else f"open_lab_{selected.slot}")
                self._state = "picker"
            else:
                self._return(LabVisitResult("observed", decision.kind, decision,
                                             confirmed_job=home.job))
            return None

        if self._state == "picker":
            if not picker.page or self._slot is None:
                return None
            decision = decide(self._slot, picker, research_id=selected.research if selected else 'labs.game-speed')
            if selected is not None:
                decision = replace(decision, slot=selected.slot, research_id=selected.research,
                                   strategy_revision=selected.strategy_revision)
                if (picker.entry is None or picker.entry.concept_id != selected.research
                        or picker.entry.level != selected.target_level):
                    self._return(LabVisitResult('failed', 'selected_research_mismatch', decision))
                    return None
            if decision.kind != "start":
                self._return(LabVisitResult("observed", decision.kind, decision))
                return None
            assert picker.buy_point is not None
            assert decision.price is not None and decision.wallet_coins is not None
            signature = (decision.price, decision.wallet_coins, picker.buy_point,
                         decision.game_speed_level, picker.game_speed.raw_name)
            if signature != self._picker_signature:
                self._picker_signature = signature
                self._picker_reads = 1
                return None
            self._picker_reads += 1
            if self._picker_reads < 2:
                return None
            self._purchase = decision
            self._picker_name = picker.game_speed.raw_name if picker.game_speed is not None else None
            self._tap(device, picker.buy_point, "start_game_speed" if decision.research_id == "labs.game-speed" else "select_research")
            self._state = "dialog"
            return None

        if self._state == "dialog":
            if not confirmation.page:
                return None
            purchase = self._purchase
            if (purchase is None or confirmation.name != self._picker_name
                    or confirmation.research_id != purchase.research_id
                    or confirmation.target_level != purchase.target_level
                    or confirmation.price != purchase.price
                    or confirmation.coin_balance != purchase.wallet_coins
                    or confirmation.research_point is None):
                self._return(LabVisitResult("failed", "confirmation_mismatch",
                                             LabDecision("unknown")))
                return None
            assert confirmation.price is not None and confirmation.coin_balance is not None
            signature = (confirmation.name, confirmation.price,
                         confirmation.coin_balance, confirmation.research_point)
            if signature != self._dialog_signature:
                self._dialog_signature = signature
                self._dialog_reads = 1
                return None
            self._dialog_reads += 1
            if self._dialog_reads < 2:
                return None
            if self._prepare('lab_start', confirmation.coin_balance, confirmation.price) is None:
                self._return(LabVisitResult('failed', 'lab_preparation_refused', LabDecision('unknown')))
                return None
            self._tap(device, confirmation.research_point, "confirm_game_speed" if purchase.research_id == "labs.game-speed" else "confirm_research")
            self._state = "confirm"
            return None

        if self._state in {"confirm", "confirm_slot2"}:
            return self._finish(LabVisitResult('failed', 'transaction_receipt_unavailable', LabDecision('unknown')))

        if self._state == "return":
            if confirmation.page:
                if confirmation.cancel_point is not None:
                    self._tap(device, confirmation.cancel_point, "cancel_confirmation")
                return None
            if picker.page:
                point = self._match(screen, "nav/labs_close.png")
                if point is not None:
                    self._tap(device, point, "close_picker")
                return None
            if home.page:
                if self._unlock_lab_two(home, device):
                    return None
                point = self._match(screen, "nav/tab_battle.png")
                if point is not None:
                    self._tap(device, point, "return_to_battle")
                return None
            if pages.classify_page(screen, self.templates).page == "MAIN_MENU":
                return self._finish(self._outcome or LabVisitResult(
                    "failed", "no_outcome", LabDecision("unknown")))
            return None
        return None
