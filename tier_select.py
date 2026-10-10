"""The main menu's Difficulty panel: is a higher tier waiting behind its arrow?

The panel shows "Tier N" between two chevrons. A chevron the game will act on
is drawn solid white; one it will not - the left arrow on Tier 1, the right
arrow on the highest unlocked tier - is the same shape blended into the panel
(measured: peak grey 255 lit, 107 dim, on a background near 120,50,55 BGR).

The shape is located by template, not by a stored coordinate: the panel sits
at a different height on every account layout (with or without the MILESTONES
button, on 2400- and 1920-tall frames). TM_CCOEFF_NORMED is insensitive to
that brightness blend, so one template finds both states and the brightness
inside the match decides which it is.

None, not False, when the arrow is not on the frame or its brightness is
neither measured state: "no higher tier" is a claim about an arrow this
reader has actually seen.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

import cv2

from device import Image
from geometry import supported_frame
from ocr import TextBox
from vision import TemplateCache

NEXT_TEMPLATE = 'nav/tier_next.png'
MATCH_THRESHOLD = .9
# Measured: a lit chevron peaks at 255 grey, a dim one at 107.
LIT_MIN_GREY = 200
DIM_MAX_GREY = 150


@dataclass(frozen=True)
class NextTier:
    """The right chevron: where it is, and whether it leads anywhere."""

    available: bool
    point: tuple[int, int]
    score: float


def read_next(screen: Image, templates: TemplateCache) -> NextTier | None:
    """The right-hand tier chevron on a main-menu frame, or None."""
    height, width = screen.shape[:2]
    if not supported_frame(width, height):
        return None
    try:
        template = templates.get(NEXT_TEMPLATE)
    except (OSError, ValueError, AttributeError):
        return None
    th, tw = template.shape[:2]
    # The right chevron is right of centre. Searching only there keeps the
    # mirrored left chevron, and every icon in the left column, out of play.
    x0 = width // 2
    band = screen[:, x0:]
    if band.shape[0] < th or band.shape[1] < tw:
        return None
    result = cv2.matchTemplate(band, template, cv2.TM_CCOEFF_NORMED)
    _, best, _, (lx, ly) = cv2.minMaxLoc(result)
    if best < MATCH_THRESHOLD:
        return None
    masked = result.copy()
    masked[max(ly - th, 0):ly + th, max(lx - tw, 0):lx + tw] = -1.
    if cv2.minMaxLoc(masked)[1] >= MATCH_THRESHOLD:
        return None
    patch = cv2.cvtColor(band[ly:ly + th, lx:lx + tw], cv2.COLOR_BGR2GRAY)
    peak = int(patch.max())
    if peak >= LIT_MIN_GREY:
        available = True
    elif peak <= DIM_MAX_GREY:
        available = False
    else:
        return None
    return NextTier(available, (x0 + lx + tw // 2, ly + th // 2), float(best))


@dataclass(frozen=True)
class TierPanel:
    tier: int
    next: NextTier
    previous: NextTier | None


def read_panel(screen: Image, templates: TemplateCache,
               boxes: Sequence[TextBox]) -> TierPanel | None:
    """Read an exact Tier N label aligned with the matched difficulty arrow."""
    right = read_next(screen, templates)
    if right is None:
        return None
    candidates = []
    width = screen.shape[1]
    for box in boxes:
        match = re.fullmatch(r"Tier\s+(\d{1,2})", box.text.strip(), re.IGNORECASE)
        rect = box.rect
        if (match and box.confidence >= .9
                and width * .35 <= rect.x + rect.w / 2 < right.point[0]
                and abs(rect.y + rect.h / 2 - right.point[1]) <= screen.shape[0] * .02
                and 1 <= int(match[1]) <= 99):
            candidates.append((int(match[1]), rect.x))
    if len(candidates) != 1:
        return None
    template = cv2.flip(templates.get(NEXT_TEMPLATE), 1)
    th, tw = template.shape[:2]
    y0 = max(0, right.point[1] - th * 2)
    band = screen[y0:right.point[1] + th * 2, :width // 2]
    result = cv2.matchTemplate(band, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(result)
    masked = result.copy()
    masked[max(y - th, 0):y + th, max(x - tw, 0):x + tw] = -1.
    ambiguous = cv2.minMaxLoc(masked)[1] >= MATCH_THRESHOLD
    previous = None
    if score >= MATCH_THRESHOLD and not ambiguous and x + tw <= candidates[0][1]:
        patch = cv2.cvtColor(band[y:y + th, x:x + tw], cv2.COLOR_BGR2GRAY)
        peak = int(patch.max())
        if peak >= LIT_MIN_GREY or peak <= DIM_MAX_GREY:
            previous = NextTier(peak >= LIT_MIN_GREY, (x + tw // 2, y0 + y + th // 2), float(score))
    return TierPanel(candidates[0][0], right, previous)
