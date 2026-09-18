"""The reroll worker claims only the measured first Workshop coin grant."""

from __future__ import annotations

import numpy as np
import cv2
import json
from pathlib import Path

from config import Rect
from fleet.tutorial import workshop_coin_claim
from ocr import TextBox


def _boxes() -> tuple[TextBox, ...]:
    return (
        TextBox("WORKSHOP", .994, Rect(372, 511, 337, 49)),
        TextBox("Spend coins to permanently increase", .991,
                Rect(156, 845, 773, 47)),
        TextBox("Here are some additional coins to get", .973,
                Rect(150, 1189, 782, 52)),
        TextBox("50", 1., Rect(530, 1350, 77, 59)),
        TextBox("CLAIM", .997, Rect(472, 1544, 139, 43)),
    )


def test_measured_workshop_coin_grant_claim() -> None:
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    assert workshop_coin_claim(frame, _boxes()) == (541, 1565)


def test_bluestacks_native_coin_grant_uses_observed_claim() -> None:
    root = Path(__file__).parent / "fixtures"
    frame = cv2.imread(str(root / "workshop_coin_grant_bluestacks_1920.png"))
    rows = json.loads((root / "ocr" /
                       "workshop_coin_grant_bluestacks_1920.json").read_text())
    boxes = tuple(TextBox(row["text"], row["confidence"], Rect(*row["rect"]))
                  for row in rows)
    assert workshop_coin_claim(frame, boxes) == (541, 1326)


def test_unmeasured_or_ambiguous_claim_is_held() -> None:
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    boxes = _boxes()
    assert workshop_coin_claim(frame, boxes[:-1]) is None
    assert workshop_coin_claim(frame, (*boxes, boxes[-1])) is None
    assert workshop_coin_claim(frame, (*boxes[:-1], TextBox(
        "CLAIM", .5, boxes[-1].rect))) is None
    assert workshop_coin_claim(frame[:2000], boxes) is None
