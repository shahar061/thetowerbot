"""Which screen is the game on?

Anchors are small crops unique to one screen. Every scan matches all of them
full-frame and takes the argmax; below ANCHOR_THRESHOLD the answer is UNKNOWN
and the bot holds.

Every anchor is still searched over the whole frame, but coarse-then-fine
(vision.two_step_score): the full-resolution score is only computed around
the half-size greyscale hit, which cut classification cost about tenfold on
the fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import config
import run_hud
import vision
from device import Image


class ScreenState(str, Enum):
    MAIN_MENU = "MAIN_MENU"
    IN_RUN = "IN_RUN"
    GAME_OVER = "GAME_OVER"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ScreenReading:
    """One frame's classification, with every anchor's score for debugging."""

    state: ScreenState
    confidence: float
    scores: dict[str, float]
    top_left: tuple[int, int] | None = None
    # Where the run HUD's cash counter matched, or None when no run is
    # showing. Carried on the reading so the wallet read uses the counter
    # found on THIS frame - see tower_bot's note on anchor/region drift.
    cash_top_left: tuple[int, int] | None = None


def classify(
    screen: Image,
    cache: vision.TemplateCache,
    threshold: float = config.ANCHOR_THRESHOLD,
) -> ScreenReading:
    scores: dict[str, float] = {}
    positions: dict[str, tuple[int, int]] = {}

    coarse = vision.coarse_image(screen)
    for name, template_path in config.SCREEN_ANCHORS.items():
        score, top_left = vision.two_step_score(
            screen, cache.get(template_path),
            coarse_screen=coarse, coarse_template=cache.coarse(template_path))
        scores[name] = score
        positions[name] = top_left

    # IN_RUN is scored by the cash counter, not by the upgrade panel's header
    # bar that SCREEN_ANCHORS points at. The bar is a different crop per tab
    # - the committed template is the ATTACK one, and DEFENSE and UTILITY
    # score 0.46 and 0.33 against it - so scoring the screen on it left two
    # of the three tabs reading UNKNOWN for the whole run.
    #
    # The panel match is still taken, and still supplies top_left, because
    # the speed controls are measured from it. But it is only handed out
    # when it is strong enough to mean anything: on the other two tabs it
    # is not, and every consumer of an IN_RUN top_left already handles None.
    panel = scores[ScreenState.IN_RUN.value]
    cash = run_hud.find_cash(screen, cache, threshold)
    scores[ScreenState.IN_RUN.value] = cash.score
    if panel < threshold:
        positions[ScreenState.IN_RUN.value] = None

    winner = max(scores, key=lambda name: scores[name])
    confidence = scores[winner]

    # GAME_OVER outranks a cash-scored IN_RUN whenever it stands on its own.
    # The death modal does not cover the HUD, so the counter scores ~1.000 on
    # exactly the frames where the modal is the answer, and the two would
    # otherwise be separated by the margin between two near-perfect matches.
    if winner == ScreenState.IN_RUN.value and scores[ScreenState.GAME_OVER.value] >= threshold:
        winner = ScreenState.GAME_OVER.value
        confidence = scores[winner]

    if confidence < threshold:
        return ScreenReading(
            ScreenState.UNKNOWN, confidence, scores, cash_top_left=cash.top_left
        )

    return ScreenReading(
        ScreenState(winner), confidence, scores,
        top_left=positions[winner], cash_top_left=cash.top_left,
    )


class ScreenTracker:
    """Debounces raw readings into confirmed screen transitions.

    Capture lands inside the death modal's fade animation, so a single frame
    is not trustworthy. A transition is declared only after `confirmations`
    consecutive identical readings; an interrupted run resets the count.

    `state` starts at UNKNOWN as a placeholder, so it alone cannot tell "not
    looked yet" from "looked and did not recognise it". `confirmed` makes
    that distinction: callers that react to UNKNOWN (snapshotting the frame,
    publishing UnknownScreen) must require both.
    """

    def __init__(self, confirmations: int = config.SCREEN_CONFIRMATIONS) -> None:
        self._confirmations = confirmations
        self.state = ScreenState.UNKNOWN
        self._confirmed = False
        self._pending: ScreenState | None = None
        self._streak = 0

    @property
    def confirmed(self) -> bool:
        """True once any state has been confirmed by consecutive readings."""
        return self._confirmed

    def observe(self, reading: ScreenReading) -> ScreenState | None:
        """Feed one reading. Returns the new state on a confirmed transition."""
        # `self._confirmed` guards the short-circuit deliberately: before the
        # first confirmation `state` is a placeholder, so UNKNOWN readings
        # must be allowed to confirm UNKNOWN. Otherwise a bot launched on an
        # unmodelled screen never confirms anything and never snapshots the
        # very frame the trail exists to capture.
        if self._confirmed and reading.state is self.state:
            self._pending = None
            self._streak = 0
            return None

        if reading.state is self._pending:
            self._streak += 1
        else:
            self._pending = reading.state
            self._streak = 1

        if self._streak < self._confirmations:
            return None

        self.state = reading.state
        self._confirmed = True
        self._pending = None
        self._streak = 0
        return self.state
