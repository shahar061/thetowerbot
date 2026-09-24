"""Which battle upgrade tab is showing, from its heading bar's colour.

No OCR. The same approach as floating_gem.py: a colour test in a measured
window. The heading bar is blue on ATTACK, red on DEFENSE and yellow on
UTILITY; its white text is unsaturated and drops out of the median.
"""

from __future__ import annotations

from typing import Literal, cast

import cv2
import numpy as np

import config
from config import Rect
from device import Image

Tab = Literal["ATTACK", "DEFENSE", "UTILITY"]


def classify(screen: Image, heading_band: Rect) -> Tab | None:
    """The tab whose hue the band's saturated pixels centre on, or None.

    None when too little of the band is saturated and bright (a popup, a
    menu) or when the median hue is outside every tab's range. The median is
    taken per tab on the circular offset from that tab's hue, so a red band
    split across 178 and 2 still centres on 175.
    """
    band = screen[heading_band.y:heading_band.y + heading_band.h,
                  heading_band.x:heading_band.x + heading_band.w]
    if band.size == 0:
        return None
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    lit = (hsv[..., 1] > config.BATTLE_TAB_MIN_SV) & (hsv[..., 2] > config.BATTLE_TAB_MIN_SV)
    if lit.sum() < config.BATTLE_TAB_MIN_FRACTION * lit.size:
        return None
    hues = hsv[..., 0][lit].astype(np.int16)
    for tab, reference in config.BATTLE_TAB_HUES.items():
        offsets = (hues - reference + 90) % 180 - 90
        if abs(float(np.median(offsets))) <= config.BATTLE_TAB_HUE_TOLERANCE:
            return cast(Tab, tab)
    return None


def classify_frame(screen: Image) -> Tab | None:
    """classify() in this frame size's measured heading band; None off a measured size."""
    bands = config.BATTLE_BANDS.get((screen.shape[1], screen.shape[0]))
    return None if bands is None else classify(screen, bands.heading)
