"""Notice a worker making no progress, try one safe way out, else pause it.

Every screen reader is written after its screen has been seen, so a popup the
game has never shown before always gets through somehow. It stops a worker in
one of two ways. Either the frame is covered but still classifies as a known
page, and every tap lands on the popup (the supervisor then logs "changed
nothing ... treating it as having no effect", again and again), or the frame
classifies UNKNOWN and the recovery preflight blocks every action. Both were
observed lasting hours.

The watchdog notices either, then escalates:

1. ESCAPE - tap one button whose label is on a short safe list (CLAIM, OK,
   CLOSE, ...), and only on a frame that shows no spending words. It tries this
   at most MAX_ESCAPES times before ordinary progress resumes.
2. PAUSE - the caller saves the frame and its OCR text as evidence, publishes
   WorkerStalled and pauses the worker, which then waits for an operator
   instead of tapping blindly.

Progress means an action whose frame changed. A frame that changes with no
action pending proves nothing: a battle animates on its own.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

from ocr import TextBox

ESCAPE = 'escape'
PAUSE = 'pause'
SCREEN_ID = 'STALL_ESCAPE'
MAX_ESCAPES = 3

# Checked in this order, so a reward's CLAIM wins over the SKIP beside it.
_SAFE_LABELS: tuple[tuple[str, frozenset[str]], ...] = (
    ('CLAIM', frozenset({'claim'})),
    ('COLLECT', frozenset({'collect'})),
    ('OK', frozenset({'ok', 'okay'})),
    ('CONTINUE', frozenset({'continue', 'taptocontinue'})),
    ('CLOSE', frozenset({'close', 'x', '×', '✕'})),
    ('NO THANKS', frozenset({'nothanks', 'notnow', 'later', 'maybelater'})),
    ('SKIP', frozenset({'skip'})),
)
# A frame showing any of these may be a purchase, an upgrade or an ad; no
# button on it is safe to press without knowing the screen.
_DANGER = re.compile(r'\b(buy|purchase|upgrade|spend|watch|ads?|unlock\s+for)\b|[$€£]', re.I)
_MIN_CONFIDENCE = .9


def _normalized(text: str) -> str:
    return re.sub(r'[\s!.]', '', text.casefold())


def _trusted(box: TextBox) -> bool:
    return (math.isfinite(box.confidence) and box.confidence >= _MIN_CONFIDENCE
            and box.rect.w > 0 and box.rect.h > 0)


def escape_button(boxes: tuple[TextBox, ...]) -> tuple[str, tuple[int, int]] | None:
    """The one safe button to press on an unrecognized frame, or None."""
    trusted = [box for box in boxes if _trusted(box)]
    if any(_DANGER.search(box.text) for box in trusted):
        return None
    for label, spellings in _SAFE_LABELS:
        found = [box for box in trusted if _normalized(box.text) in spellings]
        if len(found) > 1:
            return None
        if found:
            rect = found[0].rect
            return label, (rect.x + rect.w // 2, rect.y + rect.h // 2)
    return None


class StallWatchdog:
    """Counts evidence of no progress from the supervisor's verdicts."""

    def __init__(self, clock: Callable[[], float], *, no_effect_limit: int,
                 blocked_limit: float) -> None:
        self.clock = clock
        self.no_effect_limit = no_effect_limit
        self.blocked_limit = blocked_limit
        self.reset()

    def reset(self) -> None:
        self._no_effect = 0
        self._blocked_since: float | None = None
        self._escape_in_flight = False
        self.escapes = 0

    def note(self, *, had_pending: bool, reason: str) -> None:
        """Feed one supervisor verdict: whether an action was awaiting its
        effect before this frame, and the reason the supervisor gave."""
        if reason == 'unknown_screen':
            if self._blocked_since is None:
                self._blocked_since = self.clock()
            return
        self._blocked_since = None
        if reason == 'action_no_effect':
            self._no_effect += 1
        elif had_pending and reason == 'fresh_evidence':
            self._no_effect = 0
            if self._escape_in_flight:
                self._escape_in_flight = False
            else:
                self.escapes = 0

    def escaped(self) -> None:
        """The caller pressed an escape button; count it and start over."""
        self.escapes += 1
        self._no_effect = 0
        self._blocked_since = None
        self._escape_in_flight = True

    @property
    def reason(self) -> str | None:
        if self._no_effect >= self.no_effect_limit:
            return f'no_effect_x{self._no_effect}'
        if (self._blocked_since is not None
                and self.clock() - self._blocked_since >= self.blocked_limit):
            return f'unknown_screen_{self.clock() - self._blocked_since:.0f}s'
        return None

    def verdict(self) -> str | None:
        if self.reason is None:
            return None
        return ESCAPE if self.escapes < MAX_ESCAPES else PAUSE
