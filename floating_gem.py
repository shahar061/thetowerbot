"""Spot the free gem that orbits the tower during a battle.

Colour, not a template. Every other detector in this codebase matches a
template against a fixed spot, and neither half of that works here: the
sprite MOVES along the ring, and it rotates and pulses as it goes, which is
exactly the case `cv2.matchTemplate` handles worst. What does not change is
its colour - hue 151, measured off the HUD gem counter, which is the same
artwork at a smaller size.

That leaves one number doing all the discriminating, so it is worth saying
what it is separated FROM. The red enemy diamonds are the same shape, the
same size and nearly the same brightness; they sit at hue 177. The two
other things on screen wearing hue 151 are the HUD counter and the ad-gem
button, and both are excluded geometrically - see FLOATING_GEM_SEARCH.

Pure: a frame and an anchor in, a sighting or None out. No device, no bus,
no state. What to do about a sighting belongs to the caller.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import cv2
import numpy as np

import config
import digits
from device import Image

logger = logging.getLogger("tower_bot.floating_gem")


class Sighting(NamedTuple):
    """Where the gem is, in absolute frame coordinates.

    Absolute, not relative to the search window: `point` is handed straight
    to a tap and `box` straight to the frame overlay, and neither caller
    knows the crop happened.
    """

    point: tuple[int, int]
    box: tuple[int, int, int, int]
    area: int


def find(screen: Image, anchor: tuple[int, int]) -> Sighting | None:
    """The floating gem in this frame, or nothing.

    `anchor` is the cash counter's top-left - the same anchor the wallet
    read uses, already computed once per scan by the loop.
    """
    window = digits.crop(screen, config.FLOATING_GEM_SEARCH, anchor)
    if window is None:
        return None

    hsv = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(
        hsv,
        np.array(config.FLOATING_GEM_HSV_LOW, dtype=np.uint8),
        np.array(config.FLOATING_GEM_HSV_HIGH, dtype=np.uint8),
    )
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)

    # The largest blob that is big enough to be the sprite rather than one
    # of the particles it trails, and dim enough not to be a boss, which
    # shares the hue but outshines the gem - see FLOATING_GEM_MAX_VALUE.
    # Largest rather than first: label order is raster order, so "first"
    # would prefer whichever particle happens to sit highest on the screen.
    best = -1
    best_area = 0
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        if area < config.FLOATING_GEM_MIN_AREA or area <= best_area:
            continue
        if np.median(hsv[..., 2][labels == index]) > config.FLOATING_GEM_MAX_VALUE:
            continue
        best, best_area = index, area
    if best < 0:
        return None

    origin_x = anchor[0] + config.FLOATING_GEM_SEARCH.dx
    origin_y = anchor[1] + config.FLOATING_GEM_SEARCH.dy
    centre_x, centre_y = centroids[best]
    box = (
        origin_x + int(stats[best, cv2.CC_STAT_LEFT]),
        origin_y + int(stats[best, cv2.CC_STAT_TOP]),
        int(stats[best, cv2.CC_STAT_WIDTH]),
        int(stats[best, cv2.CC_STAT_HEIGHT]),
    )
    sighting = Sighting(
        point=(origin_x + int(round(centre_x)), origin_y + int(round(centre_y))),
        box=box,
        area=best_area,
    )
    logger.debug("floating gem at %s, area %d", sighting.point, sighting.area)
    return sighting
