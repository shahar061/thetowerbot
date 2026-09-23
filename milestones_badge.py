"""Whether the main menu's MILESTONES button carries its red count badge.

The badge is the game's own statement that the ladder has something to claim,
so it catches every reward a crossed wave threshold does not point at: one
owed from before a restart, one on a tier the bot has since left, one a failed
walk left behind. It is read by colour, not OCR: the count inside it does not
matter to the walk, which claims everything `Claim All` offers anyway.

None, not False, when the button itself is not on the frame - "no badge" is a
claim about a button this reader has actually seen.
"""

from __future__ import annotations

import cv2
import numpy as np

import config
from device import Image
from vision import TemplateCache, locate_template


def badge_visible(screen: Image, templates: TemplateCache) -> bool | None:
    """True/False for the badge beside a located MILESTONES button, else None."""
    button = locate_template(screen, templates.get(config.NAV_TARGETS['MILESTONES']),
                             config.DEFAULT_THRESHOLD)
    if button is None:
        return None
    region = config.MILESTONES_BADGE_REGION
    x0, y0 = button.top_left[0] + region.dx, button.top_left[1] + region.dy
    height, width = screen.shape[:2]
    patch = screen[max(0, y0):min(height, y0 + region.h),
                   max(0, x0):min(width, x0 + region.w)]
    if patch.size == 0:
        return None
    return red_pixels(patch) >= config.MILESTONES_BADGE_MIN_PIXELS


def red_pixels(patch: Image) -> int:
    """Count saturated, bright red pixels - the badge fill, not the purple frame.

    Hue wraps at 180 in OpenCV, so red is both ends of the range. The button's
    own neon border is magenta-purple (hue ~140-150), well clear of both.
    """
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    hue, saturation, value = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    red = ((hue < 8) | (hue > 172)) & (saturation > 180) & (value > 150)
    return int(np.count_nonzero(red))
