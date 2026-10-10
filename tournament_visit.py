"""Free-ticket navigation with durable intent before the entry tap."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from device import Image
from ocr import TextBox
from tournament_policy import TournamentConfig, OpeningCash, PurchaseCursor, advance_purchase, resolve_policy
from tournament_screen import TournamentStats, scan
from tournament_store import TournamentStore


def event_window(now: float) -> str | None:
    date = datetime.fromtimestamp(now, timezone.utc)
    return date.date().isoformat() if date.weekday() in (2, 5) else None


class TournamentVisit:
    def __init__(self, store: TournamentStore, config: TournamentConfig) -> None:
        self.store, self.config = store, config
        self.reason: str | None = None
        self._opened = False
        self._result: TournamentStats | None = None
        self._ok_tapped = False
        self._return_tapped = False
        if self.in_run:
            # Purchase proof is observed on a later frame. A restart cannot
            # know whether an unreceipted tap spent cash; end the opening.
            self.store.close_opening()

    @property
    def in_run(self) -> bool:
        attempt = self.store.pending()
        return attempt is not None and attempt.stage == 'playing'

    @property
    def owns_navigation(self) -> bool:
        attempt = self.store.pending()
        return self._opened or attempt is not None and attempt.stage != 'playing'

    def due(self, now: float) -> bool:
        window = event_window(now)
        return self.config.enabled and window is not None and not self.store.deferred(window, now) and not self.store.seen(window)

    def snapshot(self) -> dict[str, Any]:
        return {**self.store.snapshot(), 'reason': self.reason}

    def take_result(self) -> TournamentStats | None:
        result, self._result = self._result, None
        return result

    def policy(self, observations: dict[str, Any], combat: dict[str, Any]) -> Any:
        from dataclasses import replace
        attempt = self.store.pending()
        assert attempt is not None and attempt.stage == 'playing'
        cursor = PurchaseCursor(attempt.opening_spent, attempt.growth_index, attempt.receipt_index)
        config = replace(self.config, opening_cash=OpeningCash()) if attempt.opening_closed else self.config
        policy = resolve_policy(config, cursor, observations, combat)
        return replace(policy, decision_token=f'{attempt.attempt_id}:{cursor.receipt_index}')

    def purchased(self, upgrade_id: str, cost: int | None) -> None:
        attempt = self.store.pending()
        if attempt is None or attempt.stage != 'playing':
            return
        # Unknown cash debit closes the opening, never replenishes its budget.
        debit = cost if type(cost) is int and cost >= 0 else self.config.opening_cash.cash_budget or 0
        cursor = PurchaseCursor(attempt.opening_spent, attempt.growth_index, attempt.receipt_index)
        self.store.save_cursor(advance_purchase(cursor, upgrade_id, debit, self.config.rules))

    def advance(self, frame: Image, boxes: tuple[TextBox, ...], device: Any, *, now: float, main_menu: bool = False) -> bool:
        reading = scan(frame, boxes)
        attempt = self.store.pending()
        if (main_menu or reading.menu is not None) and not reading.blocked and reading.page is None:
            if attempt is not None and attempt.stage == 'result':
                self.store.complete(now=now)
                self._opened = self._ok_tapped = self._return_tapped = False
                self.reason = None
                return False
            if self._return_tapped or self._opened and attempt is None:
                self._opened = self._ok_tapped = self._return_tapped = False
                return False
        if reading.hud_marker:
            if attempt is not None:
                self.store.entered(None, now=now)
                self._opened = False
            else:
                self.reason = 'unowned_tournament'
                self._opened = True  # Prevent falling through to farming purchases.
            return False
        if reading.stats is not None:
            if attempt is None:
                self.reason = 'unowned_tournament'
                self._opened = True
                return False
            stats = reading.stats
            if self.store.result(wave=stats.wave, rank=stats.rank, coins=stats.coins,
                                 ad_coins=stats.ad_coins, killed_by=stats.killed_by, now=now):
                self._result = stats
            if stats.ok is not None and not self._ok_tapped:
                self._ok_tapped = True
                device.click(*stats.ok.point)
                return True
            return False
        if reading.buy_ticket is not None:
            self.reason = 'paid_entry_blocked'
            device.click(*reading.buy_ticket.point)
            return True
        if reading.username is not None:
            # Name entry remains user setup until an observed keyboard flow is
            # qualified. A supplied name cannot authorize blind text input.
            self.reason = 'setup_required'
            if window := event_window(now):
                self.store.defer(window, self.reason, now+300)
            if reading.username.close is not None:
                self._opened = True
                device.click(*reading.username.close.point)
                return True
            return False
        if reading.profile is not None and self._opened:
            device.click(*reading.profile.point)
            return True
        if reading.page is not None:
            page = reading.page
            self._opened = True
            if attempt is not None:
                if attempt.stage == 'entry_pending' and page.tickets == 0:
                    self.store.reconcile_exhausted(now=now)
                    self.reason = 'entry_unconfirmed'
                if self.store.pending().stage == 'result' and page.return_to_game and not self._return_tapped:
                    self._return_tapped = True
                    device.click(*page.return_to_game.point)
                    return True
                self.reason = self.reason or 'reconciling_entry'
                return False
            window = event_window(now)
            if not self.config.enabled or window is None or self.store.seen(window):
                self.reason = 'already_entered'
            elif page.tickets is None:
                self.reason = 'entry_unverified'
            elif page.tickets < 1:
                self.reason = 'no_free_entry'
            elif page.join_time_left_s is None or page.join_time_left_s <= 0 or not page.entry_is_ticket or page.battle is None:
                self.reason = 'entry_unverified'
            else:
                self._ok_tapped = self._return_tapped = False
                self.store.begin(window, page.league, page.tickets, now=now)
                device.click(*page.battle.point)
                self.reason = 'awaiting_entry'
                return True
            if window is not None:
                until = now + (page.join_time_left_s or 3600) if self.reason == 'no_free_entry' else now+60
                self.store.defer(window, self.reason, until)
            if page.return_to_game and not self._return_tapped:
                self._return_tapped = True
                device.click(*page.return_to_game.point)
                return True
            return False
        if reading.menu is not None and attempt is None and self.due(now):
            self._opened = True
            device.click(*reading.menu.point)
            return True
        return False
