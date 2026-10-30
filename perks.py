"""Read the perk panel and pick its first offered row, one fresh frame at a time."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

import events
from config import Rect
from device import Image
from geometry import supported_frame
from ocr import TextBox
from vision import TemplateCache, locate_template

SCREEN_ID = 'PERK_CHOICE'
logger = logging.getLogger('tower_bot.perks')


class Policy(Protocol):
    auto_pick_perks: bool


@dataclass(frozen=True)
class Offer:
    label: str
    point: tuple[int, int]


@dataclass(frozen=True)
class Choice:
    offers: tuple[Offer, ...]
    selected: tuple[str, ...]
    appearance: bytes

    @property
    def first(self) -> Offer:
        return self.offers[0]


@dataclass(frozen=True)
class Summary:
    selected: tuple[str, ...]
    close: tuple[int, int]


def _norm(text: str) -> str:
    return ' '.join(text.casefold().split())


def _heading(text: str) -> str:
    return ''.join(text.casefold().split())


def _trusted(box: TextBox) -> bool:
    return (math.isfinite(box.confidence) and box.confidence >= .9
            and box.rect.w > 0 and box.rect.h > 0)


def _one(boxes: tuple[TextBox, ...], text: str) -> TextBox | None:
    found = [b for b in boxes if _trusted(b) and _heading(b.text) == _heading(text)]
    return found[0] if len(found) == 1 else None


def _captions(boxes: tuple[TextBox, ...]) -> tuple[str, ...]:
    """Join horizontal fragments and wrapped lines, keeping cards separate."""
    lines: list[list[TextBox]] = []
    for box in sorted(boxes, key=lambda b: (b.rect.y, b.rect.x)):
        if lines and abs(box.rect.y - lines[-1][0].rect.y) < box.rect.h * .6:
            lines[-1].append(box)
        else:
            lines.append([box])
    captions: list[str] = []
    previous_bottom = 0
    for line in lines:
        text = ' '.join(b.text.strip() for b in sorted(line, key=lambda b: b.rect.x))
        top = min(b.rect.y for b in line)
        line_height = max(b.rect.h for b in line)
        if captions and top - previous_bottom < line_height * .8:
            captions[-1] += ' ' + text
        else:
            captions.append(text)
        previous_bottom = max(b.rect.y + b.rect.h for b in line)
    return tuple(_norm(text) for text in captions)


def panel_visible(screen: Image, boxes: tuple[TextBox, ...]) -> bool:
    """Recognize the panel even when its first offer cannot be read safely."""
    height, width = screen.shape[:2]
    if not supported_frame(width, height):
        return False
    title, choose = _one(boxes, 'perks'), _one(boxes, 'choose a new perk')
    selected = _one(boxes, 'selected perks')
    if title is None or selected is None:
        return False
    if not (all(abs(b.rect.x + b.rect.w / 2 - width / 2) < width * .15
                for b in (title, selected)) and height * .08 < title.rect.y):
        return False
    if choose is None:
        return 0 < selected.rect.y - (title.rect.y + title.rect.h) < height * .08
    return (abs(choose.rect.x + choose.rect.w / 2 - width / 2) < width * .15
            and title.rect.y < choose.rect.y
            and choose.rect.y + choose.rect.h < selected.rect.y < height * .8)


def read_summary(screen: Image, boxes: tuple[TextBox, ...],
                 templates: TemplateCache) -> Summary | None:
    if not panel_visible(screen, boxes) or _one(boxes, 'choose a new perk') is not None:
        return None
    title, selected = _one(boxes, 'perks'), _one(boxes, 'selected perks')
    assert title is not None and selected is not None
    height, width = screen.shape[:2]
    x0, y0 = int(width * .8), max(0, title.rect.y - 40)
    patch = screen[y0:min(height, title.rect.y + title.rect.h + 40), x0:]
    match = locate_template(patch, templates.get('perks/close.png'), .95)
    if match is None:
        return None
    labels = _captions(tuple(b for b in boxes if _trusted(b)
                            and selected.rect.y + selected.rect.h < b.rect.y < height * .8))
    return Summary(labels, (x0 + match.center[0], y0 + match.center[1]))


def panel_candidate(screen: Image) -> bool:
    """Cheap hint to request full OCR; never sufficient evidence for a tap."""
    height, width = screen.shape[:2]
    if not supported_frame(width, height):
        return False
    bright = (np.max(screen, axis=2) >= 190).astype(np.uint8)
    contours, _ = cv2.findContours(bright, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    return any(w >= width * .75 and h >= height * .4 and y > height * .1
               for x, y, w, h in (cv2.boundingRect(c) for c in contours))


def read_choice(screen: Image, boxes: tuple[TextBox, ...]) -> Choice | None:
    if not panel_visible(screen, boxes):
        return None
    height, width = screen.shape[:2]
    choose = _one(boxes, 'choose a new perk')
    selected = _one(boxes, 'selected perks')
    if choose is None or selected is None:
        return None
    # Offered rows have bright rectangular outlines. Selected perks below
    # the divider are filled cards; never use their captions as tap targets.
    hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array((0, 0, 190)), np.array((179, 255, 255)))
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    rows: list[Rect] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if (w < width * .65 or not height * .045 < h < height * .16
                or y <= choose.rect.y + choose.rect.h or y + h >= selected.rect.y
                or abs(x + w / 2 - width / 2) > width * .1):
            continue
        rows.append(Rect(x, y, w, h))
    unique: list[Rect] = []
    for row in sorted(rows, key=lambda r: (r.y, -r.w)):
        if not unique or row.y > unique[-1].y + unique[-1].h:
            unique.append(row)
    # If the top border is unreadable, the second row must not become first.
    if not unique or unique[0].y - (choose.rect.y + choose.rect.h) > height * .055:
        return None
    offers: list[Offer] = []
    for row in unique:
        labels = sorted((b for b in boxes if _trusted(b)
                         and row.x < b.rect.x + b.rect.w / 2 < row.x + row.w
                         and row.y < b.rect.y + b.rect.h / 2 < row.y + row.h),
                        key=lambda b: (b.rect.y, b.rect.x))
        if not labels:
            return None
        offers.append(Offer(' '.join(b.text.strip() for b in labels),
                            (row.x + row.w // 2, row.y + row.h // 2)))
    chosen = tuple(_norm(b.text) for b in sorted(boxes, key=lambda b: (b.rect.y, b.rect.x))
                   if _trusted(b) and selected.rect.y + selected.rect.h < b.rect.y < height * .8)
    # The opaque interior is stable while a choice waits. OCR variations
    # alone must never count as a successful pick or rearm the same row.
    # A fixed frame rectangle is essential: moving OCR boxes must not
    # turn identical pixels into different thumbnails.
    interior = screen[int(height * .2):int(height * .8),
                      int(width * .15):int(width * .85)]
    appearance = cv2.resize(cv2.cvtColor(interior, cv2.COLOR_BGR2GRAY),
                            (64, 64), interpolation=cv2.INTER_AREA).tobytes()
    return Choice(tuple(offers), chosen, appearance)


def read_button(screen: Image, boxes: tuple[TextBox, ...]) -> tuple[int, int] | None:
    height, width = screen.shape[:2]
    if not supported_frame(width, height):
        return None
    labels = [b for b in boxes if _trusted(b) and _heading(b.text) in {'newperk', 'new', 'perk'}
              and 0 <= b.rect.y < height * .1 and width * .25 < b.rect.x < width * .75]
    labels.sort(key=lambda b: b.rect.x)
    if _heading(' '.join(b.text for b in labels)) != 'newperk':
        return None
    if max(b.rect.y for b in labels) - min(b.rect.y for b in labels) > 15:
        return None
    x0, y0 = min(b.rect.x for b in labels), min(b.rect.y for b in labels)
    x1 = max(b.rect.x + b.rect.w for b in labels)
    y1 = max(b.rect.y + b.rect.h for b in labels)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        return None
    hsv = cv2.cvtColor(screen[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    purple = ((hsv[..., 0] >= 130) & (hsv[..., 0] <= 165)
              & (hsv[..., 1] >= 80) & (hsv[..., 2] >= 150))
    if np.mean(purple) < .3:
        return None
    return (x0 + x1) // 2, (y0 + y1) // 2


class PerkPicker:
    def __init__(self, bus: Any) -> None:
        self._bus = bus
        self._templates = TemplateCache(Path(__file__).parent / 'templates')
        self._phase = 'idle'
        self._started = 0.0
        self._choice: Choice | None = None
        self._blocked: Choice | None = None
        self._run_id: int | None = None
        self._candidate: Choice | None = None

    @property
    def active(self) -> bool:
        return self._phase != 'idle'

    def cancel(self, reason: str, detail: str) -> None:
        if self.active:
            self._bus.publish(events.Skipped(action='perks', reason=reason, detail=detail))
        self._phase = 'idle'
        self._choice = None
        self._candidate = None

    def observe(self, screen: Image, boxes: tuple[TextBox, ...], device: Any,
                policy: Policy, *, now: float, in_run: bool,
                run_id: int | None = None) -> bool:
        """True owns this scan, including waits and confirmation without a tap."""
        if not policy.auto_pick_perks:
            self.cancel('disabled', 'Automatic perk picking disabled')
            return False
        panel = panel_visible(screen, boxes)
        choice = read_choice(screen, boxes) if panel else None
        summary = read_summary(screen, boxes, self._templates) if panel else None
        if self.active and now - self._started >= 20:
            self._blocked = self._choice
            self.cancel('unconfirmed', 'Perk choice did not confirm within 20 seconds')
            return True
        if self._phase == 'closing':
            if in_run and not panel and not panel_candidate(screen):
                self._phase = 'idle'
            return True
        if summary is not None:
            if self._phase == 'confirming' and self._choice is not None:
                if _norm(self._choice.first.label) not in summary.selected:
                    return True
                self._record_pick()
            self._phase = 'closing'
            self._started = now
            self._tap(device, summary.close, 'perks:close')
            return True
        if self._phase == 'confirming':
            changed = False
            if choice is not None and self._choice is not None:
                delta = np.abs(np.frombuffer(choice.appearance, dtype=np.uint8).astype(int)
                               - np.frombuffer(self._choice.appearance, dtype=np.uint8))
                changed = (np.count_nonzero(delta >= 12) >= 16
                           and (choice.offers, choice.selected)
                           != (self._choice.offers, self._choice.selected))
            stable_new = (changed and self._candidate is not None
                          and (choice.offers, choice.selected)
                          == (self._candidate.offers, self._candidate.selected))
            self._candidate = choice if changed else None
            if (in_run and not panel and not panel_candidate(screen)) or stable_new:
                assert self._choice is not None
                self._record_pick()
                self._phase = 'idle'
            return True
        if panel:
            if (choice is None or self._blocked is not None
                    and choice.appearance == self._blocked.appearance):
                return True
            self._choice = choice
            self._phase = 'confirming'
            self._started = now
            self._run_id = run_id
            self._tap(device, choice.first.point, 'perks:choose_first')
            return True
        if self.active:
            return True
        if in_run:
            self._blocked = None
            button = read_button(screen, boxes)
            if button is not None:
                self._phase = 'opening'
                self._started = now
                self._run_id = run_id
                self._tap(device, button, 'perks:open')
                return True
        return False

    def _tap(self, device: Any, point: tuple[int, int], action: str) -> None:
        device.click(*point)
        self._bus.publish(events.Tapped(action=action, x=point[0], y=point[1], score=1.0))

    def _record_pick(self) -> None:
        assert self._choice is not None
        self._bus.publish(events.PerkPicked(perk=self._choice.first.label, run_id=self._run_id))
        logger.info('Selected first offered perk: %s', self._choice.first.label)
        self._choice = None
        self._candidate = None
