"""Shared real Cards captures and explicit OCR mutation helpers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import config
import ocr

FIXTURES = Path(__file__).parent / 'fixtures'


def recorded(name: str) -> tuple[ocr.TextBox, ...]:
    return tuple(ocr.TextBox(b['text'], b['confidence'], config.Rect(*b['rect']))
                 for b in json.loads((FIXTURES / 'ocr' / f'{name}.json').read_text()))


def frame(name: str) -> Any:
    image = cv2.imread(str(FIXTURES / f'{name}.png'))
    assert image is not None, f'missing fixture: {name}.png'
    return image


def replaced(boxes: tuple[ocr.TextBox, ...], text: str,
             *, into: str, confidence: float | None = None) -> tuple[ocr.TextBox, ...]:
    """The recorded boxes with one box's text (and confidence) swapped.

    Geometry is kept exactly where it was measured, so a case only ever
    changes what the page says - never where the reader believes it sits.
    """
    return tuple(ocr.TextBox(into, confidence if confidence is not None else b.confidence, b.rect)
                 if b.text == text else b for b in boxes)
