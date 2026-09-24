"""Frame-local, numbered reward badges anchored to their actual menu controls."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

import config
import ocr
from account_collection import locate_control
from account_screens import ControlTarget
from device import Image
from geometry import supported_frame
from vision import TemplateCache

MAIL_TEMPLATE = 'nav/mail.png'


@dataclass(frozen=True)
class Badge:
    count: int
    control: ControlTarget


def read_badge(screen: Image, templates: TemplateCache,
               kind: Literal['missions', 'mail']) -> Badge | None:
    """A red disc alone (notably settings) is never a reward notification.

    Offsets bound a search around the control, not a stored click coordinate.
    Both anchors and badge sizes were measured on native 1080-wide captures.
    """
    height, width = screen.shape[:2]
    if not supported_frame(width, height):
        return None
    name = config.NAV_TARGETS['MISSIONS'] if kind == 'missions' else MAIL_TEMPLATE
    try:
        control = locate_control(screen, templates.get(name), kind, .90)
    except (OSError, ValueError, AttributeError):
        return None
    if control is None or control.status != 'located' or control.rect is None:
        return None
    x, y, w, h = control.rect
    # MISSIONS template is its caption; mail is the envelope icon, with the
    # count above-left. Never search the settings control between them.
    if kind == 'missions':
        x0, y0, x1, y1 = x - 35, y + 20, x + 30, y + 100
    else:
        x0, y0, x1, y1 = x - 40, y - 50, x + 25, y + 20
    patch = screen[max(0, y0):min(height, y1), max(0, x0):min(width, x1)]
    if not patch.size:
        return None
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    red = (((hsv[..., 0] < 8) | (hsv[..., 0] > 172))
           & (hsv[..., 1] > 180) & (hsv[..., 2] > 150)).astype(np.uint8)
    contours, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    discs = [cv2.boundingRect(c) for c in contours if 250 < cv2.contourArea(c) < 2500]
    if len(discs) != 1:
        return None
    bx, by, bw, bh = discs[0]
    if not .7 < bw / bh < 1.4:
        return None
    crop = patch[by:by + bh, bx:bx + bw]
    # Plain red settings dots have no white glyph, irrespective of OCR noise.
    white = np.all(crop > 190, axis=2)
    if np.count_nonzero(white) < 10:
        return None
    try:
        boxes = ocr.read(cv2.resize(crop, None, fx=3, fy=3), min_confidence=.9)
    except Exception:
        return None
    if len(boxes) != 1 or boxes[0].confidence < .9:
        return None
    label = boxes[0].text.strip()
    if not label.isascii() or not label.isdecimal() or not 0 < int(label) < 100:
        return None
    return Badge(int(label), control)
