"""Read the one-time Workshop coin grant on a fresh reroll account."""

from __future__ import annotations

import math

from config import Rect
from device import Image
from geometry import anchored_y, supported_frame
from ocr import TextBox


_MARKERS = (
    ("WORKSHOP", Rect(300, 450, 480, 170)),
    ("Spend coins to permanently increase", Rect(100, 790, 880, 120)),
    ("Here are some additional coins to get", Rect(100, 1130, 880, 125)),
    ("50", Rect(445, 1290, 205, 155)),
    ("CLAIM", Rect(360, 1470, 355, 190)),
)


def workshop_coin_claim(frame: Image, boxes: tuple[TextBox, ...]) -> tuple[int, int] | None:
    """Return the measured Claim center only when the whole grant is readable."""
    height, width = frame.shape[:2]
    if not supported_frame(width, height):
        return None
    matched: list[TextBox] = []
    for label, bounds in _MARKERS:
        bounds = Rect(bounds.x, anchored_y(bounds.y, height, 'center'),
                      bounds.w, bounds.h)
        found = tuple(box for box in boxes if box.text.strip() == label
                      and math.isfinite(box.confidence) and box.confidence >= .9
                      and box.rect.w > 0 and box.rect.h > 0
                      and bounds.x <= box.rect.x and bounds.y <= box.rect.y
                      and box.rect.x + box.rect.w <= bounds.x + bounds.w
                      and box.rect.y + box.rect.h <= bounds.y + bounds.h)
        if len(found) != 1:
            return None
        matched.append(found[0])
    claim = matched[-1].rect
    return claim.x + claim.w // 2, claim.y + claim.h // 2
