"""Background automation bot for the Android game "The Tower".

Game actions run through ADB, so the emulator window never needs focus and the
mouse is never hijacked. The optional BlueStacks Air host update notice check
uses a guarded macOS Accessibility press outside the Android game:

    screen capture  ->  device.screenshot()     (PIL image, converted in memory)
    template match  ->  cv2.matchTemplate
    click           ->  device.click(x, y)       (an `input tap` under the hood)

Usage:
    python tower_bot.py                 # run the loop
    python tower_bot.py --once          # a few scans, enough to settle on the real screen
    python tower_bot.py --debug-scores  # one frame, one table of every template's score
    python tower_bot.py --tui           # live panel instead of log lines
    python tower_bot.py --web           # dashboard: pause, retune, loop runs
    python tower_bot.py --web --idle    # dashboard with no bot - press Start
"""

from __future__ import annotations

from account_collection import StatsCollection, at_home
from cards_intro import CardsIntro, popup_visible as cards_popup_visible
import battle_menu
import battle_upgrade_info
import events_badge
import events_screen
from battle_menu_state import BattleMenuState
from battle_menu_visit import BattleMenuVisit, Outcome as BattleMenuOutcome
import milestones_badge
import menu_badges
from notification_state import MissionReceiptBus, NotificationState
from evidence_scope import FactScope
import mail_screen
import nav_arrow
import tier_select
from milestones_claim import MilestonesClaim
from milestones_screen import MilestonesReadings, parse_frame as parse_milestones_frame
from missions_claim import MissionsClaim
from missions_screen import MissionsReadings
from missions_visit import MissionsVisit
from lab_plan import LabDecision, LabVisitOptions
from lab_visit import LabVisit
from lab_routes import research_gate, unlock_gate
import lab_screen
from identity_reverify import IdentityReverifier
from labs import LabsState
from local_env import load_local_env
from account_state import AccountState, AccountRepository
from account_screens import ScreenReadings

import argparse
import hashlib
import dataclasses
import ipaddress
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, Callable

from adbutils import AdbDevice

if TYPE_CHECKING:
    from fastapi import FastAPI

import claim_schedule
import config
import db
import digits
import events
import free_ticket
import game_over
import gem_claim
import stall_watchdog
import jitter
import ledger
import ocr
import pages
import screens
import speed
from maintenance_schedule import DueAction, MaintenanceSchedule
import transactions
import unlocked_screen
import vision
from affordability import (
    AffordabilityCheck,
    BrightnessAffordability,
    DigitAffordability,
)
from autopilot import AutopilotState, BattleAutopilot
from combat_context import RunIdentity, build_revision, frame_combat
from perception import observe_frame, panel_visible, read_cash
from control import Controls, Live
from device import EmulatorError, Image, capture_screen, connect_device, tap
from fleet.runtime import RuntimeIsolationError, WorkerRuntime, reserve_endpoint
from fleet.identity import Attempt
from frames import FrameBuffer
from navigate import Navigator
from runner import BotRunner, RunnerError
from runtime_identity import PROCESS_IDENTITY
from runtime_records import PROCESS_BOOT_ID, RuntimeRecords
from supervisor import DeviceSupervisor, RecoveryState
from runs import RunTracker
from shopping import ShoppingSession, header_numbers
from snapshots import SnapshotWriter
from sinks.log import LogSink
from sinks.sse import SseSink
from sinks.state import BotState, StateSink
from sinks.store import StoreSink
from sinks.tui import TuiSink
from strategy import MIN_INTERVAL, ControlError, Shopping, Strategy, StrategyStore
from telegram_report import TelegramConfig, TelegramReporter
from telegram_settings import TelegramSettingsStore, legacy_telegram_interval_from_env

logger = logging.getLogger("tower_bot")
_FAILED_MILESTONES_RETRY_SECONDS = 60.
# A planned Lab start that ended without a verified start is not re-armed for
# the same (slot, research, level, revision) and slot evidence until this passes.
LAB_ACTION_BACKOFF_SECONDS = 900.


def popup_flags(boxes: tuple[ocr.TextBox, ...]) -> tuple[bool, bool]:
    """(online_required, session_conflict) from one read of the frame."""
    text = " ".join(box.text.lower() for box in boxes)
    online_required = (
        ("online" in text and ("required" in text or "connect" in text))
        or ("internet" in text and ("required" in text or "connect" in text))
    )
    session_conflict = (
        ("another device" in text and ("logged in" in text or "in use" in text))
        or "logged in elsewhere" in text
        or "new session detected" in text
        or "cloud session different than local session" in text
    )
    return online_required, session_conflict


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------
def _recovery_frame_image(frame: Any) -> Any:
    """Lazy import: recovery modules load only when recovery is in use."""
    from recovery_image import encode_recovery_image
    return encode_recovery_image(frame)


class TowerBot:
    def __init__(
        self,
        device: AdbDevice,
        templates: vision.TemplateCache,
        bus: events.EventBus,
        affordability_check: AffordabilityCheck | None = None,
        controls: Controls | None = None,
        checks: dict[str, AffordabilityCheck | None] | None = None,
        reader: digits.NumberReader | None = None,
        shopping: ShoppingSession | None = None,
        first_run_id: int = 1,
        best_wave: int | None = None,
        frames: FrameBuffer | None = None,
        screen_confirmations: int = config.SCREEN_CONFIRMATIONS,
        navigation_cooldown: float = config.NAVIGATION_COOLDOWN_SECONDS,
        autopilot_state: AutopilotState | None = None,
        account_state: AccountState | None = None,
        collection: StatsCollection | None = None,
        visit: MissionsVisit | None = None,
        claim: MissionsClaim | None = None,
        missions: MissionsReadings | None = None,
        milestones_claim: MilestonesClaim | None = None,
        milestones: MilestonesReadings | None = None,
        unknown_dir: Path | None = None,
        supervisor: DeviceSupervisor | None = None,
        reroll_progress: Any | None = None,
        progress: Any | None = None,
        recovery: Any | None = None,
        identity_reverifier: Callable[[], None] | None = None,
    ) -> None:
        self.account_state = account_state
        self._screen_readings = account_state.screen_readings if account_state is not None else ScreenReadings()
        self._screen_readings.reset_current()
        # Owned by the runner when there is one, so a browser can arm a
        # transaction the loop then walks. A bot built without one gets its
        # own idle instance rather than an AttributeError on every scan.
        self.collection = collection if collection is not None else StatsCollection()
        # The same arrangement for the Missions page: the runner owns them
        # when there is one, and a bot built without either gets its own.
        self.visit = visit if visit is not None else MissionsVisit()
        self.claim = claim if claim is not None else MissionsClaim()
        self.missions = missions if missions is not None else MissionsReadings()
        # The MILESTONES ladder and its reward modal, on the identical terms.
        self.milestones_claim = (milestones_claim if milestones_claim is not None
                                else MilestonesClaim())
        self.milestones = milestones if milestones is not None else MilestonesReadings()
        # Owned by the bot, not the runner: nothing arms it from the browser.
        # It is offered from the main menu whenever the Cards tab carries its
        # green "new" arrow - see _offer_cards_intro.
        self.cards_intro = CardsIntro()
        self.device = device
        self.supervisor = supervisor
        self.progress = progress
        self.templates = templates
        self.bus = bus
        # Optional: --tui and --once have nobody to show a frame to, and every
        # test predating this constructs a bot without one.
        self.frames = frames
        self.affordability: AffordabilityCheck = affordability_check or BrightnessAffordability()
        self.reader = reader if reader is not None else digits.NumberReader()
        # Built once, per process, by build_shopping() - see that function's
        # docstring for why (the header glyph atlas it gates on is exactly as
        # expensive to build as the digit atlas `checks` already amortises).
        # Absent entirely, every existing test that constructs a bot without
        # `shopping=` still gets a session - just one that carries its own
        # reason and declines every begin(), rather than an AttributeError
        # the first time run_once() reaches self.shopping.active.
        self.shopping: ShoppingSession = shopping if shopping is not None else ShoppingSession(
            templates, bus, self.reader,
            disabled_reason="no shopping session was configured for this bot",
        )
        self.wallet: int | None = None
        self.autopilot = BattleAutopilot(autopilot_state, bus, account_state)
        self.shopping.account_state = account_state
        safety_journal = getattr(self.shopping, 'journal', None)
        if account_state is not None and isinstance(safety_journal, transactions.TransactionJournal):
            account_state.attach_safety_storage(safety_journal.path)
        self.reroll_progress = reroll_progress
        self.lab_runtime = None
        self.maintenance = MaintenanceSchedule()
        self.maintenance_status: str | None = None
        self._deadline_speed: DueAction | None = None
        self._lab_visit_revision: int | None = None
        # Planned Lab choices whose route is uncalibrated: visible, never executed.
        self.lab_route_pending: tuple[str, ...] = ()
        # Last armed planned Lab start: (key, slot evidence, backoff-until).
        self._lab_action_last: tuple[tuple, tuple | None, float] | None = None
        self.lab_visit: LabVisit | None = (LabVisit(templates, journal=safety_journal,
            account_state=account_state, slot_observer=self._observe_lab_runtime,
            authorize=self._authorize_lab, event_sink=self.bus.publish)
            if reroll_progress is not None or account_state is not None and safety_journal is not None else None)
        self.lab_state: LabsState | None = (LabsState(account_state)
                                          if self.lab_visit is not None else None)
        self._bind_lab_runtime()
        self._last_lab_confirmation: tuple[float | None, int, int] | None = None
        self._last_wave_progress: tuple[int | None, int] | None = None
        # The last HUD wave the autopilot read. Reported on IN_RUN scans only.
        self._scan_wave: int | None = None
        self._last_verified_purchases = self.autopilot.state.snapshot()['verified_purchases']
        self._research_until: float | None = None
        self._progress_wait: tuple[str, str, float] | None = None
        self._reroll_shopping_policy = None
        self._reroll_shopping_base = None
        if reroll_progress is not None:
            self.shopping.reroll_observe_price = reroll_progress.observe_price
            self.shopping.reroll_observe_prices = getattr(reroll_progress, "observe_prices", None)
            self.shopping.reroll_replan = self._replan_reroll_shopping
            self.shopping.reroll_purchase_reason = getattr(reroll_progress, "purchase_reason", None)
            self.shopping.price_quotes = getattr(reroll_progress, 'price_quotes', None)
            self.shopping.inspection_resolved = getattr(reroll_progress, 'inspection_resolved', None)
        self.shopping.observations = self.autopilot.state
        # Read once, here, rather than per scan: both configure an object
        # that carries state across scans (the tracker's part-confirmed
        # reading, the navigator's last-navigation timestamp), and changing
        # either under a running one has no correct answer. This is the
        # "applies on next Start" boundary the dashboard labels.
        self.tracker = screens.ScreenTracker(confirmations=screen_confirmations)
        self.stall_dir = unknown_dir if unknown_dir is not None else config.UNKNOWN_DIR
        self.shopping.evidence_dir = self.stall_dir
        self.stall_watchdog = stall_watchdog.StallWatchdog(
            time.time, no_effect_limit=config.STALL_NO_EFFECT_LIMIT,
            blocked_limit=config.STALL_BLOCKED_SECONDS)
        # O4 model-assisted recovery: a RecoveryCoordinator the runner owns
        # and closes. Stepped only on this scan thread; see _step_recovery.
        self.recovery = recovery
        # Bounded in-process account re-verification (runner-provided, reroll
        # workers only). Shared with the Workshop inspection so a stale
        # identity cannot hold a pending purchase forever.
        self.identity_reverification: IdentityReverifier | None = (
            IdentityReverifier(identity_reverifier) if identity_reverifier is not None else None)
        self.shopping.identity_reverifier = self.identity_reverification
        self._recovery_screen: str | None = None
        self._recovery_screen_generation = 0
        self._recovery_dispatched: Any = None
        self._recovery_acted = False
        self._recovery_status_key: str | None = None
        self._recovery_failed = False
        if recovery is not None and isinstance(safety_journal, transactions.TransactionJournal):
            recovery.set_journal(safety_journal)
        self.snapshots = SnapshotWriter(
            unknown_dir if unknown_dir is not None else config.UNKNOWN_DIR,
            config.UNKNOWN_MIN_INTERVAL,
            config.UNKNOWN_KEEP,
            config.UNKNOWN_HASH_DISTANCE,
        )
        # One source of truth for every live setting. A Controls built here
        # would need a Strategy to hold, and inventing one would compete with
        # StrategyStore.ensure_seeded() - so callers build it and pass it.
        self.controls = controls if controls is not None else Controls(
            strategy=Strategy.from_config()
        )
        # Built once, here, and never in a request handler. A None entry means
        # that strategy is unavailable on this machine - which is what lets
        # PATCH /api/control refuse it with a reason instead of silently
        # handing back a different check.
        self.checks: dict[str, AffordabilityCheck | None] = checks or {
            self.controls.snapshot().strategy.affordability: self.affordability
        }
        self.navigator = Navigator(templates, bus, cooldown=navigation_cooldown)
        self.speed = speed.SpeedController(bus=bus)
        # Always on, with no strategy field of its own: the gem is free, it
        # is rare, and there is no configuration anyone would want other
        # than "take it". It still only ever acts inside a confirmed,
        # unpaused battle - that is the branch it lives in, not a setting.
        self.gem = gem_claim.FloatingGemClaim(bus=bus, reader=self.reader)
        # Consecutive scans a menu page has held every action with nothing
        # walking. See the deadlock note in run_once.
        self._held_scans = 0
        # When the IN_RUN preflight last read the whole frame. See
        # _preflight_boxes: the backstop for a popup the bands cannot see.
        self._battle_full_read_at = float("-inf")
        # Whether this scan's IN_RUN preflight ran the backstop's full read,
        # successful or not; the menu readers run on such a scan too.
        self._battle_backstop_scan = False
        # Upgrade-info panels can cover the death dialog while its cash HUD
        # still reads IN_RUN. Only a fresh, labelled overlay can authorize a
        # dismissal; retries are spaced and capped until a clear full read.
        self._battle_info_dismiss_at = float("-inf")
        self._battle_info_dismiss_count = 0
        self._battle_info_full_read_at = float("-inf")
        # Consecutive scans device recovery has blocked while a walk was
        # armed. See the note beside the recovery gate in run_once.
        self._recovery_blocked_scans = 0
        self.runs = RunTracker(first_run_id)
        # When each claim last landed, and the wave each tier's ladder was
        # claimed at. In-memory for this slice: a restart re-offers a claim,
        # and the walk itself refuses if there is nothing to take. Persisting
        # this belongs with the wallet, in B07.
        self._last_claim: dict[str, float] = {}
        # Every tier has its own MILESTONES ladder, so the wave that owes a
        # claim is the best on the tier last played, not the account's best
        # overall. Keyed by the tier the death modal read; None is "no run
        # with a read tier has ended yet", which falls back to `_best_wave`.
        self._ladder_tier: int | None = None
        self._tier_best_wave: dict[int, int] = {}
        self._claimed_wave: dict[int | None, int] = {}
        # Claim accounting is provisional until the menu walk finishes. A
        # failed lookup must restore the prior wave and badge, then retry
        # with a short backoff instead of hiding a visible reward for an hour.
        self._milestones_attempt: tuple[int | None, int | None, float | None, bool] | None = None
        self._milestones_retry_at = 0.
        # Whether the last main menu frame that showed the MILESTONES button
        # showed its badge - see milestones_badge.
        self._milestones_badge = False
        self._missions_badge = False
        self._mail_badge = False
        self._events_badge = False
        # When the tier arrow was last tapped: the panel redraws after a tap,
        # so the next read waits out the same cooldown navigation does.
        self._tier_tap_at = float('-inf')
        self._last_menu_badge_check_at: float | None = None
        self._notifications = NotificationState()
        self._notification_scope: dict[str, Any] | None = None
        self._capture_sequence = 0
        self._mission_attempt = False
        self._mail_recovery_taps = 0
        self._mail_recovery_at = float('-inf')
        # The account's best-ever wave. Seeded here from whatever the caller
        # read at startup (see prepare_store(), which reads db.best_wave())
        # and kept fresh in-process from every RunEnded from then on (see
        # run_once) rather than queried per scan. None means unread, not
        # zero: claim_schedule.due() deliberately owes nothing on a None
        # best wave rather than guessing.
        self._best_wave: int | None = best_wave
        self._screen: Image | None = None
        self._last_click: dict[str, float] = {}
        # Which pace run_forever waits at after this scan - see
        # Strategy.interval_for. Starts False: the first frame is usually home.
        self._last_scan_in_battle = False
        self._running = True
        # Makes the between-scan sleep interruptible. A plain time.sleep()
        # ignores stop(): PEP 475 means it resumes after a signal handler
        # returns rather than aborting, and at a browser-set MAX_INTERVAL of
        # 3600s that turns Ctrl+C into an hour-long hang.
        self._stopping = threading.Event()
        # The in-battle menu's badged icons: a visit is opportunistic, not
        # scheduled, so it owns no strategy field of its own either - see
        # the gem's comment above for why that pattern repeats here.
        self.battle_menu = BattleMenuVisit(
            bus, self.templates,
            BattleMenuState(self.progress.path.with_name("battle-menu-state.json")
                            if self.progress is not None else None),
            sleep=self._stopping.wait,
        )

    @property
    def screen_state(self) -> screens.ScreenState:
        return self.tracker.state

    # -- screen state ------------------------------------------------------
    def refresh_screen(self) -> Image:
        """Capture a fresh frame and keep it as the current screen."""
        self._screen_fact_scope = self.account_state.verified_scope if self.account_state is not None else None
        if self.progress is None:
            self._screen = capture_screen(self.device)
        else:
            with self.progress.phase('capture', 20):
                self._screen = capture_screen(self.device)
            self.progress.observe_capture()
        self._screen_captured_at = time.time()
        self._screen_captured_monotonic = time.monotonic()
        self._capture_sequence += 1
        self._bind_lab_runtime()
        self._bind_notification_scope()
        if self.frames is not None:
            self.frames.publish(self._screen)
        return self._screen

    def _bind_notification_scope(self) -> None:
        """Use only an account-verified worker attempt for durable state."""
        if self.progress is None:
            return
        heartbeat = self.progress.snapshot()
        keys = ("account_id", "lease_id", "attempt_id", "generation")
        if any(not isinstance(heartbeat.get(key), str) or not heartbeat[key]
               for key in keys):
            return
        scope = {key: heartbeat[key] for key in keys}
        verified = (getattr(self.account_state, "verified_scope", None)
                    if self.account_state is not None else None)
        if (isinstance(verified, FactScope)
                and verified.account_id == scope["account_id"]
                and verified.lease_id == scope["lease_id"]
                and verified.generation == scope["generation"]):
            scope["fact_epoch"] = verified.epoch
        if scope != self._notification_scope:
            if self._notification_scope is not None and self._mission_attempt:
                self.claim.cancel("account_scope_changed",
                                  "Mission claim scope changed before confirmation.")
                self._mission_attempt = False
            self._notifications = NotificationState(
                self.progress.path.with_name("mission-notification-state.json"),
                scope=scope)
            self._notification_scope = scope

    def _observe_menu_notifications(self, now: float) -> None:
        """Observe fixed menu controls even while paused; observation taps nothing."""
        frame_id = f"{PROCESS_BOOT_ID}:{self._capture_sequence}"
        missions = menu_badges.read_notification(self.screen, self.templates, "missions")
        labs = menu_badges.read_notification(self.screen, self.templates, "labs")
        self._notifications.observe("missions", missions, now, frame_id=frame_id)
        self._notifications.observe("labs", labs, now, frame_id=frame_id)
        self._missions_badge = missions is True
        self._mail_badge = menu_badges.read_badge(
            self.screen, self.templates, "mail") is not None
        self._events_badge = events_badge.badge_visible(self.screen, self.templates) is True
        self._last_menu_badge_check_at = now

    @property
    def screen(self) -> Image:
        if self._screen is None:
            return self.refresh_screen()
        return self._screen

    def run_identity(self, settings: Live) -> RunIdentity:
        """Which run, and which build, this scan is observing.

        Assembled once per scan from three independent sources - the run
        tracker's open run, the account revision the Workshop was last read
        into, and the purpose the run was started for - because a battle fact
        is only meaningful under all three. Any of them may be None: not
        knowing the build is not evidence that the build changed, and
        combat_context treats it that way.
        """
        return RunIdentity(
            run_id=self.runs.current_id,
            build_revision=build_revision(self.account_state),
            purpose=settings.strategy.autopilot.purpose,
        )

    # -- the core helper ---------------------------------------------------
    def find_and_click_image(
        self,
        action: config.Action,
        boxes: list[dict[str, Any]] | None = None,
        tuning: Strategy | None = None,
    ) -> bool:
        """Find the action's template on the current screen and tap it.

        Rejects are ordered cheapest-first (spec section 7): screen, then
        match score, then brightness, then cooldown.

        `boxes`, when given, collects this match for the overlay - a plain
        local list owned by run_once() for the whole scan, not FrameBuffer
        directly. run_once() hands the finished list to frames.set_boxes()
        in one atomic swap once every action has been tried, rather than
        each match landing in the buffer the instant it is found: a reader
        between two such landings would catch the frame's matches only
        partially drawn.

        `tuning`, when given, carries every policy value this method
        reads - the click_cooldown to gate against and the three jitter
        numbers - as ONE object, which run_once() takes from the single
        snapshot at the top of its pass. `None` (the default) falls back to
        a fresh `self.controls.snapshot()` here instead, which is what lets
        a test or any other direct caller invoke this method without first
        constructing a settings object of its own. One object rather than a
        scalar per field is deliberate: the four values must describe the
        same instant as each other, and threading them separately is how
        they would eventually stop doing so. Re-reading per call is exactly
        what the loop path must NOT do, though: this
        method is called once per matched rule inside run_once()'s action
        loop without a `break`, and a PATCH landing between two of those
        calls (run_forever and serve_web run on different threads) would
        otherwise let one row's cooldown be judged against a click_cooldown
        from a different instant than the strategy that selected and
        ordered the rows - the same class of hazard run_once()'s own
        docstring warns about for the wallet and the price gate, just on a
        narrower field.
        """
        # `cooldown_key` is purely internal - a stable per-template handle
        # for `_last_click`. `name` is what leaves the class: it is what
        # every Tapped/Skipped event carries, so the log, the dashboard feed
        # and the stored `events.action` column all speak the same
        # vocabulary as the control page, which gates on action.name too.
        cooldown_key = action.template
        name = action.name

        if self.tracker.state is not screens.ScreenState.IN_RUN:
            # Defence in depth, and deliberately silent. run_once already
            # skips the whole action loop and publishes exactly one
            # screen_gated event per scan; publishing here too would emit one
            # per action - 4 identical events every 2s, ~172k a day.
            return False

        template = self.templates.get(action.template)
        match = vision.locate_template(self.screen, template, action.threshold)
        if match is None:
            return False

        # The BUY point, not the label's own centre - config.buy_point()
        # documents why: the label is itself a button, so a tap there (or a
        # crosshair drawn there) marks the wrong square. Computed once, up
        # front, so the box recorded below and the eventual tap agree.
        #
        # Jittered HERE, before the box below is recorded and before the
        # Tapped event is published, rather than inside device.tap(): the
        # overlay's crosshair and the event's coordinates must be the pixel
        # actually tapped, not the pixel we would have tapped without
        # jitter. Putting the offset in tap() would make both lie by up to
        # tap_jitter_px, on exactly the page you open to find out why a
        # purchase missed.
        policy = self.controls.snapshot().strategy if tuning is None else tuning
        tap_x, tap_y = jitter.point(
            *config.buy_point(match.top_left), policy.tap_jitter_px
        )

        box: dict[str, Any] | None = None
        if boxes is not None:
            # Recorded whether or not the tap happens, because "matched but
            # rejected" is exactly what you open the device view to see.
            height, width = template.shape[:2]
            box = {
                "name": name,
                "x": int(match.top_left[0]),
                "y": int(match.top_left[1]),
                "w": int(width),
                "h": int(height),
                "tap_x": int(tap_x),
                "tap_y": int(tap_y),
                "score": float(match.score),
                "tapped": False,
            }
            boxes.append(box)

        ok, detail = self.affordability.affordable(
            self.screen, match, template, action
        )
        if not ok:
            # "unaffordable" means we read both numbers and the wallet was
            # short. "dimmed" means we could not tell and fell back to the
            # brightness heuristic. Collapsing them would hide exactly the
            # regression phase 3 exists to fix.
            reason = (
                "unaffordable"
                if self.affordability.last_price is not None
                else "dimmed"
            )
            self.bus.publish(
                events.Skipped(action=name, reason=reason, detail=detail)
            )
            return False

        now = time.monotonic()
        # stretch(), not spread(): click_cooldown is a functional minimum
        # (see config.CLICK_COOLDOWN_SECONDS), so jitter may only ever make
        # the gap longer. Jittering it downward would re-admit the burst of
        # taps on an already-animating button that the cooldown exists to
        # stop.
        effective_cooldown = jitter.stretch(
            policy.click_cooldown, policy.timing_jitter
        )
        if now - self._last_click.get(cooldown_key, 0.0) < effective_cooldown:
            self.bus.publish(events.Skipped(action=name, reason="cooldown"))
            return False

        # The reaction gap. Passing the stop event's wait() as the sleeper
        # keeps Stop instant: a shutdown ends the pause instead of sleeping
        # it out.
        jitter.pause(
            policy.tap_delay, policy.timing_jitter, sleep=self._stopping.wait
        )
        tap(self.device, tap_x, tap_y)
        self._last_click[cooldown_key] = now
        self.bus.publish(
            events.Tapped(
                action=name,
                x=tap_x,
                y=tap_y,
                score=match.score,
                price=self.affordability.last_price,
                wallet=self.affordability.last_wallet,
            )
        )
        if box is not None:
            # `box` is still the same dict already appended to `boxes` above
            # - the swap into FrameBuffer has not happened yet, so there is
            # nothing there yet for mark_tapped() to find by name. Flipping
            # the local dict in place is what mark_tapped() would do to it
            # once it is FrameBuffer's; FrameBuffer.mark_tapped() itself
            # stays available for a caller that already holds published
            # boxes (see its own tests).
            box["tapped"] = True
        return True

    # -- the death modal ---------------------------------------------------
    def _read_modal_stats(
        self, ended: events.RunEnded, anchor: tuple[int, int]
    ) -> events.RunEnded:
        """Fill wave / coins / tier from the death modal.

        Only ever called on a CONFIRMED game over, so the modal has already
        survived two consecutive readings and its fade has finished - the same
        debounce that stops phantom run boundaries also guarantees the numbers
        are fully drawn.

        Wave holds a constant offset from the matched game_over template, but
        tier and coins do not: a record run grows a "New Highest Wave!" line
        that pushes them down 49px while the modal's top edge rises as it
        re-centres. Those two are found by their own caption instead.
        """
        killed_by, ad_coins = self._read_modal_extras()
        return dataclasses.replace(
            ended,
            wave=self.reader.read(
                self.screen, config.MODAL_WAVE_REGION, anchor, "modal"
            ),
            coins=self.reader.read_at_caption(
                self.screen, config.MODAL_COINS_CAPTION, config.MODAL_COINS_REGION,
                "modal",
            ),
            tier=self.reader.read_at_caption(
                self.screen, config.MODAL_TIER_CAPTION, config.MODAL_TIER_REGION,
                "modal",
            ),
            killed_by=killed_by,
            ad_coins=ad_coins,
        )

    def _read_modal_extras(self) -> tuple[str | None, int | None]:
        """Killed By and ad coins by OCR on the confirmed game-over frame.

        Best effort by design: the glyph-read wave/coins/tier stay the run's
        authority, and nothing here may stop the run from ending.
        """
        try:
            return game_over.run_extras(game_over.parse_frame(self.screen, ocr.read(self.screen)))
        except Exception:
            logger.exception("Game-over OCR failed; killed_by and ad_coins left empty")
            return None, None

    # -- main loop ---------------------------------------------------------
    def run_cap_reached(
        self, max_runs: int | None = None, strategy: Strategy | None = None
    ) -> bool:
        """Has the bot completed as many runs as it was asked for?

        The explicit parameter wins when set - that is the non-web path,
        which has a CLI flag and no strategy loaded. Otherwise the strategy
        supplies it. The two can never both be meaningfully set in the web
        path, because the CLI persists its flag into the strategy rather
        than carrying it alongside (see the spec, section 10).

        The `strategy` fallback to a fresh snapshot is NOT the re-read hazard
        that was removed from find_and_click_image: run_once always hands
        down the snapshot its own pass took, so a mid-pass PATCH cannot split
        one scan across two policies, while run_forever calls this with no
        strategy *between* passes - where reading the newest one is the whole
        point, because a run cap raised from the dashboard should take effect
        on the next iteration rather than the next restart.
        """
        cap = max_runs
        if cap is None:
            source = strategy if strategy is not None else self.controls.snapshot().strategy
            cap = source.max_runs
        return cap is not None and self.runs.completed >= cap

    def _manage_speed(
        self,
        settings: Live,
        anchor: tuple[int, int] | None,
        commands: tuple[str, ...],
    ) -> bool:
        """Drive the in-battle speed widget: browser commands, then policy.

        Both paths are refused off the battle screen and while paused. The
        arrow coordinates are anchored to the IN_RUN template, so away from
        that screen they are not "the wrong button" - they are a point on
        whatever menu happens to be showing, which on the workshop page is a
        purchase.

        A command SUPPRESSES the target for the rest of this scan, and that is
        not politeness - it is correctness. settle() decides from
        `self.screen`, captured before the command's tap landed, so the game
        has not redrawn the readout yet. Left to run, it would decide from a
        reading the tap just invalidated and fire a second time, turning one
        button press into two steps and overshooting the very target it was
        heading for.

        The suppression lasts exactly one scan. The target is standing policy,
        so the next pass - working from a fresh frame - takes over again and
        pulls the speed back. Nothing tries to reconcile the two beyond that:
        a user who wants a manual speed to stick clears the target.
        """
        if anchor is None or settings.paused:
            return False
        if self.shopping.reconciliation_pending or self.autopilot.pending is not None:
            self._maintenance_reason('pending_purchase')
            return False

        current = speed.read(self.screen, self.templates, anchor)
        scope = getattr(self, '_screen_fact_scope', None)
        captured = getattr(self, '_screen_captured_at', None)
        if (self.lab_runtime is not None and self.account_state is not None
                and scope is not None and scope == self.account_state.verified_scope
                and captured is not None and self.account_state.accepts_capture(scope, captured)
                and 0 <= time.time()-captured <= 30.):
            self.lab_runtime.observe_speed(current, observed_at=captured)

        for command in commands:
            self.speed.tap(
                self.device,
                "up" if command == "speed_up" else "down",
                anchor,
                tuning=settings.strategy,
                source="web",
            )
        if commands:
            return True

        manual = settings.strategy.target_speed
        auto = settings.strategy.auto_fastest
        target = manual
        if target is None and self.reroll_progress is not None:
            target = self.reroll_progress.speed_target()
        if auto and manual is None:
            capability = self.lab_runtime.snapshot().verified_speed if self.lab_runtime is not None else None
            target = max(target or 0., capability or 0., current or 0.) or None
        if manual is not None or auto:
            for action in self.maintenance.due(time.time()):
                if action.kind == 'speed_check' and current is not None and self.maintenance.claim(action):
                    self.speed.rearm_completion(action.generation)
                    self._deadline_speed = action
            if auto and manual is None and self._deadline_speed is not None:
                target = max(target or 0., self._deadline_speed.target_speed or 0.) or None
        if self._deadline_speed is not None:
            reason = ('speed_unreadable' if current is None else
                      'speed_check_exhausted' if self.speed.exhausted else 'verifying_speed')
            self._maintenance_reason(reason)

        changed = self.speed.settle(
            self.screen,
            self.device,
            self.templates,
            target=target,
            anchor=anchor,
            tuning=settings.strategy,
        ) is not None
        if any(a.reason == 'timer_correction_limit' for a in self.maintenance.due(time.time())):
            self._maintenance_reason('timer_correction_limit')
        elif self._deadline_speed is not None and not changed and self.speed.exhausted:
            self._maintenance_reason('speed_check_exhausted')
        return changed

    def _maintenance_reason(self, reason: str) -> None:
        if self.maintenance_status != reason:
            self.bus.publish(events.Skipped(action='maintenance', reason=reason,
                                           detail='Deadline verification: ' + reason))
        self.maintenance_status = reason

    def _update_maintenance(self, settings: Live, state: screens.ScreenState) -> None:
        if self.lab_runtime is not None:
            self.maintenance.observe(self.lab_runtime.snapshot())
        self.maintenance.tick(time.time(), time.monotonic(), paused=settings.paused)
        if not self.maintenance.due(time.time()):
            self.maintenance_status = None
            return
        if settings.paused:
            reason = 'paused'
        elif self.shopping.reconciliation_pending or self.autopilot.pending is not None:
            reason = 'pending_purchase'
        elif state is not screens.ScreenState.IN_RUN or self._any_walk_active():
            reason = 'unsafe_screen'
        else:
            reason = self.maintenance_status if self._deadline_speed is not None else 'due'
        self._maintenance_reason(reason or 'due')

    def _ladder_waves(self) -> tuple[int | None, int | None]:
        """(best, claimed-at) for the ladder of the tier last played."""
        tier = self._ladder_tier
        best = self._best_wave if tier is None else self._tier_best_wave.get(tier)
        return best, self._claimed_wave.get(tier)

    def _replan_reroll_shopping(self) -> Shopping | None:
        """The strategy's next Workshop choice, mid-visit, after a purchase."""
        if self.reroll_progress is None or self._reroll_shopping_base is None:
            return None
        policy = self.reroll_progress.shopping_policy(self._reroll_shopping_base)
        self._reroll_shopping_policy = policy
        return policy

    def _claim_owed(self, settings: Any) -> bool:
        """True if a due free claim is worth leaving the death screen for.

        RETRY never passes the main menu, which is the only screen a claim is
        offered on - so without this a claim is only ever taken when some
        other errand happens to go home.
        """
        claims = settings.strategy.claims
        if self._notifications.verification_due(time.time()):
            return not self.claim.active and not self.milestones_claim.active
        return (claims.enabled and not self.claim.active and not self.milestones_claim.active
                and self._claim_due(settings) is not None)

    def _menu_badge_check_due(self, settings: Any) -> bool:
        """Visit home hourly so a badge cannot stay unseen in the RETRY loop."""
        if not settings.strategy.claims.enabled:
            return False
        last = self._last_menu_badge_check_at
        return (last is None or time.time() - last >=
                claim_schedule.MIN_BADGE_HOURS * claim_schedule.SECONDS_PER_HOUR)

    def _claim_due(self, settings: Any) -> claim_schedule.ClaimKind | None:
        """Apply the failed-walk backoff to the shared claim cadence."""
        claims = settings.strategy.claims
        best_wave, claimed_wave = self._ladder_waves()
        now = time.time()
        mission_row = self._notifications.snapshot()["kinds"]["missions"]
        mission_identity_unverified = (self.progress is not None
            and self.account_state is not None
            and (self._notification_scope is None
                 or type(self._notification_scope.get("fact_epoch")) is not int))
        return claim_schedule.due(
            claim_schedule.ClaimState(
                last_missions=self._last_claim.get("missions"),
                last_milestones=self._last_claim.get("milestones"),
                best_wave=best_wave,
                claimed_best_wave=claimed_wave,
                milestones_badge=(self._milestones_badge
                                  and now >= self._milestones_retry_at),
                # NotificationState owns mission edges and retry backoff. A
                # raw one-frame badge may not bypass its confirmation gate.
                missions_badge=False,
                mail_badge=self._mail_badge,
                last_mail=self._last_claim.get("mail"),
                events_badge=self._events_badge,
                last_events=self._last_claim.get("events"),
                missions_notification_due=self._notifications.eligible("missions", now),
                missions_blocked=(mission_row["uncertain"] or mission_row["in_flight"]
                                  or mission_row["prior_uncertain"]
                                  or self._notifications.snapshot()["pending_claim"] is not None
                                  or mission_identity_unverified),
            ),
            now=now,
            missions_every_hours=claims.missions_every_hours,
            milestones_on_new_best=(claims.milestones_on_new_best
                                    and now >= self._milestones_retry_at),
        )

    def _settle_mission_attempt(self) -> None:
        """Record only a finished counter-confirmed walk as a current claim."""
        if not self._mission_attempt or self.claim.active:
            return
        self._mission_attempt = False
        result = self.claim.snapshot()["result"]
        now = time.time()
        if self._notifications.snapshot()["pending_claim"] is not None:
            self._notifications.finish("missions", now, claimed=None)
        elif result is not None and self.claim.snapshot()["claimed"] > 0:
            self._notifications.finish("missions", now, claimed=True)
        else:
            self._notifications.finish("missions", now, claimed=False)

    def _settle_lab_notification(self) -> None:
        """A purple-dot Labs visit that ended without a result still ends.

        Pause, walk cancels and scope changes cancel the visit without a
        result, so ``_finish_lab_visit`` never runs; without this the labs
        notification stays in flight for the whole worker attempt.
        """
        if self.lab_visit is not None and self.lab_visit.active:
            return
        if self._notifications.snapshot()["kinds"]["labs"]["in_flight"]:
            self._notifications.finish("labs", time.time(), claimed=False)

    def _settle_milestones_attempt(self) -> None:
        """Commit a completed walk; roll back and back off after a failed one."""
        attempt = self._milestones_attempt
        if attempt is None or self.milestones_claim.active:
            return
        self._milestones_attempt = None
        result = self.milestones_claim.snapshot()["result"]
        if result is not None and result["status"] == "completed":
            return
        tier, previous_wave, previous_claim_at, previous_badge = attempt
        if previous_wave is None:
            self._claimed_wave.pop(tier, None)
        else:
            self._claimed_wave[tier] = previous_wave
        if previous_claim_at is None:
            self._last_claim.pop("milestones", None)
        else:
            self._last_claim["milestones"] = previous_claim_at
        self._milestones_badge = self._milestones_badge or previous_badge
        self._milestones_retry_at = time.time() + _FAILED_MILESTONES_RETRY_SECONDS
        logger.info("Milestones walk failed (%s); retry in %.0f seconds.",
                    result["reason"] if result is not None else "missing_result",
                    _FAILED_MILESTONES_RETRY_SECONDS)

    def _offer_cards_intro(self) -> bool:
        """Arm the first Cards visit if the tab still carries its arrow.

        Not gated on claims.enabled: the visit is owed once, spends nothing,
        and the arrow it answers to is gone as soon as it has run. Only
        offered while no other walk holds the menus.
        """
        if (self.collection.active or self.visit.active or self.claim.active
                or self.milestones_claim.active):
            return False
        if not self.cards_intro.due(time.time()):
            return False
        if nav_arrow.arrow_over(self.screen, self.templates, 'CARDS') is not True:
            return False
        return self.cards_intro.request()

    def _advance_tier(self, settings: Any) -> bool:
        """Tap the lit right tier arrow, so BATTLE always starts the highest tier.

        True holds BATTLE for this frame: after a tap, until the panel has
        redrawn and been read again. Only a plain BATTLE menu is touched - a
        suspended run's RESUME BATTLE is left on the tier it was started on.
        """
        now = time.monotonic()
        if now - self._tier_tap_at < config.NAVIGATION_COOLDOWN_SECONDS:
            return True
        battle = config.NAV_BUTTONS["MAIN_MENU"][0][1]
        if vision.locate_template(self.screen, self.templates.get(battle), .8) is None:
            return False
        arrow = tier_select.read_next(self.screen, self.templates)
        if arrow is None or not arrow.available:
            return False
        x, y = jitter.point(*arrow.point, settings.strategy.tap_jitter_px)
        jitter.pause(settings.strategy.tap_delay, settings.strategy.timing_jitter)
        tap(self.device, x, y)
        self._tier_tap_at = now
        logger.info("A higher tier is available; tapped the tier arrow at %s.", arrow.point)
        self.bus.publish(events.Tapped(action='tier_next', x=x, y=y, score=arrow.score))
        self.bus.publish(events.TierAdvanced(point=arrow.point))
        return True

    def _menu_tab_unlocked(self, tab: str) -> bool:
        """Check the unlocked icon before a reroll visit navigates to a tab."""
        match = vision.locate_template(
            self.screen, self.templates.get(f"nav/tab_{tab}.png"), .94)
        return match is not None

    def _offer_claim(self, settings: Any) -> str | None:
        """Offer the one claim the cadence says is owed, if any.

        Offers rather than arms: at most one maintenance walk may hold the
        menus, so request() returning False - or a walk already being active
        - is the ordinary case and is survived silently. Returns the kind
        armed, so the caller can log it, or None.
        """
        claims = settings.strategy.claims
        if settings.paused or not claims.enabled:
            return None
        if self.claim.active or self.milestones_claim.active:
            return None
        best_wave, _ = self._ladder_waves()
        kind = self._claim_due(settings)
        if kind is None:
            return None
        if kind in ('mail', 'events'):
            if not (self.claim.request_mail() if kind == 'mail'
                    else self.claim.request_events()):
                return None
            self._last_claim[kind] = time.time()
            return kind
        walk = self.claim if kind == "missions" else self.milestones_claim
        if not walk.request():
            return None
        if kind == "missions":
            self._notifications.begin("missions", time.time())
            self._mission_attempt = True
        if kind == "milestones":
            self._milestones_attempt = (
                self._ladder_tier, self._claimed_wave.get(self._ladder_tier),
                self._last_claim.get("milestones"), self._milestones_badge,
            )
        self._last_claim[kind] = time.time()
        if kind == "milestones":
            if best_wave is not None:
                self._claimed_wave[self._ladder_tier] = best_wave
            self._milestones_badge = False
        return kind

    def _preflight_boxes(self, reading: screens.ScreenReading,
                         reads: ocr.FrameReads) -> tuple[ocr.TextBox, ...]:
        """The boxes the supervisor preflight checks for recovery modals.

        Off IN_RUN, the whole frame. On IN_RUN, the two battle bands (spec
        P2). A recovery modal sits mid-screen, outside both bands, so a frame
        whose bands show no upgrade panel (the P1 safety net) falls back to
        the whole frame - and so does one scan every
        config.BATTLE_FULL_READ_EVERY seconds, the backstop for a popup that
        leaves the panel readable.

        A reroll account's Workshop coin grant (fleet.tutorial) has markers
        between the two bands; its popup does not classify as IN_RUN, so it
        takes the whole-frame path.
        """
        if reading.state is not screens.ScreenState.IN_RUN:
            return reads.full()
        now = time.monotonic()
        if now - self._battle_full_read_at >= config.BATTLE_FULL_READ_EVERY:
            self._battle_backstop_scan = True
            boxes = reads.full()
            # Only a read that succeeded pays the backstop; a failed one is
            # due again next scan.
            self._battle_full_read_at = now
            return boxes
        if not self._battle_panel_visible(reads):
            return reads.full()
        return reads.battle()

    def _battle_panel_visible(self, reads: ocr.FrameReads) -> bool:
        """Spec P1's safety net: does this battle frame show its upgrade panel?

        Heading text in the bands or a heading colour. False - including when
        the bands could not be read - means something may cover the panel,
        and the menu readers and the full-frame preflight run on this scan.
        """
        try:
            boxes = reads.battle()
        except Exception:  # noqa: BLE001 - an unread frame is not a visible panel
            return False
        return panel_visible(reads.screen, boxes)

    def _any_walk_active(self) -> bool:
        return (self.collection.active or self.visit.active
                or self.claim.active or self.milestones_claim.active
                or self.cards_intro.active
                or (self.lab_visit is not None and self.lab_visit.active)
                or self.battle_menu.active)

    # -- O4 recovery (scan thread only) -------------------------------------
    def _recovery_active(self) -> bool:
        """False once a coordinator failure latched recovery off for this bot."""
        return self.recovery is not None and not self._recovery_failed

    def _recovery_wanted(self, stall_verdict: str | None) -> bool:
        recovery = self.recovery
        return self._recovery_active() and (recovery.owns_lane or (
            recovery.enabled and stall_verdict in (stall_watchdog.ESCAPE, stall_watchdog.PAUSE)))

    def _recovery_journal(self) -> tuple[int, bool]:
        journal = getattr(self.shopping, 'journal', None)
        if isinstance(journal, transactions.TransactionJournal):
            return journal.recovery_snapshot()  # Cached, nonblocking.
        return 0, True  # No purchase journal: pending state unknown, so blocked.

    def _recovery_context(self, settings: Any, screen: str, escape: Any) -> Any:
        """This completed scan as an immutable RecoveryContext, or None when the
        verified attempt scope is unavailable (then recovery is never used)."""
        from recovery_policy import (RecoveryCandidate, RecoveryContext, RecoveryScope,
                                     VerifiedControl)
        if self.progress is None or self.supervisor is None:
            return None
        beat = self.progress.snapshot()
        keys = ('worker_id', 'account_id', 'lease_id', 'attempt_id', 'generation', 'boot_id')
        if any(not isinstance(beat.get(key), str) or not beat[key] for key in keys):
            return None
        epoch, pending = self._recovery_journal()
        if screen != self._recovery_screen:
            self._recovery_screen = screen
            self._recovery_screen_generation += 1
        candidates = ()
        if escape is not None:
            label, box = escape
            height, width = self.screen.shape[:2]
            rect = box.rect
            candidates = (RecoveryCandidate(
                candidate_id=f'stall:{label}', action_id='close_overlay', screen=screen,
                control_generation=0, target=label, expected_postcondition='overlay_absent',
                control=VerifiedControl(x=rect.x, y=rect.y, width=rect.w, height=rect.h,
                                        frame_width=width, frame_height=height)),)
        scope = RecoveryScope(
            worker=beat['worker_id'], account_id=beat['account_id'], lease_id=beat['lease_id'],
            attempt_id=beat['attempt_id'], attempt_generation=beat['generation'],
            boot_id=beat['boot_id'], worker_generation=int(beat.get('clock_epoch') or 0),
            strategy_revision=getattr(self.controls, 'recovery_revision', 0),
            device_command_generation=getattr(self.supervisor, 'command_generation', 0),
            pending_transaction_generation=epoch)
        return RecoveryContext(
            scope=scope, screen=screen, screen_generation=self._recovery_screen_generation,
            observation_generation=self._capture_sequence,
            observed_at_monotonic=getattr(self, '_screen_captured_monotonic', time.monotonic()),
            candidates=candidates, paused=settings.paused,
            identity_conflict=getattr(self.supervisor, 'current_account', None) != scope.account_id,
            pending_transaction=pending)

    def _recovery_live(self, scanned: Any) -> Any:
        """The scanned context with the gates as they are right now, for the final
        guard inside DeviceSupervisor.recovery_tap. Nonblocking; never reads the
        supervisor's in-guard BLOCKED state as an identity conflict."""
        epoch, pending = self._recovery_journal()
        scope = scanned.scope.model_copy(update={
            'device_command_generation': getattr(self.supervisor, 'command_generation', 0),
            'strategy_revision': getattr(self.controls, 'recovery_revision', 0),
            'pending_transaction_generation': epoch})
        return scanned.model_copy(update={
            'scope': scope, 'paused': self.controls.snapshot().paused,
            'pending_transaction': pending,
            'identity_conflict': getattr(self.supervisor, 'current_account', None)
                                 != scope.account_id})

    def _step_recovery(self, settings: Any, screen: str, escape: Any, *, stalled: bool,
                       ready: bool, readable: bool, boxes: tuple[ocr.TextBox, ...],
                       unrenamed: str = "UNKNOWN") -> bool:
        """Advance the coordinator one scan. True means it owns the action lane.

        Exceptions never end the scan loop: they return the lane to the
        existing watchdog path.
        """
        self._recovery_acted = False
        recovery = self.recovery
        try:
            context = self._recovery_context(settings, screen, escape)
            if context is None:
                return False
            dispatched = self._recovery_dispatched
            # Positive evidence only: a readable frame classified as a known
            # screen, with trusted OCR, no spending/ad text, and neither the
            # tapped label nor any higher-priority safe label anywhere. Anything
            # else is "not confirmed" and times out to no_effect.
            postcondition = bool(
                dispatched is not None and readable
                and unrenamed not in ("UNKNOWN", stall_watchdog.SCREEN_ID)
                and stall_watchdog.overlay_absent(boxes, dispatched.target))

            def dispatch(candidate: Any, guard: Any) -> None:
                control = candidate.control
                x, y = control.x + control.width // 2, control.y + control.height // 2
                # The serial DeviceSupervisor re-runs guard after its durable
                # checkpoint, immediately before the ADB click.
                self.supervisor.recovery_tap(x, y, guard=guard)
                self._recovery_dispatched, self._recovery_acted = candidate, True
                self.bus.publish(events.Tapped(action=f'recovery:{candidate.target}',
                                               x=x, y=y, score=1.0))

            can_tap = ready and callable(getattr(self.supervisor, 'recovery_tap', None))
            frame = self.screen
            owned = recovery.step(
                context, stalled=stalled, deterministic=False,
                dispatch=dispatch if can_tap else None,
                live_context=lambda: self._recovery_live(context),
                semantic_postcondition=postcondition,
                pause=lambda: self._pause_for_stall(boxes),
                # Encoded only when a provider request is built (<=2 per incident).
                image=lambda: _recovery_frame_image(frame))
            if not recovery.owns_lane:
                self._recovery_dispatched = None
        except Exception:  # noqa: BLE001 - recovery must never end the scan loop
            # Latch recovery off for this bot and give the lane back to the
            # unchanged watchdog path (its counts are preserved, not reset).
            logger.exception("Recovery coordinator step failed; recovery disabled for this bot")
            self._recovery_failed = True
            self._recovery_dispatched = None
            try:
                recovery.close()  # Scan thread; never joins. The runner re-closes safely.
            except Exception:  # noqa: BLE001
                logger.exception("Recovery coordinator close failed")
            return False
        self._publish_recovery_status()
        return owned

    def _publish_recovery_status(self) -> None:
        if self.recovery is None:
            return
        try:
            status = self.recovery.status()
        except Exception:  # noqa: BLE001
            return
        key = json.dumps({k: v for k, v in status.items()
                          if k not in ('observed_at', 'calls_observed_at', 'budget_observed_at')},
                         sort_keys=True, default=str)
        if key != self._recovery_status_key:
            self._recovery_status_key = key
            self.bus.publish(events.RecoveryStatusChanged(status=status))

    def _pause_for_stall(self, boxes: tuple[ocr.TextBox, ...]) -> None:
        """Give up on a stall: keep the evidence, pause, and say so."""
        reason = self.stall_watchdog.reason or "stalled"
        stem = self.stall_dir / f"stall-{time.strftime('%Y%m%d-%H%M%S')}"
        snapshot = ""
        try:
            import cv2
            self.stall_dir.mkdir(parents=True, exist_ok=True)
            if cv2.imwrite(str(stem.with_suffix(".png")), self.screen):
                snapshot = str(stem.with_suffix(".png"))
            stem.with_suffix(".json").write_text(json.dumps(
                [{"text": b.text, "confidence": float(b.confidence), "rect": list(b.rect)}
                 for b in boxes], indent=2) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001 - missing evidence must not stop the pause
            logger.exception("Could not save the stall evidence")
        changed = self.controls.apply({"paused": True})
        if changed:
            self.bus.publish(events.ControlChanged(changed=changed, source="watchdog"))
        logger.error("No progress (%s) and no safe way out; paused. Evidence: %s",
                     reason, snapshot or "not saved")
        self.bus.publish(events.WorkerStalled(
            reason=reason, stage="paused", snapshot_path=snapshot))
        self.stall_watchdog.reset()

    def _cancel_walks(self, reason: str, detail: str) -> None:
        """End whichever walk is armed. Each cancel is idempotent."""
        for walk in (self.collection, self.visit, self.claim,
                     self.milestones_claim, self.cards_intro, self.battle_menu):
            walk.cancel(reason, detail)
        if self.lab_visit is not None:
            self.lab_visit.cancel(reason)

    def _observe_menu_wallet(self, coins: int | None, gems: int | None, *, evidence_ref: str) -> None:
        """Publish this capture before maintenance policy; fetch time is never evidence."""
        from evidence_scope import BalanceInterval, FactScope
        scope = getattr(self, '_screen_fact_scope', None)
        observed_at = getattr(self, '_screen_captured_at', None)
        if (self.account_state is None or not isinstance(scope, FactScope)
                or scope != self.account_state.verified_scope
                or type(observed_at) not in (float, int)
                or not 0 <= time.time() - observed_at <= 30):
            return
        for currency, amount in (('coins', coins), ('gems', gems)):
            if amount is not None and (type(amount) is not int or amount < 0):
                continue
            self.account_state.observe_balance(BalanceInterval.from_reading(
                currency, amount, scope, observed_at, evidence_ref))

    def _observe_labs_capture(self, boxes: tuple[ocr.TextBox, ...]) -> None:
        """Tag the current reader output before any maintenance input changes it."""
        from evidence_scope import FactScope
        from lab_runtime import _catalog_revision
        scope = getattr(self, '_screen_fact_scope', None)
        observed_at = getattr(self, '_screen_captured_at', None)
        if (self.lab_state is None or self.account_state is None
                or not isinstance(scope, FactScope) or scope != self.account_state.verified_scope
                or type(observed_at) not in (float, int)
                or not 0 <= time.time() - observed_at <= 30):
            return
        observation = lab_screen.read_slots(self.screen, boxes, observed_at=observed_at)
        picker = lab_screen.read_picker(self.screen, boxes)
        if picker.page and picker.game_speed is not None:
            observation = dataclasses.replace(observation, entries=(picker.game_speed,))
        self.lab_state.observe(observation, scope=scope, catalog_revision=_catalog_revision())

    def _bind_lab_runtime(self) -> None:
        from lab_runtime import LabRuntime, LabScope
        scope = self.account_state.verified_scope if self.account_state else None
        journal = getattr(self.shopping, 'journal', None)
        if self.lab_visit is None:
            return
        if scope is None or not isinstance(journal, transactions.TransactionJournal):
            self.lab_runtime = self.lab_visit.runtime = None
            self.maintenance = MaintenanceSchedule()
            self._deadline_speed = None
            return
        expected = LabScope(scope.account_id, scope.lease_id, scope.generation, scope.epoch)
        if self.lab_runtime is None or self.lab_runtime.scope != expected:
            if self.lab_visit.active:
                self.lab_visit.cancel('account_scope_changed')
            if self.maintenance.account_id != scope.account_id:
                self.maintenance = MaintenanceSchedule(journal.path.parent, scope.account_id)
            self._deadline_speed = None
            self.lab_runtime = LabRuntime(journal.path.parent, scope.account_id,
                lease_id=scope.lease_id, generation=scope.generation, epoch=scope.epoch)
        self.lab_visit.runtime = self.lab_runtime

    def _observe_lab_runtime(self, reading: LabsReading) -> None:
        scope = getattr(self, '_screen_fact_scope', None)
        if (self.lab_runtime is None or self.account_state is None or scope is None
                or scope != self.account_state.verified_scope
                or reading.observed_at != getattr(self, '_screen_captured_at', None)
                or not self.account_state.accepts_capture(scope, reading.observed_at)
                or not 0 <= time.time()-reading.observed_at <= 30):
            return
        self.lab_runtime.observe(reading)
        self.maintenance.observe(self.lab_runtime.snapshot())
        self.maintenance.acknowledge_observation(self.lab_runtime.snapshot(), now=time.time())

    def _authorize_lab(self, operation: str, decision: LabDecision | None, now: float) -> bool:
        """Recheck the assigned route, its calibration gate and shared funds at the spend boundary."""
        if self.reroll_progress is None or self.account_state is None or self.lab_runtime is None:
            return False
        route_runtime = getattr(self.reroll_progress, 'route_runtime', None)
        revision = route_runtime.current().revision if route_runtime is not None else None
        if revision != self._lab_visit_revision:
            return False
        options = self.reroll_progress.lab_visit_options()
        if operation == 'lab_unlock':
            return (unlock_gate(2).enabled and options.unlock_slot2
                    and options.min_gems == self.lab_visit._options.min_gems)
        if not options.start_research or decision is None:
            return False
        slot, research, target = decision.slot, decision.research_id, decision.target_level
        if not research_gate(slot, research).enabled:
            return False
        # A selected action binds slot, research, level and the strategy
        # revision it was planned under; any drift refuses the spend.
        selected = self.lab_visit.selected_action if self.lab_visit is not None else None
        if selected is not None and (selected.slot, selected.research, selected.target_level,
                                     selected.strategy_revision) != (slot, research, target,
                                                                     decision.strategy_revision):
            return False
        if decision.strategy_revision is not None and decision.strategy_revision != revision:
            return False
        if route_runtime is None:
            return (slot, research) == (1, 'labs.game-speed') and decision.strategy_revision is None
        snapshot = self.lab_runtime.snapshot()
        facts = self.account_state.lab_facts(snapshot, now=now)
        if facts is None:
            return False
        plan = self.reroll_progress.lab_strategy_plan(snapshot, available_coins=facts.available_coins, now=now)
        action = self.account_state.lab_action(plan, snapshot, revision=revision, now=now) if plan else None
        return (action is not None and action.slot == slot and action.research == research
                and action.target_level == target and action.strategy_revision == revision)

    def _plan_lab_action(self, now: float) -> Any | None:
        """One route-gated start from the assigned plan; uncalibrated choices only surface.

        Uses the plan's route gates (via choose_lab_action and research_gate),
        never automated_list(). Only called from the safe MAIN_MENU branch.
        """
        route_runtime = getattr(self.reroll_progress, 'route_runtime', None)
        if (route_runtime is None or self.account_state is None or self.lab_runtime is None):
            return None
        snapshot = self.lab_runtime.snapshot()
        facts = self.account_state.lab_facts(snapshot, now=now)
        if facts is None:
            return None
        plan = self.reroll_progress.lab_strategy_plan(snapshot, available_coins=facts.available_coins, now=now)
        if plan is None:
            return None
        self._note_lab_route_pending(plan)
        action = self.account_state.lab_action(plan, snapshot, revision=route_runtime.current().revision, now=now)
        if action is None or action.operation != 'start' or not research_gate(action.slot, action.research).enabled:
            return None
        return action

    def _note_lab_route_pending(self, plan: Any) -> None:
        """Publish uncalibrated planned work once per change, never per scan."""
        pending = tuple(sorted(
            f"Lab {slot.slot} {slot.next.lab_id}: {line}"
            for slot in plan.slots if slot.next is not None and not slot.automated
            for line in slot.why if line.startswith('Planning only:')))
        gems_next = plan.gems.next.block_id if plan.gems.next is not None else None
        pending += tuple(line for line in plan.gems.why
                         if gems_next is not None and line.startswith(f'{gems_next}: Planning only:'))
        if pending != self.lab_route_pending:
            self.lab_route_pending = pending
            if pending:
                self.bus.publish(events.Skipped(action='labs', reason='lab_route_calibration_required',
                                                detail='; '.join(pending)))

    def _request_planned_lab_visit(self, now: float, due: bool) -> bool:
        """Arm a Labs visit on the safe menu: the gated planned action, else the legacy check.

        A planned start arms only for due work or a fresh plan/slot change, and
        after any visit that did not verify a start, the same action backs off.
        The legacy cadence-paced check (`due`) is unchanged; while nothing is
        armed, BATTLE navigation proceeds normally.
        """
        options = self.reroll_progress.lab_visit_options()
        action = self._plan_lab_action(now)
        if action is not None:
            key = (action.slot, action.research, action.target_level, action.strategy_revision)
            record = next((r for r in getattr(self.lab_runtime.snapshot(), 'slots', ())
                           if getattr(r, 'slot', None) == action.slot), None)
            evidence = ((record.state, record.research_id, record.transaction_id)
                        if record is not None else None)
            last = self._lab_action_last
            repeat = last is not None and last[0] == key and last[1] == evidence
            if not (repeat and (now < last[2] or not due)):
                if self.lab_visit.request(action, options=options):
                    # Pessimistic: only a verified start clears the backoff.
                    self._lab_action_last = (key, evidence, now + LAB_ACTION_BACKOFF_SECONDS)
                    return True
                return False
        return due and self.lab_visit.request(None, options=options)

    def _settle_planned_lab_attempt(self, result: Any) -> None:
        """A verified start ends the backoff; the due requirement still applies."""
        last = self._lab_action_last
        if last is not None and self.lab_visit.selected_action is not None and result.status == 'started':
            self._lab_action_last = (last[0], last[1], 0.)

    def _finish_lab_visit(self, result: Any) -> None:
        """Record only verified research and lab-slot purchases."""
        self._settle_planned_lab_attempt(result)
        lab_notice = self._notifications.snapshot()["kinds"]["labs"]
        if lab_notice["in_flight"]:
            # One visit may not clear every lab badge. Require a clear edge
            # before a future generation; a persistent dot backs off.
            self._notifications.finish("labs", time.time(), claimed=False)
        if self.reroll_progress is None:
            return
        if result.slot2_status in {"locked", "owned"}:
            self.reroll_progress.note_lab_slot2(result.slot2_status, result.gem_balance)
        if (result.gems_before is not None and result.observed_gem_spend == 100
                and result.gem_balance == result.gems_before - 100 and result.unlock_transaction_key is None):
            self.bus.publish(events.LabSlotUnlocked(
                slot=2, price=100, gems_before=result.gems_before,
                gems_after=result.gem_balance))
        decision = result.decision
        if (result.confirmed_job is not None
                and result.confirmed_job.completes_at is not None):
            self._research_until = result.confirmed_job.completes_at
        if result.status == "started" and result.confirmed_job is not None:
            self.reroll_progress.note_lab_observation(LabDecision(
                "wait_running", job_completes_at=result.confirmed_job.completes_at,
                game_speed_level=decision.game_speed_level))
            if (decision.wallet_coins is not None and decision.price is not None
                    and result.observed_coin_spend == decision.price):
                key = (result.confirmed_job.completes_at,
                       decision.price, decision.wallet_coins)
                if key != self._last_lab_confirmation:
                    self._last_lab_confirmation = key
                    if self.lab_state is not None:
                        for observation in result.confirmed_readings:
                            self.lab_state.observe(observation)
                    if result.transaction_key is None:
                        self.bus.publish(events.LabResearchStarted(
                            concept_id="labs.game-speed", price=decision.price,
                            coins_before=decision.wallet_coins,
                            coins_after=decision.wallet_coins - decision.price,
                            completes_at=result.confirmed_job.completes_at))
                    # The confirmed debit spent the lab savings.
                    self.reroll_progress.note_lab_coin_debit()
        else:
            # With auto-start off the visit only looked; keep the saved Game
            # Speed evidence rather than overwriting it with "inspect".
            if result.reason != "auto_start_off":
                self.reroll_progress.note_lab_observation(decision)
        logger.info("Lab 1 visit ended: %s (%s)%s", result.status, result.reason,
                    "; Lab 2 unlocked" if result.observed_gem_spend == 100 else "")

    def run_once(self, max_runs: int | None = None) -> bool:
        """Record a scan only when its pass returned normally."""
        if self.progress is None:
            return self._run_once(max_runs)
        self._progress_wait = None
        with ocr.progress_scope(self.progress):
            with self.progress.phase('scan', 90):
                result = self._run_once(max_runs)
                if self._screen is not None:
                    settings = self.controls.snapshot()
                    if settings.paused:
                        self.progress.wait('pause', 'operator resumes the worker', time.time() + 3600)
                    elif self._progress_wait is not None:
                        self.progress.wait(*self._progress_wait)
                    elif self._research_until is not None and self._research_until > time.time():
                        self.progress.wait('research_running', 'Game Speed research finishes',
                                           self._research_until)
                    elif self.supervisor is not None and (
                            recovery := self.supervisor.status()).retry_at is not None:
                        self.progress.wait('cooldown', 'device recovery retry', recovery.retry_at)
                    else:
                        autopilot_wait = self.autopilot.state.snapshot()
                        if autopilot_wait['phase'] == 'saving':
                            self.progress.wait('funds', 'battle cash reaches the next upgrade price',
                                               time.time() + 30)
                        else:
                            self.progress.clear_wait()
                    autopilot = self.autopilot.state.snapshot()
                    purchases = autopilot['verified_purchases']
                    if purchases > self._last_verified_purchases:
                        purchased = autopilot['last_purchase'] or {}
                        kind = ('workshop_purchase' if purchased.get('context') == 'workshop'
                                else 'battle_purchase')
                        evidence = str(purchased.get('upgrade_id', 'unknown'))
                        self.progress.meaningful_progress(
                            kind, evidence)
                        if kind == 'workshop_purchase':
                            self.progress.observe_capability('workshop', 'progress', evidence, 120)
                    self._last_verified_purchases = purchases
                    self.progress.complete_scan(
                        self.runs.current_id, hashlib.sha256(self._screen.tobytes()).hexdigest())
        return result

    def _run_once(self, max_runs: int | None = None) -> bool:
        """One scan pass over the configured actions. True if anything clicked.

        `max_runs` only suppresses auto-navigation. The scan that ends run N
        is also the scan that would tap RETRY on the way out, so without this
        the loop breaks at the top of the next iteration having already
        started run N+1 in the emulator.
        """
        started = time.monotonic()
        self._bind_notification_scope()
        self._settle_milestones_attempt()
        self._settle_mission_attempt()
        self._settle_lab_notification()
        if self._notifications.retain_exhausted_claim(time.time()):
            logger.warning("Unproven mission claim retained uncredited after verification "
                           "exhaustion; mission claims resume.")
        if (self.claim.active and not self._mission_attempt
                and self.claim.snapshot().get("target") not in ("mail", "events")):
            mission_row = self._notifications.snapshot()["kinds"]["missions"]
            if (mission_row["uncertain"] or mission_row["in_flight"]
                    or mission_row["prior_uncertain"]
                    or (self.progress is not None and self.account_state is not None
                        and (self._notification_scope is None or type(
                            self._notification_scope.get("fact_epoch")) is not int))):
                self.claim.cancel("prior_claim_unresolved",
                                  "A prior mission claim outcome needs reconciliation.")
            else:
                # Browser-armed walks enter through the same durable receipt
                # gate as scheduled walks, before they can reach a tap.
                self._notifications.begin("missions", time.time())
                self._mission_attempt = True
        # Exactly one snapshot for the whole pass. Re-reading mid-scan would
        # let a setting change underneath a half-finished scan - the wallet
        # read with one strategy and the price gate applied with another.
        settings = self.controls.snapshot()
        chosen = self.checks.get(settings.strategy.affordability)
        if chosen is not None:
            self.affordability = chosen
        self.refresh_screen()

        reading = screens.classify(self.screen, self.templates)
        self._update_maintenance(settings, reading.state)
        # One OCR result and one digest for this frame, shared by the
        # preflight, the menu readers and the autopilot - see ocr.FrameReads.
        # Menu frames may reuse the last full read while nothing on screen
        # changed (spec P3); battle frames always read their bands fresh. So
        # does any scan with a walk or a shopping visit active: it tapped and
        # now reads what the tap did.
        acting = self._any_walk_active() or self.shopping.active
        reads = ocr.FrameReads(self.screen,
                               reuse=reading.state is not screens.ScreenState.IN_RUN
                               and not acting)
        self._battle_backstop_scan = False
        tutorial_claim = None
        unlocked = None
        ticket = None
        escape = None
        preflight_boxes = None
        battle_context = (reading.state in (screens.ScreenState.IN_RUN,
                                            screens.ScreenState.GAME_OVER)
                          or (reading.state is screens.ScreenState.UNKNOWN
                              and reading.cash_top_left is not None))
        self._last_scan_in_battle = battle_context
        info_dismiss = None
        recovery_escape = None
        unrenamed_screen = reading.state.value
        if self.supervisor is not None:
            recovery_status = getattr(self.supervisor, "status", None)
            watched = callable(recovery_status)
            had_pending = watched and recovery_status().pending_action
            stall_verdict = self.stall_watchdog.verdict() if watched else None
            stall_reason = self.stall_watchdog.reason or ""
            observed_screen = reading.state.value
            if observed_screen == "UNKNOWN":
                try:
                    observed_screen = pages.classify_page(self.screen, self.templates).page
                except Exception:  # noqa: BLE001 - unreadable page is no permission to act
                    observed_screen = "UNKNOWN"
            try:
                boxes = (reads.full() if self._battle_info_dismiss_count
                         else self._preflight_boxes(reading, reads))
                preflight_boxes = boxes
                if reading.state is screens.ScreenState.UNKNOWN:
                    battle_context = (battle_context or
                                      battle_upgrade_info.death_controls_visible(boxes, self.screen.shape))
                if battle_context:
                    info_dismiss = battle_upgrade_info.dismiss_point(boxes, self.screen.shape)
                    if info_dismiss is not None:
                        observed_screen = "BATTLE_UPGRADE_INFO"
                # Lab dialogs have no reliable pages.py anchor. While a Lab
                # visit owns them, name the OCR-verified page even if an
                # underlying Labs anchor matched through the dialog.
                if self.lab_visit is not None and self.lab_visit.active:
                    if lab_screen.read_confirmation(self.screen, boxes).page:
                        observed_screen = "LAB_CONFIRMATION"
                    elif lab_screen.read_picker(self.screen, boxes).page:
                        observed_screen = "LAB_PICKER"
                    elif lab_screen.read_home(self.screen, boxes).page:
                        observed_screen = "LABS"
                # The in-battle menu and the pages it opens have no
                # screens/pages anchor either (they classify UNKNOWN). While a
                # battle menu visit owns them, name them so the supervisor
                # can authorize the visit's next tap; a stray open menu with
                # no visit stays UNKNOWN and is left to ordinary recovery.
                if self.battle_menu.active and observed_screen == "UNKNOWN":
                    if battle_menu.close_point(self.screen, self.templates) is not None:
                        observed_screen = "BATTLE_MENU"
                    elif battle_menu.read_page(boxes).page != "none":
                        observed_screen = "BATTLE_MENU_PAGE"
                # The Free Ticket offer covers the main menu without hiding
                # its anchors, so it is looked for on MAIN_MENU too; its
                # reveal shares the milestone reward modal's SKIP + CLAIM
                # layout, so it is named before the milestone parse runs.
                if observed_screen in ("UNKNOWN", "MAIN_MENU"):
                    ticket = free_ticket.read(self.screen, boxes)
                    if ticket is not None:
                        observed_screen = ticket.screen
                if observed_screen == "UNKNOWN":
                    # Recovery preflight runs before MilestonesReadings.scan.
                    # A valid ladder or reward modal must be named here or the
                    # supervisor blocks the active claim walk before it can act.
                    milestone_frame = parse_milestones_frame(self.screen, boxes)
                    if milestone_frame is not None:
                        observed_screen = milestone_frame.screen_id
                    # The first Cards visit's intro and gems reward are named
                    # only while that walk is out, so it can clear them.
                    if (observed_screen == "UNKNOWN" and self.cards_intro.active
                            and cards_popup_visible(self.screen, self.templates)):
                        observed_screen = "CARDS_INTRO"
                if self.reroll_progress is not None:
                    from fleet.tutorial import workshop_coin_claim
                    tutorial_claim = workshop_coin_claim(self.screen, boxes)
                    if tutorial_claim is not None:
                        observed_screen = "WORKSHOP_TUTORIAL_CLAIM"
                if observed_screen == "UNKNOWN":
                    unlocked = unlocked_screen.read(self.screen, boxes)
                    if unlocked is not None:
                        observed_screen = unlocked_screen.SCREEN_ID
                if observed_screen == "UNKNOWN":
                    inbox_preflight = mail_screen.parse(self.screen, boxes)
                    footer = inbox_preflight.back
                    if (inbox_preflight.visible and footer is not None
                            and footer.rect is not None
                            and footer.rect[1] > self.screen.shape[0] * .85):
                        observed_screen = "INBOX"
                if observed_screen == "UNKNOWN":
                    # Named the same way, or the events walk cannot close the
                    # first-visit EVENT INFORMATION card nor anything leave.
                    events_preflight = events_screen.parse(self.screen, boxes)
                    footer = events_preflight.back
                    if (events_preflight.visible and footer is not None
                            and footer.rect is not None
                            and footer.rect[1] > self.screen.shape[0] * .85):
                        observed_screen = "EVENTS"
                # Last, so it outranks every reader: a stall means whatever
                # this frame was named, taps on it have stopped working.
                if (stall_verdict == stall_watchdog.ESCAPE
                        and not settings.paused):
                    escape = stall_watchdog.escape_button(boxes)
                    if escape is not None:
                        observed_screen = stall_watchdog.SCREEN_ID
                unrenamed_screen = observed_screen
                if (escape is None and self._recovery_wanted(stall_verdict)
                        and not settings.paused):
                    # Host-verified dismissal candidate for model recovery at
                    # deterministic exhaustion (or while an episode owns the lane).
                    # The rename only lets the supervisor permit the guarded
                    # tap; the watchdog still sees the unrenamed frame below.
                    recovery_escape = stall_watchdog.escape_box(boxes)
                    if recovery_escape is not None:
                        observed_screen = stall_watchdog.SCREEN_ID
                online_required, session_conflict = popup_flags(boxes)
                readable = True
            except Exception:  # noqa: BLE001 - an unreadable modal may cover an anchor
                online_required = False
                session_conflict = False
                readable = False
            recovery = self.supervisor.observe(
                frame_digest=reads.digest,
                observed_at=getattr(self, "_screen_captured_at", time.time()),
                screen=observed_screen,
                account_id=self.supervisor.current_account,
                online_required=online_required, readable=readable,
                session_conflict=session_conflict,
            )
            if watched:
                if settings.paused:
                    self.stall_watchdog.reset()
                    if self._recovery_active() and self.recovery.owns_lane:
                        # Only lets pause invalidate the episode or a resume clear
                        # it; a paused scan takes no input, so the ordinary
                        # observation work below still runs, exactly as off mode.
                        self._step_recovery(
                            settings, observed_screen, recovery_escape, stalled=False,
                            ready=False, readable=readable, boxes=preflight_boxes or (),
                            unrenamed=unrenamed_screen)
                else:
                    reason = recovery_status().reason
                    if (recovery_escape is not None and unrenamed_screen == "UNKNOWN"
                            and reason != "action_no_effect"):
                        # The supervisor saw STALL_ESCAPE only so a guarded
                        # recovery tap can be permitted; for stall accounting
                        # the frame is still unknown, so the blocked timer
                        # keeps running instead of silently clearing.
                        reason = "unknown_screen"
                    self.stall_watchdog.note(had_pending=had_pending, reason=reason)
                    verdict = self.stall_watchdog.verdict()
                    exhausted = verdict == stall_watchdog.PAUSE or (
                        verdict == stall_watchdog.ESCAPE
                        and stall_verdict == stall_watchdog.ESCAPE
                        and escape is None)
                    if self._recovery_active() and (exhausted or self.recovery.owns_lane):
                        owned_before = self.recovery.owns_lane
                        if self._step_recovery(
                                settings, observed_screen, recovery_escape, stalled=exhausted,
                                ready=recovery is RecoveryState.READY, readable=readable,
                                boxes=preflight_boxes or (), unrenamed=unrenamed_screen):
                            # Recovery owns the action lane: no ordinary input.
                            return self._recovery_acted
                        if owned_before and self._recovery_active():
                            # The episode ended (recovered or released): start
                            # the watchdog afresh instead of pausing on stale counts.
                            self.stall_watchdog.reset()
                            exhausted = False
                    if exhausted:
                        self._pause_for_stall(preflight_boxes or ())
                        return False
            if (recovery is not RecoveryState.READY and self.identity_reverification is not None
                    and not settings.paused and self.supervisor.device is not None
                    and self.supervisor.current_account is None
                    and observed_screen in ("MAIN_MENU", "GAME_OVER")
                    and self.identity_reverification.attempt("account_unverified")):
                # A mid-run reconnect cleared the verified account: re-prove it
                # with the bounded, fenced identity walk instead of blocking
                # every later scan forever.
                self.autopilot.suspend("Account re-verification holds actions")
                return True
            if recovery is not RecoveryState.READY:
                self.autopilot.suspend("Device recovery blocked actions")
                # A blocked pass returns before any walk's advance() runs, so
                # no walk's own wait budget can ever run out here: an armed
                # walk would report "running" for as long as recovery stays
                # blocked - observed for hours behind an unrecognised
                # full-screen card. Past the limit the walks are ended
                # instead, the same four cancels the runner makes on a stop.
                if self._any_walk_active():
                    self._recovery_blocked_scans += 1
                    if self._recovery_blocked_scans > config.RECOVERY_BLOCKED_WALK_LIMIT:
                        self._cancel_walks(
                            "recovery_blocked",
                            "Device recovery blocked every scan for too long; "
                            "the walk was ended without finishing.")
                        self._recovery_blocked_scans = 0
                return False
            self._recovery_blocked_scans = 0
            if escape is not None:
                label, point = escape
                logger.warning("No progress (%s); pressing %s at %s to get out.",
                               stall_reason, label, point)
                self.bus.publish(events.WorkerStalled(
                    reason=stall_reason, stage="escape", button=label))
                self.device.click(*point)
                self.stall_watchdog.escaped()
                self.bus.publish(events.Tapped(
                    action="stall:escape", x=point[0], y=point[1], score=1.0))
                return True
            if unlocked is not None:
                if (self.reroll_progress is not None
                        and unlocked_screen.is_labs_unlock(unlocked.caption)):
                    self.reroll_progress.note_lab_unlocked(
                        "unlock_card", caption=unlocked.caption)
                if settings.paused:
                    return False
                logger.info("Dismissing a content unlock card: %s", unlocked.caption)
                self.device.click(*unlocked.ok)
                self.bus.publish(events.Tapped(
                    action="unlocked:ok", x=unlocked.ok[0], y=unlocked.ok[1], score=1.0))
                return True
            if ticket is not None:
                if settings.paused:
                    return False
                logger.info("Claiming the tournament Free Ticket (%s).", ticket.screen)
                self.device.click(*ticket.claim)
                self.bus.publish(events.Tapped(
                    action="free_ticket:claim", x=ticket.claim[0], y=ticket.claim[1],
                    score=1.0))
                return True
            if tutorial_claim is not None:
                if settings.paused:
                    return False
                self.device.click(*tutorial_claim)
                self.bus.publish(events.Tapped(
                    action="reroll:workshop_tutorial_claim", x=tutorial_claim[0],
                    y=tutorial_claim[1], score=1.0))
                return True
        # An active in-battle menu visit owns every frame until it finishes,
        # even off IN_RUN (a destination page classifies UNKNOWN), so it is
        # stepped here, ahead of every reader below - but only after the
        # recovery gate above, like every other multi-frame walk: in worker
        # mode each tap goes through DeviceSupervisor, which accepts one
        # input per observed frame, so the frame must be observed (and, off
        # IN_RUN, named BATTLE_MENU / BATTLE_MENU_PAGE in the preflight)
        # before the visit may act on it. The stall watchdog therefore
        # notes these frames like any other; a visit is bounded
        # (BATTLE_MENU_STEP_FRAMES per step, a capped queue), so it cannot
        # itself hold the watchdog off indefinitely. A paused bot never
        # steps it - the visit is cancelled further down, untouched.
        if not settings.paused and self.battle_menu.active:
            outcome = self.battle_menu.observe(
                screen=self.screen, boxes=reads.full, device=self.device,
                policy=settings.strategy, now=time.time(),
                in_run=reading.state is screens.ScreenState.IN_RUN)
            if outcome is not BattleMenuOutcome.IDLE:
                if self.frames is not None:
                    self.frames.set_boxes([])
                self.bus.publish(events.ScanCompleted(
                    screen=reading.state.value,
                    duration_ms=(time.monotonic() - started) * 1000, wallet=self.wallet))
                return True
        if battle_context or reading.state is screens.ScreenState.UNKNOWN:
            try:
                if self._battle_info_dismiss_count:
                    boxes = reads.full()
                elif preflight_boxes is not None:
                    boxes = preflight_boxes
                elif reading.state is screens.ScreenState.IN_RUN:
                    moment = time.monotonic()
                    if (not self._battle_panel_visible(reads)
                            or moment - self._battle_info_full_read_at
                            >= config.BATTLE_FULL_READ_EVERY):
                        boxes = reads.full()
                        self._battle_info_full_read_at = moment
                    else:
                        boxes = ()
                else:
                    boxes = reads.full()
                if reading.state is screens.ScreenState.UNKNOWN:
                    battle_context = (battle_context or
                                      battle_upgrade_info.death_controls_visible(boxes, self.screen.shape))
                info_dismiss = (battle_upgrade_info.dismiss_point(boxes, self.screen.shape)
                                if battle_context else None)
            except Exception:  # noqa: BLE001 - unreadable evidence grants no tap
                info_dismiss = None
            if info_dismiss is not None:
                self.autopilot.suspend("Upgrade info overlay; actions held")
                moment = time.monotonic()
                if (not settings.paused and self._battle_info_dismiss_count < 3
                        and moment - self._battle_info_dismiss_at >= 2.):
                    x, y = info_dismiss
                    self.device.click(x, y)
                    self._battle_info_dismiss_count += 1
                    self._battle_info_dismiss_at = moment
                    self.bus.publish(events.Tapped(
                        action="battle_upgrade_info:dismiss", x=x, y=y, score=1.0))
                    return True
                self.bus.publish(events.Skipped(
                    action="*", reason="battle_upgrade_info_guard",
                    detail="Upgrade explanation is open; waiting for safe dismissal"))
                return False
            if self._battle_info_dismiss_count:
                self._battle_info_dismiss_count = 0
        previous = self.tracker.state
        if self.tracker.observe(reading) is not None:
            if self.account_state is not None:
                self.account_state.reset_confirmation()
            self.bus.publish(
                events.ScreenChanged(
                    prev=previous.value,
                    curr=self.tracker.state.value,
                    confidence=reading.confidence,
                    scores=reading.scores,
                )
            )
            run_event = self.runs.transition(self.tracker.state, time.monotonic())
            if run_event is not None:
                if isinstance(run_event, events.RunStarted):
                    run_event = dataclasses.replace(run_event, purpose=settings.strategy.autopilot.purpose)
                if isinstance(run_event, events.RunEnded):
                    if (
                        self.tracker.state is screens.ScreenState.GAME_OVER
                        and reading.top_left is not None
                    ):
                        run_event = self._read_modal_stats(run_event, reading.top_left)
                    # Only ever raise the recorded best: a None wave (no
                    # modal reading, an abandoned run) must not lower or
                    # clear it. The same value sinks/store.py writes into
                    # the runs table's `wave` column, kept fresh here so
                    # _offer_claim never queries the database per scan.
                    if run_event.wave is not None and (
                        self._best_wave is None or run_event.wave > self._best_wave
                    ):
                        self._best_wave = run_event.wave
                    if run_event.tier is not None:
                        self._ladder_tier = run_event.tier
                        if run_event.wave is not None and run_event.wave > (
                            self._tier_best_wave.get(run_event.tier, 0)
                        ):
                            self._tier_best_wave[run_event.tier] = run_event.wave
                self.bus.publish(run_event)
                self.autopilot.suspend("Run boundary", clear_battle=True)

        state = self.tracker.state
        shopping_policy = settings.strategy.shopping
        # The menu header, read once per menu frame. The reroll policy below
        # must see this frame's balance: after an unconfirmed spend the
        # ledger's wallet is unknown, and only a fresh menu reading proves it
        # again. Read after the policy, the first menu frame after a run
        # decided "wallet unknown" and navigation started the next battle.
        menu_header: tuple[tuple[int, int] | None, int | None, int | None] | None = None
        if (not self.shopping.active
                and not self.shopping.reconciliation_pending
                and state is screens.ScreenState.MAIN_MENU
                and reading.state is screens.ScreenState.MAIN_MENU):
            menu_anchor = pages.classify_page(self.screen, self.templates).top_left
            menu_header = (menu_anchor, *header_numbers(self.screen, "MAIN_MENU", menu_anchor))
            self._observe_menu_wallet(menu_header[1], menu_header[2], evidence_ref=reads.digest)
            if self.reroll_progress is not None:
                self.reroll_progress.note_menu_wallet(menu_header[1])
        if self.reroll_progress is not None:
            if self.shopping.active and self._reroll_shopping_policy is not None:
                shopping_policy = self._reroll_shopping_policy
                if self.reroll_progress.route_changed_since_policy():
                    # End the current visit before taking a spend action with
                    # a rule that no longer belongs to the published route.
                    shopping_policy = dataclasses.replace(
                        shopping_policy, enabled=False, workshop=())
                    self._reroll_shopping_policy = None
            elif state is screens.ScreenState.MAIN_MENU and not self.shopping.reconciliation_pending:
                self._reroll_shopping_base = shopping_policy
                shopping_policy = self.reroll_progress.shopping_policy(shopping_policy)
                self._reroll_shopping_policy = shopping_policy
                evaluation = getattr(self.reroll_progress, '_route_evaluation', None)
                if (self.progress is not None and evaluation is not None
                        and evaluation.status == 'unknown'):
                    self.progress.observe_capability(
                        'workshop', 'actionable_unknown',
                        self.reroll_progress.route_error or 'prerequisite_unknown', 120)

        # A crashed spend owns the device, even if startup finds a different
        # screen. No speed, claim, navigation or battle action may precede proof.
        self._bind_lab_runtime()
        pending_lab = self.lab_visit.pending_transaction if self.lab_visit is not None else None
        if pending_lab is not None or self.lab_visit is not None and self.lab_visit.has_recovery_receipts:
            self.controls.drain()
            self.wallet = None
            self.autopilot.suspend('Pending Lab transaction; read-only inspection')
            if not settings.paused:
                if not self.lab_visit.active and self.lab_visit.recovery_status != 'lab_reconciliation_route_unavailable':
                    self.lab_visit.request(LabVisitOptions(start_research=False, unlock_slot2=False))
                try:
                    lab_boxes = reads.full()
                except Exception:
                    lab_boxes = ()
                self._observe_labs_capture(lab_boxes)
                self.lab_visit.advance(self.screen, lab_boxes, self.device, time.monotonic(),
                    observed_at=self._screen_captured_at, capture_scope=self._screen_fact_scope)
            self._progress_wait = ('settling', self.lab_visit.recovery_status or 'pending Lab verification', time.time()+15)
            self.bus.publish(events.ScanCompleted(screen=state.value,
                duration_ms=(time.monotonic()-started)*1000, wallet=None))
            return self.lab_visit.last_tap is not None
        # An unresolved purchase owns the scan only while its bounded read-only
        # inspection (or identity re-verification) can make progress. When it
        # is held, battles and scans continue; the open intent keeps every new
        # spend refused by TransactionJournal.prepare.
        if self.shopping.reconciliation_pending and (
                settings.paused or self.shopping.inspect(
                    self.screen, self.device, paused=settings.paused,
                    progress=self.progress, tuning=settings.strategy)):
            if self.progress is not None:
                transaction = self.shopping._unanswered_transaction()
                transaction_key = transaction.key if transaction is not None else 'unknown'
                self._progress_wait = (
                    'settling', f'purchase {transaction_key} reconciled', time.time() + 15)
            self.controls.drain()
            self.wallet = None
            self.autopilot.suspend("Transaction restart reconciliation; actions held")
            if self.frames is not None:
                self.frames.set_boxes([])
            self.bus.publish(events.ScanCompleted(
                screen=state.value, duration_ms=(time.monotonic() - started) * 1000,
                wallet=None,
            ))
            return False

        # A Labs visit owns the device until it returns to Battle. Its OCR
        # reader and state machine authorize at most one measured tap here.
        if self.lab_visit is not None and self.lab_visit.active:
            self.controls.drain()
            self.wallet = None
            self.autopilot.suspend("Labs visit holds actions")
            if settings.paused:
                self.lab_visit.cancel("paused")
                self.bus.publish(events.Skipped(
                    action="*", reason="paused", detail="Lab visit cancelled"))
                acted = False
            else:
                try:
                    lab_boxes = reads.full()
                except Exception:
                    lab_boxes = ()
                self._observe_labs_capture(lab_boxes)
                result = self.lab_visit.advance(
                    self.screen, lab_boxes, self.device, time.monotonic(),
                    observed_at=self._screen_captured_at, capture_scope=self._screen_fact_scope)
                action = self.lab_visit.last_tap
                acted = action is not None
                if action is not None:
                    self.bus.publish(events.Tapped(
                        action=f"lab:{action[0]}", x=action[1], y=action[2], score=1.0))
                if result is not None:
                    self._finish_lab_visit(result)
            if self.frames is not None:
                self.frames.set_boxes([])
            self.bus.publish(events.ScanCompleted(
                screen=state.value, duration_ms=(time.monotonic() - started) * 1000,
                wallet=None))
            return acted

        # Account overlays can retain a MAIN_MENU anchor underneath them. This
        # passive reader owns the frame before every possible action path,
        # including paused scans and an already-active shopping visit.
        screen_readings = self.account_state.screen_readings if self.account_state is not None else self._screen_readings
        walking_before = (self.collection.active or self.visit.active
                          or self.claim.active or self.milestones_claim.active
                          or self.cards_intro.active)
        # Spec P1: on a battle frame whose upgrade panel is readable, no
        # account panel, missions page or milestones screen is up - they are
        # menu overlays. MAIN_MENU, GAME_OVER and UNKNOWN keep all three,
        # because account overlays can keep a MAIN_MENU anchor visible
        # underneath. A covered panel (the safety net), a walk in progress or
        # a backstop scan (whose full read they reuse) also keeps them.
        if (reading.state is screens.ScreenState.IN_RUN and not walking_before
                and not self._battle_backstop_scan
                and self._battle_panel_visible(reads)):
            # The outcome each reader gives a frame it cannot measure (its
            # unsupported-geometry branch): no conclusion, and no hold.
            screen_readings.observe(None)
            self.missions.observe(None)
            self.milestones.observe(None)
            panel = missions_page = milestones_page = False
            inbox = mail_screen.MailReading()
            events_page = events_screen.EventsReading()
        else:
            panel = screen_readings.scan(self.screen)
            # The same passive ownership for the Daily Missions page. The bot
            # has no verified target on it, so a tap aimed at the menu
            # underneath would land somewhere nobody chose - it holds actions
            # exactly as a panel does, whether or not a visit is walking.
            # The missions and milestones readers share the scan's one
            # full-frame read (ocr.FrameReads). On a failed read each falls
            # back to its own attempt and reports its own error, which is the
            # behaviour they had before this was shared.
            try:
                shared_boxes = reads.full()
            except Exception:
                shared_boxes = None
            missions_page = self.missions.scan(self.screen, boxes=shared_boxes)
            intent = self._notifications.snapshot()["pending_claim"]
            if intent is not None and not self.claim.active:
                proof = None
                source = intent.get("scope", {})
                epoch = intent.get("fact_epoch")
                if (self.account_state is not None and type(epoch) is int
                        and all(isinstance(source.get(key), str) and source[key]
                                for key in ("account_id", "lease_id", "generation"))):
                    original = FactScope(account_id=source["account_id"],
                                         lease_id=source["lease_id"],
                                         generation=source["generation"], epoch=epoch)
                    proof = self.account_state.continuity(original, now=time.time())
                self._notifications.reconcile_claim(
                    self.missions.claim_evidence(),
                    frame_id=f"{PROCESS_BOOT_ID}:{self._capture_sequence}",
                    now=time.time(), continuity=proof)
            # The same passive ownership for both MILESTONES screens. This
            # matters most for the reward modal: it is a full-screen overlay
            # carrying a tappable CLAIM, and config.NAV_DISMISS - walked by
            # shopping.py's OPEN_CARDS step - holds nav/claim_reward.png and
            # nav/skip.png, both of which match it at 1.0000.
            milestones_page = self.milestones.scan(self.screen, boxes=shared_boxes)
            inbox = (mail_screen.parse(self.screen, shared_boxes)
                     if shared_boxes is not None else mail_screen.MailReading(error='mail_unreadable'))
            events_page = (events_screen.parse(self.screen, shared_boxes)
                           if shared_boxes is not None
                           else events_screen.EventsReading(error='events_unreadable'))
        if (not inbox.visible and inbox.error is None
                and not events_page.visible and events_page.error is None):
            self._mail_recovery_taps = 0
        if (self.reroll_progress is not None and not settings.paused
                and not panel and not missions_page and not milestones_page and not inbox.visible
                and not self.shopping.active and not self.shopping.reconciliation_pending
                and not self.collection.active and not self.visit.active
                and not self.claim.active and not self.milestones_claim.active
                and not self.cards_intro.active
                and at_home(state.value, screen_readings.current_evidence())
                and self.reroll_progress.stats_due()):
            if self.collection.request():
                self.reroll_progress.note_stats_requested()
        # Pause is the operator's stop-touching-my-device control, and this
        # block is the one path that taps while the guard is up - so pause
        # has to reach it. The runner already refuses to ARM a transaction on
        # a paused bot; ending one already walking is the same rule applied
        # at the next step boundary, before that step can act.
        if self.collection.active and settings.paused:
            self.collection.cancel(
                'paused', 'The bot was paused mid-transaction; it was not resumed.')
        if self.visit.active and settings.paused:
            self.visit.cancel(
                'paused', 'The bot was paused mid-visit; it was not resumed.')
        if self.claim.active and settings.paused:
            self.claim.cancel(
                'paused', 'The bot was paused mid-claim; it was not resumed.')
        if self.milestones_claim.active and settings.paused:
            self.milestones_claim.cancel(
                'paused', 'The bot was paused mid-claim; it was not resumed.')
        if self.cards_intro.active and settings.paused:
            self.cards_intro.cancel(
                'paused', 'The bot was paused mid-visit; it was not resumed.')
        if self.battle_menu.active and settings.paused:
            self.battle_menu.cancel(
                'paused', 'The bot was paused mid-visit; it was not resumed.')
        # An armed transaction owns the frame the same way a panel does, on
        # the menu as well as on the page itself: these are the only
        # sanctioned exceptions to the guard above, and nothing else may tap
        # while one walks. Their steps refuse to act on any frame this same
        # scan did not identify - see account_collection and missions_visit.
        # At most one is ever armed; the runner refuses to arm the second.
        # A page holding actions with NOTHING walking is the one shape of
        # hold that can never end on its own, and it is a real deadlock
        # rather than a slow recovery: the frame only changes when something
        # taps, and the hold is what forbids tapping. Observed live after a
        # milestone paid `Unlock Lab` - the full-screen ceremony carries a
        # SKIP, milestones_screen.scan reads that as "a milestones screen is
        # up", and the claim walk had already given up (modal_unreadable,
        # correctly refusing to tap what it could not read). ~180 consecutive
        # held scans, freed only by hand.
        #
        # Counted here rather than inside the readers because no single
        # reader can see the condition: "a page is up" is one module's
        # answer and "nothing is walking" is another's.
        walking_now = (self.collection.active or self.visit.active
                       or self.claim.active or self.milestones_claim.active
                       or self.cards_intro.active)
        # Cancelling a mail walk while paused can leave a full-screen Inbox
        # whose lifecycle state is UNKNOWN. Its current semantic footer is
        # the only permitted recovery action; never restart reward traversal.
        # The Events page is left the same way, by its `Tap To Return To
        # Game` footer: the stall watchdog will not press anything there,
        # because its mission text ("Buy 20 cards") reads as a purchase.
        stranded = inbox if inbox.visible else events_page if events_page.visible else None
        if stranded is not None and not walking_now and not self.shopping.active:
            page_name = 'mail' if stranded is inbox else 'events'
            self.controls.drain()
            self.wallet = None
            self.autopilot.suspend(f'{page_name} page recovery; actions held')
            footer = stranded.back
            recovered = False
            moment = time.monotonic()
            if (not settings.paused and settings.strategy.auto_navigate
                    and self._mail_recovery_taps < 3
                    and moment - self._mail_recovery_at >= config.NAVIGATION_COOLDOWN_SECONDS
                    and footer is not None and footer.status == 'located'
                    and footer.point is not None and footer.rect is not None
                    and footer.rect[1] > self.screen.shape[0] * .85):
                x, y = jitter.point(*footer.point, settings.strategy.tap_jitter_px)
                jitter.pause(settings.strategy.tap_delay, settings.strategy.timing_jitter)
                tap(self.device, x, y)
                self._mail_recovery_taps += 1
                self._mail_recovery_at = moment
                recovered = True
                self.bus.publish(events.Tapped(action=f'{page_name}_recovery:return', x=x, y=y,
                                              score=footer.score))
            else:
                self.bus.publish(events.Skipped(action='*', reason=f'{page_name}_recovery_guard',
                                               detail=f'The {page_name} page is open; '
                                               'waiting for a safe return.'))
            if self.frames is not None:
                self.frames.set_boxes([])
            self.bus.publish(events.ScanCompleted(screen=state.value,
                duration_ms=(time.monotonic() - started) * 1000, wallet=None))
            return recovered
        if (panel or missions_page or milestones_page) and not walking_now:
            self._held_scans += 1
        else:
            self._held_scans = 0

        # Past the limit the guard stops OWNING the frame - it does not stop
        # holding taps. The action loop below is still gated on IN_RUN, which
        # a menu page is not, so nothing starts buying; what it buys back is
        # the rest of the pass, and with it navigation, whose NAV_DISMISS set
        # already carries the skip and claim-reward buttons these ceremonies
        # are built from.
        deadlocked = self._held_scans > config.HELD_PAGE_SCAN_LIMIT

        if not deadlocked and (
                panel or missions_page or milestones_page or self.collection.active
                or self.visit.active or self.claim.active
                or self.milestones_claim.active or self.cards_intro.active):
            self.controls.drain()
            self.wallet = None
            if panel:
                reason, detail = ('account_screen_guard',
                                  'Account panel or OCR error; actions held')
            elif missions_page:
                reason, detail = ('missions_screen_guard',
                                  'The missions page is up; actions held')
            elif milestones_page:
                reason, detail = ('milestones_screen_guard',
                                  'A milestones screen is up; actions held')
            elif self.collection.active:
                reason, detail = ('collect_stats_transaction',
                                  'A read-only Collect stats transaction holds actions')
            elif self.claim.active:
                reason, detail = ('missions_claim_transaction',
                                  'A Missions claim walk holds actions')
            elif self.milestones_claim.active:
                reason, detail = ('milestones_claim_transaction',
                                  'A Milestones claim walk holds actions')
            elif self.cards_intro.active:
                reason, detail = ('cards_intro_transaction',
                                  'The first Cards visit holds actions')
            else:
                reason, detail = ('missions_visit_transaction',
                                  'A read-only Missions visit holds actions')
            self.autopilot.suspend('Account screen observation; actions held' if panel else detail)
            if self.collection.active:
                walking = 'collect_stats'
                action = self.collection.advance(
                    screen=self.screen, device=self.device, templates=self.templates,
                    readings=screen_readings, state=state.value, tuning=settings.strategy,
                )
            elif self.visit.active:
                walking = 'missions_visit'
                action = self.visit.advance(
                    screen=self.screen, device=self.device, templates=self.templates,
                    readings=screen_readings, missions=self.missions,
                    state=state.value, tuning=settings.strategy,
                )
            elif self.claim.active:
                walking = 'missions_claim'
                action = self.claim.advance(
                    screen=self.screen, device=self.device, templates=self.templates,
                    readings=screen_readings, missions=self.missions,
                    bus=MissionReceiptBus(self.bus, self._notifications),
                    state=state.value, tuning=settings.strategy,
                )
            elif self.milestones_claim.active:
                walking = 'milestones_claim'
                action = self.milestones_claim.advance(
                    screen=self.screen, device=self.device, templates=self.templates,
                    readings=screen_readings, milestones=self.milestones, bus=self.bus,
                    state=state.value, tuning=settings.strategy,
                )
            elif self.cards_intro.active:
                walking = 'cards_intro'
                action = self.cards_intro.advance(
                    screen=self.screen, device=self.device, templates=self.templates,
                    readings=screen_readings, state=state.value, tuning=settings.strategy,
                )
                if not self.cards_intro.active:
                    logger.info("First Cards visit ended: %s",
                                self.cards_intro.snapshot()['result'])
            else:
                walking, action = None, None
            # A tap nobody can find afterwards is the failure this guards
            # against: the transaction's taps get the same Tapped event and
            # the same overlay crosshair as every other tap path, and the
            # "actions held" skip is published only on the scans where that
            # is actually what happened.
            if self.frames is not None:
                self.frames.set_boxes([] if action is None else [{
                    'name': f'{walking}:{action.name}',
                    'x': int(action.rect[0]) if action.rect else int(action.x),
                    'y': int(action.rect[1]) if action.rect else int(action.y),
                    'w': int(action.rect[2]) if action.rect else 0,
                    'h': int(action.rect[3]) if action.rect else 0,
                    'tap_x': int(action.x), 'tap_y': int(action.y),
                    'score': float(action.score), 'tapped': True,
                }])
            if action is None:
                self.bus.publish(events.Skipped(action='*', reason=reason, detail=detail))
            else:
                self.bus.publish(events.Tapped(
                    action=f'{walking}:{action.step}', x=action.x, y=action.y,
                    score=action.score,
                ))
            self.bus.publish(events.ScanCompleted(screen=state.value,
                duration_ms=(time.monotonic() - started) * 1000, wallet=None))
            return action is not None

        # The wallet region is anchored to the IN_RUN template, so it can only
        # be read on that screen. Clear it elsewhere: a stale wallet would let
        # the affordability gate approve a purchase using last run's cash.
        #
        # BOTH states, not just the tracker's: the tracker is debounced, so
        # mid-fade it still says IN_RUN while the frame is already the death
        # modal. `reading.top_left` is then the GAME_OVER anchor, and the
        # wallet region measured from it lands somewhere else entirely. The
        # anchor and the region have to come from the same frame.
        in_run_anchor = (
            reading.top_left
            if (
                state is screens.ScreenState.IN_RUN
                and reading.state is screens.ScreenState.IN_RUN
                and reading.top_left is not None
            )
            else None
        )

        # Measured from the cash counter this frame matched, not from the
        # panel anchor above. The panel is pinned to the bottom of the screen
        # and the wallet to the top, and emulators reserve different amounts
        # of the top for a display cutout, so the gap between the two is not
        # a constant - see config.WALLET_FROM_CASH. Anchoring the wallet to
        # the counter also means it survives the DEFENSE and UTILITY tabs,
        # where there is no panel anchor to measure from at all.
        #
        # Still gated on IN_RUN, and for the original reason: the counter is
        # visible behind the death modal too, so this would otherwise read a
        # finished run's cash and let the affordability gate spend it.
        cash_anchor = (
            reading.cash_top_left
            if (
                state is screens.ScreenState.IN_RUN
                and reading.state is screens.ScreenState.IN_RUN
                and reading.cash_top_left is not None
            )
            else None
        )

        self.wallet = None
        if cash_anchor is not None:
            self.wallet = self.reader.read(
                self.screen, config.WALLET_FROM_CASH, cash_anchor, "wallet"
            )
            if self.wallet is None and (settings.strategy.autopilot.enabled or self.autopilot.has_work):
                self.wallet = read_cash(self.screen, cash_anchor, config.WALLET_FROM_CASH)
        if isinstance(self.affordability, DigitAffordability):
            self.affordability.wallet = self.wallet

        # Drained every pass, whatever the screen, and deliberately: a
        # command the loop cannot honour right now is discarded rather than
        # banked. Holding it would fire the tap the moment the bot next
        # entered a run - which can be minutes later, long after the user
        # who pressed the button stopped watching for it.
        commands = self.controls.drain()
        speed_changed = self._manage_speed(settings, in_run_anchor, commands)
        if speed_changed and (self.reroll_progress is not None or self._deadline_speed is not None):
            # The readout changes after the tap. Give it a fresh frame before
            # any gem claim, in-battle purchase, or navigation can act.
            if self.frames is not None:
                self.frames.set_boxes([])
            self.bus.publish(events.ScanCompleted(
                screen=state.value, duration_ms=(time.monotonic() - started) * 1000,
                wallet=self.wallet,
            ))
            return True

        # A visit owns the frame while it runs. Three things below key off
        # this rather than off the screen state, because the pages a visit
        # walks are UNKNOWN to the tracker by design - see pages.py.
        if (not settings.paused and self.shopping.inspection_requested(self.progress)
                and self.shopping.inspect(self.screen, self.device, paused=settings.paused,
                                          progress=self.progress, tuning=settings.strategy)):
            self.bus.publish(events.ScanCompleted(screen=state.value,
                duration_ms=(time.monotonic()-started)*1000, wallet=None))
            return False
        # Reaching here with an open intent means its reconciliation is held
        # (an owned inspection returned above): only a live visit step
        # suppresses navigation; spending stays refused by the open intent.
        visiting = self.shopping.visit_in_progress

        # Which menu page this is, when the tracker cannot say. ScreenState
        # models the run lifecycle only, so WORKSHOP and CARDS both read
        # UNKNOWN to it by design (see pages.py) - but "unknown" and
        # "unmodelled" are not the same thing, and everything below this line
        # that used to conflate them got it wrong: the frame was filed as a
        # mystery, reported as one, and left un-navigated.
        #
        # Computed only on the UNKNOWN path, which is the only path that asks.
        # Four templates at ~130ms is real, and this buys it on precisely the
        # scans where the bot has nothing else to do with the frame - the
        # action loop below gates on IN_RUN. `not visiting` because a live
        # visit owns the frame and already classifies it for itself.
        menu_page = pages.UNKNOWN
        if self.tracker.confirmed and state is screens.ScreenState.UNKNOWN and not visiting:
            menu_page = pages.classify_page(self.screen, self.templates).page

        # A CONFIRMED unknown, not the tracker's initial placeholder value.
        # Snapshotting on the placeholder means scan 1 of every launch saves
        # a perfectly recognisable screen; at 50 kept files, 50 launches
        # would evict every genuine one. `not visiting` on top of that: a
        # workshop or cards page reads UNKNOWN to this tracker by design (see
        # pages.py), so without this guard every shopping visit would fill
        # unknown/ with pictures of the very pages it is deliberately
        # visiting, evicting the genuine unmodelled screens the directory
        # exists to hold.
        #
        # And `menu_page` for the case `visiting` always missed: parked on a
        # menu page with no visit running - an idle bot, a visit that just
        # ended, a device a human left on the workshop - the guard above was
        # simply off. Measured live: fifty consecutive snapshots of the
        # UTILITY tab, every one of them a page classify_page names at 1.000,
        # filling the whole directory and evicting every genuine find.
        if (self.tracker.confirmed and state is screens.ScreenState.UNKNOWN
                and not visiting and menu_page == pages.UNKNOWN):
            path = self.snapshots.maybe_write(self.screen)
            if path is not None:
                best = max(reading.scores, key=lambda name: reading.scores[name])
                self.bus.publish(
                    events.UnknownScreen(
                        snapshot_path=str(path),
                        best_anchor=best,
                        best_score=reading.scores[best],
                    )
                )

        # The screen gate is hoisted out of the action loop so an idle bot
        # emits ONE skip per scan rather than one per action.
        clicked = False
        # Collected locally and swapped into `frames` in one atomic call
        # once the loop below is done, rather than each match landing there
        # the instant it is found - see FrameBuffer.set_boxes(). Stays empty
        # here whenever the loop below does not run (paused, screen-gated),
        # matching add_box() never having been called in those cases before.
        boxes: list[dict[str, Any]] = []
        if settings.paused:
            self.autopilot.suspend("Paused from the control room")
            # Still scanning, still reporting - just not acting. One skip per
            # scan, not one per action, matching the screen gate below.
            self.bus.publish(
                events.Skipped(action="*", reason="paused", detail="paused from the dashboard")
            )
        elif state is screens.ScreenState.IN_RUN:
            # The free gem orbiting the tower, before any buying. It costs
            # nothing, it pays account gems rather than the per-run cash
            # everything below spends, and it is gone in seconds - so it
            # does not queue behind an autopilot pass.
            #
            # Gated on cash_anchor, which the wallet read has already found
            # this scan: without the anchor there is no HUD to measure the
            # search region or the gem counter from, and a fixed-pixel
            # fallback would be wrong on any emulator with a display cutout.
            if cash_anchor is not None and self.gem.observe(
                screen=self.screen, anchor=cash_anchor, device=self.device,
                policy=settings.strategy, now=time.time(),
                run_id=self.runs.current_id,
            ):
                # The gem tap changed the game after this frame was captured.
                # A purchase or navigation tap based on the same image would
                # race it, and the supervisor rightly rejects that second
                # action until a new frame confirms the first one.
                if self.frames is not None:
                    self.frames.set_boxes([])
                self.bus.publish(events.ScanCompleted(
                    screen=state.value, duration_ms=(time.monotonic() - started) * 1000,
                    wallet=self.wallet, wave=self._reported_wave(state),
                ))
                return True

            # The in-battle menu's badged hamburger, opportunistic like the
            # gem above: only when nothing else already owns the tap (a
            # walk, an active shopping visit, or an open Lab transaction /
            # Workshop purchase acknowledgement - the same "pending_purchase"
            # pair the deadline-speed gate above checks).
            if (not self.gem.active and not self.shopping.active
                    and not self.shopping.reconciliation_pending
                    and self.autopilot.pending is None
                    and not self._any_walk_active()
                    and self.battle_menu.observe(
                        screen=self.screen, boxes=reads.full, device=self.device,
                        policy=settings.strategy, now=time.time(), in_run=True,
                    ) is BattleMenuOutcome.TAPPED):
                if self.frames is not None:
                    self.frames.set_boxes([])
                self.bus.publish(events.ScanCompleted(
                    screen=state.value, duration_ms=(time.monotonic() - started) * 1000,
                    wallet=self.wallet, wave=self._reported_wave(state),
                ))
                return True

            # The strategy's rows, in the strategy's order - order IS
            # priority. Before, this walked config.ACTIONS and used the
            # settings only as an on/off filter, so neither reordering nor
            # a per-row threshold could reach the matcher.
            if settings.strategy.autopilot.enabled or self.autopilot.has_work:
                # Deliberately NOT gated on `in_run_anchor`. That anchor is
                # the ATTACK tab's header crop, and it used to be what made
                # the screen IN_RUN at all, so requiring it here was free.
                # Once IN_RUN became the cash counter's job, the panel match
                # started coming back None on the DEFENSE and UTILITY tabs -
                # and this gate quietly became "the autopilot only runs on
                # ATTACK". Every economy preset opens UTILITY as its first
                # act, so one tab tap parked the autopilot for the rest of
                # the run: it could not read the tab it had just opened, and
                # could not navigate back off it either. `step()` takes no
                # anchor; it re-reads the panel from the frame itself.
                if not speed_changed and not commands:
                    # One read of this frame serves both the route decision
                    # and the step. The context alone would hand the route
                    # the previous scan's wave, already expired at the battle
                    # scan pace, and a route without a wave turns buying off.
                    observation = observe_frame(self.screen, "battle", reads=reads)
                    if self.progress is not None and self.runs.current_id is not None:
                        self.progress.observe_run(
                            self.runs.current_id,
                            wave=observation.combat.get("wave"),
                            game_speed=observation.combat.get("game_speed"),
                            observed_at=observation.observed_at,
                        )
                    combat = frame_combat(self.autopilot.context.combat(time.time()), observation)
                    wave_value = combat.get('wave')
                    wave_number = (int(wave_value) if isinstance(wave_value, (int, str))
                                   and str(wave_value).isdigit() else None)
                    if wave_number is not None:
                        self._scan_wave = wave_number
                    if (self.progress is not None and wave_number is not None
                            and (self._last_wave_progress is None
                                 or self._last_wave_progress[0] != self.runs.current_id
                                 or wave_number > self._last_wave_progress[1])):
                        self.progress.meaningful_progress('wave',
                            f'{self.runs.current_id}:{wave_number}')
                        self._last_wave_progress = (self.runs.current_id, wave_number)
                    battle_policy = (self.reroll_progress.battle_policy(
                        settings.strategy.autopilot,
                        self.autopilot.state.rows("battle", time.time(), self.run_identity(settings)),
                        run_id=self.runs.current_id,
                        wave=(int(value) if (value := combat.get("wave")) is not None else None),
                        cash=self.wallet,
                        combat=combat)
                                     if self.reroll_progress is not None else settings.strategy.autopilot)
                    clicked = self.autopilot.step(self.screen, self.device, battle_policy,
                                                   cash=self.wallet, observation=observation,
                                                   cooldown=settings.strategy.click_cooldown,
                                                   run_id=self.runs.current_id,
                                                   identity=self.run_identity(settings),
                                                   elapsed=self.runs.elapsed(time.monotonic()),
                                                   reads=reads)
                    # The autopilot reads the panel itself, so its rows are
                    # the only description of this frame anything has. Left
                    # out, the set_boxes() below blanks the device view on
                    # every scan the autopilot owns - which, once it owns
                    # in-run buying, is every scan of every run.
                    boxes.extend(self.autopilot.boxes)
            else:
                self.autopilot.suspend("Autopilot is off; legacy purchases are active")
                for rule in settings.strategy.actions:
                    if not rule.enabled:
                        continue
                    if self.find_and_click_image(
                        rule.as_action(), boxes, tuning=settings.strategy
                    ):
                        clicked = True
        else:
            self.autopilot.suspend(f"Waiting for battle ({state.value})")
            # Names the menu page when there is one to name. "screen is
            # UNKNOWN" is true of the workshop and useless on it: the feed's
            # job here is to say what the bot is waiting on, and "the
            # WORKSHOP page" is the answer a reader can act on.
            detail = f"screen is {state.value}"
            if menu_page != pages.UNKNOWN:
                detail = f"{detail} (the {menu_page} page)"
            self.bus.publish(
                events.Skipped(action="*", reason="screen_gated", detail=detail)
            )

        if self.frames is not None:
            self.frames.set_boxes(boxes)

        if (state is screens.ScreenState.MAIN_MENU
                and reading.state is screens.ScreenState.MAIN_MENU):
            self._observe_menu_notifications(time.time())

        # Reserve this frame for a due Workshop visit before navigation can
        # start the next battle. Newly begun visits advance on the next frame.
        if (
            not visiting and state is screens.ScreenState.MAIN_MENU
            and reading.state is screens.ScreenState.MAIN_MENU
            and not settings.paused
            and not self.run_cap_reached(max_runs, settings.strategy)
        ):
            # A claim walk is a peer of a shopping visit, not a companion:
            # only one maintenance walk may hold the menus. So the claim is
            # only offered on a frame the visit declined - reading begin()'s
            # existing answer rather than re-deriving the same decision.
            badge = milestones_badge.badge_visible(self.screen, self.templates)
            if badge is not None:
                self._milestones_badge = badge
            if menu_header is None:
                menu_anchor = pages.classify_page(self.screen, self.templates).top_left
                menu_header = (menu_anchor, *header_numbers(self.screen, "MAIN_MENU", menu_anchor))
                self._observe_menu_wallet(menu_header[1], menu_header[2], evidence_ref=reads.digest)
            menu_anchor, menu_coins, menu_gems = menu_header
            if self.reroll_progress is not None:
                self.reroll_progress.note_menu_wallet(menu_coins)
                self.reroll_progress.resource_evaluation(menu_coins, menu_gems)
            # The Workshop tutorial grants 50 coins. Visit it first on a fresh
            # account so the first affordable upgrade can use that grant.
            initial_workshop = (self.reroll_progress is not None
                                and self.reroll_progress.initial_workshop_due()
                                and self._menu_tab_unlocked("workshop"))
            if (self._notifications.verification_due(time.time())
                    and not self.collection.active and not self.claim.active
                    and not self.milestones_claim.active and self.visit.request()):
                self._notifications.note_verification_visit(time.time())
                logger.info("Armed a bounded read-only Missions reconciliation visit.")
            elif (self.lab_visit is not None
                    and self.maintenance.inspection_navigable(time.time())
                    and self.lab_visit.tab_status(self.screen) == 'unlocked'
                    and self.lab_visit.request(LabVisitOptions(start_research=False, unlock_slot2=False))):
                # Paced: an inspection a visit cannot acknowledge (partial
                # strip, clock step) never re-arms on every menu pass.
                self.maintenance.note_inspection_visit(time.time())
                self._maintenance_reason('inspecting_labs')
            elif initial_workshop and self.shopping.begin(
                    shopping_policy, self.runs.completed):
                logger.info("Starting the first Workshop visit for the tutorial coin grant.")
            # The first Cards visit is owed once and pays gems. After Workshop,
            # reroll claims rewards and checks Labs only when the tab is unlocked.
            elif self._offer_cards_intro():
                logger.info("Armed the first Cards visit from the main menu.")
            elif self.lab_visit is not None and self.reroll_progress is not None:
                armed = self._offer_claim(settings)
                if armed is not None:
                    logger.info("Armed a %s claim from the main menu.", armed)
                else:
                    # Persist only an explicit lock or unlocked-tab match.
                    # An ambiguous frame remains unknown and never authorizes
                    # a tap into Labs.
                    labs_tab_status = self.lab_visit.tab_status(self.screen)
                    if labs_tab_status == "unlocked":
                        self.reroll_progress.note_lab_unlocked("labs_tab")
                    elif labs_tab_status == "locked":
                        self.reroll_progress.note_lab_locked("labs_tab")
                    lab_notice_due = self._notifications.eligible("labs", time.time())
                    if (labs_tab_status == "unlocked"
                            and self._request_planned_lab_visit(time.time(), lab_notice_due or self.reroll_progress.lab_due(
                                wallet_coins=menu_coins, wallet_gems=menu_gems,
                            ))):
                        route_runtime = getattr(self.reroll_progress, 'route_runtime', None)
                        self._lab_visit_revision = route_runtime.current().revision if route_runtime is not None else None
                        if lab_notice_due:
                            self._notifications.begin("labs", time.time())
                        logger.info("Armed Labs check from the confirmed unlocked tab.")
                    else:
                        if self._menu_tab_unlocked("workshop"):
                            self.shopping.begin(shopping_policy, self.runs.completed)
            elif not self.shopping.begin(shopping_policy, self.runs.completed):
                armed = self._offer_claim(settings)
                if armed is not None:
                    logger.info("Armed a %s claim from the main menu.", armed)

        if (
            settings.strategy.auto_navigate
            and not settings.paused
            and not self.run_cap_reached(max_runs, settings.strategy)
            and not visiting and not self.shopping.visit_in_progress
            and not self.claim.active and not self.milestones_claim.active
            and not self.cards_intro.active
            and not (self.lab_visit is not None and self.lab_visit.active)
        ):
            # Navigator taps BATTLE on MAIN_MENU on a cooldown - left alone
            # it would start a run in the middle of a shopping errand.
            #
            # A claim walk is the same kind of maintenance visit as shopping,
            # and needs the same suppression here - but is not folded into
            # `visiting` above. `visiting` feeds shopping.advance() directly
            # a few lines below (`elif visiting: self.shopping.advance(...)`),
            # so widening its meaning to cover claim walks would call
            # shopping.advance() on a frame only a claim armed. The claim can
            # only just have gone active THIS frame - _offer_claim() runs
            # after `visiting` is captured - so the frame this guards is
            # exactly the one on which request() flips .active from False to
            # True; every later frame is already caught by the "actions held"
            # early return above (self.claim.active there), which never
            # reaches this block at all.
            # On GAME_OVER it taps RETRY, which starts the next run without
            # passing through MAIN_MENU - and the begin() above is only ever
            # offered a MAIN_MENU frame. So a due visit has to be claimed
            # here, one screen early, or the bot never reaches the menu to
            # be asked at all. Same gate begin() uses, so a detour is only
            # taken when the visit it exists for will actually start.
            if not (state is screens.ScreenState.MAIN_MENU
                    and self._advance_tier(settings)):
                self.navigator.maybe_navigate(
                    self.screen,
                    state,
                    self.device,
                    now=time.monotonic(),
                    tuning=settings.strategy,
                    go_home=((self.shopping.due(shopping_policy, self.runs.completed)
                              and (self.reroll_progress is None
                                   or self.reroll_progress.initial_workshop_due()
                                   or self.reroll_progress.workshop_worthwhile(
                                       publish_estimate=state is screens.ScreenState.GAME_OVER)))
                             or self._claim_owed(settings)
                             # A held purchase re-inspects only on MAIN_MENU; detour
                             # home when its paced retry is due (never spends).
                             or (state is screens.ScreenState.GAME_OVER
                                 and self.shopping.reconciliation_retry_due(time.time()))
                             or (state is screens.ScreenState.GAME_OVER
                                 and self._menu_badge_check_due(settings))
                             or (state is screens.ScreenState.GAME_OVER
                                 and self.reroll_progress is not None
                                 and self.reroll_progress.stats_due())
                             or (state is screens.ScreenState.GAME_OVER
                                 and self.reroll_progress is not None
                                 and self.reroll_progress.lab_due())),
                    # The way off a menu page. NAV_BUTTONS is keyed by
                    # ScreenState, which has no member for one, so the bot could
                    # neither act on the workshop (the loop above gates on
                    # IN_RUN) nor leave it: measured live, twenty unbroken
                    # minutes on the UTILITY tab. UNKNOWN with nothing named
                    # still taps nothing - see config.MENU_NAV_BUTTONS.
                    menu_page=("MILESTONES" if deadlocked and milestones_page
                               and self.milestones.current_evidence()['screen_id']
                               == 'milestones.ladder' else
                               None if menu_page == pages.UNKNOWN else menu_page),
                    # Only once the hold above has proved itself permanent. A
                    # ceremony has no exit button of its own, so without this
                    # the released guard buys nothing: navigation looks for a
                    # menu page's exit, finds none, and taps nothing forever.
                    dismiss=deadlocked,
                )

        # Checked after navigation, and begin() checked after advance() below:
        # a visit that just ended this same scan must not restart within it,
        # and must not race the tap navigation just skipped above.
        if visiting and settings.paused:
            # Freeze, don't unwind. advance() is the one tap path that spends
            # currency, so pause has to suppress it too, not just the start
            # of a visit - "still scanning, not tapping" has to hold
            # mid-errand. Ending the visit instead would want a return-to-
            # Battle tap of its own, which is exactly what pause forbids;
            # `visiting` stays True below (shopping.active is untouched), so
            # navigation and unknown-snapshot suppression both stay in
            # force too - the bot is still sitting on a menu page either
            # way. The paused Skipped event published above already makes
            # this visible on the feed. The visit simply resumes, from
            # wherever it left off, on the next unpaused scan.
            pass
        elif visiting:
            self.shopping.advance(
                self.screen,
                self.device,
                shopping_policy,
                tuning=settings.strategy,
            )

        self.bus.publish(
            events.ScanCompleted(
                screen=state.value,
                duration_ms=(time.monotonic() - started) * 1000,
                wallet=self.wallet,
                wave=self._reported_wave(state),
            )
        )
        return clicked

    def _reported_wave(self, state: screens.ScreenState) -> int | None:
        """The wave a ScanCompleted may carry: the last one read, IN_RUN only.

        Leaving the run forgets it, so the next run never starts on the
        previous run's wave.
        """
        if state is not screens.ScreenState.IN_RUN:
            self._scan_wave = None
        return self._scan_wave

    def run_forever(
        self,
        interval: float | None = None,
        max_runs: int | None = None,
    ) -> None:
        """Run scans back to back until stop() is called or max_runs is hit.

        `interval` has two distinct meanings, deliberately:

        - `None` (the default, and what `BotRunner._run()` passes in
          production - see runner.py) means the dashboard owns the pace.
          `strategy.interval_for(...)` (battle or menu pace)
          is re-read after every scan, so a change made from the
          browser takes effect on the very next sleep rather than requiring a
          restart.
        - An explicit number is a caller override for the normal scan interval.
          A future maintenance deadline can shorten it; it is never written into
          Controls - this is the seam the tests use to run the loop without
          sleeping (`run_forever(interval=0.0)`). Controls.apply() enforces
          MIN_INTERVAL/MAX_INTERVAL because a browser-supplied value has to
          be sane; a test's 0.0 is not a browser and must not be bound by
          those rules.

        The between-scan wait is `self._stopping.wait(...)`, not
        `time.sleep(...)`: stop() sets that event, so a shutdown ends the
        wait immediately regardless of how large the interval is, rather
        than sleeping it out (which a plain time.sleep() would do - PEP 475
        resumes it after a signal handler returns instead of aborting it).
        """
        if interval is None:
            live = self.controls.snapshot().strategy
            logger.info(
                "Bot started - scanning every %.1fs in battle, %.1fs outside. "
                "Ctrl+C to stop.", live.interval, live.menu_interval,
            )
        else:
            logger.info("Bot started - scanning every %.1fs. Ctrl+C to stop.", interval)
        while self._running:
            # Checked before run_once(): if the limit is already reached at
            # entry, the loop must return without scanning at all, not after
            # one more pass.
            #
            # One snapshot feeds both the check and the message. The cap is
            # logged RESOLVED rather than as the parameter, because in the
            # web path the parameter is None: BotRunner._run() calls
            # run_forever() with no arguments and the cap comes from the
            # strategy. "%d" % None raises inside logging, so the operator
            # would get a "--- Logging error ---" traceback at exactly the
            # moment the line exists to explain - the dashboard parking with
            # a stopped bot.
            capped = self.controls.snapshot().strategy
            if self.run_cap_reached(max_runs, capped):
                logger.info(
                    "Reached the run cap of %d, stopping.",
                    max_runs if max_runs is not None else capped.max_runs,
                )
                break
            try:
                self.run_once(max_runs=max_runs)
            except EmulatorError as exc:
                logger.error("Device error: %s", exc)
                self._report(f"Device error: {exc}")
                if self.supervisor is not None and self.supervisor.device is None:
                    self.supervisor.recover()
            except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the loop
                logger.exception("Unexpected error during scan")
                self._report(f"Unexpected error during scan: {exc}")
            # Re-read every iteration (when interval is None) rather than
            # once at the top of the loop - see the docstring above. Chosen
            # after the scan, because the scan is what says battle or menu.
            if interval is None:
                live = self.controls.snapshot().strategy
                # spread(), not stretch(): unlike the cooldowns, the interval
                # is a target rather than a floor, and a loop that only ever
                # waited longer than its nominal interval would still be a
                # metronome - just a slower one. Clamped at MIN_INTERVAL so a
                # dial already at the floor cannot jitter below it into the
                # busy loop that floor exists to prevent.
                current_interval = max(
                    MIN_INTERVAL,
                    jitter.spread(
                        live.interval_for(self._last_scan_in_battle),
                        live.timing_jitter,
                    ),
                )
            else:
                # An explicit override is used exactly as given - see the
                # docstring. Tests pass 0.0 to run the loop without sleeping,
                # and jittering that would reintroduce the sleep.
                current_interval = interval
            if interval is None and self.autopilot.pending is not None:
                # A tap is waiting on the frame that confirms it - and that
                # decides the next one - so fetch it promptly.
                current_interval = min(current_interval, config.BATTLE_FOLLOWUP_SECONDS)
            current_interval = self.maintenance.wait_seconds(time.time(), current_interval)
            self._stopping.wait(current_interval)
        logger.info("Bot stopped.")

    def _report(self, message: str) -> None:
        """Put a failure on the event stream, not just in the log.

        Under --tui the log is suppressed entirely, so this is the only way a
        device failure reaches the panel instead of silently stalling it.
        """
        self.bus.publish(
            events.BotError(message=message, traceback=traceback.format_exc())
        )

    def stop(self, *_: object) -> None:
        self._running = False
        # Interrupts an in-flight self._stopping.wait() in run_forever(), so
        # shutdown does not have to wait out whatever interval is current.
        self._stopping.set()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Background bot for The Tower.")
    parser.add_argument("--host", default=config.DEVICE_HOST, help="emulator ADB host")
    parser.add_argument("--port", type=int, default=config.DEVICE_PORT, help="emulator ADB port")
    parser.add_argument(
        "--dismiss-bluestacks-upgrade", action="store_true",
        help="close only the measured BlueStacks Air host update notice for the exact instance",
    )
    parser.add_argument(
        "--game-package", default=None,
        help="verified Android package to resume after reconnect; omitted means no relaunch",
    )
    parser.add_argument(
        "--interval", type=float, default=None,
        help=(
            "seconds between scans - overrides the active strategy AND is "
            "saved into it"
        ),
    )
    parser.add_argument(
        "--once", action="store_true",
        help="scan just enough times for the screen tracker to settle, then exit",
    )
    parser.add_argument(
        "--debug-scores", action="store_true",
        help=(
            "one-shot diagnostic: capture a single frame and print every "
            "action's and screen anchor's match score (plus brightness "
            "ratio for actions), then exit without scanning or tapping"
        ),
    )
    parser.add_argument(
        "--tui", action="store_true", help="live terminal panel instead of log lines"
    )
    parser.add_argument(
        # BooleanOptionalAction, so --no-auto-navigate exists too. With a
        # plain store_true the flag was a one-way switch: passing it saved
        # True into the strategy (see apply_cli_overrides), and nothing on
        # the command line could ever put it back - the only way off was the
        # dashboard. `default=None` still distinguishes "not passed" from
        # "passed False", which is what keeps an absent flag from
        # overwriting the saved profile.
        "--auto-navigate", action=argparse.BooleanOptionalAction, default=None,
        help="tap RETRY / BATTLE to loop runs unattended - overrides and saves",
    )
    parser.add_argument(
        "--max-runs", type=int, default=None,
        help="stop after this many runs - overrides and saves",
    )
    parser.add_argument(
        "--affordability", choices=("digits", "brightness"), default=None,
        help=(
            "how to decide an upgrade is buyable: read the numbers (exact) "
            "or compare brightness (the older heuristic) - overrides and "
            "saves. digits falls back to brightness on its own when no "
            "atlas is built"
        ),
    )
    parser.add_argument(
        "--web", action="store_true",
        help="serve the dashboard while the bot runs (default: off)",
    )
    parser.add_argument(
        "--web-host", default=config.WEB_HOST,
        help="dashboard bind address - loopback by default, and there is no auth",
    )
    parser.add_argument("--web-port", type=int, default=config.WEB_PORT)
    parser.add_argument(
        "--telegram-interval", type=float, default=None,
        help="seconds between Telegram status digests "
             "(default: $TELEGRAM_SUMMARY_SECONDS, else "
             f"{config.TELEGRAM_SUMMARY_SECONDS:.0f})",
    )
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="suppress Telegram digests even when the environment configures them",
    )
    parser.add_argument("--worker-id", default=None, help="fleet worker identity")
    parser.add_argument("--lease-id", default=None, help="fleet lease identity")
    parser.add_argument("--attempt-id", default=None, help="fleet attempt identity")
    parser.add_argument("--runtime-root", type=Path, default=None, help="fleet runtime parent")
    parser.add_argument("--bluestacks-instance", default=None,
                        help="named BlueStacks instance for this fleet worker")
    parser.add_argument("--bluestacks-pool", type=Path, default=None,
                        help="read-only, manually provisioned BlueStacks pool JSON")
    parser.add_argument("--reroll-pool", type=Path, default=None,
                        help="live host-bound manually selected Reroll pool")
    parser.add_argument("--fleet-capacity", type=int, default=None,
                        help="explicit maximum BlueStacks instance count")
    parser.add_argument("--fleet-name-prefix", default=None,
                        help="exact host-generated fresh instance prefix, ending in underscore")
    parser.add_argument("--fleet-root", type=Path, default=None,
                        help="private fleet request and qualification state directory")
    parser.add_argument(
        "--db", default=str(config.DB_PATH), help="SQLite file for the event log"
    )
    parser.add_argument(
        "--no-store", dest="store", action="store_false", default=True,
        help="do not persist events to SQLite",
    )
    parser.add_argument(
        "--strategy", default=None,
        help="which saved strategy to load (default: the active one)",
    )
    parser.add_argument(
        "--idle", action="store_true",
        help=(
            "with --web, serve the dashboard without starting the bot - "
            "press Start in the browser"
        ),
    )
    args = parser.parse_args(argv)
    supplied = sys.argv[1:] if argv is None else argv
    args.web_port_explicit = any(
        item == "--web-port" or item.startswith("--web-port=") for item in supplied
    )
    return args


def resolve_worker_runtime(args: argparse.Namespace) -> WorkerRuntime | None:
    fields = (args.worker_id, args.lease_id, args.attempt_id, args.runtime_root)
    if not any(value is not None for value in fields):
        return None
    if not all(value is not None for value in fields):
        raise ValueError("worker identity requires worker, lease, attempt, and runtime root")
    if not args.web_port_explicit:
        raise ValueError("worker identity requires an explicit web port")
    if not all(str(value).strip() for value in fields):
        raise ValueError("worker identity fields cannot be empty")
    return WorkerRuntime.for_worker(args.runtime_root, args.worker_id, args.web_port)


def build_affordability(
    strategy: str, atlas_root: Path | None = None
) -> AffordabilityCheck:
    """Pick the affordability check, degrading when digits are unavailable.

    Digits need a labelled atlas that only exists once someone has run
    build_atlas.py. Without one, fall back rather than fail: brightness is the
    floor, and a bot that refuses to start is worse than one that guesses at
    brightness like it did before.

    Every size class has to be present, not just one. The price is what gates
    a purchase, so a bot that could read the wallet but never the price would
    be gating on nothing at all.
    """
    if strategy == "brightness":
        return BrightnessAffordability()

    cache = digits.AtlasCache(
        atlas_root if atlas_root is not None else config.ATLAS_DIR
    )
    missing = [name for name in digits.SIZE_CLASSES if cache.get(name) is None]
    if missing:
        logger.warning(
            "no glyph atlas for %s - falling back to brightness affordability. "
            "Run: uv run build_atlas.py --size-class <name>",
            ", ".join(missing),
        )
        return BrightnessAffordability()

    return DigitAffordability(digits.NumberReader(cache), BrightnessAffordability())


def build_checks(
    atlas_root: Path | None = None,
) -> dict[str, AffordabilityCheck | None]:
    """Build every affordability strategy once. Safe to call on every start.

    `digits` degrades to brightness when no atlas exists, and a None entry
    here is how the rest of the app notices - see `_reconcile_affordability`
    for the one place that has to act on it.

    `atlas_root` exists so this can be exercised without a device: it is
    threaded straight through to `build_affordability`.

    Deliberately does not touch `Controls`. This returns a
    bot-affecting-only dict; `Controls` is built once, at process startup,
    and lives for as long as the server does - conflating the two here would
    make it possible to hand out a fresh `Controls` while the HTTP routes
    kept patching the old one, with the running bot never seeing the
    dashboard's edits.

    `BotRunner` does NOT call this: it is handed the dict in its constructor
    and reuses it for every bot it ever starts, because checks are
    per-process, not per-bot (rebuilding the glyph atlas on every Start would
    make the button slow for no gain). "Safe to call on every start" above is
    about this function being free of hidden state, not an invitation to.
    """
    brightness = BrightnessAffordability()
    digits_check = build_affordability("digits", atlas_root=atlas_root)
    return {
        "brightness": brightness,
        "digits": digits_check if isinstance(digits_check, DigitAffordability) else None,
    }


def _reconcile_affordability(
    loaded: Strategy, checks: dict[str, AffordabilityCheck | None]
) -> Strategy:
    """Downgrade `loaded.affordability` if the atlas this machine has cannot
    serve it.

    Must run exactly once, at the point the long-lived `Controls` is built -
    not on every start(), or a restart could silently re-seed a method the
    operator had already been downgraded away from. The CLI flags that
    override strategy fields are applied by the caller (see plan 3's
    --strategy handling), not here: this function's job is only to reconcile
    the requested policy with the checks that actually built.
    """
    affordability = loaded.affordability
    if checks.get(affordability) is None:
        affordability = "brightness"
    return dataclasses.replace(loaded, affordability=affordability)


def build_checks_and_controls(
    loaded: Strategy, atlas_root: Path | None = None
) -> tuple[dict[str, AffordabilityCheck | None], Controls]:
    """Thin wrapper over build_checks() + _reconcile_affordability(), kept
    for main()'s one-time startup call. Seeds Controls from what actually got
    built rather than from `loaded.affordability` alone - the identity check
    inside _reconcile_affordability is how the dashboard can refuse a switch
    to digits with a reason rather than quietly handing back brightness.

    Not for BotRunner: it must never build a Controls of its own (see
    build_checks()'s docstring) or a checks dict of its own (checks are
    per-process, not per-bot - the CPU cost of rebuilding the digit atlas on
    every Start would be silly, and the whole point of the split above is
    that restarting a bot must not re-run this).
    """
    checks = build_checks(atlas_root=atlas_root)
    controls = Controls(strategy=_reconcile_affordability(loaded, checks))
    return checks, controls




def build_shopping(
    bus: events.EventBus | None,
    templates: vision.TemplateCache | None,
    reader: digits.NumberReader | None = None,
    atlas_root: Path | None = None,
    db_path: Path | None = None,
) -> ShoppingSession:
    """Build the shopping session, disabling it with a reason if this
    machine cannot read the screen at all.

    Always returns a *session* - never None. A None return would force
    every call site to branch before it could do anything, and TowerBot
    would need a null object anyway; this mirrors how build_affordability
    already degrades, handing back a working object of a lesser kind
    rather than nothing at all.

    The gate is the OCR engine (spec §10). Every visit re-reads the coin
    balance before it considers a row, and that read is OCR's now; a
    balance that comes back None aborts the visit outright, because
    shopping.py treats an unread number as "stop", never "guess". So an
    engine that will not import can never approve a purchase, and saying so
    once at startup beats a session that looks armed and silently declines
    forever. Prices still come off the glyph atlas until Phase 2 moves them
    too, which only widens what a missing engine costs.

    This used to gate on the header glyph atlas instead, which meant
    shopping stayed off until somebody played a session that pushed every
    missing digit through the coin balance by hand. Reading the header with
    OCR retired that requirement along with the harvesting - see
    shopping.header_numbers().
    """
    cache = digits.AtlasCache(
        atlas_root if atlas_root is not None else config.ATLAS_DIR
    )
    reader = reader if reader is not None else digits.NumberReader(cache)

    disabled_reason: str | None = None
    if not ocr.available():
        disabled_reason = "the OCR engine will not load"
        logger.warning(
            "shopping disabled: %s - see the traceback logged by tower_bot.ocr, "
            "and check that rapidocr_onnxruntime installed cleanly",
            disabled_reason,
        )

    return ShoppingSession(
        templates, bus, reader, disabled_reason=disabled_reason,
        # The journal owns a path rather than a connection, so a session
        # built here is still usable from the scan-loop thread.
        journal=transactions.TransactionJournal(db_path if db_path is not None else config.DB_PATH),
    )


def print_debug_scores(screen: Image, templates: vision.TemplateCache) -> None:
    """One-shot threshold-tuning diagnostic.

    Prints the best match score (and, for actions, the brightness ratio the
    affordability gate relies on) for every configured template against a
    single captured frame. Anchors are included deliberately: they are what
    you need to diagnose why a frame is reading as UNKNOWN.

    Self-contained by design - no debug flag threads through TowerBot itself.
    """
    print(f"{'action':<28}{'best_score':>12}{'brightness':>12}")
    for action in config.ACTIONS:
        template = templates.get(action.template)
        score, top_left = vision.best_score(screen, template)
        match = vision.Match(center=(0, 0), score=score, top_left=top_left)
        brightness = vision.brightness_ratio(screen, match, template)
        print(f"{action.template:<28}{score:>12.3f}{brightness:>12.3f}")

    print()
    print(f"{'screen anchor':<28}{'best_score':>12}")
    for name, template_path in config.SCREEN_ANCHORS.items():
        score, _ = vision.best_score(screen, templates.get(template_path))
        print(f"{name:<28}{score:>12.3f}")


def configure_logging(tui: bool) -> None:
    """Set up stdlib logging, or get it out of the TUI's way.

    rich's Live owns the terminal under --tui; stdlib logging writing to
    stderr draws straight over the panel. No FAILURE is lost by silencing
    it: those reach the panel as BotError events on the bus. Log-only
    output - a DEBUG line with no event of its own - is lost under --tui,
    so anything that has to be grepped out of the log rather than read off
    the bus must be run WITHOUT --tui.

    The logger NAME is in the format on purpose: a format that omits it
    turns "the logger this was looking for never fired" and "the evidence
    was never written down" into the same empty grep.
    """
    if tui:
        logging.basicConfig(level=logging.CRITICAL, handlers=[logging.NullHandler()])
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)-20s %(message)s",
        datefmt="%H:%M:%S",
    )


def warn_if_web_host_exposed(host: str) -> None:
    """Nudge for the "reach it from my laptop" reflex.

    config.WEB_HOST's docstring carries the real warning, but nobody reads a
    default's docstring on the way to overriding it with --web-host. The
    dashboard serves live screenshots and full event history with no auth,
    and - now that the control plane is wired in - lets a caller start and
    stop the bot, rewrite what it buys, and create or delete strategy files
    on disk, so binding anything but loopback deserves pushback at the point
    someone is actually about to do it.
    """
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not a literal IP (a hostname, a typo) - can't prove it is loopback,
        # so treat it the same as "not loopback" rather than staying silent.
        loopback = False
    if not loopback:
        logger.warning(
            "--web-host %s is not loopback - the dashboard's live "
            "screenshots, event history, and control over the bot (start, "
            "stop, rewrite what it buys, create or delete strategy files on "
            "disk) will be reachable by anyone on this network, and there is "
            "no authentication.",
            host,
        )


def install_signal_handlers(bot: TowerBot) -> None:
    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %s - shutting down after this scan.", signum)
        bot.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def prepare_store(
    path: Path, retention_days: int = config.EVENT_RETENTION_DAYS
) -> tuple[int, int, int | None]:
    """Create the database, prune it, and report what to seed the counters to.

    Returns `(max_seq, max_run_id, best_wave)`. All three are in-process
    values that would otherwise restart from scratch on every launch: seq
    would collide with stored rows on the events primary key and break SSE
    resume across a restart, run ids would overwrite the previous session's
    runs one at a time, and a freshly seeded `best_wave` of None would leave
    a milestones claim already owed unoffered until the next run ends - see
    TowerBot._best_wave's docstring for why that startup gap matters.

    Read BEFORE pruning, deliberately: a seq that was already handed out must
    never be reissued, even for an event old enough to have just aged out of
    retention. Pruning is disk hygiene, not a reason to rewind the counter.
    `best_wave` is unaffected by either the prune (it only touches `events`)
    or by closing abandoned runs (that only backdates `ended_at`, never
    `wave`), but it is read here alongside the other two seeds anyway, for
    the same "what should the in-process counters be seeded to" reason they
    are.

    Backfills the ledger BEFORE pruning too, and for a related reason: the
    ledger is the permanent account history and `events` is not, so an event
    already past the retention window has to reach the ledger on its way out.
    Backfill refuses to run once the ledger holds anything, so this is a
    one-time migration despite being called on every launch.

    Also closes out any run a killed process left with `ended_at IS NULL`:
    without a RunEnded, the row - and the dashboard's "live" badge on it -
    would otherwise persist forever. This startup migration runs before the telemetry sink and account repository
    begin collecting observations.
    """
    conn = db.connect(path)
    try:
        seed_seq = db.max_seq(conn)
        last_run = db.max_run_id(conn)
        seed_best_wave = db.best_wave(conn)
        abandoned = db.close_abandoned_runs(conn)
        if abandoned:
            logger.info("Closed %d run(s) left live by a killed process", abandoned)
        written = ledger.backfill(conn)
        if written:
            logger.info("Backfilled %d ledger line(s) from stored events", written)
        repaired = ledger.repair_unproven_buys(conn)
        if repaired:
            logger.info("Proved the price of %d bought ledger line(s)", repaired)
        removed = db.prune_events(conn, retention_days)
        if removed:
            logger.info("Pruned %d events older than %d days", removed, retention_days)
        return seed_seq, last_run, seed_best_wave
    finally:
        conn.close()


def apply_cli_overrides(
    store: StrategyStore, loaded: Strategy, args: argparse.Namespace
) -> Strategy:
    """Fold explicitly-passed flags into the loaded strategy, and save.

    Persisting is deliberate. The alternative - override without saving -
    reintroduces exactly the file-versus-live drift the strategy design pays
    to avoid, and does it where it is hardest to notice: the dashboard would
    show a value the file does not hold, with nothing on screen saying why.
    Persisting is occasionally surprising; drift is quietly wrong, and the
    log line below is what makes the surprise discoverable.

    Every one of these defaults to None in parse_args precisely so "not
    passed" and "passed the default value" are different here.
    """
    overrides = {
        "interval": args.interval,
        "auto_navigate": args.auto_navigate,
        "max_runs": args.max_runs,
        "affordability": args.affordability,
    }
    supplied = {key: value for key, value in overrides.items() if value is not None}
    if not supplied:
        return loaded

    updated = dataclasses.replace(loaded, **supplied)
    store.save(updated)
    logger.info(
        "Applied and saved CLI override(s) into strategy %r: %s",
        updated.name,
        ", ".join(f"{key}={value}" for key, value in sorted(supplied.items())),
    )
    return updated


def serve_web(
    runner: BotRunner,
    app: FastAPI,
    *,
    host: str,
    port: int,
    shutdown: threading.Event,
    start_immediately: bool = True,
) -> None:
    """Run the server on this thread, and a bot beside it on the runner's.

    Restructured from owning a worker thread to owning a BotRunner. The
    reasoning that shaped the old version all survives - it just moved:

    uvicorn installs its own SIGINT/SIGTERM handlers and can only do that
    from the main thread, so it still gets the main thread. The scan loop
    still runs on a worker, and they still genuinely run in parallel despite
    the GIL, because cv2.matchTemplate releases it for the duration of a
    match.

    `shutdown` is what `stop` used to be, narrowed: it means the PROCESS is
    going down, never merely the bot. It is still watched rather than only
    set, because the shutdown route has no handle on the Server; still set by
    _Server.handle_exit BEFORE graceful shutdown begins, so the SSE and MJPEG
    generators can end themselves inside its normal short path rather than
    waiting out the backstop; and still set in this function's finally, so a
    stream started after everything else ended is caught.

    What changed: a bot ending no longer ends the server. `--max-runs` now
    parks the dashboard with a stopped bot rather than exiting, which is the
    whole point - there is a Start button to press.

    No join here anymore, bounded or otherwise: `BotRunner.stop()` already
    does its own bounded join with a fixed timeout, so a second one here
    would just be redundant - and, worse, sized off a bot's `Controls` that
    under `--idle` before the first Start may not reflect anything the
    runner is actually running.
    """
    import uvicorn

    class _Server(uvicorn.Server):
        def handle_exit(self, sig: int, frame: FrameType | None) -> None:
            shutdown.set()
            super().handle_exit(sig, frame)

    config_ = uvicorn.Config(
        app, host=host, port=port, log_level="warning",
        # uvicorn's default here is None, which means "wait forever" for
        # in-flight responses - and an SSE feed or an MJPEG stream is
        # in-flight for as long as the tab is open. This is a BACKSTOP, not
        # the fix: the fix is `shutdown` being set before graceful shutdown
        # begins (see handle_exit above), so both generators end themselves
        # in the normal path. The 2s is what keeps a stream that somehow
        # missed the flag from hanging Ctrl+C indefinitely.
        timeout_graceful_shutdown=2,
    )
    server = _Server(config_)

    def _watch_shutdown() -> None:
        # The only waiter on `shutdown`. The route can set the flag but has
        # no other way to reach the runner or the Server.
        shutdown.wait()
        runner.stop(operator=False)
        server.should_exit = True

    threading.Thread(target=_watch_shutdown, name="shutdown-watch", daemon=True).start()

    if start_immediately:
        try:
            runner.start(operator=False)
        except RunnerError as exc:
            # Without --idle the user asked for a bot, so a device that is
            # not there is worth saying loudly - but not worth refusing to
            # serve over: the dashboard can show the error and offer Start.
            logger.error("%s - the dashboard is up; press Start to retry", exc)

    try:
        server.run()
    finally:
        shutdown.set()
        runner.stop(operator=False)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.tui)
    # Before anything reads a credential; fleet workers inherit the result.
    load_local_env()
    try:
        runtime = resolve_worker_runtime(args)
        pools = (args.bluestacks_pool is not None) + (args.reroll_pool is not None)
        if (args.bluestacks_instance is None and pools) or (args.bluestacks_instance is not None and pools != 1):
            raise ValueError("BlueStacks instance requires exactly one manual pool")
        if args.bluestacks_instance is not None and (
            runtime is None or not args.web or args.once or args.debug_scores
        ):
            raise ValueError("named BlueStacks requires a supervised fleet dashboard worker")
        if args.bluestacks_instance is not None and not args.game_package:
            raise ValueError("named BlueStacks requires --game-package for verified relaunch")
        fleet_settings = (args.fleet_capacity, args.fleet_name_prefix, args.fleet_root)
        if any(value is not None for value in fleet_settings):
            if (not all(value is not None for value in fleet_settings) or not args.web
                    or not args.idle or args.web_host not in {"127.0.0.1", "localhost", "::1"}
                    or args.bluestacks_instance is not None):
                raise ValueError("fleet provisioning requires explicit capacity, prefix, root, and a loopback idle dashboard")
            from fleet.dashboard import FleetPolicy
            FleetPolicy(capacity=args.fleet_capacity, name_prefix=args.fleet_name_prefix)
    except ValueError as exc:
        logger.error("identity incident: %s", exc)
        return 1
    try:
        endpoint = f"{args.host}:{args.port}"
        reservation = runtime.reserve(endpoint) if runtime is not None else reserve_endpoint(endpoint)
        with reservation:
            if runtime is not None:
                runtime.ensure_directories()
            return _main(args, runtime)
    except RuntimeIsolationError as exc:
        logger.error("%s", exc)
        return 1


def _main(args: argparse.Namespace, runtime: WorkerRuntime | None) -> int:
    from bluestacks import BlueStacksAdapter, ManualPool

    runtime_records = RuntimeRecords(runtime.root / "runtime-records.json") if runtime is not None else None
    if runtime_records is not None:
        try:
            runtime_records.begin(
                worker_id=args.worker_id, endpoint=f"{args.host}:{args.port}",
                lease_id=args.lease_id, attempt_id=args.attempt_id,
                identity=PROCESS_IDENTITY, boot_id=PROCESS_BOOT_ID, pid=os.getpid(),
            )
        except (OSError, ValueError) as exc:
            logger.error("runtime startup record unavailable: %s", exc)
            return 1

    def fail_worker_start(reason: str) -> int:
        logger.error("identity incident: %s", reason)
        if runtime_records is not None:
            try:
                runtime_records.fail_startup(
                    boot_id=PROCESS_BOOT_ID, pid=os.getpid(), reason=reason,
                )
            except (OSError, ValueError):
                logger.exception("Could not persist worker startup failure")
        return 1

    if args.reroll_pool is not None and not args.store:
        return fail_worker_start("reroll progression requires a stored, account-bound worker database")
    attempt = None
    if runtime is not None:
        if args.reroll_pool is not None:
            from web.account_catalog import registered_worker

            registration = registered_worker(runtime.root)
            if registration is None:
                return fail_worker_start("reroll worker registration unavailable")
            try:
                registered = json.loads((runtime.root / "fleet-registration.json").read_text())
                binding = json.loads(Path(registered["binding"]).read_text())
                attempt = Attempt(**{key: binding[key] for key in (
                    "worker_id", "endpoint", "lease_id", "attempt_id", "generation", "created_at")})
            except (OSError, ValueError, TypeError, KeyError):
                return fail_worker_start("reroll attempt binding unavailable")
            if (attempt.worker_id != args.worker_id
                    or attempt.endpoint != f"{args.host}:{args.port}"
                    or attempt.lease_id != args.lease_id
                    or attempt.attempt_id != args.attempt_id
                    or registered.get("web_port") != runtime.web_port):
                return fail_worker_start("reroll attempt changed")
        else:
            attempt = Attempt.new(args.worker_id, f"{args.host}:{args.port}",
                                  args.lease_id, args.attempt_id)
        # Attach the validated attempt to the child's already-durable source
        # record, still before OCR/check construction or ADB connection.
        try:
            runtime_records.start(
                attempt, PROCESS_IDENTITY, boot_id=PROCESS_BOOT_ID, pid=os.getpid(),
            )
        except (OSError, ValueError) as exc:
            logger.error("runtime startup record unavailable: %s", exc)
            return 1
    host_adapter = None
    if args.bluestacks_pool is not None and runtime is not None:
        host_adapter = BlueStacksAdapter(ManualPool(args.bluestacks_pool),
                                         staging_root=runtime.checkpoint_root)
    elif args.reroll_pool is not None and runtime is not None:
        from fleet.bluestacks_air import BlueStacksAirInventory, _EXECUTABLE, live_process_rows
        from fleet.manual_air_worker import ManualAirWorker

        if args.bluestacks_instance == "Tiramisu64_6":
            logger.error("protected Tower template cannot be a reroll worker")
            return 1
        endpoint = f"{args.host}:{args.port}"
        try:
            members = json.loads(args.reroll_pool.read_text(encoding="utf-8"))
            selected = [item for item in members
                        if isinstance(item, dict) and item.get("name") == args.bluestacks_instance]
        except (OSError, ValueError, TypeError):
            logger.error("identity incident: reroll pool is unreadable")
            return 1
        if (len(selected) != 1 or selected[0].get("endpoint") != endpoint
                or selected[0].get("lease_id") != args.lease_id):
            logger.error("identity incident: reroll pool member changed")
            return 1
        inventory = BlueStacksAirInventory(
            Path("/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf"),
            _EXECUTABLE, live_process_rows)
        host_adapter = BlueStacksAdapter(
            ManualAirWorker(args.bluestacks_instance, endpoint, args.lease_id,
                            inventory=inventory), staging_root=runtime.checkpoint_root)
    fleet_controller = None
    if (args.web and args.bluestacks_instance is None
            and args.web_host in {"127.0.0.1", "localhost", "::1"}
            and args.fleet_root is None):
        from fleet.setup import FleetSetupService

        fleet_controller = FleetSetupService(
            Path.home() / ".local/share/thetowerbot/fleet",
            qualification_root=Path.home() / ".local/share/thetowerbot",
        )
    elif args.fleet_root is not None:
        from fleet.bluestacks_air import (
            BlueStacksAirDriver, BlueStacksAirInventory, MacOSMultiInstanceManager,
            _EXECUTABLE, live_process_rows,
        )
        from fleet.dashboard import FleetController, FleetPolicy

        policy = FleetPolicy(capacity=args.fleet_capacity, name_prefix=args.fleet_name_prefix)
        fleet_driver = BlueStacksAirDriver(
            scope=None,
            inventory_source=BlueStacksAirInventory(
                Path("/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf"),
                _EXECUTABLE, live_process_rows),
            manager=MacOSMultiInstanceManager(), fresh_prefix=policy.name_prefix,
            timeout=30.,
        )
        fleet_controller = FleetController(
            BlueStacksAdapter(fleet_driver, staging_root=args.fleet_root / "locks"),
            qualification_path=args.fleet_root / "m05.json",
            state_path=args.fleet_root / "jobs.json", policy=policy,
        )
    host_popup_checker = None
    if (args.dismiss_bluestacks_upgrade and host_adapter is not None
            and sys.platform == "darwin"):
        from fleet.bluestacks_upgrade import close_upgrade_for_instance

        host_popup_checker = close_upgrade_for_instance

    def connect_standalone() -> Any:
        # A host update notice sits above Android and is invisible to ADB.
        # Resolve only one exact local BlueStacks instance for the endpoint;
        # diagnostics remain read-only and named fleet workers use their lease.
        if (args.dismiss_bluestacks_upgrade and host_adapter is None
                and not args.debug_scores and sys.platform == "darwin"):
            from fleet.bluestacks_upgrade import close_upgrade_for_endpoint

            try:
                outcome = close_upgrade_for_endpoint(args.host, args.port)
                if outcome == "unconfirmed":
                    logger.warning("BlueStacks upgrade dialog close could not be verified")
            except Exception:
                logger.warning("BlueStacks upgrade dialog check unavailable", exc_info=True)
        return connect_device(host=args.host, port=args.port)

    # A dashboard (--web without --once, since --once always wins) connects
    # lazily instead: the device becomes the runner's device_factory below,
    # so a dead emulator surfaces as a 503 from /api/bot/start rather than
    # `main()` refusing to serve at all. Every other path - --once,
    # plain logging, --tui - has no dashboard to report a failure into, so
    # it still connects eagerly and fails fast the way it always has.
    # --debug-scores is one of those: it captures a frame and exits before
    # anything ever serves, with or without --web, so it always needs a
    # device up front - deferring it here would only trade a clean
    # "no emulator" error for capture_screen(None) blowing up below.
    serving = args.web and not args.once and not args.debug_scores
    device = None
    if not serving:
        try:
            device = connect_standalone()
        except EmulatorError as exc:
            logger.error("%s", exc)
            return 1

    if args.debug_scores:
        frame = capture_screen(device)
        print_debug_scores(frame, vision.TemplateCache(config.TEMPLATE_DIR))
        return 0

    db_path = runtime.db_path if runtime is not None else Path(args.db)
    if runtime is not None and args.store:
        from web.account_catalog import registered_worker
        registration = registered_worker(runtime.root)
        if registration is None:
            logger.error("identity incident: worker registration is missing or invalid")
            return 1
        try:
            db.bind_account(db_path, registration.account_id or "")
        except ValueError as exc:
            logger.error("identity incident: %s", exc)
            return 1
    seed_seq, last_run, seed_best_wave = (
        prepare_store(db_path) if args.store else (0, 0, None)
    )

    account_state = AccountState(AccountRepository(db_path) if args.store else None)
    reroll_progress = None
    if args.reroll_pool is not None and runtime is not None:
        from fleet.reroll_progress import RerollProgress
        from fleet.build_route_runtime import BuildRouteRuntime
        reroll_progress = RerollProgress(runtime.root, registered["account_id"], account_state)
        reroll_progress.route_runtime = BuildRouteRuntime(
            runtime.root.parent.parent, runtime.root.name, registered["account_id"])

    bus = events.EventBus(start_seq=seed_seq)
    state = BotState()
    # Resolved before the sink list is built because it decides one of the
    # entries: the digests read BotState, and BotState is only fed when
    # something below appends a StateSink. --once is excluded outright - a
    # single scan has no "still running" to report on, and a digest for it
    # would arrive after the process had already exited.
    if args.reroll_pool is not None and runtime is not None:
        telegram_root = runtime.root.parent.parent
    elif callable(getattr(fleet_controller, "reroll_snapshot", None)):
        telegram_root = fleet_controller.root
    else:
        telegram_root = db_path.parent
    telegram_store = TelegramSettingsStore(
        telegram_root / "telegram-settings.json",
        initial_interval_seconds=legacy_telegram_interval_from_env(),
    )
    # A fleet worker inherits the coordinator's environment, but only the
    # coordinator should send an aggregate digest to that chat.
    telegram_settings = (
        None if args.once or args.no_telegram or args.reroll_pool is not None
        else TelegramConfig.from_env(interval=args.telegram_interval)
    )
    sinks: list[events.Sink] = [TuiSink(state=state) if args.tui else LogSink()]
    if runtime is not None and args.reroll_pool is not None and not args.tui:
        from fleet.reroll_journal import RerollJournal
        from sinks.reroll_journal import RerollJournalSink

        sinks = [RerollJournalSink(RerollJournal(runtime.root.parent.parent),
                                  runtime.worker_id)]
    if args.store:
        sinks.append(StoreSink(db_path))
    if (args.web or telegram_settings is not None) and not args.tui:
        # Under --tui the panel's sink already feeds the shared state.
        sinks.append(StateSink(state))
    sse = SseSink() if args.web else None
    frames = FrameBuffer() if args.web else None
    # The PROCESS going down, not the bot - see runner.BotRunner for that
    # half. serve_web() and event_stream() watch this separately from
    # uvicorn's own should_exit so a held-open dashboard tab is told too.
    shutdown = threading.Event()

    for sink in sinks:
        bus.subscribe(sink)
    if sse is not None:
        bus.subscribe(sse)  # no thread to start: it appends and returns

    if args.web and not args.once:
        # print(), not logger.info(), and before sink.start() below: under
        # --tui, TuiSink.start() hands the terminal to rich's Live, and
        # configure_logging(tui=True) sets the root logger to CRITICAL with
        # a NullHandler either way - both would silently swallow this line
        # if it ran any later (task 8, minor 6). `not args.once` alongside
        # that: --once wins over --web, so printing unconditionally would
        # advertise a dashboard that never starts (M1).
        where = f"http://{args.web_host}:{args.web_port}"
        if args.idle:
            print(f"Dashboard on {where} - no bot running, press Start")
        else:
            print(f"Dashboard on {where}")

    # Bound before the try so the finally can close it whether or not the
    # block below got far enough to start it.
    reporter: TelegramReporter | None = None

    # Everything from start() onwards is inside the try: anything raising
    # between starting the consumer threads and the loop would otherwise
    # leave them running and, under --tui, leave rich's Live holding the
    # terminal.
    try:
        for sink in sinks:
            sink.start()

        if device is not None:
            # Nothing to verify yet under a lazy device: the runner connects
            # (or doesn't) once start() actually runs, well after this point.
            try:
                frame = capture_screen(device)
            except Exception as exc:  # noqa: BLE001 - a bad guard frame must not abort startup
                logger.warning("Could not capture a frame to verify resolution: %s", exc)
            else:
                height, width = frame.shape[:2]
                if (width, height) != config.EXPECTED_RESOLUTION:
                    logger.warning(
                        "Emulator is %dx%d but templates were captured at %dx%d. "
                        "Template matching is not scale-invariant - re-capture them.",
                        width, height, *config.EXPECTED_RESOLUTION,
                    )

        store = StrategyStore(directory=runtime.strategy_root if runtime is not None else None)
        try:
            loaded = store.load(args.strategy) if args.strategy else store.ensure_seeded()
        except ControlError as exc:
            # Every profile on disk failed to parse - almost always one
            # hand-edited file with a trailing comma. Same treatment as a
            # missing emulator: say which directory to look in and exit,
            # rather than dumping a traceback the owner has to decode.
            logger.error(
                "%s - fix or delete the offending file in %s", exc, store.directory
            )
            return 1
        loaded = apply_cli_overrides(store, loaded, args)

        checks, controls = build_checks_and_controls(loaded)
        # checks[controls.snapshot().strategy.affordability] is never None
        # here: build_checks_and_controls() already seeded controls' strategy
        # to an affordability name whose check built (falling back to
        # "brightness" itself when it did not), so a "checks[...] or
        # checks['brightness']" fallback would be dead code.
        loaded_affordability = controls.snapshot().strategy.affordability
        # Built once, here, for the same reason `checks` is: the header
        # glyph atlas it gates on is exactly as expensive to build as the
        # digit atlas `checks` already amortises across every bot this
        # process ever starts.
        shopping_session = build_shopping(
            bus, vision.TemplateCache(config.TEMPLATE_DIR), db_path=db_path,
        )

        if telegram_settings is not None:
            # Started here rather than beside the sinks because it reports
            # `paused`, and Controls does not exist until the strategy has
            # loaded. Its first digest goes out immediately, which is also
            # the "bot just came up" signal.
            reporter = TelegramReporter(
                state, telegram_settings, controls=controls,
                preferences=telegram_store,
                fleet=(fleet_controller if callable(getattr(fleet_controller, "reroll_snapshot", None)) else None),
                interval_override_seconds=(telegram_settings.interval if args.telegram_interval is not None else None),
            )
            reporter.start()
            logger.info(
                "Telegram digests configured for chat %s; schedule follows saved settings%s",
                telegram_settings.chat_id,
                " (CLI interval override active)" if args.telegram_interval is not None else "",
            )

        if args.once:
            # A single scan never settles the debounced tracker (it needs
            # SCREEN_CONFIRMATIONS consecutive identical readings), so a
            # lone run_once() would always report UNKNOWN even when the
            # game is clearly on GAME_OVER at 0.998. Scan enough times to
            # settle so --once actually names the real screen.
            bot = TowerBot(
                device=device,
                templates=vision.TemplateCache(config.TEMPLATE_DIR),
                bus=bus,
                affordability_check=checks[loaded_affordability],
                controls=controls,
                checks=checks,
                shopping=shopping_session,
                first_run_id=last_run + 1,
                best_wave=seed_best_wave,
                account_state=account_state,
                reroll_progress=reroll_progress,
                frames=frames,
                unknown_dir=runtime.evidence_root if runtime is not None else None,
            )
            install_signal_handlers(bot)
            for _ in range(config.SCREEN_CONFIRMATIONS):
                bot.run_once()
        elif args.web:
            warn_if_web_host_exposed(args.web_host)

            from web.app import create_app

            runner = BotRunner(
                bus=bus,
                controls=controls,
                state=state,
                templates=vision.TemplateCache(config.TEMPLATE_DIR),
                device_factory=connect_standalone,
                checks=checks,
                shopping=shopping_session,
                frames=frames,
                first_run_id=last_run + 1,
                best_wave=seed_best_wave,
                account_state=account_state,
                reroll_progress=reroll_progress,
                runtime_records_path=(runtime.root / "runtime-records.json")
                if runtime is not None else None,
                unknown_dir=runtime.evidence_root if runtime is not None else None,
                attempt=attempt,
                binding_path=(runtime.checkpoint_root / f"{attempt.generation}.json")
                if runtime is not None and attempt is not None else None,
                supervisor_path=(runtime.checkpoint_root / "supervisor.json")
                if runtime is not None else None,
                game_package=args.game_package,
                host_adapter=host_adapter,
                host_instance=args.bluestacks_instance,
                host_popup_checker=host_popup_checker,
            )

            app = create_app(
                state=state, sse=sse, bus=bus,
                db_path=db_path if args.store else None,
                shutdown=shutdown,
                controls=controls,
                checks=checks,
                frames=frames,
                runner=runner,
                unknown_dir=runtime.evidence_root if runtime is not None else config.UNKNOWN_DIR,
                store=store,
                shopping=shopping_session,
                fleet=fleet_controller,
                telegram_store=telegram_store,
                telegram_reporter=reporter,
                telegram_interval_override=(telegram_settings.interval if args.telegram_interval is not None
                                            and telegram_settings is not None else None),
                telegram_suppressed=args.no_telegram or args.reroll_pool is not None,
            )
            # No signal handlers of ours here: uvicorn installs its own and
            # would overwrite them anyway.
            serve_web(
                runner, app, host=args.web_host, port=args.web_port,
                shutdown=shutdown, start_immediately=not args.idle,
            )
        else:
            bot = TowerBot(
                device=device,
                templates=vision.TemplateCache(config.TEMPLATE_DIR),
                bus=bus,
                affordability_check=checks[loaded_affordability],
                controls=controls,
                checks=checks,
                shopping=shopping_session,
                first_run_id=last_run + 1,
                best_wave=seed_best_wave,
                account_state=account_state,
                reroll_progress=reroll_progress,
                frames=frames,
                unknown_dir=runtime.evidence_root if runtime is not None else None,
            )
            install_signal_handlers(bot)
            # No explicit interval, so run_forever re-reads
            # bot.controls.snapshot().strategy.interval every iteration -
            # the loaded strategy is the one source of truth for the pace
            # even without --web. --max-runs is passed explicitly too: it
            # wins over the strategy's own max_runs when set (see
            # run_cap_reached's docstring), and apply_cli_overrides has
            # already folded and saved it into the strategy either way, so
            # the two never actually disagree.
            bot.run_forever(max_runs=args.max_runs)
    finally:
        # Before the sinks: the reporter reads BotState, and a digest
        # rendered while the consumer threads are being torn down would
        # describe a half-stopped bot on its way out.
        if reporter is not None:
            reporter.close()
        for sink in sinks:
            sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
