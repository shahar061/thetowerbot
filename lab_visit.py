"""Bounded, route-gated Lab execution using the shared purchase authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from pathlib import Path
import time
from typing import Callable, ClassVar, Iterable

from account_state import AccountState
from evidence_scope import BalanceInterval, FactScope
from lab_runtime import LabRuntime, LabScope, _catalog_revision
import events
import lab_catalog
import transactions

from device import AdbDevice, Image, tap
from geometry import anchored_point, supported_frame
from lab_plan import LabDecision, LabVisitOptions, decide
from fleet.resource_blocks import LabAction
from lab_picker import PickerSearch, SWIPE_SECONDS
from lab_screen import (LabConfirmationReading, LabHomeReading, LabPickerReading, LockedSlot, PickerCard,
                        read_confirmation, read_gem_unlock_confirmation, read_home, read_picker, read_slots,
                        read_picker_page, read_selected_home, read_selected_picker,
                        read_repeat_controls)
import ocr
import pages
import vision
from labs import LabJob, LabsReading
from lab_starter_rollout import LabStarterRollout, StarterChange, StarterGate, start_key, starter_gate
from lab_unlock_rollout import LabUnlockRollout, RolloutChange

logger = logging.getLogger(__name__)

# Body lines unique to the first-visit LABS info popup, whitespace removed.
_INTRO_LINES = ("LABSGRANTYOU", "GEMRUSHEFFICIENCY")
_NEW_RESEARCH_LINES = ("NEWRESEARCHES", "NEWRESEARCHESAVAILABLE")


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
    # Slots proven started in this visit, in order; direct start fills several.
    started_slots: tuple[int, ...] = ()
    # The research each proven start began, in the same order as started_slots.
    started_research: tuple[str, ...] = ()
    # (research, coins) for each proven start, in the same order: the journal-proven
    # spend, else the planned price. The lab coin jar is debited once per start.
    started_spends: tuple[tuple[str, int], ...] = ()
    # (level, completes_at) of a Game Speed start in this visit, even when it was
    # not the last start: the slot-1 cadence must record it running.
    game_speed_start: tuple[int | None, float | None] | None = None
    # The visit re-planned from a fresh strip after its last proven start.
    replanned: bool = False
    # A later attempt that ended after a proven start; the started result is kept.
    attempt: LabVisitResult | None = None


# Post-tap reads: an unclassified frame counts toward the limit only after the
# owned or still-locked forms fail. The scan cap bounds a slow settle.
_UNLOCK_STRIKES = 3
_UNLOCK_SCANS = 8
# A start tap that has neither proven nor refuted itself after this many scans
# is uncertain: the hold stays and a canary halts its slot.
_START_SCANS = 8
# Scan counts are a proxy for time, and between games scans run ~3x faster
# (config.MENU_FAST_SCAN_SECONDS), so each count limit also needs this much of
# the visit's own clock: since the start tap, and since an 8-scan stage or the
# 24-scan picker search began. At the ~2 s menu interval the count binds.
LAB_START_PROOF_MIN_SECONDS = 20.0
# The same floor for a gem unlock tap: its strike and scan limits (and the
# confirmation-remained limit) also need this much of the visit's clock since the tap.
LAB_UNLOCK_PROOF_MIN_SECONDS = 20.0
LAB_STAGE_MIN_SECONDS = 16.0
LAB_SEARCH_STAGE_MIN_SECONDS = 40.0
# Picker-search frames kept as a miss's evidence: the first and the last three.
_SEARCH_FRAMES_KEPT = 4


def _picker_unknown_reason(card: PickerCard | None) -> str:
    """Why a picker frame gave no decision for its research, for the log."""
    if card is None:
        return 'card missing'
    if not card.fully_visible:
        return 'card clipped'
    if card.price is None and not card.maxed:
        return 'price unread'
    return 'entry unread'


def _squash(name: str | None) -> str | None:
    """A read name with its whitespace removed, for comparing two OCR reads of one card."""
    return "".join(name.split()) if name is not None else None


def _inside(point: tuple[int, int], tile: tuple[int, int, int, int]) -> bool:
    x, y, width, height = tile
    return x <= point[0] < x + width and y <= point[1] < y + height


def _debit_mismatch(txn: transactions.Transaction, gems_after: int) -> bool:
    """The gems moved by an amount the header's rounding cannot make either nothing or the price.

    An unchanged or lagging header is not a mismatch: the debit may still show.
    """
    assert txn.wallet_before is not None and txn.price is not None
    drop = txn.wallet_before - gems_after
    slack = transactions.reading_tolerance(txn.wallet_before, gems_after)
    return abs(drop) > slack and abs(drop - txn.price) > slack


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
        starter: LabStarterRollout | None = None,
        plan_action: Callable[[float], LabAction | None] | None = None,
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
        self.starter = starter
        self.plan_action = plan_action
        self._canary_sweep_at = float('-inf')
        self._boxes: tuple[ocr.TextBox, ...] = ()
        self._rehearsing = False
        self._search: PickerSearch | None = None
        self._search_frames: list[Image] = []
        # Picker frames of one start (the first and the last three), saved on a stage timeout.
        self._picker_frames: list[Image] = []
        self._picker_unknown_logged = False
        self._start_tap: tuple[str, int] | None = None
        self._start_frames: list[Image] = []
        self._start_scans = 0
        # The visit clock (advance's `now`) at this start's Research tap.
        self._start_tapped_at = 0.
        self._unsafe_tap = False
        self._picker_seconds: float | None = None
        # (transaction key, coin read) of the last _recover read that saw the
        # started slot still idle; a second matching read refutes the start.
        self._idle_refute: tuple[str, int] | None = None
        self._capture_at = 0.
        self._capture_scope: FactScope | None = None
        self._reading: LabsReading | None = None
        self._emitted: set[str] = set()
        self.recovery_status: str | None = None
        # Why the last _prepare returned None; one short code per refusal path.
        self.preparation_refusal: str | None = None
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
        self._owned_canary_checked = False
        self._unlock_tap: tuple[str, int] | None = None
        self._unlock_frames: list[Image] = []
        self._unlock_scans = 0
        self._unlock_strikes = 0
        # The visit clock (advance's `now`) at this unlock's gem tap.
        self._unlock_tapped_at = 0.
        self._debit_strike = False
        self._unlanded_signature: tuple[int, LockedSlot, int] | None = None
        self._unlock_dialog_signature: tuple[int, int, tuple[int, int]] | None = None
        self._unlock_dialog_reads = 0
        self._unlock_confirmation_tapped = False
        self._options = LabVisitOptions()
        self.selected_action: LabAction | None = None
        self.pending_action: LabAction | None = None
        self._stage_name = 'idle'
        self._stage_scans = 0
        self._stage_started = 0.
        self.last_tap: tuple[str, int, int] | None = None
        self._repeat_seen: tuple | None = None
        self._repeat_pending: tuple | None = None
        self._repeat_reads = 0
        self._repeat_scans = 0
        self._repeat_at = float('-inf')
        self._repeat_failed = False

    # One start's state, with the values request() gives a fresh visit. Between
    # starts _next_start clears exactly these; the visit's scan and time budget,
    # unlock state, options and journal carry over.
    _PER_START_DEFAULTS: ClassVar[dict[str, object]] = {
        'selected_action': None, 'pending_action': None, '_slot': None, '_purchase': None,
        '_search': None, '_search_frames': [], '_picker_frames': [],
        '_picker_signature': None, '_picker_reads': 0, '_dialog_signature': None, '_dialog_reads': 0,
        '_unavailable_signature': None, '_unavailable_reads': 0, '_picker_name': None,
        '_mismatch_signature': None,
        '_picker_seconds': None,
        '_start_tap': None, '_start_scans': 0, '_start_tapped_at': 0., '_start_frames': [],
        '_rehearsing': False,
    }
    _PER_START_FIELDS: ClassVar[tuple[str, ...]] = tuple(_PER_START_DEFAULTS)

    @property
    def active(self) -> bool:
        return self._state != "idle"

    def _reset_start(self) -> None:
        """Clear one start's state; a list default is copied, never shared."""
        for name in self._PER_START_FIELDS:
            default = self._PER_START_DEFAULTS[name]
            setattr(self, name, list(default) if isinstance(default, list) else default)

    def request(self, action: LabAction | LabVisitOptions | None = None, *,
                options: LabVisitOptions | None = None) -> bool:
        if self.active:
            return False
        if isinstance(action, LabVisitOptions):
            options, action = action, None
        if action is not None:
            if (action.operation not in ('start', 'rehearse') or type(action.slot) is not int
                    or action.slot not in range(1, 6) or type(action.target_level) is not int
                    or action.target_level < 1 or type(action.strategy_revision) is not int):
                self.recovery_status = 'invalid_lab_action'
                return False
            self.pending_action = action
            self.release_stale_canaries((action.slot,))
            gate = self.gate(action.slot, action.research, options=options)
            if gate.mode == 'blocked' or (action.operation == 'start' and not gate.enabled):
                self.recovery_status = 'starter_gate_refused'
                return False
        self._reset_start()
        self.selected_action = self.pending_action = action
        self._rehearsing = action is not None and action.operation == 'rehearse'
        self._options = options or LabVisitOptions()
        self._repeat_seen = self._repeat_pending = None
        self._repeat_reads = self._repeat_scans = 0
        self._repeat_at = float('-inf')
        self._repeat_failed = False
        self._stage_name, self._stage_scans, self._stage_started = 'idle', 0, 0.
        self._state = "open"
        self._started_at = 0.
        self._scans = 0
        self._outcome = None
        self._home_seen = None
        self._unlock_signature = None
        self._unlock_reads = 0
        self._unlock_done = False
        self._owned_canary_checked = False
        self._unlock_tap = None
        self._unlock_frames = []
        self._unlock_scans = 0
        self._unlock_strikes = 0
        self._unlock_tapped_at = 0.
        self._unlanded_signature = None
        self._unlock_dialog_signature = None
        self._unlock_dialog_reads = 0
        self._unlock_confirmation_tapped = False
        self._boxes = ()
        self._unsafe_tap = False
        self._idle_refute = None
        self.last_tap = None
        self.recovery_status = None
        self.preparation_refusal = None
        return True

    def release_stale_canaries(self, slots: Iterable[int] = range(1, 6)) -> None:
        """Free a start canary that can no longer prove its slot, as the unlock path does.

        Another worker's canary is released once it left the pool or plays another
        account; this worker's own canary only when promoted on another account.
        Legacy Game Speed never reaches the rollout, so slot 1 is safe to include.
        """
        if self.starter is None or self.worker is None:
            return
        state, account = self.starter.state(), self._account()
        for slot in slots:
            key = start_key(slot)
            record = state.rollout(key)
            if record.stage != 'canary':
                continue
            if record.canary_worker != self.worker:
                change = self.starter.release_absent_canary(key)
            elif account is not None and record.canary_account != account:
                change = self.starter.release_canary(key, expected_worker=self.worker,
                                                     expected_account=record.canary_account)
            else:
                continue
            if change.before != change.after:
                logger.info("Lab start rollout %s: canary %s on %s released to dry run", key,
                            record.canary_worker, record.canary_account)

    def sweep_stale_canaries(self, now: float) -> None:
        """release_stale_canaries for every slot, at most once a minute (the planner's scan path).

        A blocked gate plans no action for the slot, so without this sweep no
        request() would ever release a canary that left the pool.
        """
        if now < self._canary_sweep_at:
            return
        self._canary_sweep_at = now + 60.
        self.release_stale_canaries()

    def gate(self, slot: int, research: str, *, options: LabVisitOptions | None = None) -> StarterGate:
        """This worker's gate right now. The file is re-read on every call."""
        state = self.starter.state() if self.starter is not None else None
        return starter_gate(state, slot, research, self.worker, self._account(),
                            direct_start=(options or self._options).direct_start)

    def _account(self) -> str | None:
        scope = self.account_state.verified_scope if self.account_state else None
        return scope.account_id if scope is not None else None

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
        return self._scope_check()[0]

    def _scope_check(self) -> tuple[FactScope | None, str | None]:
        """The verified scope of the current capture, or why there is none."""
        scope = self.account_state.verified_scope if self.account_state else None
        if scope is None:
            return None, 'scope_unverified'
        if scope != self._capture_scope:
            return None, 'capture_scope_mismatch'
        if (self.runtime is None or self.runtime.scope
                != LabScope(scope.account_id, scope.lease_id, scope.generation, scope.epoch)):
            return None, 'runtime_scope_mismatch'
        if not self.account_state.accepts_capture(scope, self._capture_at):
            return None, 'capture_rejected'
        if not 0 <= self.wall_clock()-self._capture_at <= 30:
            return None, 'capture_stale'
        return scope, None

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

    def _refuse(self, operation: str, reason: str, detail: str = '') -> None:
        self.preparation_refusal = reason
        logger.warning("%s preparation refused: %s%s", operation, reason, f" ({detail})" if detail else "")

    def _prepare(self, operation: str, wallet: int, price: int, *,
                 unlock_slot: int | None = None) -> transactions.Transaction | None:
        self.preparation_refusal = None
        scope, problem = self._scope_check()
        now = self.wall_clock()
        if scope is None:
            return self._refuse(operation, problem or 'scope_unverified',
                                f"capture {now - self._capture_at:.1f}s old")
        if self.journal is None:
            return self._refuse(operation, 'journal_unavailable')
        if self._reading is None:
            return self._refuse(operation, 'slots_unread')
        if self.account_state.safety_path != self.journal.path:
            return self._refuse(operation, 'safety_path_mismatch')
        purchase = self._purchase
        target = purchase.target_level if purchase is not None else None
        selected_slot = purchase.slot if purchase is not None else 1
        research_id = purchase.research_id if purchase is not None else 'labs.game-speed'
        if operation == 'lab_start':
            if self._rehearsing or not self.gate(selected_slot, research_id).enabled:
                return self._refuse(operation, 'starter_gate_refused', f"Lab {selected_slot} {research_id}")
            slot = self.runtime.snapshot().slots[selected_slot-1]
            if type(target) is not int or target < 1:
                return self._refuse(operation, 'target_level_invalid', f"target {target!r}")
            if not slot.confirmed:
                return self._refuse(operation, 'slot_unconfirmed', f"Lab {selected_slot}")
            if slot.state != 'idle':
                return self._refuse(operation, 'slot_not_idle', f"Lab {selected_slot} {slot.state}")
            if slot.observed_at is None or not 0 <= now-slot.observed_at <= 30:
                return self._refuse(operation, 'slot_snapshot_stale', f"Lab {selected_slot} read "
                    + ("never" if slot.observed_at is None else f"{now - slot.observed_at:.1f}s ago"))
        elif unlock_slot is None or unlock_slot not in self._options.unlock_slots:
            return self._refuse(operation, 'unlock_slot_not_requested', f"Lab {unlock_slot}")
        elif price != lab_catalog.lab_slot_gems(unlock_slot):
            return self._refuse(operation, 'unlock_price_mismatch', f"Lab {unlock_slot} {price} gems")
        elif not self.unlock_allowed(unlock_slot, scope.account_id):
            return self._refuse(operation, 'unlock_not_allowed', f"Lab {unlock_slot}")
        elif not self._reading.strip_read():
            return self._refuse(operation, 'slot_strip_unread')
        elif (self._reading.slots_owned != unlock_slot - 1
                or self.runtime.snapshot().slots_owned != unlock_slot - 1):
            return self._refuse(operation, 'slots_owned_mismatch', f"read {self._reading.slots_owned}, "
                                f"runtime {self.runtime.snapshot().slots_owned}, Lab {unlock_slot}")
        currency = 'coins' if operation == 'lab_start' else 'gems'
        balance = BalanceInterval.from_reading(currency, wallet, scope, self._capture_at,
                                               self._reading.frame_digest)
        self.account_state.observe_balance(balance)
        unlock = operation == 'lab_unlock'
        decision = LabDecision('unlock_slot', price=price, slot=unlock_slot) if unlock else self._purchase
        if self.authorize is not None and not self.authorize(operation, decision, now):
            return self._refuse(operation, 'authorize_refused')
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
            return self._refuse(operation, 'transaction_in_flight')
        if txn is None:
            return self._refuse(operation, 'journal_refused')
        if txn.stage != transactions.Stage.INTENDED:
            return self._refuse(operation, 'transaction_not_intended', str(txn.stage))
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
        # Any read that does not qualify below breaks the run of idle reads.
        prior_idle, self._idle_refute = self._idle_refute, None
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
            if txn.operation == 'lab_start':
                if (current and record.state == 'researching'
                        and record.research_id == txn.before.get('research_id')
                        and record.target_level == txn.before.get('target_level')):
                    effect = True
                elif (record.confirmed and record.observed_at == self._capture_at and record.state == 'idle'
                        and self._capture_at - boundary >= transactions.UNLANDED_SETTLE_SECONDS
                        and home.coin_balance is not None):
                    # The runtime keeps an idle slot's old start time, so an unlanded
                    # tap is refuted by two consecutive idle reads with one wallet,
                    # the same rule as an unlanded unlock.
                    signature = (txn.key, home.coin_balance)
                    if prior_idle == signature:
                        effect = False
                    self._idle_refute = signature
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
            outcome = None
            if txn.operation == 'lab_start' and effect is False:
                outcome = self.journal.refute_unlanded_start(txn.key, proof, now=self.wall_clock())
            if outcome is None or outcome.verdict != transactions.Verdict.REFUTED:
                outcome = self.journal.reconcile(txn.key, proof, now=self.wall_clock())
            if outcome.verdict in (transactions.Verdict.BOUGHT, transactions.Verdict.FREE):
                self._restore_receipts()
                if txn.operation == 'lab_start':
                    self._purchase = LabDecision('start', txn.price, txn.wallet_before,
                                                game_speed_level=txn.before['target_level'],
                                                slot=txn.before['slot'], research_id=txn.before['research_id'])
                    job = next((j for j in self._reading.jobs if j.slot == txn.before['slot']), None)
                    self.pending_action = None
                    self._note_start(txn, 'bought')
                    self._start_tap = None
                    reason = 'game_speed_confirmed' if txn.before['research_id'] == 'labs.game-speed' else 'research_confirmed'
                    self._next_start(LabVisitResult('started', reason, self._purchase,
                        confirmed_job=job, observed_coin_spend=outcome.spent, transaction_key=txn.key))
                else:
                    # A canary's tap settled on a later visit (the tapping one was
                    # cancelled or restarted) still promotes the slot.
                    self._note_unlock(txn, 'bought')
                    self._return(self._unlock_outcome('observed', 'slot_unlocked',
                        slot_status=_slot_status(home, self._reading), unlocked_slot=txn.before['slot'],
                        gem_balance=home.gem_balance, gems_before=txn.wallet_before,
                        observed_gem_spend=outcome.spent, unlock_transaction_key=txn.key))
                self.recovery_status = 'settled'
            elif outcome.verdict == transactions.Verdict.REFUTED:
                self._restore_receipts()
                self._note_start(txn, 'not_charged')
                self._start_tap = None
                self._return(LabVisitResult('observed', 'unclaimed_dispatch_refuted', LabDecision(
                    'unknown', slot=txn.before['slot'], research_id=txn.before['research_id'])))
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
            point = self._unlocked_tab_match(screen)
            if point is not None:
                self._tap(device, point, 'inspect_pending_lab')
        else:
            self.recovery_status = 'lab_reconciliation_route_unavailable'

    def tab_unlocked(self, screen: Image) -> bool:
        """Return whether the actionable Labs tab is visible on this frame."""
        return self.tab_status(screen) == "unlocked"

    def tab_status(self, screen: Image) -> str:
        """Read Labs as locked, unlocked, or unknown without tapping it."""
        unlocked = self._unlocked_tab_match(screen) is not None
        locked = self._locked_tab_match(screen)
        if unlocked == locked:
            return "unknown"
        return "unlocked" if unlocked else "locked"

    def _unlocked_tab_match(self, screen: Image) -> tuple[int, int] | None:
        """Match the flask's rim and neck, above the game's New! overlay.

        The completion badge sits above the icon. Both badges leave its top
        35 pixels intact. Restrict this smaller shape to the known Labs tab
        so a similar detail elsewhere cannot authorize navigation.
        """
        height, width = screen.shape[:2]
        template = self.templates.get("nav/tab_labs.png")
        template_height, template_width = template.shape[:2]
        if not supported_frame(width, height):
            return None
        x, y = anchored_point(screen, (765, 2264), 'bottom')
        region = screen[y:y + template_height, x:x + template_width]
        if region.shape[:2] != (template_height, template_width):
            return None
        if vision.locate_template(region[:35], template[:35], .94) is None:
            return None
        return x + template_width // 2, y + template_height // 2

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

    def _safe(self, point: tuple[int, int]) -> bool:
        """False on a Rush button or the gem price under it: those spend gems."""
        for box in self._boxes:
            if "".join(box.text.upper().split()) == "RUSH":
                x, y, w, h = box.rect
                if x - 30 <= point[0] <= x + w + 30 and y - 30 <= point[1] <= y + 4 * h:
                    return False
        return True

    def _tap(self, device: AdbDevice, point: tuple[int, int], action: str) -> None:
        if not self._safe(point):
            logger.warning("Refused lab tap %s at %s: on a Rush control", action, point)
            self._unsafe_tap = True
            return
        tap(device, *point)
        self.last_tap = (action, *point)

    def _finish(self, outcome: LabVisitResult) -> LabVisitResult:
        prior = self._outcome
        if prior is not None and prior.started_slots and not outcome.started_slots:
            # A failure after proven starts still reports which slots started.
            outcome = replace(outcome, started_slots=prior.started_slots,
                              started_research=prior.started_research,
                              started_spends=prior.started_spends,
                              game_speed_start=prior.game_speed_start,
                              replanned=prior.replanned)
        self._state = "idle"
        self._outcome = outcome
        return outcome

    def _reconcile_repeat(self, screen: Image, device: AdbDevice) -> bool:
        """Read twice, set once, verify twice; an uncertain toggle is never retried."""
        desired = self._options.native_repeat
        if desired == 'unchanged' or self._repeat_failed:
            return False
        if self._capture_at <= self._repeat_at:
            return True
        self._repeat_at = self._capture_at
        controls = read_repeat_controls(screen, self._boxes)
        jobs = {job.slot: job for job in self._reading.jobs} if self._reading is not None else {}
        if self._repeat_pending is not None:
            slot, research, level, point = self._repeat_pending
            job = jobs.get(slot)
            observed = next((c for c in controls if c.slot == slot and c.point == point), None)
            same = job is not None and job.concept_id == research and job.target_level == level
            self._repeat_scans += 1
            self._repeat_reads = self._repeat_reads + 1 if same and observed is not None and observed.state == desired else 0
            if self._repeat_reads >= 2:
                self._repeat_pending = self._repeat_seen = None
                self._repeat_reads = 0
                self.recovery_status = 'lab_repeat_verified'
            elif self._repeat_scans >= 6:
                self._repeat_failed = True
                self.recovery_status = 'lab_repeat_unverified'
                logger.warning("Lab %s auto research %s was not verified; no repeat tap", slot, desired)
                return False
            else:
                return True
        for control in controls:
            job = jobs.get(control.slot)
            if (control.state == desired or job is None or job.status != 'researching'
                    or job.concept_id is None or job.target_level is None):
                continue
            key = (control.slot, job.concept_id, job.target_level, control.point)
            signature = (*key, control.state)
            if self._repeat_seen != signature:
                self._repeat_seen = signature
                return True
            decision = LabDecision(desired, slot=control.slot, research_id=job.concept_id,
                                   game_speed_level=job.target_level)
            if (self._scope() is None or self.authorize is None
                    or not self.authorize('lab_repeat', decision, self.wall_clock())):
                self._repeat_failed = True
                self.recovery_status = 'lab_repeat_authorization_refused'
                return False
            self._tap(device, control.point, f'lab_repeat_{desired}_{control.slot}')
            self._repeat_pending = key
            self._repeat_reads = self._repeat_scans = 0
            return True
        self._repeat_seen = None
        return False

    def _return(self, outcome: LabVisitResult) -> None:
        if self._home_seen is not None:
            home, reading = self._home_seen
            if not outcome.slot_status:
                outcome = replace(outcome, slot_status=_slot_status(home, reading))
            if outcome.observed_gem_spend == 0 and outcome.gem_balance is None:
                outcome = replace(outcome, gem_balance=home.gem_balance)
        self._outcome = outcome
        self._state = "return"

    def _next_start(self, outcome: LabVisitResult) -> None:
        """After a proven start, fill the next idle slot under direct start, else return.

        It runs only once the journal settled the previous start, so one
        transaction is open at a time; the planner hook re-plans from the next
        fresh, complete strip read, with every per-start check applied again.
        """
        slot, research = outcome.decision.slot, outcome.decision.research_id
        prior = self._outcome
        slots = (prior.started_slots if prior is not None else ()) + (
            (slot,) if slot is not None else ())
        started = (prior.started_research if prior is not None else ()) + (
            (research,) if research is not None else ())
        decision = outcome.decision
        spent = (outcome.observed_coin_spend if outcome.observed_coin_spend > 0
                 else decision.price if type(decision.price) is int and decision.price > 0 else 0)
        spends = (prior.started_spends if prior is not None else ()) + (
            ((research, spent),) if research is not None and spent > 0 else ())
        game_speed = prior.game_speed_start if prior is not None else None
        if research == 'labs.game-speed':
            job = outcome.confirmed_job
            game_speed = (decision.game_speed_level, job.completes_at if job is not None else None)
        outcome = replace(outcome, started_slots=slots, started_research=started,
                          started_spends=spends, game_speed_start=game_speed, replanned=False)
        # Loop only where the planner hook in advance() can choose the next start.
        if not (self._options.direct_start and self._options.start_research
                and self._options.native_repeat == 'unchanged' and not self._unlock_done
                and self.plan_action is not None and self.runtime is not None):
            self._return(outcome)
            return
        # A later timeout or empty plan still reports "started"; the visit
        # re-reads the strip after this start, so no follow-up visit is owed.
        self._outcome = replace(outcome, replanned=True)
        self._reset_start()
        self._state = "home"

    def _end_attempt(self, outcome: LabVisitResult) -> None:
        """Return to battle; a start proven earlier in this visit keeps its result."""
        prior = self._outcome
        if prior is not None and prior.status == 'started':
            logger.info("Lab attempt after started slots %s ended: %s (%s)",
                        prior.started_slots, outcome.status, outcome.reason)
            outcome = replace(prior, attempt=outcome)
        self._return(outcome)

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

    def _unlock_slot(self, home: LabHomeReading, screen: Image, device: AdbDevice,
                     now: float) -> bool:
        """On the way out, rehearse or unlock the gem lane's next slot once per visit.

        True keeps the visit on Labs this scan: a first read, or a tap that needs
        its post-tap reads. The rollout record decides between rehearsal, tap and skip.
        Only the first locked tile on screen is acted on, and only when the gem
        lane names that same slot; a stale slot record never moves the target.
        """
        locked, scope = home.next_locked, self._scope()
        if self._unlock_done or self.rollout is None or self.worker is None or scope is None:
            return False
        self._halt_owned_canary(scope)
        if locked is None or locked.slot not in self._options.unlock_slots:
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
            return self._tap_unlock(locked, gems, screen, device, now)
        if state.stage == 'dry_run':
            change = self.rollout.note_dry_run(slot, self.worker, price, gems, self.wall_clock(),
                                               account_id=scope.account_id)
            if change.before.stage == 'dry_run' and not change.halted:
                self._emit(events.LabUnlockRehearsed(slot=slot, price=price, gems=gems))
            self._publish_change(change)
            return False
        self._skip(slot, 'waiting_for_canary')
        return False

    def _halt_owned_canary(self, scope: FactScope) -> None:
        """Halt this worker's canary slot that is already owned with no unlock left to settle.

        Once per visit, on a confirmed strip read. The canary's unlock can only be
        proven through its own open transaction; with none open, a slot this
        account owns was bought some other way (a landed tap judged not charged,
        a lost rollout write, an owner's reconciliation), and nothing would ever
        move the slot on again. The owner sees the halt and can Reset it.
        """
        reading = self._reading
        if self._owned_canary_checked or reading is None or not reading.strip_read():
            return
        snapshot = self.runtime.snapshot()
        if (not snapshot.strip_complete or snapshot.observed_at != self._capture_at
                or snapshot.slots_owned != reading.slots_owned or reading.slots_owned < 2):
            return
        self._owned_canary_checked = True
        if self.pending_transaction is not None:
            return
        for slot, state in self.rollout.slots().items():
            if (slot <= reading.slots_owned and state.stage == 'canary'
                    and state.canary_worker == self.worker and state.canary_account == scope.account_id):
                self._publish_change(self.rollout.halt_canary(
                    slot, self.worker, scope.account_id, f"Slot {slot} owned without a proven canary unlock"))

    def _tap_unlock(self, locked: LockedSlot, gems: int, screen: Image, device: AdbDevice,
                    now: float) -> bool:
        assert locked.price is not None and locked.point is not None
        if not self._safe(locked.point):
            # Refuse before preparing: a refused tap must never leave an acted transaction.
            logger.warning("Refused lab unlock tap at %s: on a Rush control", locked.point)
            # A research start proven earlier in the visit keeps its result so the
            # spend is still recorded; the refusal shows in recovery_status.
            prior = self._outcome
            self.recovery_status = 'unsafe_tap_target'
            self._finish(prior if prior is not None and prior.status == 'started' else
                         LabVisitResult('failed', 'unsafe_tap_target', LabDecision('unknown')))
            return True
        txn = self._prepare('lab_unlock', gems, locked.price, unlock_slot=locked.slot)
        if txn is None:
            self.recovery_status = f'lab_preparation_refused:{self.preparation_refusal}'
            return False
        self._unlock_tap = (txn.key, locked.slot)
        self._unlock_frames = [screen]
        self._unlock_scans = self._unlock_strikes = 0
        self._unlock_tapped_at = now
        self._debit_strike = False
        self._unlanded_signature = None
        self._unlock_dialog_signature = None
        self._unlock_dialog_reads = 0
        self._unlock_confirmation_tapped = False
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
                           screen: Image, now: float) -> LabVisitResult | None:
        """Sort this visit's own unlock tap into bought, not landed, or uncertain.

        Bought: slot N confirmed owned since the tap and the gems down by the price.
        Not landed: two matching Labs home reads, taken after the tap, with slot N
        still the first locked tile at its price and the gems unchanged. A debit
        that provably is not the price is uncertain at once. Anything else is a
        strike, including a confirmed owned slot whose gem header is unreadable or
        not yet down; three strikes, or eight post-tap scans, is uncertain once
        LAB_UNLOCK_PROOF_MIN_SECONDS of the visit's clock have passed since the tap.
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
                if _debit_mismatch(txn, home.gem_balance):
                    return self._unlock_uncertain(txn, slot, 'gem debit did not match the price')
            if current:
                # Owned, but the header is unreadable or has not caught up with the
                # debit yet: an unclassified read, and the transaction stays open.
                self._unlock_strikes += 1
                self._debit_strike = True
            # Owned but not yet confirmed: it breaks any run of still-locked reads.
            self._unlanded_signature = None
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
            self._debit_strike = False
            self._unlanded_signature = None
        if ((self._unlock_strikes >= _UNLOCK_STRIKES or self._unlock_scans >= _UNLOCK_SCANS)
                and now - self._unlock_tapped_at >= LAB_UNLOCK_PROOF_MIN_SECONDS):
            return self._unlock_uncertain(txn, slot, 'gem debit was not proven' if self._debit_strike
                                          else 'post-tap screen was not understood')
        return None

    def _note_unlock(self, txn: transactions.Transaction, outcome: str) -> None:
        """Report a settled canary tap to the rollout; a no-op for any other worker or stage."""
        if self.rollout is not None and self.worker is not None:
            self._publish_change(self.rollout.note_unlock(txn.before['slot'], self.worker, txn.key,
                                                          outcome, at=self.wall_clock()))

    def _unlock_outcome(self, status: str, reason: str, **fields: object) -> LabVisitResult:
        """The unlock's result, keeping what this visit already learned about research.

        A research start confirmed earlier in the visit keeps its status and
        reason, and so does an auto-start-off read, which TowerBot uses to keep
        the saved Game Speed evidence; any other earlier outcome keeps its job
        and decision. The unlock's own verdict is in its fields and recovery_status.
        """
        prior = self._outcome
        if prior is None:
            return LabVisitResult(status, reason, LabDecision('unknown'), **fields)
        if prior.status == 'started' or prior.reason == 'auto_start_off':
            return replace(prior, **fields)
        return replace(prior, status=status, reason=reason, **fields)

    def _unlock_bought(self, txn: transactions.Transaction, home: LabHomeReading,
                       outcome: transactions.Outcome) -> None:
        slot = txn.before['slot']
        self._restore_receipts()  # publishes LabSlotUnlocked with the journal's values
        self._note_unlock(txn, 'bought')
        self._unlock_tap = None
        self.recovery_status = 'settled'
        self._return(self._unlock_outcome('observed', 'slot_unlocked',
            slot_status=_slot_status(home, self._reading), gem_balance=home.gem_balance,
            gems_before=txn.wallet_before, observed_gem_spend=outcome.spent,
            unlock_transaction_key=txn.key, unlocked_slot=slot))
        return None

    def _unlock_missed(self, txn: transactions.Transaction, home: LabHomeReading) -> None:
        self._restore_receipts()
        self._note_unlock(txn, 'not_charged')
        self._unlock_tap = None
        self._return(self._unlock_outcome('observed', 'unlock_not_landed',
            slot_status=_slot_status(home, self._reading), gem_balance=home.gem_balance))
        return None

    def _publish_starter(self, change: StarterChange | None) -> None:
        if change is None:
            return
        if change.promoted:
            self._emit(events.LabStarterPromoted(key=change.key, stage=change.promoted))
        if change.halted:
            self._emit(events.LabStarterHalted(key=change.key, reason=change.after.halted_reason or ""))

    def _rehearse(self, purchase: LabDecision, price: int) -> None:
        """A clean confirmation read: record it, then the return path taps Cancel."""
        if self.starter is not None and self.worker is not None:
            self._publish_starter(self.starter.note_start_dry_run(
                purchase.slot, self.worker, self._account(), purchase.research_id,
                purchase.target_level, price, self._picker_seconds, self._capture_at))
        self._emit(events.LabStartRehearsed(slot=purchase.slot, research_id=purchase.research_id,
                                            level=purchase.target_level, price=price,
                                            seconds=self._picker_seconds))
        self._end_attempt(LabVisitResult('observed', 'research_rehearsed', purchase))

    def _note_miss(self, research: str) -> None:
        """Record the miss first; frames are written only for a miss the rollout kept."""
        if self.starter is None or self.worker is None:
            return
        lab = self.starter.note_research_miss(research, self.worker, self._account(), self._capture_at)
        if lab is None or not lab.misses or lab.misses[-1] != float(self._capture_at):
            return  # spaced too closely, or the lab is already rehearsed or blocked
        evidence = self._save_evidence(f"lab-search-{research}", self._capture_at, self._search_frames)
        self.starter.attach_miss_evidence(research, self._capture_at, evidence)

    def _note_start(self, txn: transactions.Transaction, outcome: str) -> None:
        """Report a settled start to the starter rollout; Game Speed in slot 1 is not in it."""
        if (self.starter is None or self.worker is None or txn.operation != 'lab_start'
                or (txn.before['slot'], txn.before['research_id']) == (1, 'labs.game-speed')):
            return
        if outcome == 'not_charged' and txn.acted_at is None:
            # Refuted before the tap was ever dispatched (a crash after prepare):
            # it says nothing about whether the canary's tap lands.
            return
        self._publish_starter(self.starter.note_start(
            txn.before['slot'], self.worker, self._account(), txn.key, outcome, at=self.wall_clock()))

    def _start_uncertain(self, txn: transactions.Transaction) -> LabVisitResult:
        """Keep the transaction open (the read-only hold) and halt a canary's slot."""
        slot = txn.before['slot']
        stamp = txn.acted_at if txn.acted_at is not None else self.wall_clock()
        evidence = self._save_evidence(f"lab-start-slot{slot}", stamp, self._start_frames)
        if self.starter is not None and self.worker is not None:
            self._publish_starter(self.starter.halt_canary(
                start_key(slot), self.worker, self._account(), "Start was not proven", evidence))
        self._start_tap = None
        self.recovery_status = 'lab_start_uncertain'
        return self._finish(LabVisitResult('failed', 'lab_start_uncertain', LabDecision('unknown'),
                                           transaction_key=txn.key))

    def _save_evidence(self, stem: str, stamp: float, frames: list[Image]) -> tuple[str, ...]:
        """Frames as evidence/<stem>-<ts>-<k>.png. Best effort: unwritable frames are skipped."""
        if self.evidence_dir is None:
            return ()
        import cv2
        saved = []
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            for index, image in enumerate(frames):
                path = self.evidence_dir / f"{stem}-{int(stamp)}-{index}.png"
                if cv2.imwrite(str(path), image):
                    saved.append(str(path))
        except (OSError, cv2.error) as exc:
            logger.warning("Lab evidence %s not fully saved to %s (%s)", stem, self.evidence_dir, exc)
        return tuple(saved)

    def _unlock_uncertain(self, txn: transactions.Transaction, slot: int,
                          reason: str) -> LabVisitResult:
        """Keep the transaction open (the worker's read-only hold) and halt a canary's slot."""
        evidence = self._save_evidence(
            f"lab-unlock-slot{slot}", txn.acted_at if txn.acted_at is not None else self.wall_clock(),
            self._unlock_frames)
        state = self.rollout.slot(slot)
        if state.stage == 'canary' and state.canary_worker == self.worker:
            self._publish_change(self.rollout.halt(slot, reason, evidence))
        self._unlock_tap = None
        self.recovery_status = 'lab_unlock_uncertain'
        # The tap may have landed: record no slot status from before it.
        return self._finish(self._unlock_outcome('failed', 'lab_unlock_uncertain', slot_status=(),
                                                 gems_before=txn.wallet_before))

    @staticmethod
    def _new_research_notice_point(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> tuple[int, int] | None:
        """Only the unique, centered OK below both new-research notice lines."""
        labels = (*_NEW_RESEARCH_LINES, "OK")
        matches = {label: [box for box in boxes if "".join(box.text.upper().split()) == label]
                   for label in labels}
        if any(len(found) != 1 or not .9 <= found[0].confidence <= 1.
               for found in matches.values()):
            return None
        title, body, ok = (matches[label][0] for label in labels)
        height, width = screen.shape[:2]
        x, y, w, h = ok.rect
        center = x + w // 2, y + h // 2
        if (not title.rect.y < body.rect.y < y or w <= 0 or h <= 0
                or not .35 * width <= center[0] <= .65 * width
                or not .4 * height <= center[1] <= .75 * height):
            return None
        return center

    def advance(
        self, screen: Image, boxes: tuple[ocr.TextBox, ...],
        device: AdbDevice, now: float,
        *, observed_at: float | None = None, capture_scope: FactScope | None = None,
    ) -> LabVisitResult | None:
        if not self.active:
            return None
        self.last_tap = None
        self._boxes = boxes
        capture_at = self.wall_clock() if observed_at is None else observed_at
        if capture_at <= self._capture_at:
            return None
        self._capture_at, self._capture_scope = capture_at, capture_scope
        if self._started_at == 0.:
            self._started_at = now
        self._scans += 1
        scan_cap, time_cap = (64, 120.) if self._search is not None else (48, 90.)
        # Each start proven in this visit buys the next one a full budget.
        extra = len(self._outcome.started_slots) if self._outcome is not None else 0
        scan_cap, time_cap = scan_cap + 48 * extra, time_cap + 90. * extra
        if self._scans > scan_cap or now - self._started_at > time_cap:
            pending = self.pending_transaction
            # This visit's own start tap, still unproven, is uncertain: keep the
            # hold, save the evidence and halt a canary, as the scan limit would.
            if self._start_tap is not None and pending is not None and pending.key == self._start_tap[0]:
                return self._start_uncertain(pending)
            # An unsettled unlock tap keeps its hold without halting the slot; the
            # next visit's recovery settles it (and promotes a canary's slot).
            if pending is not None:
                # A scoped, post-tap inspection can prove the Lab changed while
                # abbreviated wallet text cannot prove the exact debit. The
                # journal retains that inspection even if the job has since
                # finished; it releases only inspected reservations, leaving
                # the spend unknown rather than inventing a ledger amount.
                if (self.journal is not None and pending.operation == 'lab_start'
                        and pending.scope is not None):
                    self.journal.close_unproven(
                        pending.key, reason='lab verification timed out after inspection',
                        now=self.wall_clock())
                if self.pending_transaction is None:
                    self.recovery_status = 'lab_reconciliation_closed_unproven'
                    return self._finish(LabVisitResult(
                        'observed', 'lab_reconciliation_unproven', LabDecision('unknown'),
                        transaction_key=pending.key))
                self.recovery_status = 'lab_reconciliation_route_unavailable'
            return self._finish(self._outcome or LabVisitResult(
                "failed", "visit_timeout", LabDecision("unknown")))
        if self._unsafe_tap:
            self._unsafe_tap = False
            return self._finish(LabVisitResult('failed', 'unsafe_tap_target', LabDecision('unknown')))

        # Both informational notices cover still-readable slot cards. Hold
        # the whole frame until the notice is safely dismissed.
        new_research_notice = any("".join(box.text.upper().split()) in _NEW_RESEARCH_LINES
                                  for box in boxes)
        if new_research_notice or any("".join(box.text.upper().split()).startswith(_INTRO_LINES)
                                      for box in boxes):
            pending = self.pending_transaction
            scope = self._scope()
            if pending is not None and (scope is None or not self.account_state.identity_fresh(now=self.wall_clock())
                    or pending.scope != scope and (pending.scope is None or
                        self.account_state.continuity(pending.scope, now=self.wall_clock()) is None)):
                self.recovery_status = 'lab_scope_continuity_unavailable'
                return None
            point = (self._new_research_notice_point(screen, boxes) if new_research_notice
                     else self._match(screen, "nav/labs_close.png"))
            if point is not None:
                self._tap(device, point, "dismiss_new_researches" if new_research_notice
                          else "close_labs_intro")
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
        if (selected is None and self.plan_action is not None
                and self._state in {'open', 'home'} and home.page
                and self._options.start_research and self._options.native_repeat == 'unchanged'
                and not self._unlock_done and self.pending_transaction is None
                and self.runtime is not None):
            snapshot = self.runtime.snapshot()
            if snapshot.strip_complete and snapshot.observed_at == capture_at:
                candidate = self.plan_action(capture_at)
                if (candidate is not None and candidate.operation in {'start', 'rehearse'}
                        and type(candidate.slot) is int and candidate.slot in range(1, 6)
                        and type(candidate.target_level) is int and candidate.target_level >= 1
                        and type(candidate.strategy_revision) is int
                        and self.gate(candidate.slot, candidate.research).mode == candidate.operation):
                    self.selected_action = self.pending_action = selected = candidate
                    self._rehearsing = candidate.operation == 'rehearse'
                    if selected.slot != 1:
                        home = read_selected_home(screen, boxes, slot=selected.slot,
                                                  observed_at=capture_at)
        picker = (read_selected_picker(screen, boxes, research_id=selected.research)
                  if selected is not None and selected.research != 'labs.game-speed'
                  else self.picker_reader(screen, boxes))
        confirmation = self.confirmation_reader(screen, boxes)
        if home.page:
            self._home_seen = (home, self._reading)
        pending = self.pending_transaction
        if pending is not None:
            if self._unlock_tap is not None and pending.key == self._unlock_tap[0]:
                gem_dialog = read_gem_unlock_confirmation(screen, boxes)
                if gem_dialog.page:
                    slot = self._unlock_tap[1]
                    if len(self._unlock_frames) < _UNLOCK_SCANS + 1:
                        self._unlock_frames.append(screen)
                    scope = self._scope()
                    if (pending.operation != 'lab_unlock' or pending.before['slot'] != slot
                            or gem_dialog.price != pending.price
                            or gem_dialog.gem_balance != pending.wallet_before
                            or gem_dialog.confirm_point is None or scope is None
                            or pending.scope != scope):
                        return self._unlock_uncertain(pending, slot,
                                                      'gem confirmation did not match pending unlock')
                    if self._unlock_confirmation_tapped:
                        self._unlock_scans += 1
                        if (self._unlock_scans >= _UNLOCK_STRIKES
                                and now - self._unlock_tapped_at >= LAB_UNLOCK_PROOF_MIN_SECONDS):
                            return self._unlock_uncertain(pending, slot,
                                                          'gem confirmation remained after tap')
                        return None
                    signature = (gem_dialog.price, gem_dialog.gem_balance,
                                 gem_dialog.confirm_point)
                    if signature != self._unlock_dialog_signature:
                        self._unlock_dialog_signature, self._unlock_dialog_reads = signature, 1
                        return None
                    self._unlock_dialog_reads += 1
                    if self._unlock_dialog_reads >= 2:
                        self._tap(device, gem_dialog.confirm_point, f'confirm_lab_slot_{slot}')
                        self._unlock_confirmation_tapped = self.last_tap is not None
                        if self._unlock_confirmation_tapped:
                            self._unlock_scans = self._unlock_strikes = 0
                    return None
                self._unlock_dialog_signature, self._unlock_dialog_reads = None, 0
                return self._settle_own_unlock(pending, home, screen, now)
            if self._start_tap is not None and pending.key == self._start_tap[0]:
                self._start_frames.append(screen)
                self._start_scans += 1
                if (self._start_scans > _START_SCANS
                        and now - self._start_tapped_at >= LAB_START_PROOF_MIN_SECONDS):
                    return self._start_uncertain(pending)
            self._recover(pending, home, picker, confirmation, screen, device)
            return None
        if self._stage_name != self._state:
            self._stage_name, self._stage_scans, self._stage_started = self._state, 0, now
            self._picker_unknown_logged = False
        self._stage_scans += 1
        searching = self._search is not None
        budget = 6 if self._state == 'return' else 24 if self._state == 'picker' and searching else 8
        if self._options.native_repeat != 'unchanged' and self._state in {'home', 'return'}:
            budget = 32
        # Fast between-games scans spend a count in a fraction of its intended
        # time: the 8- and 24-scan budgets also need their minimum wall time.
        floor = (LAB_SEARCH_STAGE_MIN_SECONDS if budget == 24
                 else LAB_STAGE_MIN_SECONDS if budget == 8 else 0.)
        elapsed = now - self._stage_started
        repeat_stage = self._options.native_repeat != 'unchanged' and self._state in {'home', 'return'}
        if ((self._stage_scans > budget and elapsed >= floor)
                or elapsed > (60 if searching or repeat_stage else 30)):
            timeout = LabVisitResult('failed', f'{self._state}_stage_timeout', LabDecision('unknown'))
            if self._state == 'picker':
                target = selected.research if selected is not None else 'labs.game-speed'
                self._save_evidence(f"lab-picker-timeout-{target}", self._capture_at, self._picker_frames)
            outcome = self._outcome or timeout
            if self._state != 'return' and outcome.status == 'started':
                # A start proven earlier in this visit keeps its result; this attempt rides along.
                outcome = replace(outcome, attempt=timeout)
            if self._state == 'return' or not (home.page or picker.page or confirmation.page):
                return self._finish(outcome)
            self._return(outcome)
            return None
        if self._state == "open":
            if home.page:
                self._state = "home"
            elif pages.classify_page(screen, self.templates).page == "MAIN_MENU":
                point = self._unlocked_tab_match(screen)
                if point is not None:
                    self._tap(device, point, "open_labs")
                    self._state = "home"
                return None
            else:
                return self._finish(LabVisitResult("failed", "not_at_menu", LabDecision("unknown")))

        if self._state == "home":
            if not home.page:
                return None
            if self._reconcile_repeat(screen, device):
                return None
            if (selected is None and self.plan_action is not None
                    and self._options.start_research and self._options.native_repeat == 'unchanged'
                    and self.runtime is not None):
                snapshot = self.runtime.snapshot()
                if not snapshot.strip_complete or snapshot.observed_at != capture_at:
                    return None
            decision = decide(home, None)
            if selected is not None:
                # The selected slot's card was read: its result must not pass for Lab 1's.
                decision = replace(decision, slot=selected.slot, research_id=selected.research,
                                   strategy_revision=selected.strategy_revision)
            if not self._options.start_research and self.runtime is not None:
                snapshot = self.runtime.snapshot()
                if (not self._reading.strip_read() or snapshot.slots_owned is None
                        or not all(record.confirmed and record.observed_at == capture_at
                                   for record in snapshot.slots[:snapshot.slots_owned])):
                    return None
            if decision.kind == "inspect" and not self._options.start_research:
                # Auto-start is off: read the slot, never open the picker.
                self._end_attempt(LabVisitResult("observed", "auto_start_off", decision,
                                                  confirmed_job=home.job))
            elif decision.kind == "inspect" and home.slot_point is not None:
                # A slot confirmed on an earlier visit stays confirmed; the spend
                # boundary needs its idle proof from this visit's own strip read.
                record = (self.runtime.snapshot().slots[(selected.slot if selected else 1)-1]
                          if self.runtime is not None else None)
                if record is not None and not (record.confirmed and record.observed_at == capture_at):
                    return None
                self._slot = home
                self._tap(device, home.slot_point, "open_lab_one" if selected is None or selected.slot == 1 else f"open_lab_{selected.slot}")
                self._state = "picker"
            elif decision.kind == "inspect" and selected is not None and self._options.start_research:
                # The selected card can be readable while another card makes
                # this frame's owned strip incomplete. Retry for a fresh safe
                # tap target within the existing home-stage timeout.
                return None
            else:
                self._end_attempt(LabVisitResult("observed", decision.kind, decision,
                                                  confirmed_job=home.job))
            return None

        if self._state == "picker":
            if not picker.page or self._slot is None:
                return None
            self._picker_frames.append(screen)
            if len(self._picker_frames) > _SEARCH_FRAMES_KEPT:
                del self._picker_frames[1]
            target = selected.research if selected is not None else 'labs.game-speed'
            general = target != 'labs.game-speed'
            page = read_picker_page(screen, boxes)
            if self._search is None and not general:
                # The game keeps the picker's scroll position: a Game Speed card
                # that is not fully on the page is searched for like any other.
                card = page.card(target)
                if page.open and (card is None or not card.fully_visible):
                    self._search = PickerSearch(target, screen.shape[1])
            if general or self._search is not None:
                # Every general selection goes through the search: "found" (the card is
                # fully inside the list) falls through to the two-read selection below.
                # A clipped card must scroll, not read as unaffordable.
                if self._search is None:
                    self._search = PickerSearch(target, screen.shape[1])
                step = self._search.step(page)
                if step.kind != 'found':
                    # Evidence of a miss: the first page and the last three.
                    self._search_frames.append(screen)
                    if len(self._search_frames) > _SEARCH_FRAMES_KEPT:
                        del self._search_frames[1]
                if step.kind == 'swipe' and step.swipe is not None:
                    device.swipe(*step.swipe, SWIPE_SECONDS)
                    self._picker_signature, self._picker_reads = None, 0
                    return None
                if step.kind == 'not_found':
                    if general:
                        # Game Speed stays out of the starter rollout's miss records.
                        self._note_miss(target)
                    self._end_attempt(LabVisitResult('failed', 'research_not_found', LabDecision('unknown')))
                    return None
                if step.kind == 'wait':
                    return None
            decision = decide(self._slot, picker, research_id=selected.research if selected else 'labs.game-speed')
            if selected is not None:
                decision = replace(decision, slot=selected.slot, research_id=selected.research,
                                   strategy_revision=selected.strategy_revision)
                entry = picker.entry
                if entry is not None and entry.level != selected.target_level:
                    # Two identical reads before giving up, so the Labs capture
                    # stores the level the picker shows and the next plan
                    # targets it instead of mismatching on every visit.
                    signature = (entry.concept_id, entry.level, entry.status)
                    # A different level breaks any run of matching start reads.
                    self._picker_signature, self._picker_reads = None, 0
                    if entry.concept_id == selected.research and signature != self._mismatch_signature:
                        self._mismatch_signature = signature
                        return None
                if entry is not None and (entry.concept_id != selected.research
                                          or entry.level != selected.target_level):
                    self._end_attempt(LabVisitResult('failed', 'selected_research_mismatch', decision))
                    return None
                self._mismatch_signature = None
            if decision.kind != "start":
                self._picker_signature, self._picker_reads = None, 0
                entry = picker.entry
                unavailable = (selected is not None and entry is not None
                               and entry.concept_id == selected.research
                               and entry.level == selected.target_level
                               and entry.status == 'unavailable'
                               and decision.price is not None
                               and decision.wallet_coins is not None
                               and decision.wallet_coins >= decision.price)
                if unavailable:
                    signature = (selected.research, selected.target_level,
                                 decision.price, decision.wallet_coins)
                    if signature != self._unavailable_signature:
                        self._unavailable_signature, self._unavailable_reads = signature, 1
                    else:
                        self._unavailable_reads += 1
                    if self._unavailable_reads >= 2:
                        self._end_attempt(LabVisitResult('observed', 'research_unavailable', decision))
                    return None
                self._unavailable_signature, self._unavailable_reads = None, 0
                if decision.kind == 'unknown':
                    # OCR and card visibility can flicker. Stay in the picker
                    # until the bounded stage timeout instead of aborting on one frame.
                    if not self._picker_unknown_logged:
                        self._picker_unknown_logged = True
                        logger.info("Lab picker read unknown for %s: %s", target,
                                    _picker_unknown_reason(page.card(target)))
                    return None
                self._end_attempt(LabVisitResult("observed", decision.kind, decision))
                return None
            self._unavailable_signature, self._unavailable_reads = None, 0
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
            self._picker_seconds = picker.entry.duration_s if picker.entry is not None else None
            self._tap(device, picker.buy_point, "start_game_speed" if decision.research_id == "labs.game-speed" else "select_research")
            self._state = "dialog"
            return None

        if self._state == "dialog":
            if not confirmation.page:
                return None
            purchase = self._purchase
            # OCR may read one name with inner spaces and the other without.
            if (purchase is None or confirmation.name is not None
                    and _squash(confirmation.name) != _squash(self._picker_name)
                    or confirmation.research_id is not None
                    and confirmation.research_id != purchase.research_id
                    or confirmation.target_level is not None
                    and confirmation.target_level != purchase.target_level
                    or confirmation.price is not None and confirmation.price != purchase.price
                    or confirmation.coin_balance is not None
                    and confirmation.coin_balance != purchase.wallet_coins):
                self._end_attempt(LabVisitResult("failed", "confirmation_mismatch",
                                                  LabDecision("unknown")))
                return None
            if (confirmation.name is None or confirmation.research_id is None
                    or confirmation.target_level is None or confirmation.price is None
                    or confirmation.coin_balance is None or confirmation.research_point is None):
                # A missing OCR field is not a contradictory confirmation.
                # Keep reading until the bounded dialog timeout; never tap on
                # an incomplete frame.
                self._dialog_signature, self._dialog_reads = None, 0
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
            if self._rehearsing:
                self._rehearse(purchase, confirmation.price)
                return None
            if not self._safe(confirmation.research_point):
                # Refuse before preparing: a refused tap must never leave an acted transaction.
                logger.warning("Refused lab research tap at %s: on a Rush control", confirmation.research_point)
                return self._finish(LabVisitResult('failed', 'unsafe_tap_target', LabDecision('unknown')))
            txn = self._prepare('lab_start', confirmation.coin_balance, confirmation.price)
            if txn is None:
                self._end_attempt(LabVisitResult('failed', f'lab_preparation_refused:{self.preparation_refusal}',
                                                 LabDecision('unknown')))
                return None
            self._tap(device, confirmation.research_point, "confirm_game_speed" if purchase.research_id == "labs.game-speed" else "confirm_research")
            if (purchase.slot, purchase.research_id) != (1, 'labs.game-speed'):
                self._start_tap, self._start_frames, self._start_scans = (txn.key, purchase.slot), [screen], 0
                self._start_tapped_at = now
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
                if self._reconcile_repeat(screen, device):
                    return None
                if self._unlock_slot(home, screen, device, now):
                    return None if self.active else self._outcome
                point = self._match(screen, "nav/tab_battle.png")
                if point is not None:
                    self._tap(device, point, "return_to_battle")
                return None
            if pages.classify_page(screen, self.templates).page == "MAIN_MENU":
                return self._finish(self._outcome or LabVisitResult(
                    "failed", "no_outcome", LabDecision("unknown")))
            return None
        return None
