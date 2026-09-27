"""Bounded, redacted PNG of a scanned frame for a recovery provider request.

Only the pixels leave: no path, OCR text, host geometry or metadata. The frame
is downscaled to at most ``MAX_DIMENSION`` and re-encoded, shrinking (and
finally dropping to grayscale) until it fits ``MAX_BYTES``. Encoding runs only
when a provider request is actually about to be built, never on ordinary scans.
"""
from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from recovery_policy import RecoveryImage

# Must match openrouter_recovery's transport limits.
MAX_BYTES = 65_536
MAX_DIMENSION = 1024
_SCALES = (1.0, .75, .5, .375, .25)


def encode_recovery_image(frame: Any) -> RecoveryImage | None:
    if not isinstance(frame, np.ndarray) or frame.ndim not in (2, 3) or frame.size == 0:
        return None
    height, width = frame.shape[:2]
    base = min(1.0, MAX_DIMENSION / max(height, width))
    for gray in (False, True):
        image = frame
        if gray and image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        for scale in _SCALES:
            factor = base * scale
            size = (max(1, int(width * factor)), max(1, int(height * factor)))
            resized = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
            ok, encoded = cv2.imencode('.png', resized, [cv2.IMWRITE_PNG_COMPRESSION, 9])
            if ok and len(encoded) <= MAX_BYTES:
                return RecoveryImage(media_type='image/png', data=encoded.tobytes(),
                                     width=size[0], height=size[1])
    return None
