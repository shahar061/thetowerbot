"""One frame of the MILESTONES screens: the ladder, and the reward ceremony.

Two screens, one reader, because a walk moves between them one tap at a time
and both must hold every other action while they are up.

This reader answers exactly one question for the transaction - is `Claim All`
present - plus the tier for the record. It deliberately does NOT classify the
reward slots. Their three states (padlock / glow / green check) are told apart
by icon art this codebase has recorded no bounds for, and it is not needed:
what was actually claimed is read from the modal's own text, which is
OCR-readable, exact, and names its own currency.

`Claim All` is matched by EXACT equality against 'claimall'. That is the mirror
of the missions reader's exact 'claim' match, which exists specifically so that
THIS button is rejected there.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import threading
import time
from typing import Any

import ocr
import screen_discovery
import tiles
from geometry import anchored_y, supported_frame
from device import Image

_MIN_CONFIDENCE = .90
_EXPECTED_FRAME = (2400, 1080)

LADDER_SCREEN = 'milestones.ladder'
MODAL_SCREEN = 'milestones.reward_modal'

_CLAIM_ALL_LABEL = 'claimall'

# Against RAW text, not a normalised label: the captures carry both `25 COINS`
# and `15GEMS`, and normalise() would strip the space from one and not create
# it in the other. \b so that '25 COINSXYZ' is not read as 25 coins.
_REWARD = re.compile(r'(\d+)\s*(coins|gems)\b', re.I)
_PROGRESS = re.compile(r'(\d+)\s*/\s*(\d+)')

# Where the modal draws its reward line. Absolute, because the modal carries no
# title to measure from, and captured on ONE layout only - see the spec Limits.
_MODAL_REWARD_Y = (1316, 1396)


@dataclass(frozen=True)
class MilestonesReading:
    """One frame of a MILESTONES screen.

    `claim_all` IS a tap target - the only one this module produces - and it is
    only ever valid for the frame it was read from.
    """

    screen_id: str
    observed_at: float
    tier: int | None
    claim_all: tuple[int, int, int, int] | None
    reward_text: str | None
    currency: str | None
    amount: int | None
    modal_action: str | None = None
    modal_control: tuple[int, int, int, int] | None = None
    modal_index: int | None = None
    modal_total: int | None = None


def _trusted(box: ocr.TextBox) -> bool:
    return math.isfinite(box.confidence) and _MIN_CONFIDENCE <= box.confidence <= 1.


def reward_of(text: str) -> tuple[str | None, int | None]:
    """The currency and amount a reward line names, or (None, None).

    (None, None) is 'this reward moved no currency' - `Unlock Lab` is a real
    reward on the recorded ladder - which the ledger encodes as delta=0. It is
    deliberately the same answer as an unrecognised line: see the spec's Limits
    for why that conflation is accepted rather than hidden.
    """
    found = _REWARD.search(text)
    if found is None:
        return (None, None)
    return (found[2].lower(), int(found[1]))


def _claim_all(boxes: tuple[ocr.TextBox, ...]) -> tuple[int, int, int, int] | None:
    """The one `Claim All` button, or nothing.

    Exactly one or none: two are a misread, and tapping the first of two would
    be picking a tap by luck. Exact equality on the normalised label, never a
    substring - the reason is in this module's docstring.
    """
    hits = [b for b in boxes if _trusted(b)
            and tiles.normalise(b.text) == _CLAIM_ALL_LABEL]
    if len(hits) != 1:
        return None
    rect = hits[0].rect
    return (rect.x, rect.y, rect.w, rect.h)


def _modal_reward(boxes: tuple[ocr.TextBox, ...], frame_height: int) -> str | None:
    shift = anchored_y(0, frame_height, 'center')
    hits = [b for b in boxes if _trusted(b)
            and _MODAL_REWARD_Y[0] + shift <= b.rect.y <= _MODAL_REWARD_Y[1] + shift]
    return hits[0].text if len(hits) == 1 else None


def _modal_progress(boxes: tuple[ocr.TextBox, ...], frame_height: int) -> tuple[int | None, int | None]:
    hits = [m for b in boxes if _trusted(b)
            and frame_height - 180 <= b.rect.y <= frame_height - 80
            if (m := _PROGRESS.fullmatch(b.text.strip())) is not None]
    if len(hits) != 1:
        return None, None
    index, total = map(int, hits[0].groups())
    return (index, total) if 1 <= index <= total <= 20 else (None, None)


def parse_frame(screen: Image, boxes: tuple[ocr.TextBox, ...], *,
                now: float | None = None) -> MilestonesReading | None:
    """Read a recorded MILESTONES screen, or nothing at all.

    The page must identify itself through screen_discovery first, so a frame
    this module cannot name yields no reading rather than a partial one.

    `boxes`, when supplied, MUST be a read of THIS `screen`. Nothing here
    verifies that pairing - only pixel-level checks touch `screen` itself -
    so a foreign box set is read with full confidence as this frame.
    """
    moment = time.time() if now is None else now
    if not math.isfinite(moment):
        return None
    found = screen_discovery.discover(screen, boxes, 'milestones')
    if found.screen_id is None or not found.readable:
        return None
    if found.screen_id == MODAL_SCREEN:
        text = _modal_reward(boxes, screen.shape[0])
        currency, amount = reward_of(text) if text is not None else (None, None)
        action = screen_discovery.milestone_modal_action(boxes, frame_height=screen.shape[0])
        if action is None:
            return None
        index, total = _modal_progress(boxes, screen.shape[0])
        if (action[0] == 'next' and (index is None or total is None or index >= total)
                or action[0] == 'claim' and index is not None and index != total):
            return None
        rect = action[1].rect
        return MilestonesReading(MODAL_SCREEN, moment, None, None,
                                 text, currency, amount, action[0],
                                 (rect.x, rect.y, rect.w, rect.h), index, total)
    return MilestonesReading(LADDER_SCREEN, moment,
                             screen_discovery.milestones_tier(boxes, frame_height=screen.shape[0]),
                             _claim_all(boxes), None, None, None)


class MilestonesReadings:
    """What the last frame showed, for the guard and for a claim transaction."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reading: MilestonesReading | None = None
        self._error: str | None = None
        self._scanned = False

    def current_evidence(self) -> dict[str, Any]:
        """What every transaction tests home against.

        Separate from claim_evidence for the reason missions_screen states:
        widening this payload would make an unrelated caller's home assertion
        depend on claiming.
        """
        with self._lock:
            return {'screen_id': None if self._reading is None else self._reading.screen_id,
                    'error': self._error, 'scanned': self._scanned}

    def claim_evidence(self) -> dict[str, Any]:
        """What a claim transaction needs from the frame just scanned.

        `claim_all` is a target on THIS frame; an unscanned or unreadable frame
        carries none rather than the last frame's.
        """
        with self._lock:
            reading = self._reading
            return {'screen_id': None if reading is None else reading.screen_id,
                    'error': self._error, 'scanned': self._scanned,
                    'tier': None if reading is None else reading.tier,
                    'claim_all': None if reading is None else reading.claim_all,
                    'reward_text': None if reading is None else reading.reward_text,
                    'currency': None if reading is None else reading.currency,
                    'amount': None if reading is None else reading.amount,
                    'modal_action': None if reading is None else reading.modal_action,
                    'modal_control': None if reading is None else reading.modal_control,
                    'modal_index': None if reading is None else reading.modal_index,
                    'modal_total': None if reading is None else reading.modal_total}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            reading = self._reading
            return {'screen_id': None if reading is None else reading.screen_id,
                    'error': self._error, 'scanned': self._scanned,
                    'tier': None if reading is None else reading.tier,
                    'claimable': reading is not None and reading.claim_all is not None}

    def observe(self, reading: MilestonesReading | None, *, error: str | None = None,
                scanned: bool = False) -> None:
        with self._lock:
            self._reading = reading
            self._error = error
            self._scanned = scanned or reading is not None

    def scan(self, screen: Image, *, boxes: tuple[ocr.TextBox, ...] | None = None) -> bool:
        """Update from this frame; return whether all actions must hold.

        Actions hold whenever a milestones screen is up OR MIGHT BE - the
        distinction below is why this probes presence before parsing, the way
        missions_screen.scan probes screen_discovery._missions_title before
        parse_frame. `parse_frame` returning None conflates two different
        facts: "no milestones page is up" and "one is up but unreadable", and
        collapsing them to a release would be wrong for the second. That
        matters most for the reward modal: it is a full-screen overlay
        carrying a tappable CLAIM, and config.NAV_DISMISS - which shopping.py
        walks to clear first-visit popups - contains nav/claim_reward.png and
        nav/skip.png, BOTH of which score ~0.99999 on it. A HOLD-vs-RELEASE
        decision that trusted `parse_frame is None` alone would release on an
        unreadable modal precisely when the guard is needed most, letting
        that walk silently claim a reward with no ledger line, or tap SKIP
        and discard one.

        Presence is the ladder title OR a trusted SKIP in its widened band OR
        a trusted Claim All - never bare CLAIM: the missions page carries
        four CLAIM boxes, so using it here would call a definite missions
        frame a possible milestones one. Claim All does not share that
        hazard: measured across all 29 committed OCR fixtures it appears on
        exactly one, the claimable ladder, so - like SKIP - it is a
        discriminating anchor on its own, and `_claim_all`'s exact-equality
        match (never a substring) is what keeps it that way.

        `boxes` lets the caller hand in a frame read it already paid for. A
        tick runs two full-frame readers; reading the same bytes twice is a
        cost that only grows as readers are added.

        `boxes`, when supplied, MUST be a read of THIS `screen`. Nothing here
        verifies that pairing - only pixel-level checks (the shape guard
        below, screen_discovery's own frame checks) touch `screen` itself -
        so a foreign box set is accepted as a confident reading of a frame
        that is not actually on screen.
        """
        self.observe(None)
        if not supported_frame(screen.shape[1], screen.shape[0]):
            return False
        try:
            if boxes is None:
                boxes = ocr.read(screen, strict=True)
            present = (screen_discovery._milestones_title(boxes) is not None
                      or screen_discovery._modal_skip(boxes, frame_height=screen.shape[0]) is not None
                      or _claim_all(boxes) is not None)
            if not present:
                # Examined the whole frame, and no milestones anchor anywhere:
                # not in the bands screen_discovery searches, and no Claim
                # All either. This is the one branch that may stand for "no
                # milestones page is up".
                self.observe(None, scanned=True)
                return False
            reading = parse_frame(screen, boxes)
            self.observe(reading, scanned=True,
                        error=None if reading is not None else
                        'A milestones screen is up but could not be read reliably')
            return True
        except Exception:
            # Unscanned, not clear: a failed reader has looked at nothing.
            self.observe(None, error='Milestones reading failed; actions held for this scan')
            return True
