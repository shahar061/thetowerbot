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
_MIN_CONFIDENCE = .9
# The recorded card carries three boxes (SKIP, caption, OK). A menu page with
# an "... unlocked" row and some OK somewhere is dense with text; this bound
# keeps the reader to the near-empty ceremony without guessing what a future
# card's icon or subtitle might add.
_MAX_BOXES = 8
# OK is centred on the card; a tenth of the frame width either side.
_CENTRE_TOLERANCE = .1


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
    oks = [box for box in boxes if _trusted(box) and box.text.strip().upper() == 'OK']
    if len(captions) != 1 or len(oks) != 1:
        return None
    caption, ok = captions[0], oks[0]
    x = ok.rect.x + ok.rect.w // 2
    y = ok.rect.y + ok.rect.h // 2
    width = frame.shape[1]
    if abs(x - width / 2) > width * _CENTRE_TOLERANCE:
        return None
    if ok.rect.y <= caption.rect.y + caption.rect.h:
        return None
    return Unlocked(caption.text.strip(), (x, y))
