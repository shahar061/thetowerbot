"""Bounded observation navigation. This module has no purchase operation."""
from __future__ import annotations

from typing import Any
import time

import events
import config
import pages
import vision
from device import Image, tap
from supervisor import RecoveryPreflightBlocked


HELD_RETRY_SECONDS = 900.


class WorkshopInspection:
    def __init__(self) -> None:
        self.key: str | None = None
        self.steps = 0
        self.category_index = 0
        self.scans = 0
        self.status = 'idle'
        self.retry_at: float | None = None
        self._backoff_key: str | None = None
        self._sample: tuple | None = None
        self._previous_view: tuple | None = None
        self._down = False
        self.hold_reason: str | None = None
        self._held_until: float | None = None
        self._held_key: str | None = None
        self._held_reason: str | None = None

    @staticmethod
    def _request_key(session: Any, progress: Any | None) -> str | None:
        request = (progress.snapshot().get('capabilities', {}).get('workshop', {})
                   if progress is not None else {})
        if not request.get('observation_due'):
            return None
        scope = session.account_state.verified_scope if session.account_state is not None else None
        return repr((request.get('since_utc'), request.get('evidence_ref'), scope))

    def _reset(self, key: str | None) -> None:
        self.key = key
        self.steps = self.scans = self.category_index = 0
        self._sample = self._previous_view = None
        self._down = False
        self.status = 'observing' if key is not None else 'idle'
        self._backoff_key = self.retry_at = None
        self._held_until = self.hold_reason = self._held_key = self._held_reason = None

    def _backoff(self, session: Any, now: float, *, completed: bool = False) -> None:
        self._backoff_key, self.key = self.key, None
        self.status, self.retry_at = 'backoff', now + 30
        if completed:
            self.category_index = 0
            self._down = False
            self._previous_view = None
        session._bus.publish(events.ShoppingUnavailable(reason=
            'Workshop observation incomplete; bounded read-only inspection will retry after 30 seconds'))

    def requested(self, session: Any, progress: Any | None) -> bool:
        if session.reconciliation_pending:
            return True  # Pending spends never release the device through ordinary backoff.
        key = self._request_key(session, progress)
        if key is None:
            self._reset(None)
            return False
        return not (key == self._backoff_key and self.retry_at is not None
                    and time.time() < self.retry_at)

    def _start_interval(self, txn: Any, reason: str, now: float) -> None:
        """Holds a MAIN_MENU pass evaluated but cannot advance (held_pending,
        independent_proof_required, scope_continuity_unavailable) wait the held
        interval, so GAME_OVER detours and identity walks never repeat every run."""
        if self._held_until is None or self._held_key != txn.key:
            self._held_until, self._held_key, self._held_reason = now + HELD_RETRY_SECONDS, txn.key, reason

    def _holding(self, txn: Any, now: float) -> bool:
        """The held interval for this exact intent is still running."""
        return (self._held_until is not None and self._held_key == txn.key
                and now < self._held_until)

    def _hold(self, session: Any, reason: str) -> bool:
        """Release the scan: an unresolved purchase blocks spending, never battle.

        ``TransactionJournal.prepare`` keeps refusing every new spend while
        the intent stays open, so ordinary battle/scan flow cannot spend.
        """
        if self.hold_reason != reason:
            self.hold_reason = reason
            session._bus.publish(events.ShoppingUnavailable(reason=(
                f'Purchase reconciliation held ({reason}); spending stays blocked, battles '
                'continue. Operator: tools/reconcile_transaction.py')))
        self.status = f'held:{reason}'
        return False

    def retry_due(self, session: Any, now: float) -> bool:
        """A held purchase's paced MAIN_MENU retry is due (GAME_OVER -> HOME).

        Only a menu pass can re-search or re-verify, so GAME_OVER detours home
        when the next bounded step is due: the held search interval elapsed,
        the identity re-verification pacer is due, or a fresh identity lets
        the read-only search proceed. Paced by those intervals, never every run.
        """
        txn = session._unanswered_transaction()
        if txn is None or txn.scope is None:
            return False  # Legacy rows recover read-only on any page.
        if self._holding(txn, now):
            return False  # The re-verification pacer alone never shortens a hold.
        state = session.account_state
        if state is None or state.verified_scope is None or not state.identity_fresh(now=now):
            reverifier = getattr(session, 'identity_reverifier', None)
            return reverifier is not None and reverifier.due()
        return True

    def advance(self, session: Any, screen: Image, device: Any, *, paused: bool,
                progress: Any | None) -> bool:
        if not self.requested(session, progress):
            return False
        txn = session._unanswered_transaction()
        reading = pages.classify_page(screen, session._templates)
        if txn is None and reading.page not in ('MAIN_MENU', 'WORKSHOP'):
            return False  # Let the current run finish; never abandon a battle.
        if paused:
            return True
        state = session.account_state
        now = time.time()
        if txn is not None:
            if txn.scope is None:
                # Legacy unscoped row: its historical read-only recovery path
                # (never a device action) plus the operator reconcile command.
                session._recover_transaction(reading, screen)
                return self._hold(session, 'legacy_unscoped_intent')
            if reading.page not in ('MAIN_MENU', 'WORKSHOP'):
                return self._hold(session, 'waiting_for_menu')
            if self._holding(txn, now):
                if txn.currency != 'coins' or txn.category not in config.WORKSHOP_TABS:
                    # New independent evidence on this page may still settle
                    # it; this read-only check never sends input.
                    session._recover_transaction(reading, screen)
                return self._hold(session, self._held_reason or 'held_pending')
            if self._held_until is not None:
                # Interval elapsed, or a different intent: bounded periodic retry.
                self._held_until = self._held_key = self._held_reason = None
                self.steps = self.scans = 0
            if state is None or state.verified_scope is None or not state.identity_fresh(now=now):
                reverifier = getattr(session, 'identity_reverifier', None)
                if (reading.page == 'MAIN_MENU' and reverifier is not None
                        and reverifier.attempt('pending_purchase_reconciliation')):
                    self.status = 'reverifying_identity'
                    return True  # The bounded identity walk used the device.
                return self._hold(session, 'identity_stale')
            scope = state.verified_scope
            if txn.scope != scope and state.continuity(txn.scope, now=now) is None:
                self._start_interval(txn, 'scope_continuity_unavailable', now)
                return self._hold(session, 'scope_continuity_unavailable')
            if txn.currency != 'coins' or txn.category not in config.WORKSHOP_TABS:
                # Cards/Labs require their own semantics, never reinterpret a debit.
                session._recover_transaction(reading, screen)
                if session._unanswered_transaction() is None:
                    return False  # Settled by this evidence; nothing held.
                self._start_interval(txn, 'independent_proof_required', now)
                return self._hold(session, 'independent_proof_required')
            self.hold_reason = None
            key, category = txn.key, txn.category
        elif state is None or state.verified_scope is None:
            return False
        else:
            key = self._request_key(session, progress)
        if self.key != key:
            if txn is None and key == self._backoff_key:
                # Continue a budget-limited sweep rather than repeatedly visiting
                # the first category and starving later/offscreen prerequisites.
                self.key, self.status = key, 'observing'
                self.steps = self.scans = 0
                self._sample = self._backoff_key = self.retry_at = None
            else:
                self._reset(key)
        if txn is None:
            category = ('ATTACK', 'DEFENSE', 'UTILITY')[self.category_index]
        if self.steps >= 16 or self.scans >= 32:
            if txn is not None:
                # The reservation stays held; the device returns to battle and
                # the read-only search retries after a bounded interval.
                self._start_interval(txn, 'held_pending', now)
                return self._hold(session, 'held_pending')
            self._backoff(session, now)
            return False
        self.scans += 1
        try:
            if reading.page == 'MAIN_MENU':
                match = vision.locate_template(screen, session._templates.get(
                    config.NAV_TARGETS['WORKSHOP']), session._threshold)
                if match is not None:
                    tap(device, *match.center)
                    self.steps += 1
                return True
            if reading.page != 'WORKSHOP':
                return True
            from shopping import _heading_names, _row_named, header_numbers, observe_frame
            if not _heading_names(screen, category):
                lane = tuple(config.WORKSHOP_TABS).index(category)
                width = screen.shape[1] // 4
                template = session._templates.get(config.WORKSHOP_TABS[category])
                match = vision.locate_template(screen[:, lane*width:(lane+1)*width],
                    template[:template.shape[0]//2], session._threshold)
                if match is not None:
                    tap(device, match.top_left[0] + lane*width + template.shape[1]//2,
                        match.top_left[1] + template.shape[0]//2)
                    self.steps += 1
                return True
            observation = observe_frame(screen, 'workshop')
            if observation.category != category or not observation.frame_digest:
                return True
            if txn is not None:
                row = _row_named(txn.item, observation.rows, category)
                if row is None and observation.heading_y is not None:
                    from autopilot import scroll_panel
                    scroll_panel(device, screen, observation.heading_y, down=self.steps >= 4)
                    self.steps += 1
                    return True
                session._recover_transaction(reading, screen, observation)
                if not session.reconciliation_pending:
                    self.key = None
                return True
            # Confirm every viewport twice before evaluating its facts or scrolling.
            state.observe_account(observation)
            coins, _ = header_numbers(screen, reading.page, reading.top_left)
            if session.reroll_observe_prices is not None:
                session.reroll_observe_prices({r.upgrade_id: r.price for r in observation.rows
                    if r.status == 'available'}, coins)
            if session.observations is not None:
                session.observations.observe(observation)
            viewport = (category, tuple((r.upgrade_id, r.rect) for r in observation.rows))
            semantic_sample = (viewport, tuple((r.value, r.price, r.status) for r in observation.rows))
            if self._sample != semantic_sample:
                self._sample = semantic_sample
                return True
            self._sample = None
            if session.inspection_resolved is not None and session.inspection_resolved():
                if progress is not None:
                    progress.observe_capability('workshop', 'progress', 'fresh Workshop inspection', 120)
                self._reset(None)
                return True
            # First seek the top, then sweep down to a repeated viewport. This
            # observes offscreen prerequisites without guessing which unknown
            # route expression named them. Every swipe goes through the guard.
            if viewport == self._previous_view:
                if not self._down:
                    self._down = True
                elif self.category_index < 2:
                    self.category_index += 1
                    self._down = False
                    self._previous_view = None
                    return True
                else:
                    self._backoff(session, now, completed=True)
                    return True
            if observation.heading_y is not None and observation.rows:
                from autopilot import scroll_panel
                scroll_panel(device, screen, observation.heading_y, down=self._down)
                self.steps += 1
                self._previous_view = viewport
            return True
        except RecoveryPreflightBlocked:
            return True
