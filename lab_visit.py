"""Bounded, route-gated Lab execution using the shared purchase authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import time
from typing import Callable

from account_state import AccountState
from evidence_scope import BalanceInterval, FactScope
from lab_runtime import LabRuntime, LabScope, _catalog_revision
import events
import lab_catalog
import transactions

from device import AdbDevice, Image, tap
from lab_plan import LabDecision, LabVisitOptions, decide
from fleet.resource_blocks import LabAction
from lab_routes import research_gate
from lab_screen import (LabConfirmationReading, LabHomeReading, LabPickerReading, LockedSlot,
                        read_confirmation, read_home, read_picker, read_slots,
                        read_selected_home, read_selected_picker)
import ocr
import pages
import vision
from labs import LabJob, LabsReading
from lab_unlock_rollout import LabUnlockRollout, RolloutChange

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
    slot_status: tuple[tuple[int, str], ...] = ()
    gem_balance: int | None = None
    gems_before: int | None = None
    observed_gem_spend: int = 0
    transaction_key: str | None = None
    unlock_transaction_key: str | None = None
    unlocked_slot: int | None = None


# Post-tap reads: an unclassified frame counts toward the limit only after the
# owned or still-locked forms fail. The scan cap bounds a slow settle.
_UNLOCK_STRIKES = 3
_UNLOCK_SCANS = 8


def _inside(point: tuple[int, int], tile: tuple[int, int, int, int]) -> bool:
    x, y, width, height = tile
    return x <= point[0] < x + width and y <= point[1] < y + height


def _slot_status(home: LabHomeReading | None, reading: LabsReading | None) -> tuple[tuple[int, str], ...]:
    """Slots 2-5 this visit proved owned or locked. Nothing when the strip was not read."""
    if home is None or reading is None or not reading.strip_read() or reading.slots_owned is None:
        return ()
    owned = reading.slots_owned
    status = [(slot, "owned") for slot in range(2, owned + 1)]
    if home.next_locked is not None and home.next_locked.slot == owned + 1:
        status.append((owned + 1, "locked"))
    return tuple(status)


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
        rollout: LabUnlockRollout | None = None,
        worker: str | None = None,
        evidence_dir: Path | None = None,
    ) -> None:
        self.templates = templates
        self.home_reader = home_reader
        self.picker_reader = picker_reader
        self.confirmation_reader = confirmation_reader
        self.slot_observer = slot_observer
        self.wall_clock = wall_clock
        self.journal, self.account_state, self.runtime = journal, account_state, runtime
        self.authorize, self.event_sink = authorize, event_sink
        self.rollout, self.worker, self.evidence_dir = rollout, worker, evidence_dir
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
        self._home_seen: tuple[LabHomeReading, LabsReading | None] | None = None
        self._unlock_signature: tuple[LockedSlot, int] | None = None
        self._unlock_reads = 0
        self._unlock_done = False
        self._unlock_tap: tuple[str, int] | None = None
        self._unlock_frames: list[Image] = []
        self._unlock_scans = 0
        self._unlock_strikes = 0
        self._unlanded_signature: tuple[int, LockedSlot, int] | None = None
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
        self._home_seen = None
        self._unlock_signature = None
        self._unlock_reads = 0
        self._unlock_done = False
        self._unlock_tap = None
        self._unlock_frames = []
        self._unlock_scans = 0
        self._unlock_strikes = 0
        self._unlanded_signature = None
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

    def unlock_allowed(self, slot: int | None, account_id: str | None) -> bool:
        """The rollout lets this worker tap `slot` while it plays `account_id`.

        Fleet stage, or this worker's own canary on the account it was promoted
        on. A canary since reassigned to another account never taps.
        """
        if (self.rollout is None or self.worker is None or account_id is None
                or lab_catalog.lab_slot_gems(slot) is None):
            return False
        state = self.rollout.slot(slot)
        return state.stage == 'fleet' or (state.stage == 'canary' and state.canary_worker == self.worker
                                          and state.canary_account == account_id)

    def _prepare(self, operation: str, wallet: int, price: int, *,
                 unlock_slot: int | None = None) -> transactions.Transaction | None:
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
        elif (unlock_slot is None or unlock_slot not in self._options.unlock_slots
                or price != lab_catalog.lab_slot_gems(unlock_slot)
                or not self.unlock_allowed(unlock_slot, scope.account_id)
                or not self._reading.strip_read() or self._reading.slots_owned != unlock_slot - 1
                or self.runtime.snapshot().slots_owned != unlock_slot - 1):
            return None
        currency = 'coins' if operation == 'lab_start' else 'gems'
        balance = BalanceInterval.from_reading(currency, wallet, scope, self._capture_at,
                                               self._reading.frame_digest)
        self.account_state.observe_balance(balance)
        unlock = operation == 'lab_unlock'
        decision = LabDecision('unlock_slot', price=price, slot=unlock_slot) if unlock else self._purchase
        if self.authorize is not None and not self.authorize(operation, decision, now):
            return None
        from concepts import REGISTRY
        intent = transactions.Intent(item=f'Lab {unlock_slot}' if unlock else REGISTRY.by_id(research_id).name,
            category='LABS', currency=currency, price=price, wallet_before=wallet,
            ts=now, operation=operation, before={
                'slot': unlock_slot if unlock else selected_slot,
                'research_id': None if unlock else research_id,
                'source_level': None if unlock else target-1,
                'target_level': None if unlock else target,
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
                if self.runtime.snapshot().slots_owned >= txn.before['slot']:
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
                        slot_status=_slot_status(home, self._reading), unlocked_slot=txn.before['slot'],
                        gem_balance=home.gem_balance, gems_before=txn.wallet_before,
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
        if self._home_seen is not None:
            home, reading = self._home_seen
            if not outcome.slot_status:
                outcome = replace(outcome, slot_status=_slot_status(home, reading))
            if outcome.observed_gem_spend == 0 and outcome.gem_balance is None:
                outcome = replace(outcome, gem_balance=home.gem_balance)
        self._outcome = outcome
        self._state = "return"

    def _emit(self, event: events.Event) -> None:
        if self.event_sink is not None:
            self.event_sink(event)

    def _publish_change(self, change: RolloutChange) -> None:
        if change.promoted:
            self._emit(events.LabUnlockPromoted(slot=change.slot, stage=change.promoted))
        if change.halted:
            self._emit(events.LabUnlockHalted(slot=change.slot, reason=change.after.halted_reason or ""))

    def _skip(self, slot: int, reason: str) -> None:
        self._emit(events.Skipped(action='lab_unlock', reason=reason, detail=f'Lab {slot}'))

    def _unlock_slot(self, home: LabHomeReading, screen: Image, device: AdbDevice) -> bool:
        """On the way out, rehearse or unlock the gem lane's next slot once per visit.

        True keeps the visit on Labs this scan: a first read, or a tap that needs
        its post-tap reads. The rollout record decides between rehearsal, tap and skip.
        Only the first locked tile on screen is acted on, and only when the gem
        lane names that same slot; a stale slot record never moves the target.
        """
        locked, scope = home.next_locked, self._scope()
        if (self._unlock_done or self.rollout is None or self.worker is None or scope is None
                or locked is None or locked.slot not in self._options.unlock_slots):
            return False
        catalog = lab_catalog.lab_slot_gems(locked.slot)
        if catalog is None:
            self._unlock_done = True
            self._skip(locked.slot, 'price_unknown')
            return False
        reading, gems = self._reading, home.gem_balance
        # An abbreviated header ("1.4K") may stand for less than it reads, so
        # only the least balance it can mean pays for the price and the reserve.
        if (locked.price is None or locked.point is None or locked.tile is None
                or not _inside(locked.point, locked.tile) or gems is None
                or gems - transactions.abbreviation_slack(gems) < catalog + self._options.keep_gems
                or reading is None or not reading.strip_read()
                or reading.slots_owned != locked.slot - 1):
            return False
        signature = (locked, gems)
        if signature != self._unlock_signature:
            self._unlock_signature, self._unlock_reads = signature, 1
            return True
        self._unlock_reads += 1
        if self._unlock_reads < 2:
            return True
        self._unlock_done = True
        slot, price = locked.slot, locked.price
        state = self.rollout.slot(slot)
        if state.stage == 'canary' and state.canary_worker != self.worker:
            state = self.rollout.release_absent_canary(slot).after
        elif state.stage == 'canary' and state.canary_account != scope.account_id:
            # This worker was promoted on another account: back to dry run, no tap.
            state = self.rollout.release_canary(slot, expected_worker=self.worker).after
        if state.stage == 'halted':
            self._skip(slot, 'slot_halted')
            return False
        if price != catalog:
            self._publish_change(
                self.rollout.note_dry_run(slot, self.worker, price, gems, self.wall_clock(),
                                          account_id=scope.account_id)
                if state.stage == 'dry_run' else
                self.rollout.halt(slot, f"Slot {slot} read {price} gems; the catalog says {catalog}"))
            return False
        if self.unlock_allowed(slot, scope.account_id):
            return self._tap_unlock(locked, gems, screen, device)
        if state.stage == 'dry_run':
            change = self.rollout.note_dry_run(slot, self.worker, price, gems, self.wall_clock(),
                                               account_id=scope.account_id)
            if change.before.stage == 'dry_run' and not change.halted:
                self._emit(events.LabUnlockRehearsed(slot=slot, price=price, gems=gems))
            self._publish_change(change)
            return False
        self._skip(slot, 'waiting_for_canary')
        return False

    def _tap_unlock(self, locked: LockedSlot, gems: int, screen: Image, device: AdbDevice) -> bool:
        assert locked.price is not None and locked.point is not None
        txn = self._prepare('lab_unlock', gems, locked.price, unlock_slot=locked.slot)
        if txn is None:
            self.recovery_status = 'lab_preparation_refused'
            return False
        self._unlock_tap = (txn.key, locked.slot)
        self._unlock_frames = [screen]
        self._unlock_scans = self._unlock_strikes = 0
        self._unlanded_signature = None
        self._tap(device, locked.point, f"unlock_lab_slot_{locked.slot}")
        self._state = "confirm_slot"
        return True

    def _unlock_evidence(self, txn: transactions.Transaction, home: LabHomeReading,
                         scope: FactScope, changed: bool) -> transactions.RecoveryEvidence:
        assert self._reading is not None
        return transactions.RecoveryEvidence(category='LABS', currency='gems',
            wallet_after=home.gem_balance, effect_changed=changed, observed_at=self._capture_at,
            frame_digest=self._reading.frame_digest, scope=scope, operation='lab_unlock',
            slot=txn.before['slot'])

    def _settle_own_unlock(self, txn: transactions.Transaction, home: LabHomeReading,
                           screen: Image) -> LabVisitResult | None:
        """Sort this visit's own unlock tap into bought, not landed, or uncertain.

        Bought: slot N confirmed owned since the tap and the gems down by the price.
        Not landed: two matching Labs home reads, taken after the tap, with slot N
        still the first locked tile at its price and the gems unchanged. Anything
        else is a strike; three strikes, or eight post-tap scans, is uncertain.
        """
        assert self._unlock_tap is not None
        key, slot = self._unlock_tap
        if len(self._unlock_frames) < _UNLOCK_SCANS + 1:
            self._unlock_frames.append(screen)
        self._unlock_scans += 1
        scope, reading = self._scope(), self._reading
        strip = (scope is not None and reading is not None and home.page and reading.strip_read())
        locked = home.next_locked
        if strip and reading.slots_owned >= slot:
            snapshot = self.runtime.snapshot()
            record = snapshot.slots[slot - 1]
            current = (record.confirmed and record.observed_at == self._capture_at
                       and record.started_observed_at is not None and txn.acted_at is not None
                       and record.started_observed_at > txn.acted_at
                       and snapshot.slots_owned is not None and snapshot.slots_owned >= slot)
            if current and home.gem_balance is not None:
                outcome = self.journal.reconcile(key, self._unlock_evidence(txn, home, scope, True),
                                                 now=self.wall_clock())
                if outcome.verdict == transactions.Verdict.BOUGHT and outcome.spent == txn.price:
                    return self._unlock_bought(txn, home, outcome)
                return self._unlock_uncertain(txn, slot, 'gem debit did not match the price')
        elif (strip and reading.slots_owned == slot - 1 and locked is not None
              and locked.slot == slot and locked.price == txn.price
              and home.gem_balance is not None and home.gem_balance == txn.wallet_before):
            signature = (reading.slots_owned, locked, home.gem_balance)
            if signature == self._unlanded_signature:
                outcome = self.journal.refute_unlanded_unlock(
                    key, self._unlock_evidence(txn, home, scope, False), now=self.wall_clock())
                if outcome.verdict == transactions.Verdict.REFUTED:
                    return self._unlock_missed(txn, home)
            self._unlanded_signature = signature
        else:
            self._unlock_strikes += 1
            self._unlanded_signature = None
        if self._unlock_strikes >= _UNLOCK_STRIKES or self._unlock_scans >= _UNLOCK_SCANS:
            return self._unlock_uncertain(txn, slot, 'post-tap screen was not understood')
        return None

    def _unlock_bought(self, txn: transactions.Transaction, home: LabHomeReading,
                       outcome: transactions.Outcome) -> None:
        slot = txn.before['slot']
        self._restore_receipts()  # publishes LabSlotUnlocked with the journal's values
        self._publish_change(self.rollout.note_unlock(slot, self.worker, txn.key, 'bought',
                                                      at=self.wall_clock()))
        self._unlock_tap = None
        self.recovery_status = 'settled'
        self._return(LabVisitResult('observed', 'slot_unlocked', LabDecision('unknown'),
            slot_status=_slot_status(home, self._reading), gem_balance=home.gem_balance,
            gems_before=txn.wallet_before, observed_gem_spend=outcome.spent,
            unlock_transaction_key=txn.key, unlocked_slot=slot))
        return None

    def _unlock_missed(self, txn: transactions.Transaction, home: LabHomeReading) -> None:
        self._restore_receipts()
        self._publish_change(self.rollout.note_unlock(txn.before['slot'], self.worker, txn.key,
                                                      'not_charged', at=self.wall_clock()))
        self._unlock_tap = None
        self._return(LabVisitResult('observed', 'unlock_not_landed', LabDecision('unknown'),
            slot_status=_slot_status(home, self._reading), gem_balance=home.gem_balance))
        return None

    def _save_unlock_evidence(self, slot: int, txn: transactions.Transaction) -> tuple[str, ...]:
        """The frames from the tap onward, as evidence/lab-unlock-slot<N>-<ts>-<k>.png."""
        if self.evidence_dir is None:
            return ()
        import cv2
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(txn.acted_at if txn.acted_at is not None else self.wall_clock())
        saved = []
        for index, image in enumerate(self._unlock_frames):
            path = self.evidence_dir / f"lab-unlock-slot{slot}-{stamp}-{index}.png"
            if cv2.imwrite(str(path), image):
                saved.append(str(path))
        return tuple(saved)

    def _unlock_uncertain(self, txn: transactions.Transaction, slot: int,
                          reason: str) -> LabVisitResult:
        """Keep the transaction open (the worker's read-only hold) and halt a canary's slot."""
        evidence = self._save_unlock_evidence(slot, txn)
        state = self.rollout.slot(slot)
        if state.stage == 'canary' and state.canary_worker == self.worker:
            self._publish_change(self.rollout.halt(slot, reason, evidence))
        self._unlock_tap = None
        self.recovery_status = 'lab_unlock_uncertain'
        return self._finish(LabVisitResult('failed', 'lab_unlock_uncertain', LabDecision('unknown'),
                                           gems_before=txn.wallet_before))

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
            pending = self.pending_transaction
            if self._unlock_tap is not None and pending is not None and pending.key == self._unlock_tap[0]:
                return self._unlock_uncertain(pending, self._unlock_tap[1],
                                              'the visit timed out before the tap settled')
            if pending is not None:
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
        if home.page:
            self._home_seen = (home, self._reading)
        pending = self.pending_transaction
        if pending is not None:
            if self._unlock_tap is not None and pending.key == self._unlock_tap[0]:
                return self._settle_own_unlock(pending, home, screen)
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

        if self._state in {"confirm", "confirm_slot"}:
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
                if self._unlock_slot(home, screen, device):
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
