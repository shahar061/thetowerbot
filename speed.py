"""The in-battle game speed widget: read it, and decide which arrow to tap.

Two halves, deliberately not sharing a dependency.

`read()` needs pixels. `step()` is pure arithmetic over config.SPEED_VALUES
and needs none - which is what lets the whole settle policy be tested, and
be correct, before a readout template exists for any value but x1.0.

Nothing here taps. The caller owns the device, the cooldown and the jitter,
exactly as it does for every other tap the bot makes; this module only ever
answers "what speed is showing" and "which way from here".
"""

from __future__ import annotations

import logging
from typing import Any

import config
import events
import jitter
import vision
from device import Image, tap, tap_burst
from strategy import Strategy

logger = logging.getLogger("tower_bot.speed")


def _crop(screen: Image, anchor: tuple[int, int], region: config.Region) -> Image:
    x = anchor[0] + region.dx
    y = anchor[1] + region.dy
    return screen[y : y + region.h, x : x + region.w]


def read(
    screen: Image,
    templates: vision.TemplateCache,
    anchor: tuple[int, int],
    threshold: float = config.SPEED_MATCH_THRESHOLD,
) -> float | None:
    """The speed showing between the arrows, or None if nothing matched.

    Scored against every known value and the best one wins, rather than
    returning the first over the threshold: the labels differ by one glyph,
    so "first past the post" makes the answer depend on tuple order.
    """
    window = _crop(screen, anchor, config.SPEED_READOUT_REGION)
    best: tuple[float, float] | None = None
    for value in config.SPEED_VALUES:
        template = templates.get(config.speed_template(value))
        if template.shape[0] > window.shape[0] or template.shape[1] > window.shape[1]:
            logger.warning("speed template for x%.1f is larger than the readout window", value)
            continue
        score, _ = vision.best_score(window, template)
        if score >= threshold and (best is None or score > best[1]):
            best = (value, score)
    return None if best is None else best[0]


def _arrow_point(
    direction: str, anchor: tuple[int, int], tuning: Strategy | None,
) -> tuple[int, int]:
    """The centre of one arrow, jittered and paced like every other tap."""
    region = config.SPEED_PLUS_REGION if direction == "up" else config.SPEED_MINUS_REGION
    x = anchor[0] + region.dx + region.w // 2
    y = anchor[1] + region.dy + region.h // 2
    if tuning is not None:
        x, y = jitter.point(x, y, tuning.tap_jitter_px)
        jitter.pause(tuning.tap_delay, tuning.timing_jitter)
    return x, y


class SpeedController:
    """Walks the game speed toward a target, one arrow tap per scan.

    Stateful for exactly one reason: patience. A target the account has not
    unlocked yet is not an error the bot can detect up front - the widget
    simply stops moving - and without a budget the loop taps `+` at that
    ceiling for the rest of the run. Everything else here is a comparison.

    One tap per call is the same discipline ShoppingSession.advance() runs
    under, and for the same reason: the readout does not update until the
    next frame, so a controller that looped until `current == target` would
    be deciding every step from one stale image.
    """

    def __init__(self, patience: int = 4, bus: Any | None = None) -> None:
        self._patience = patience
        self._bus = bus
        self._target: float | None = None
        self._last: float | None = None
        self._spent = 0
        self._completion_generations: set[str] = set()

    @property
    def exhausted(self) -> bool:
        return self._spent >= self._patience

    def rearm_completion(self, generation: str) -> bool:
        """A durable research generation earns at most one fresh patience budget."""
        if not generation or generation in self._completion_generations:
            return False
        self._completion_generations.add(generation)
        self._spent = 0
        return True

    def _restart(self, target: float | None) -> None:
        self._target = target
        self._last = None
        self._spent = 0

    def decide(self, current: float | None, target: float | None) -> str | None:
        """"up", "down", or None for "tap nothing this scan".

        None covers four different situations on purpose - no target set, no
        readable reading, already there, and gave up - because the caller
        does the same thing in all four, and splitting them into a status
        enum would put a match statement in the scan loop to express "do not
        tap" four ways.
        """
        if target != self._target:
            # A retuned target is a fresh instruction, not a retry of the one
            # that ran out of patience.
            self._restart(target)

        if target is None or current is None:
            return None

        if current == target:
            self._last = current
            self._spent = 0
            return None

        if current != self._last:
            # It moved. Whatever we spent getting here bought progress, so it
            # does not count against reaching the next step.
            self._last = current
            self._spent = 0

        if self._spent >= self._patience:
            return None

        self._spent += 1
        return "up" if target > current else "down"

    def tap(
        self,
        device: Any,
        direction: str,
        anchor: tuple[int, int],
        *,
        tuning: Strategy | None = None,
        source: str = "policy",
        reading: float | None = None,
        target: float | None = None,
    ) -> None:
        """Tap one arrow. The only place either path touches the device.

        The arrow is tapped at its region's centre rather than at a template
        match, which is the same call config.buy_point() already makes for
        the price strip: the widget does not move relative to the IN_RUN
        anchor, so a match would cost a full matchTemplate per scan to
        rediscover a fixed offset. The death-modal rule that forces template
        matching elsewhere is about a screen that genuinely shifts.
        """
        x, y = _arrow_point(direction, anchor, tuning)
        tap(device, x, y)

        if self._bus is not None:
            self._bus.publish(
                events.SpeedAdjusted(
                    direction=direction,
                    source=source,
                    reading=reading,
                    target=target,
                )
            )

    def max_out(
        self,
        device: Any,
        anchor: tuple[int, int],
        *,
        tuning: Strategy | None = None,
    ) -> None:
        """Tap + config.SPEED_MAX_TAPS times as ONE supervised input.

        One burst, not a loop of tap(): the device supervisor allows a single
        input per fresh frame and refuses the next, so separate taps would
        land one and raise on the second.
        """
        x, y = _arrow_point("up", anchor, tuning)
        tap_burst(device, x, y, config.SPEED_MAX_TAPS, config.SPEED_MAX_TAP_GAP_SECONDS)
        if self._bus is not None:
            self._bus.publish(events.SpeedAdjusted(direction="up", source="startup"))

    def settle(
        self,
        screen: Image,
        device: Any,
        templates: vision.TemplateCache,
        *,
        target: float | None,
        anchor: tuple[int, int],
        tuning: Strategy | None = None,
    ) -> str | None:
        """Read the widget and tap at most one arrow toward `target`."""
        current = read(screen, templates, anchor)
        direction = self.decide(current, target)
        if direction is None:
            return None

        # `current` and `target` are both non-None here: decide() returns
        # None for either, so reaching this line proves both.
        self.tap(
            device,
            direction,
            anchor,
            tuning=tuning,
            source="policy",
            reading=current,
            target=target,
        )
        return direction
