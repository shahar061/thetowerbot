"""Passive, provisional readings of measured English account panels.

These raw strings are neither permanent account facts nor executable upgrade
identities. Settings and summary are measured at both BlueStacks portrait
heights; tier rows remain qualified only at 1080x2400.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
import re
import threading
import time
from typing import Any

import ocr
from config import Rect
from device import Image
from geometry import anchored_y, supported_frame

_LABELS = (
    'Game Started', 'Coins Earned', 'Recent Coins Per Hour', 'Cash Earned',
    'Stones Earned', 'Keys Earned', 'Cells Earned', 'Cells Earned Per Hour',
    'Reroll Shards Earned', 'Damage Dealt', 'Enemies Destroyed', 'Waves Completed',
    'Upgrades Bought', 'Workshop Upgrades', 'Workshop Coins Spent',
    'Research Completed', 'Lab Coins Spent', 'Free Upgrades', 'Interest Earned',
    'Orb Kills', 'Death Ray Kills', 'Thorn Damage', 'Waves Skipped',
)
_TITLE = Rect(350, 460, 370, 110)
_MIN_CONFIDENCE = .90
_ROW_TOLERANCE = 18

# The one Settings row a read-only transaction may reach for. The measurement
# behind these bounds is that panel's single 'Stats' OCR token at
# (664, 826, 114, 42), plus tolerance - not a claim about the rest of the
# panel. A redacted capture of the panel is now recorded as
# settings_redacted.png, with the account identifier painted out of the image
# itself; the unredacted capture stays out of the repository.
_SETTINGS_STATS = Rect(600, 780, 250, 130)

# control name -> (owning screen id, normalised label, measured bounds)
_CONTROLS: tuple[tuple[str, str, str, Rect], ...] = (
    ('stats', 'account.settings', 'stats', _SETTINGS_STATS),
)


@dataclass(frozen=True)
class ControlTarget:
    """Where a named control was found on ONE frame, or why it was not.

    `status` keeps located, absent, ambiguous, unreadable and unusable
    apart deliberately: a control nobody could see is not the same fact as
    two candidates for it, and none of the four failures may become a tap.
    """

    name: str
    point: tuple[int, int] | None
    status: str
    # The evidence that located it, so a tap can be published and drawn with
    # the same numbers that justified it rather than with a bare coordinate.
    score: float = 0.
    rect: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class ScreenField:
    key: str
    label: str
    raw_value: str | None
    status: str
    confidence: float
    rect: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class TierReading:
    tier: int
    wave: ScreenField
    coins: ScreenField
    cells: ScreenField


@dataclass(frozen=True)
class ScreenReading:
    screen_id: str
    observed_at: float
    frame_width: int
    frame_height: int
    frame_digest: str
    fields: tuple[ScreenField, ...]
    tiers: tuple[TierReading, ...]


def _normal(text: str) -> str:
    return re.sub(r'[^a-z0-9]', '', text.lower())


def _inside(box: ocr.TextBox, rect: Rect) -> bool:
    b = box.rect
    return (b.w > 0 and b.h > 0 and rect.x <= b.x and rect.y <= b.y
            and b.x + b.w <= rect.x + rect.w and b.y + b.h <= rect.y + rect.h)


def _centered(rect: Rect, frame_height: int) -> Rect:
    return Rect(rect.x, anchored_y(rect.y, frame_height, 'center'), rect.w, rect.h)


def _trusted(box: ocr.TextBox) -> bool:
    return math.isfinite(box.confidence) and _MIN_CONFIDENCE <= box.confidence <= 1.


def _field(key: str, label: str, values: tuple[ocr.TextBox, ...],
           label_box: ocr.TextBox | None = None) -> ScreenField:
    if len(values) != 1:
        return ScreenField(key, label, None, 'unreadable', 0., None)
    box = values[0]
    confidences = (box.confidence, label_box.confidence) if label_box else (box.confidence,)
    if any(not math.isfinite(c) or not 0 <= c <= 1 for c in confidences):
        return ScreenField(key, label, None, 'unreadable', 0., None)
    confidence = min(confidences)
    rect = box.rect
    if label_box is not None:
        left, top = min(rect.x, label_box.rect.x), min(rect.y, label_box.rect.y)
        rect = Rect(left, top, max(rect.x + rect.w, label_box.rect.x + label_box.rect.w) - left,
                    max(rect.y + rect.h, label_box.rect.y + label_box.rect.h) - top)
    status = ('unreadable' if confidence < _MIN_CONFIDENCE else
              'insufficient_data' if _normal(box.text) == 'needmoredata' else 'observed')
    return ScreenField(key, label, box.text, status, confidence,
                       (rect.x, rect.y, rect.w, rect.h))


def _same_row(left: ocr.TextBox, right: ocr.TextBox) -> bool:
    return abs(left.rect.y + left.rect.h / 2 - right.rect.y - right.rect.h / 2) <= _ROW_TOLERANCE


def _rows_separated(rows: tuple[ocr.TextBox, ...]) -> bool:
    """Reject anchors whose matching windows or physical bounds overlap.

    Two labels within twice the value-matching tolerance could both claim a
    value between them, even when neither label is a duplicate identity.
    """
    ordered = sorted(rows, key=lambda b: b.rect.y + b.rect.h / 2)
    return all(right.rect.y + right.rect.h / 2 - left.rect.y - left.rect.h / 2 > 2 * _ROW_TOLERANCE
               and left.rect.y + left.rect.h < right.rect.y
               for left, right in zip(ordered, ordered[1:]))


def parse_frame(screen: Image, boxes: tuple[ocr.TextBox, ...], *,
                now: float | None = None, locale: str = 'en') -> ScreenReading | None:
    """Parse only measured panels. No values are inferred, merged or coerced."""
    observed_at = time.time() if now is None else now
    if not supported_frame(screen.shape[1], screen.shape[0]) or locale != 'en' or not math.isfinite(observed_at):
        return None
    frame_height = screen.shape[0]
    titles = tuple(b for b in boxes if _inside(b, _centered(_TITLE, frame_height))
                   and _normal(b.text) in ('stats', 'settings'))
    if len(titles) != 1 or not _trusted(titles[0]):
        return None
    fields: tuple[ScreenField, ...] = ()
    tiers: tuple[TierReading, ...] = ()
    if _normal(titles[0].text) == 'settings':
        # Never serialize other Settings OCR, which includes the account ID.
        versions = tuple(b for b in boxes if _inside(b, _centered(Rect(800, 1880, 170, 90), frame_height))
                         and re.fullmatch(r'v\d+\.\d+\.\d+', b.text))
        fields = (_field('game_version', 'Game version', versions),)
        screen_id = 'account.settings'
    else:
        headings = {name: tuple(b for b in boxes if _normal(b.text) == name
                    and _inside(b, _centered(Rect(x, 575, 155, 75), frame_height)))
                    for name, x in (('wave', 360), ('coins', 565), ('cells', 770))}
        if any(headings.values()):
            if frame_height == 1920:
                return None  # native BlueStacks tier rows need their own captured proof
            if any(len(bs) != 1 or not _trusted(bs[0]) for bs in headings.values()):
                return None
            rows = tuple(b for b in boxes if re.fullmatch(r'Tier\s+\d+', b.text)
                         and _inside(b, _centered(Rect(160, 640, 170, 1320), frame_height)))
            numbers = [int(re.search(r'\d+', b.text)[0]) for b in rows]
            if not rows or len(set(numbers)) != len(numbers) or any(not 1 <= n <= 24 for n in numbers):
                return None
            if not _rows_separated(rows):
                return None
            parsed = []
            for row, number in sorted(zip(rows, numbers), key=lambda pair: pair[1]):
                cells = []
                for key, x in (('wave', 360), ('coins', 565), ('cells', 770)):
                    width = 170 if key == 'coins' else 145
                    candidates = tuple(b for b in boxes if _inside(b, _centered(Rect(x, 640, width, 1320), frame_height))
                                       and _same_row(row, b))
                    field = _field(key, key.title(), candidates, row)
                    if field.raw_value is not None and not re.fullmatch(r'\d+(?:\.\d+)?[KMBT]?', field.raw_value):
                        field = ScreenField(key, key.title(), None, 'unreadable', 0., None)
                    cells.append(field)
                parsed.append(TierReading(number, *cells))
            tiers = tuple(parsed)
            screen_id = 'account.stats.tiers'
        else:
            labels = {label: tuple(b for b in boxes if _inside(b, _centered(Rect(175, 580, 420, 1290), frame_height))
                      and _normal(b.text) == _normal(label)) for label in _LABELS}
            if sum(len(bs) == 1 and _trusted(bs[0]) for bs in labels.values()) < 3:
                return None
            if not _rows_separated(tuple(b for matches in labels.values() for b in matches)):
                return None
            parsed_fields = []
            for label, matches in labels.items():
                row = matches[0] if len(matches) == 1 else None
                candidates = tuple(b for b in boxes if _inside(b, _centered(Rect(610, 580, 315, 1290), frame_height))
                                   and row is not None and _same_row(row, b))
                parsed_fields.append(_field(label.lower().replace(' ', '_'), label, candidates, row))
            fields = tuple(parsed_fields)
            screen_id = 'account.stats.summary'
    return ScreenReading(screen_id, observed_at, screen.shape[1], frame_height,
                         hashlib.sha256(screen.tobytes()).hexdigest(), fields, tiers)


def control_targets(screen_id: str, boxes: tuple[ocr.TextBox, ...], *,
                    frame_height: int = 2400) -> dict[str, ControlTarget]:
    """Tap targets derived from ONE frame's OCR, for that frame only.

    Only the named controls above are ever located, so no other Settings
    OCR - the account identifier included - leaves this module. A duplicate
    label is ambiguous and an untrusted one is unreadable; neither yields a
    point, and neither is reported as absence.
    """
    targets: dict[str, ControlTarget] = {}
    for name, owner, label, bounds in _CONTROLS:
        if owner != screen_id:
            continue
        matches = tuple(b for b in boxes if _inside(b, _centered(bounds, frame_height))
                        and _normal(b.text) == label)
        if len(matches) > 1:
            targets[name] = ControlTarget(name, None, 'ambiguous')
        elif not matches:
            targets[name] = ControlTarget(name, None, 'absent')
        elif not _trusted(matches[0]):
            targets[name] = ControlTarget(name, None, 'unreadable')
        else:
            rect = matches[0].rect
            targets[name] = ControlTarget(
                name, (rect.x + rect.w // 2, rect.y + rect.h // 2), 'located',
                matches[0].confidence, (rect.x, rect.y, rect.w, rect.h))
    return targets


class ScreenReadings:
    """Thread-safe, bounded in-memory history; no persistence or advisor input."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: str | None = None
        self._readings: dict[str, ScreenReading] = {}
        self._error: str | None = None
        # Whether the last frame was actually examined. A frame this reader
        # never looked at - the wrong geometry, or an OCR failure - yields the
        # same (None, None) pair as a clean panel-free menu, and those are not
        # the same fact. Anything that acts on "no panel" must require this.
        self._scanned = False
        # Belongs to the frame the last scan() read and to no other. Never
        # serialized: snapshot() is the API's payload, and a tap target is
        # navigation evidence for the scan loop, not an account observation.
        self._controls: dict[str, ControlTarget] = {}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {'current_screen_id': self._current,
                    'readings': [asdict(reading) for reading in self._readings.values()],
                    'error': self._error}

    def current_evidence(self) -> dict[str, Any]:
        """What the last frame showed, for a transaction step to check.

        `scanned` False means this reader reached no conclusion about that
        frame at all - never that the frame was clean.
        """
        with self._lock:
            return {'screen_id': self._current, 'error': self._error,
                    'scanned': self._scanned, 'controls': dict(self._controls)}

    def reset_current(self) -> None:
        self.observe(None)

    def observe(self, reading: ScreenReading | None, *, error: str | None = None,
                controls: dict[str, ControlTarget] | None = None,
                scanned: bool = False) -> None:
        with self._lock:
            self._current = reading.screen_id if reading is not None else None
            self._error = error
            self._scanned = scanned or reading is not None
            self._controls = dict(controls or {})
            if reading is not None:
                self._readings[reading.screen_id] = reading

    def scan(self, screen: Image) -> bool:
        """Update from the existing frame; return whether all actions must hold.

        Candidate titles block even when confidence/geometry cannot support a
        reading. OCR errors clear current identity and hold this scan too.
        """
        self.observe(None)
        observed_at = time.time()
        if not supported_frame(screen.shape[1], screen.shape[0]):
            return False
        try:
            title = _centered(_TITLE, screen.shape[0])
            crop = screen[title.y:title.y + title.h, title.x:title.x + title.w]
            titles = ocr.read(crop, strict=True, min_confidence=0.)
            if not any(_normal(b.text) in ('stats', 'settings') for b in titles):
                # A supported frame the title reader did examine: no account
                # panel title on it. That IS an observation, and the only one
                # that may stand for "the menu is clear".
                self.observe(None, scanned=True)
                return False
            boxes = ocr.read(screen, strict=True)
            reading = parse_frame(screen, boxes, now=observed_at)
            self.observe(reading, error=None if reading else 'Account panel could not be read reliably',
                         controls=control_targets(reading.screen_id, boxes,
                                                  frame_height=screen.shape[0]) if reading else None,
                         scanned=True)
            return True
        except Exception:
            # Do not leak engine diagnostics (or arbitrary OCR) through the API.
            # Unscanned, not clean: the frame was never read.
            self.observe(None, error='Account screen OCR failed; actions held for this scan')
            return True
