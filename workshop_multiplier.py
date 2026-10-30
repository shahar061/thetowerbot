"""Read the outlined buy-quantity control beside a Workshop category heading."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

import ocr
import vision
from device import Image
from geometry import supported_frame
from ocr import TextBox


@dataclass(frozen=True)
class Reading:
    mode: Literal['single', 'multiple', 'select_single', 'absent', 'unreadable']
    point: tuple[int, int] | None = None


def _expanded_picker(screen: Image, templates: vision.TemplateCache,
                     labels: tuple[TextBox, ...]) -> Reading | None:
    """The open picker replaces the category heading with five quantity buttons."""
    height, width = screen.shape[:2]
    if not any(b.text.casefold() == 'workshop' and b.confidence >= .9
               and b.rect.y < height * .1 for b in labels):
        return None
    neighbors = [b for b in labels if b.text.casefold() in {'x5', 'x10'}
                 and b.confidence >= .9 and width * .25 < b.rect.x
                 and height * .07 < b.rect.y < height * .25]
    if len(neighbors) != 2 or {b.text.casefold() for b in neighbors} != {'x5', 'x10'}:
        return None
    if abs(neighbors[0].rect.y - neighbors[1].rect.y) > 10:
        return None
    y0 = min(b.rect.y for b in neighbors) - 20
    y1 = max(b.rect.y + b.rect.h for b in neighbors) + 20
    patch = screen[y0:y1]
    outline = cv2.inRange(cv2.cvtColor(patch, cv2.COLOR_BGR2HSV),
                          np.array((80, 90, 100)), np.array((115, 255, 255)))
    contours, _ = cv2.findContours(outline, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    controls = sorted(cv2.boundingRect(c) for c in contours
                      if 85 <= cv2.boundingRect(c)[2] <= 200
                      and 35 <= cv2.boundingRect(c)[3] <= 95)
    if len(controls) != 5:
        return None
    first = controls[0]
    if any(abs(y - first[1]) > 3 or abs(w - first[2]) > 3 or abs(h - first[3]) > 3
           for x, y, w, h in controls):
        return None
    if any(not 3 <= right[0] - (left[0] + left[2]) <= 25
           for left, right in zip(controls, controls[1:])):
        return None
    template = templates.get('workshop/multiplier_x1.png')
    matches = []
    for x, y, w, h in controls:
        control = cv2.resize(patch[y:y + h, x:x + w], (template.shape[1], template.shape[0]))
        score, _ = vision.best_score(control, template)
        if score >= .97:
            matches.append((x + w // 2, y0 + y + h // 2))
    return Reading('select_single', matches[0]) if len(matches) == 1 else None


def read(screen: Image, templates: vision.TemplateCache,
         boxes: tuple[TextBox, ...] | None = None) -> Reading:
    height, width = screen.shape[:2]
    if not supported_frame(width, height):
        return Reading('unreadable')
    labels = ocr.read(screen) if boxes is None else boxes
    picker = _expanded_picker(screen, templates, labels)
    if picker is not None:
        return picker
    headings = [b for b in labels if b.confidence >= .9 and b.rect.x < width * .6
                and re.sub('[^a-z]', '', b.text.casefold()) in
                {'attackupgrades', 'defenseupgrades', 'utilityupgrades'}]
    if len(headings) != 1:
        return Reading('unreadable')
    heading = headings[0].rect
    x0, y0 = int(width * .75), max(0, heading.y - 30)
    patch = screen[y0:min(height, heading.y + heading.h + 20), x0:]
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    outline = cv2.inRange(hsv, np.array((80, 90, 100)), np.array((115, 255, 255)))
    if not np.any(outline):
        # The quantity control is locked on early accounts. Its absence
        # is established by pixels, never by missing OCR (which omits x1).
        return Reading('absent')
    contours, _ = cv2.findContours(outline, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    controls = [cv2.boundingRect(c) for c in contours
                if 85 <= cv2.boundingRect(c)[2] <= 200
                and 35 <= cv2.boundingRect(c)[3] <= 95]
    if len(controls) != 1:
        return Reading('unreadable')
    x, y, w, h = controls[0]
    point = (x0 + x + w // 2, y0 + y + h // 2)
    control = patch[y:y + h, x:x + w]
    template = templates.get('workshop/multiplier_x1.png')
    control = cv2.resize(control, (template.shape[1], template.shape[0]))
    score, _ = vision.best_score(control, template)
    return Reading('single' if score >= .97 else 'multiple', point)
