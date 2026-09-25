"""The full-screen "<something> unlocked" ceremony, and the OK that clears it.

The game announces every newly opened feature the same way: a dark
full-screen card with an icon, one "<Feature> unlocked" caption, a centred
OK button and a SKIP in the corner. "Lab unlocked" (paid by a milestone) is
the recorded one; later milestones and waves unlock other content through the
same card, so this reader keys on the shape - one "... unlocked" caption with
one centred OK below it - and never on which feature it names.

None of the anchor templates match this card, so without this reader the
recovery preflight names it UNKNOWN and blocks every pass: nothing taps, the
frame never changes, and the worker sits on it forever.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from device import Image
from ocr import TextBox

SCREEN_ID = 'CONTENT_UNLOCKED'

# "Lab unlocked", "Cards unlocked!" - and "Labunlocked", should the OCR ever
# drop the space. At least one character before "unlocked", so a bare
# "Unlocked" status label on some list row is never taken for a caption.
_CAPTION = re.compile(r'^\s*\S.*unlocked\s*[!.]?\s*$', re.I)
_UNLOCKED_LINE = re.compile(r'^\s*unlocked\s*[!.]?\s*$', re.I)
_MIN_CONFIDENCE = .9
# The recorded card carries three boxes (SKIP, caption, OK). A menu page with
# an "... unlocked" row and some OK somewhere is dense with text; this bound
# keeps the reader to the near-empty ceremony without guessing what a future
# card's icon or subtitle might add.
_MAX_BOXES = 8
# The recorded Lab and Tournament cards put SKIP at (897, 296) and OK at
# (541, 1891) on a 1080x2400 frame. Use broad normalized bounds so both
# controls corroborate the sparse caption without assuming exact pixels.
_CENTRE_TOLERANCE = .1
_SKIP_X_RANGE = (.75, .92)
_SKIP_Y_RANGE = (.08, .17)
_OK_Y_RANGE = (.72, .85)


@dataclass(frozen=True)
class Unlocked:
    caption: str
    ok: tuple[int, int]


def _trusted(box: TextBox) -> bool:
    return (math.isfinite(box.confidence) and box.confidence >= _MIN_CONFIDENCE
            and box.rect.w > 0 and box.rect.h > 0)


def read(frame: Image, boxes: tuple[TextBox, ...]) -> Unlocked | None:
    """The caption and OK centre, only when the whole card is unambiguous."""
    if len(boxes) > _MAX_BOXES:
        return None
    captions = [box for box in boxes if _trusted(box) and _CAPTION.match(box.text)]
    skips = [box for box in boxes if _trusted(box) and box.text.strip().upper() == 'SKIP']
    oks = [box for box in boxes if _trusted(box) and box.text.strip().upper() == 'OK']
    if len(skips) != 1 or len(oks) != 1:
        return None
    height, width = frame.shape[:2]
    skip = skips[0]
    skip_x = (skip.rect.x + skip.rect.w / 2) / width
    skip_y = (skip.rect.y + skip.rect.h / 2) / height
    if not (_SKIP_X_RANGE[0] <= skip_x <= _SKIP_X_RANGE[1]
            and _SKIP_Y_RANGE[0] <= skip_y <= _SKIP_Y_RANGE[1]):
        return None
    if len(captions) == 1:
        caption_text = captions[0].text.strip()
        caption_bottom = captions[0].rect.y + captions[0].rect.h
    elif not captions:
        # On the tournament card OCR gives "Tournament" and "unlocked" as
        # separate boxes. Only join a single, centred feature immediately
        # above the standalone word; the trophy icon can also produce text.
        unlocked_lines = [box for box in boxes
                          if _trusted(box) and _UNLOCKED_LINE.match(box.text)]
        if len(unlocked_lines) != 1:
            return None
        line = unlocked_lines[0]
        line_centre = line.rect.x + line.rect.w / 2
        if abs(line_centre - width / 2) > width * _CENTRE_TOLERANCE:
            return None
        features = []
        for box in boxes:
            if not _trusted(box) or box is line or box.text.strip().upper() in {'OK', 'SKIP'}:
                continue
            centre = box.rect.x + box.rect.w / 2
            gap = line.rect.y - (box.rect.y + box.rect.h)
            if (abs(centre - width / 2) <= width * _CENTRE_TOLERANCE
                    and 0 <= gap <= max(box.rect.h, line.rect.h) * 1.5):
                features.append(box)
        if len(features) != 1:
            return None
        caption_text = f'{features[0].text.strip()} {line.text.strip()}'
        caption_bottom = line.rect.y + line.rect.h
    else:
        return None
    ok = oks[0]
    x = ok.rect.x + ok.rect.w // 2
    y = ok.rect.y + ok.rect.h // 2
    if abs(x - width / 2) > width * _CENTRE_TOLERANCE:
        return None
    if not _OK_Y_RANGE[0] <= y / height <= _OK_Y_RANGE[1]:
        return None
    if ok.rect.y <= caption_bottom:
        return None
    return Unlocked(caption_text, (x, y))
