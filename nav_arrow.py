"""Whether a bottom tab carries the game's green "new" arrow.

The game floats the arrow above a tab it has just unlocked and drops it the
first time that tab is opened, so it is the game's own statement that a
first visit - and whatever that visit pays out - is still owed. Read by
colour beside the located tab, the same way milestones_badge reads its badge.

None, not False, when the tab itself is not on the frame - "no arrow" is a
claim about a tab this reader has actually seen.
"""

from __future__ import annotations

import cv2
import numpy as np

import config
from device import Image
from vision import TemplateCache, locate_template


def arrow_over(screen: Image, templates: TemplateCache, tab: str) -> bool | None:
    """True/False for the arrow over a located NAV_TARGETS tab, else None."""
    button = locate_template(screen, templates.get(config.NAV_TARGETS[tab]),
                             config.DEFAULT_THRESHOLD)
    if button is None:
        return None
    region = config.NAV_ARROW_REGION
    x0, y0 = button.top_left[0] + region.dx, button.top_left[1] + region.dy
    height, width = screen.shape[:2]
    patch = screen[max(0, y0):min(height, y0 + region.h),
                   max(0, x0):min(width, x0 + region.w)]
    if patch.size == 0:
        return None
    return green_pixels(patch) >= config.NAV_ARROW_MIN_PIXELS


def green_pixels(patch: Image) -> int:
    """Count saturated, bright green pixels - the arrow's fill and glow.

    The tab bar itself is dark purple and the tab icons desaturated, so
    nothing else near a tab comes close to this hue.
    """
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    hue, saturation, value = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    green = (hue > 40) & (hue < 85) & (saturation > 120) & (value > 150)
    return int(np.count_nonzero(green))
