"""Background automation bot for the Android game "The Tower".

Everything runs through ADB, so the emulator window never needs focus and the
mouse is never hijacked:

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

from account_collection import StatsCollection
from milestones_claim import MilestonesClaim
from milestones_screen import MilestonesReadings
from missions_claim import MissionsClaim
from missions_screen import MissionsReadings
from missions_visit import MissionsVisit
from account_state import AccountState, AccountRepository
from account_screens import ScreenReadings

import argparse
import dataclasses
import ipaddress
import logging
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

from adbutils import AdbDevice

if TYPE_CHECKING:
    from fastapi import FastAPI

import claim_schedule
import config
import db
import digits
import events
import gem_claim
import jitter
import ledger
import ocr
import pages
import screens
import speed
import transactions
import vision
from affordability import (
    AffordabilityCheck,
    BrightnessAffordability,
    DigitAffordability,
)
from autopilot import AutopilotState, BattleAutopilot
from combat_context import RunIdentity, build_revision
from perception import read_cash
from control import Controls, Live
from device import EmulatorError, Image, capture_screen, connect_device, tap
from frames import FrameBuffer
from navigate import Navigator
from runner import BotRunner, RunnerError
from runs import RunTracker
from shopping import ShoppingSession
from snapshots import SnapshotWriter
from sinks.log import LogSink
from sinks.sse import SseSink
from sinks.state import BotState, StateSink
from sinks.store import StoreSink
from sinks.tui import TuiSink
from strategy import MIN_INTERVAL, ControlError, Strategy, StrategyStore

logger = logging.getLogger("tower_bot")


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------
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
        self.device = device
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
        self.shopping.observations = self.autopilot.state
        # Read once, here, rather than per scan: both configure an object
        # that carries state across scans (the tracker's part-confirmed
        # reading, the navigator's last-navigation timestamp), and changing
        # either under a running one has no correct answer. This is the
        # "applies on next Start" boundary the dashboard labels.
        self.tracker = screens.ScreenTracker(confirmations=screen_confirmations)
        self.snapshots = SnapshotWriter(
            config.UNKNOWN_DIR,
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
        self.runs = RunTracker(first_run_id)
        # When each claim last landed, and the best wave the ladder was
        # claimed at. In-memory for this slice: a restart re-offers a claim,
        # and the walk itself refuses if there is nothing to take. Persisting
        # this belongs with the wallet, in B07.
        self._last_claim: dict[str, float] = {}
        self._claimed_best_wave: int | None = None
        # The account's best-ever wave. Seeded here from whatever the caller
        # read at startup (see prepare_store(), which reads db.best_wave())
        # and kept fresh in-process from every RunEnded from then on (see
        # run_once) rather than queried per scan. None means unread, not
        # zero: claim_schedule.due() deliberately owes nothing on a None
        # best wave rather than guessing.
        self._best_wave: int | None = best_wave
        self._screen: Image | None = None
        self._last_click: dict[str, float] = {}
        self._running = True
        # Makes the between-scan sleep interruptible. A plain time.sleep()
        # ignores stop(): PEP 475 means it resumes after a signal handler
        # returns rather than aborting, and at a browser-set MAX_INTERVAL of
        # 3600s that turns Ctrl+C into an hour-long hang.
        self._stopping = threading.Event()

    @property
    def screen_state(self) -> screens.ScreenState:
        return self.tracker.state

    # -- screen state ------------------------------------------------------
    def refresh_screen(self) -> Image:
        """Capture a fresh frame and keep it as the current screen."""
        self._screen = capture_screen(self.device)
        if self.frames is not None:
            self.frames.publish(self._screen)
        return self._screen

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
        )

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

        return self.speed.settle(
            self.screen,
            self.device,
            self.templates,
            target=settings.strategy.target_speed,
            anchor=anchor,
            tuning=settings.strategy,
        ) is not None

    def _offer_claim(self, settings: Any) -> str | None:
        """Offer the one claim the cadence says is owed, if any.

        Offers rather than arms: at most one maintenance walk may hold the
        menus, so request() returning False - or a walk already being active
        - is the ordinary case and is survived silently. Returns the kind
        armed, so the caller can log it, or None.
        """
        claims = settings.strategy.claims
        if not claims.enabled:
            return None
        if self.claim.active or self.milestones_claim.active:
            return None
        kind = claim_schedule.due(
            claim_schedule.ClaimState(
                last_missions=self._last_claim.get("missions"),
                last_milestones=self._last_claim.get("milestones"),
                best_wave=self._best_wave,
                claimed_best_wave=self._claimed_best_wave,
            ),
            now=time.time(),
            missions_every_hours=claims.missions_every_hours,
            milestones_on_new_best=claims.milestones_on_new_best,
        )
        if kind is None:
            return None
        walk = self.claim if kind == "missions" else self.milestones_claim
        if not walk.request():
            return None
        self._last_claim[kind] = time.time()
        if kind == "milestones":
            self._claimed_best_wave = self._best_wave
        return kind

    def run_once(self, max_runs: int | None = None) -> bool:
        """One scan pass over the configured actions. True if anything clicked.

        `max_runs` only suppresses auto-navigation. The scan that ends run N
        is also the scan that would tap RETRY on the way out, so without this
        the loop breaks at the top of the next iteration having already
        started run N+1 in the emulator.
        """
        started = time.monotonic()
        # Exactly one snapshot for the whole pass. Re-reading mid-scan would
        # let a setting change underneath a half-finished scan - the wallet
        # read with one strategy and the price gate applied with another.
        settings = self.controls.snapshot()
        chosen = self.checks.get(settings.strategy.affordability)
        if chosen is not None:
            self.affordability = chosen
        self.refresh_screen()

        reading = screens.classify(self.screen, self.templates)
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
                self.bus.publish(run_event)
                self.autopilot.suspend("Run boundary", clear_battle=True)

        state = self.tracker.state

        # Account overlays can retain a MAIN_MENU anchor underneath them. This
        # passive reader owns the frame before every possible action path,
        # including paused scans and an already-active shopping visit.
        screen_readings = self.account_state.screen_readings if self.account_state is not None else self._screen_readings
        panel = screen_readings.scan(self.screen)
        # The same passive ownership for the Daily Missions page. The bot has
        # no verified target on it, so a tap aimed at the menu underneath
        # would land somewhere nobody chose - it holds actions exactly as a
        # panel does, whether or not a visit is walking.
        # THREE full-frame readers over one frame. RapidOCR's cost here is
        # near-fixed rather than proportional to pixels, so reading the same
        # bytes once per reader is a second and third full price for nothing.
        # Measured on tests/fixtures/in_run_lit.png, median of 5 after a warm
        # tick: 347.9 ms for the three sharing this read, against 354.4 ms for
        # the two readers that shipped before it and 611.8 ms for the same
        # three reading independently. The third reader is free; an unshared
        # one would have cost ~75% of a tick.
        #
        # On a failed read each reader falls back to its own attempt and
        # reports its own error, which is the behaviour they had before this
        # was shared.
        try:
            shared_boxes = ocr.read(self.screen, strict=True)
        except Exception:
            shared_boxes = None
        missions_page = self.missions.scan(self.screen, boxes=shared_boxes)
        # The same passive ownership for both MILESTONES screens. This matters
        # most for the reward modal: it is a full-screen overlay carrying a
        # tappable CLAIM, and config.NAV_DISMISS - walked by shopping.py's
        # OPEN_CARDS step - holds nav/claim_reward.png and nav/skip.png, both
        # of which match it at 1.0000.
        milestones_page = self.milestones.scan(self.screen, boxes=shared_boxes)
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
                       or self.claim.active or self.milestones_claim.active)
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
                or self.milestones_claim.active):
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
                    readings=screen_readings, missions=self.missions, bus=self.bus,
                    state=state.value, tuning=settings.strategy,
                )
            elif self.milestones_claim.active:
                walking = 'milestones_claim'
                action = self.milestones_claim.advance(
                    screen=self.screen, device=self.device, templates=self.templates,
                    readings=screen_readings, milestones=self.milestones, bus=self.bus,
                    state=state.value, tuning=settings.strategy,
                )
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

        # A visit owns the frame while it runs. Three things below key off
        # this rather than off the screen state, because the pages a visit
        # walks are UNKNOWN to the tracker by design - see pages.py.
        visiting = self.shopping.active

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
                clicked = True

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
                    clicked = self.autopilot.step(self.screen, self.device, settings.strategy.autopilot,
                                                   cash=self.wallet, cooldown=settings.strategy.click_cooldown,
                                                   run_id=self.runs.current_id,
                                                   identity=self.run_identity(settings),
                                                   elapsed=self.runs.elapsed(time.monotonic()))
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
            if not self.shopping.begin(settings.strategy.shopping, self.runs.completed):
                armed = self._offer_claim(settings)
                if armed is not None:
                    logger.info("Armed a %s claim from the main menu.", armed)

        if (
            settings.strategy.auto_navigate
            and not settings.paused
            and not self.run_cap_reached(max_runs, settings.strategy)
            and not visiting and not self.shopping.active
            and not self.claim.active and not self.milestones_claim.active
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
            self.navigator.maybe_navigate(
                self.screen,
                state,
                self.device,
                now=time.monotonic(),
                tuning=settings.strategy,
                go_home=self.shopping.due(
                    settings.strategy.shopping, self.runs.completed
                ),
                # The way off a menu page. NAV_BUTTONS is keyed by
                # ScreenState, which has no member for one, so the bot could
                # neither act on the workshop (the loop above gates on
                # IN_RUN) nor leave it: measured live, twenty unbroken
                # minutes on the UTILITY tab. UNKNOWN with nothing named
                # still taps nothing - see config.MENU_NAV_BUTTONS.
                menu_page=None if menu_page == pages.UNKNOWN else menu_page,
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
                settings.strategy.shopping,
                tuning=settings.strategy,
            )

        self.bus.publish(
            events.ScanCompleted(
                screen=state.value,
                duration_ms=(time.monotonic() - started) * 1000,
                wallet=self.wallet,
            )
        )
        return clicked

    def run_forever(
        self,
        interval: float | None = None,
        max_runs: int | None = None,
    ) -> None:
        """Run scans back to back until stop() is called or max_runs is hit.

        `interval` has two distinct meanings, deliberately:

        - `None` (the default, and what `BotRunner._run()` passes in
          production - see runner.py) means the dashboard owns the pace.
          `self.controls.snapshot().strategy.interval`
          is re-read at the top of every iteration, so a change made from the
          browser takes effect on the very next sleep rather than requiring a
          restart.
        - An explicit number is a caller override. It is used exactly as
          given, every iteration, and deliberately never written into
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
        startup_interval = (
            self.controls.snapshot().strategy.interval if interval is None else interval
        )
        logger.info(
            "Bot started - scanning every %.1fs. Ctrl+C to stop.", startup_interval
        )
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
            # Re-read every iteration (when interval is None) rather than
            # once at the top of the loop - see the docstring above.
            if interval is None:
                live = self.controls.snapshot().strategy
                # spread(), not stretch(): unlike the cooldowns, the interval
                # is a target rather than a floor, and a loop that only ever
                # waited longer than its nominal interval would still be a
                # metronome - just a slower one. Clamped at MIN_INTERVAL so a
                # dial already at the floor cannot jitter below it into the
                # busy loop that floor exists to prevent.
                current_interval = max(
                    MIN_INTERVAL, jitter.spread(live.interval, live.timing_jitter)
                )
            else:
                # An explicit override is used exactly as given - see the
                # docstring. Tests pass 0.0 to run the loop without sleeping,
                # and jittering that would reintroduce the sleep.
                current_interval = interval
            try:
                self.run_once(max_runs=max_runs)
            except EmulatorError as exc:
                logger.error("Device error: %s - retrying in %.1fs", exc, current_interval)
                self._report(f"Device error: {exc}")
            except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the loop
                logger.exception("Unexpected error during scan")
                self._report(f"Unexpected error during scan: {exc}")
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
    return parser.parse_args(argv)


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
        journal=transactions.TransactionJournal(config.DB_PATH),
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
        runner.stop()
        server.should_exit = True

    threading.Thread(target=_watch_shutdown, name="shutdown-watch", daemon=True).start()

    if start_immediately:
        try:
            runner.start()
        except RunnerError as exc:
            # Without --idle the user asked for a bot, so a device that is
            # not there is worth saying loudly - but not worth refusing to
            # serve over: the dashboard can show the error and offer Start.
            logger.error("%s - the dashboard is up; press Start to retry", exc)

    try:
        server.run()
    finally:
        shutdown.set()
        runner.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.tui)

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
            device = connect_device(host=args.host, port=args.port)
        except EmulatorError as exc:
            logger.error("%s", exc)
            return 1

    if args.debug_scores:
        frame = capture_screen(device)
        print_debug_scores(frame, vision.TemplateCache(config.TEMPLATE_DIR))
        return 0

    db_path = Path(args.db)
    seed_seq, last_run, seed_best_wave = (
        prepare_store(db_path) if args.store else (0, 0, None)
    )

    account_state = AccountState(AccountRepository(db_path) if args.store else None)

    bus = events.EventBus(start_seq=seed_seq)
    state = BotState()
    sinks: list[events.Sink] = [TuiSink(state=state) if args.tui else LogSink()]
    if args.store:
        sinks.append(StoreSink(db_path))
    if args.web and not args.tui:
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

        store = StrategyStore()
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
        shopping_session = build_shopping(bus, vision.TemplateCache(config.TEMPLATE_DIR))

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
                frames=frames,
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
                device_factory=lambda: connect_device(host=args.host, port=args.port),
                checks=checks,
                shopping=shopping_session,
                frames=frames,
                first_run_id=last_run + 1,
                best_wave=seed_best_wave,
                account_state=account_state,
            )

            app = create_app(
                state=state, sse=sse, bus=bus,
                db_path=db_path if args.store else None,
                shutdown=shutdown,
                controls=controls,
                checks=checks,
                frames=frames,
                runner=runner,
                store=store,
                shopping=shopping_session,
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
                frames=frames,
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
        for sink in sinks:
            sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
