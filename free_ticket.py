"""The tournament "Free Ticket" offer, and the ticket reveal that follows it.

Once tournaments open, the game greets the main menu with a centred dialog:
a "Free Ticket" title, a line about a new tournament, and one CLAIM button.
The dimmed main menu shows around it, so every main-menu anchor still matches
and the frame classifies as MAIN_MENU - while the dialog swallows each tap
aimed at the menu. Unhandled, a worker taps BATTLE underneath it for hours.

CLAIM on the dialog opens a full-screen reveal: "<n> TICKET" centred, its own
CLAIM below it and a SKIP in the corner - the same layout as the unlock card
(unlocked_screen), with CLAIM where that card has OK.

Both are read from their recorded frames and keyed on the text the game
prints, never on the tournament artwork.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from device import Image
from ocr import TextBox

OFFER = 'FREE_TICKET_OFFER'
REWARD = 'FREE_TICKET_REWARD'

_TITLE = re.compile(r'^\s*free\s*tickets?\s*$', re.I)
_REWARD_CAPTION = re.compile(r'^\s*\d+\s*tickets?\s*$', re.I)
_MIN_CONFIDENCE = .9
_CENTRE_TOLERANCE = .1
# The recorded offer puts the title at y=835 and CLAIM at y=1452 on a
# 2400-high frame: about a quarter of the screen apart, inside one dialog.
_OFFER_MAX_GAP = .4
# The recorded reveal carries four boxes (SKIP, icon, caption, CLAIM), and
# puts SKIP at (897, 296) and CLAIM at (539, 1891) - the unlock card's layout.
_REWARD_MAX_BOXES = 8
_SKIP_X_RANGE = (.75, .92)
_SKIP_Y_RANGE = (.08, .17)
_REWARD_CLAIM_Y_RANGE = (.72, .85)


@dataclass(frozen=True)
class FreeTicket:
    screen: str
    claim: tuple[int, int]


def _trusted(box: TextBox) -> bool:
    return (math.isfinite(box.confidence) and box.confidence >= _MIN_CONFIDENCE
            and box.rect.w > 0 and box.rect.h > 0)


def _only(boxes: tuple[TextBox, ...], pattern: re.Pattern[str]) -> TextBox | None:
    found = [box for box in boxes if _trusted(box) and pattern.match(box.text)]
    return found[0] if len(found) == 1 else None


def _centre(box: TextBox) -> tuple[int, int]:
    return box.rect.x + box.rect.w // 2, box.rect.y + box.rect.h // 2


def read(frame: Image, boxes: tuple[TextBox, ...]) -> FreeTicket | None:
    """Which Free Ticket screen this is, and its CLAIM centre - or None."""
    height, width = frame.shape[:2]
    claim = _only(boxes, re.compile(r'^\s*claim\s*$', re.I))
    if claim is None:
        return None
    x, y = _centre(claim)
    if abs(x - width / 2) > width * _CENTRE_TOLERANCE:
        return None
    title = _only(boxes, _TITLE)
    if title is not None:
        title_bottom = title.rect.y + title.rect.h
        if (abs(_centre(title)[0] - width / 2) <= width * _CENTRE_TOLERANCE
                and title_bottom < claim.rect.y
                and y - title_bottom <= height * _OFFER_MAX_GAP):
            return FreeTicket(OFFER, (x, y))
        return None
    if len(boxes) > _REWARD_MAX_BOXES:
        return None
    caption = _only(boxes, _REWARD_CAPTION)
    skip = _only(boxes, re.compile(r'^\s*skip\s*$', re.I))
    if caption is None or skip is None:
        return None
    skip_x, skip_y = _centre(skip)
    if not (_SKIP_X_RANGE[0] <= skip_x / width <= _SKIP_X_RANGE[1]
            and _SKIP_Y_RANGE[0] <= skip_y / height <= _SKIP_Y_RANGE[1]):
        return None
    if (abs(_centre(caption)[0] - width / 2) > width * _CENTRE_TOLERANCE
            or caption.rect.y + caption.rect.h >= claim.rect.y
            or not _REWARD_CLAIM_Y_RANGE[0] <= y / height <= _REWARD_CLAIM_Y_RANGE[1]):
        return None
    return FreeTicket(REWARD, (x, y))
