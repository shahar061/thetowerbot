"""A bounded visit for Lab 2 unlock and Lab 1 Game Speed research."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import time
from typing import Callable

from device import AdbDevice, Image, tap
from lab_plan import LabDecision, decide
from lab_screen import (LabConfirmationReading, LabHomeReading, LabPickerReading,
                        read_confirmation, read_home, read_picker)
import ocr
import pages
import vision
from labs import LabJob, LabsReading


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


class LabVisit:
    """One tap per scan; a research tap never implies a completed purchase."""

    def __init__(
        self,
        templates: vision.TemplateCache,
        home_reader: Callable[[Image, tuple[ocr.TextBox, ...]], LabHomeReading] = read_home,
        picker_reader: Callable[[Image, tuple[ocr.TextBox, ...]], LabPickerReading] = read_picker,
        confirmation_reader: Callable[[Image, tuple[ocr.TextBox, ...]], LabConfirmationReading]
        = read_confirmation,
    ) -> None:
        self.templates = templates
        self.home_reader = home_reader
        self.picker_reader = picker_reader
        self.confirmation_reader = confirmation_reader
        self._state = "idle"
        self._started_at = 0.
        self._scans = 0
        self._picker_signature: tuple[int, int, tuple[int, int]] | None = None
        self._picker_reads = 0
        self._picker_name: str | None = None
        self._dialog_signature: tuple[str, int, int, tuple[int, int]] | None = None
        self._dialog_reads = 0
        self._slot: LabHomeReading | None = None
        self._purchase: LabDecision | None = None
        self._confirmed_reading: LabHomeReading | None = None
        self._confirmed_frame: LabsReading | None = None
        self._outcome: LabVisitResult | None = None
        self._slot2_home: LabHomeReading | None = None
        self._slot2_signature: tuple[int, int, tuple[int, int]] | None = None
        self._slot2_reads = 0
        self._slot2_confirm_reads = 0
        self.last_tap: tuple[str, int, int] | None = None

    @property
    def active(self) -> bool:
        return self._state != "idle"

    def request(self) -> bool:
        if self.active:
            return False
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
        self._confirmed_reading = None
        self._confirmed_frame = None
        self._outcome = None
        self._slot2_home = None
        self._slot2_signature = None
        self._slot2_reads = 0
        self._slot2_confirm_reads = 0
        self.last_tap = None
        return True

    def tab_unlocked(self, screen: Image) -> bool:
        """Return whether the actionable Labs tab is visible on this frame."""
        return self._match(screen, "nav/tab_labs.png") is not None

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
        if self._slot2_home is not None and outcome.status != "slot2_unlocked":
            outcome = replace(outcome, slot2_status=self._slot2_home.slot2_status,
                              gem_balance=self._slot2_home.gem_balance)
        self._outcome = outcome
        self._state = "return"

    @staticmethod
    def _recorded_home(screen: Image, home: LabHomeReading) -> LabsReading:
        height, width = screen.shape[:2]
        complete = home.slots_owned == 1 and home.job is not None
        return LabsReading(time.time(), width, height,
                           hashlib.sha256(screen.tobytes()).hexdigest(),
                           home.slots_owned, "observed" if complete else "unreadable",
                           (), (home.job,) if home.job is not None else ())

    def advance(
        self, screen: Image, boxes: tuple[ocr.TextBox, ...],
        device: AdbDevice, now: float,
    ) -> LabVisitResult | None:
        if not self.active:
            return None
        self.last_tap = None
        if self._started_at == 0.:
            self._started_at = now
        self._scans += 1
        if self._scans > 24 or now - self._started_at > 90:
            return self._finish(LabVisitResult("failed", "visit_timeout", LabDecision("unknown")))

        home = self.home_reader(screen, boxes)
        picker = self.picker_reader(screen, boxes)
        confirmation = self.confirmation_reader(screen, boxes)
        if self._scans > 18 and self._state != "return":
            if home.page or picker.page or confirmation.page:
                self._return(LabVisitResult("failed", "visit_timeout",
                                             LabDecision("unknown")))
            else:
                return self._finish(LabVisitResult("failed", "visit_timeout",
                                                    LabDecision("unknown")))
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
            if (home.slot2_status == "locked" and home.slot2_price == 100
                    and home.gem_balance is not None and home.gem_balance >= 100
                    and home.slot2_point is not None):
                signature = (home.slot2_price, home.gem_balance, home.slot2_point)
                if signature != self._slot2_signature:
                    self._slot2_signature = signature
                    self._slot2_reads = 1
                    return None
                self._slot2_reads += 1
                if self._slot2_reads < 2:
                    return None
                self._tap(device, home.slot2_point, "unlock_lab_two")
                self._state = "confirm_slot2"
                return None
            decision = decide(home, None)
            if decision.kind == "inspect" and home.slot_point is not None:
                self._slot = home
                self._tap(device, home.slot_point, "open_lab_one")
                self._state = "picker"
            else:
                self._return(LabVisitResult("observed", decision.kind, decision,
                                             confirmed_job=home.job))
            return None

        if self._state == "confirm_slot2":
            before = self._slot2_home
            if before is None or before.gem_balance is None:
                return self._finish(LabVisitResult("failed", "missing_gem_balance",
                                                    LabDecision("unknown")))
            if (home.page and home.slot2_status == "owned"
                    and home.gem_balance == before.gem_balance - 100):
                self._slot2_confirm_reads += 1
                if self._slot2_confirm_reads >= 2:
                    self._return(LabVisitResult(
                        "slot2_unlocked", "slot_two_confirmed", LabDecision("unknown"),
                        slot2_status="owned", gem_balance=home.gem_balance,
                        gems_before=before.gem_balance, observed_gem_spend=100))
            else:
                self._slot2_confirm_reads = 0
            return None

        if self._state == "picker":
            if not picker.page or self._slot is None:
                return None
            decision = decide(self._slot, picker)
            if decision.kind != "start":
                self._return(LabVisitResult("observed", decision.kind, decision))
                return None
            assert picker.buy_point is not None
            assert decision.price is not None and decision.wallet_coins is not None
            signature = (decision.price, decision.wallet_coins, picker.buy_point)
            if signature != self._picker_signature:
                self._picker_signature = signature
                self._picker_reads = 1
                return None
            self._picker_reads += 1
            if self._picker_reads < 2:
                return None
            self._purchase = decision
            self._picker_name = picker.game_speed.raw_name if picker.game_speed is not None else None
            self._tap(device, picker.buy_point, "start_game_speed")
            self._state = "dialog"
            return None

        if self._state == "dialog":
            if not confirmation.page:
                return None
            purchase = self._purchase
            if (purchase is None or confirmation.name != self._picker_name
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
            self._tap(device, confirmation.research_point, "confirm_game_speed")
            self._state = "confirm"
            return None

        if self._state == "confirm":
            purchase = self._purchase
            if purchase is None:
                return self._finish(LabVisitResult("failed", "missing_purchase", LabDecision("unknown")))
            if home.page and home.slot_status == "researching" and home.job is not None:
                job = home.job
                if (job.concept_id == "labs.game-speed" and job.slot == 1
                        and home.coin_balance is not None
                        and purchase.wallet_coins is not None
                        and purchase.price is not None
                        and home.coin_balance == purchase.wallet_coins - purchase.price):
                    previous = self._confirmed_reading
                    if previous is None:
                        self._confirmed_reading = home
                        self._confirmed_frame = self._recorded_home(screen, home)
                    elif (previous.job is not None
                          and previous.job.concept_id == job.concept_id
                          and previous.job.completes_at is not None
                          and job.completes_at is not None
                          and abs(previous.job.completes_at - job.completes_at) <= 2):
                        self._return(LabVisitResult("started", "game_speed_confirmed", purchase,
                                                     confirmed_job=job,
                                                     observed_coin_spend=purchase.price,
                                                     confirmed_readings=(self._confirmed_frame,
                                                                         self._recorded_home(screen, home))
                                                     if self._confirmed_frame is not None else ()))
                    else:
                        self._confirmed_reading = home
                        self._confirmed_frame = self._recorded_home(screen, home)
                else:
                    self._return(LabVisitResult("failed", "purchase_unconfirmed",
                                                 LabDecision("unknown")))
            return None

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
                point = self._match(screen, "nav/tab_battle.png")
                if point is not None:
                    self._tap(device, point, "return_to_battle")
                return None
            if pages.classify_page(screen, self.templates).page == "MAIN_MENU":
                return self._finish(self._outcome or LabVisitResult(
                    "failed", "no_outcome", LabDecision("unknown")))
            return None
        return None
