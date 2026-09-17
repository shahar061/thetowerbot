"""One explicit, read-only Collect stats transaction: Home -> Settings -> Stats -> Settings -> Home.

Nothing here spends, purchases or mutates game state. The only four taps it
can ever issue are the main menu's settings control, the Settings panel's
Stats row, then each panel's own close button - each located on the very
frame it is tapped from, never from a coordinate remembered across scans.

Every step verifies the screen it expects on THIS frame before it acts and
performs at most one device action. Missing evidence is never rounded up
into a tap: absent, ambiguous, unreadable and unusable stay distinct, and a
step that cannot prove where it is waits a bounded number of frames and then
stops the transaction with that reason recorded. A stopped transaction taps
nothing further, which leaves the game wherever it actually is rather than
guessing its way back.

A step acts only on a frame the account reader actually examined at the one
supported geometry, and only after that reader positively reported what is on
it - "the reader reached no conclusion" is never read as "the screen is clear".

The scan loop drives this (see tower_bot.run_once) and the runner owns the
single instance, so a transaction outlives no bot: a restart cancels it, and
so does pausing the bot mid-walk. Every tap it makes is published as a Tapped
event and drawn on the device overlay, like every other tap in the codebase.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum, auto
import threading
import time
from typing import TYPE_CHECKING, Any

import cv2

import config
import jitter
from account_screens import ControlTarget, ScreenReadings
from device import Image, tap

if TYPE_CHECKING:
    from strategy import Strategy

# Every template, region and offset in this repo was measured here. A frame
# of any other size is a frame no coordinate on it can be trusted against.
EXPECTED_FRAME = (config.EXPECTED_RESOLUTION[1], config.EXPECTED_RESOLUTION[0])

# Cut from the recorded 1080x2400 captures: the main menu's settings control
# (menu_main.png, matching main_menu.png at .994 from a different vertical
# position, which is why this is a template match and not a coordinate) and
# the Stats panel's close button (stats_summary.png, matching stats_tiers.png
# at 1.0). Neither scores above .62 on any other recorded page.
SETTINGS_TEMPLATE = 'nav/settings.png'
CLOSE_TEMPLATE = 'nav/close_panel.png'

# Well above vision.py's usual .8. A transaction that taps its way through
# unrelated menus is the failure worth refusing hardest, and both templates
# score >= .99 on every recorded frame that genuinely contains them.
MATCH_THRESHOLD = .9

# Frames one step may wait for the screen it expects before failing closed.
# The panels animate in, so the frame after a tap is routinely still the old
# one; six scans is long enough for that and short enough to stop quickly.
STEP_FRAME_BUDGET = 6

STATS_SCREENS = ('account.stats.summary', 'account.stats.tiers')
SETTINGS_SCREEN = 'account.settings'
HOME_STATE = 'MAIN_MENU'


class Step(Enum):
    IDLE = auto()
    OPEN_SETTINGS = auto()
    OPEN_STATS = auto()
    COLLECT = auto()
    CLOSE_SETTINGS = auto()
    CONFIRM_HOME = auto()


@dataclass(frozen=True)
class CollectionAction:
    """One verified device action, carrying the evidence that located it.

    Returned rather than a bare bool so the scan loop can publish and draw
    the tap it just made with the same numbers that justified it - a tap
    nobody can find afterwards is the failure mode this exists to avoid.
    """

    step: str
    name: str
    x: int
    y: int
    score: float
    rect: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class CollectionResult:
    """How the last transaction ended. `screen_id` is what was actually read."""

    status: str
    reason: str
    detail: str
    screen_id: str | None
    finished_at: float


def locate_control(screen: Image, template: Image | None, name: str,
                   threshold: float = MATCH_THRESHOLD) -> ControlTarget:
    """The one place `template` is on `screen`, or why there is no target.

    A second peak at or above the threshold, outside the winner's own
    footprint, makes the target ambiguous rather than "the better of two":
    picking one would be picking a tap by luck.
    """
    if screen.shape[:2] != EXPECTED_FRAME:
        # Ambiguous geometry stops everything: on a resized emulator the
        # winning peak proves only that some region matched, never that the
        # region is this control.
        return ControlTarget(name, None, 'unusable')
    if template is None or getattr(template, 'size', 0) == 0:
        return ControlTarget(name, None, 'unusable')
    height, width = template.shape[:2]
    if height > screen.shape[0] or width > screen.shape[1]:
        return ControlTarget(name, None, 'unusable')
    result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    _, best, _, location = cv2.minMaxLoc(result)
    if best < threshold:
        return ControlTarget(name, None, 'absent')
    masked = result.copy()
    masked[max(location[1] - height, 0):location[1] + height,
           max(location[0] - width, 0):location[0] + width] = -1.
    _, second, _, _ = cv2.minMaxLoc(masked)
    if second >= threshold:
        return ControlTarget(name, None, 'ambiguous')
    return ControlTarget(name, (location[0] + width // 2, location[1] + height // 2), 'located',
                         float(best), (location[0], location[1], width, height))


def at_home(state: str, evidence: dict[str, Any]) -> bool:
    """Home means the anchor AND a positive reading of "no panel".

    `scanned` is required, not merely a null screen id: an overlay keeps
    the main menu anchor underneath it, and a frame the reader never
    examined - the wrong geometry, or a failed engine - reports the same
    empty screen id as a genuinely clear menu. Only the examined-and-clear
    case may precede the one tap a transaction makes before it has a
    positive panel identity to check against.

    Module-level so that every transaction walking out of the main menu
    tests home the same way; missions_visit and missions_claim are the
    second and third callers.
    """
    return (state == HOME_STATE and bool(evidence['scanned'])
            and evidence['screen_id'] is None and evidence['error'] is None)


def at_page_home(state: str, account: dict[str, Any], page: dict[str, Any]) -> bool:
    """Home by the anchor, the panel reader AND a page-specific reader.

    The tracker is debounced, so for a scan or two after a walk's return
    control is tapped it still says MAIN_MENU while the frame is very much
    still the page just left. Confirming home off the anchor alone would let
    a walk report "returned to the main menu" from a page it never left. The
    page's own reader looked at the same frame, so it is asked too, and only
    its examined-and-clear answer counts - `scanned` False is a reader that
    reached no conclusion, never a clear menu.

    Generalised out of `at_missions_home`, which now delegates here, so any
    walk leaving the main menu through a reader-carrying page - missions or
    milestones alike - tests home the same way, for the identical reason
    `at_home` above is shared rather than copied: two predicates that must
    agree will otherwise drift. Measured concretely for the milestones case:
    `missions_screen.scan()` answers `scanned=True, screen_id=None` on all
    three recorded milestones captures, so testing a milestones walk's home
    against `at_missions_home` (the missions reader) would confirm "returned
    to the main menu" from a milestones page the walk never actually left -
    exactly why milestones_claim passes its OWN reader's evidence as `page`
    here instead.
    """
    return (at_home(state, account) and bool(page['scanned'])
            and page['screen_id'] is None and page['error'] is None)


def at_missions_home(state: str, account: dict[str, Any],
                     missions: dict[str, Any]) -> bool:
    """Home by the anchor, the panel reader AND the missions reader.

    Thin wrapper: `at_page_home` states the shared reasoning once; this name
    stays so `missions_visit` and `missions_claim` need no change.
    """
    return at_page_home(state, account, missions)


class ControlTaps:
    """The one device-touching path every read-only transaction shares.

    A transaction mixing this in owns `_threshold`, `_tuning`, `_step` and
    the three outcomes `_wait`, `_finish` and `_enter`. How a control is
    located, and which readings are refused rather than tapped, lives here
    once. Shared rather than copied deliberately: this is the code that
    actually touches the device, so a second transaction cannot drift from
    the refusals the first one is tested for.

    `following` is the caller's own Step enum - the two transactions walk
    different steps, and nothing here needs to know which.
    """

    _threshold: float
    _tuning: Strategy | None

    def _tap_target(self, target: ControlTarget | None, device: Any, name: str,
                    following: Enum, moment: float) -> CollectionAction | None:
        """Tap a located control, or stop on any of the four failure states.

        Jittered through the same helpers every other tap path uses, and for
        the same reasons: a pixel-exact tap at zero reaction delay is a
        signature. `click_cooldown` is deliberately not applied - as in
        navigate.Navigator, each step already gates on fresh evidence that
        cannot repeat, so there is no burst for a cooldown to suppress.
        """
        if target is None or target.status == 'absent':
            return self._wait(f'{name}_absent', f'The {name.replace("_", " ")} was not visible on '
                              'this frame, so nothing was tapped.', moment)
        if target.point is None:
            return self._finish('failed', f'{name}_{target.status}', f'The {name.replace("_", " ")} '
                                f'reading was {target.status}; refusing to tap a guessed target.',
                                moment)
        x, y = target.point
        if self._tuning is not None:
            x, y = jitter.point(x, y, self._tuning.tap_jitter_px)
            jitter.pause(self._tuning.tap_delay, self._tuning.timing_jitter)
        step = self._step.name.lower()
        tap(device, x, y)
        self._enter(following)
        return CollectionAction(step, name, int(x), int(y), target.score, target.rect)

    def _tap(self, screen: Image, device: Any, templates: Any, template: str, name: str,
             following: Enum, moment: float) -> CollectionAction | None:
        try:
            image = templates.get(template)
        except (OSError, ValueError, AttributeError):
            image = None
        return self._tap_target(locate_control(screen, image, name, self._threshold),
                                device, name, following, moment)


class StatsCollection(ControlTaps):
    """At most one read-only Collect stats transaction, driven one frame at a time."""

    def __init__(self, *, threshold: float = MATCH_THRESHOLD,
                 frame_budget: int = STEP_FRAME_BUDGET) -> None:
        self._lock = threading.RLock()
        self._threshold = threshold
        self._budget = frame_budget
        self._step = Step.IDLE
        self._waited = 0
        self._requested_at: float | None = None
        self._collected: str | None = None
        self._result: CollectionResult | None = None
        self._trail: list[str] = []
        # The live tap policy for the pass in flight, set by each advance()
        # from the caller's own snapshot - the same way ShoppingSession takes
        # it. None means no jitter, which is what a direct caller with no
        # strategy to offer should get.
        self._tuning: Strategy | None = None

    # -- reporting ---------------------------------------------------------
    @property
    def active(self) -> bool:
        with self._lock:
            return self._step is not Step.IDLE

    def snapshot(self) -> dict[str, Any]:
        """Detached. 'idle' is the absence of a transaction, not a failed one."""
        with self._lock:
            status = 'running' if self._step is not Step.IDLE else (
                self._result.status if self._result is not None else 'idle')
            return {'status': status, 'step': self._step.name.lower(),
                    'requested_at': self._requested_at, 'trail': list(self._trail),
                    'result': asdict(self._result) if self._result is not None else None}

    # -- lifecycle ---------------------------------------------------------
    def request(self, now: float | None = None) -> bool:
        """Arm a transaction. False when one is already running; never queues."""
        with self._lock:
            if self._step is not Step.IDLE:
                return False
            self._requested_at = time.time() if now is None else now
            self._result = None
            self._collected = None
            self._waited = 0
            self._trail = []
            self._enter(Step.OPEN_SETTINGS)
            return True

    def cancel(self, reason: str, detail: str, now: float | None = None) -> None:
        """End a transaction the loop can no longer honour. Idempotent."""
        with self._lock:
            if self._step is Step.IDLE:
                return
            self._finish('failed', reason, detail, time.time() if now is None else now)

    # -- one step ----------------------------------------------------------
    def advance(self, *, screen: Image, device: Any, templates: Any,
                readings: ScreenReadings, state: str, now: float | None = None,
                tuning: Strategy | None = None) -> CollectionAction | None:
        """One frame of the transaction. Returns the tap it issued, if any.

        At most one device action per call, and only after this frame's own
        evidence has identified the screen that action belongs to.
        """
        with self._lock:
            self._tuning = tuning
            if self._step is Step.IDLE:
                return None
            moment = time.time() if now is None else now
            evidence = readings.current_evidence()
            screen_id, error = evidence['screen_id'], evidence['error']

            if self._step is Step.OPEN_SETTINGS:
                if not self._home(state, evidence):
                    return self._wait('home_not_confirmed', 'The main menu was not confirmed on '
                                      'this frame, so no control was tapped.', moment)
                return self._tap(screen, device, templates, SETTINGS_TEMPLATE, 'settings_control',
                                 Step.OPEN_STATS, moment)

            if self._step is Step.OPEN_STATS:
                if error is not None:
                    return self._finish('failed', 'settings_unreadable', 'The account screen '
                                        'reader reported an error; the panel was left as it is.',
                                        moment)
                if screen_id != SETTINGS_SCREEN:
                    return self._wait('settings_not_reached', 'The Settings panel was not observed '
                                      'after the settings control was tapped.', moment)
                return self._tap_target(evidence['controls'].get('stats'), device, 'stats_control',
                                        Step.COLLECT, moment)

            if self._step is Step.COLLECT:
                if error is not None:
                    return self._finish('failed', 'stats_unreadable', 'The account screen reader '
                                        'reported an error; nothing was recorded.', moment)
                if screen_id not in STATS_SCREENS:
                    return self._wait('stats_not_reached', 'The Stats panel was not observed after '
                                      'the Stats control was tapped.', moment)
                self._collected = screen_id
                return self._tap(screen, device, templates, CLOSE_TEMPLATE, 'close_control',
                                 Step.CLOSE_SETTINGS, moment)

            if self._step is Step.CLOSE_SETTINGS:
                if error is not None:
                    return self._finish('failed', 'settings_unreadable', 'The account screen '
                                        'reader reported an error while returning from Stats.', moment)
                if screen_id != SETTINGS_SCREEN:
                    return self._wait('settings_not_restored', 'The Settings panel was not observed '
                                      'after closing Stats.', moment)
                return self._tap(screen, device, templates, CLOSE_TEMPLATE, 'close_control',
                                 Step.CONFIRM_HOME, moment)

            if not self._home(state, evidence):
                return self._wait('home_not_restored', 'The Stats panel was read, but the main '
                                  'menu was not confirmed again.', moment)
            return self._finish('completed', 'collected', 'The Stats panel was read and the game '
                                'returned to the main menu.', moment)

    # -- internals ---------------------------------------------------------
    _home = staticmethod(at_home)

    def _enter(self, step: Step) -> None:
        self._step = step
        self._waited = 0
        self._trail.append(step.name.lower())

    def _wait(self, reason: str, detail: str, moment: float) -> CollectionAction | None:
        self._waited += 1
        if self._waited > self._budget:
            return self._finish('failed', reason, detail, moment)
        return None

    def _finish(self, status: str, reason: str, detail: str,
                moment: float) -> CollectionAction | None:
        self._result = CollectionResult(status, reason, detail, self._collected, moment)
        self._step = Step.IDLE
        self._waited = 0
        return None
