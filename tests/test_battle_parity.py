"""Spec P2 parity: the two-band battle read parses as the full-frame read does.

Real engine. Each pipeline starts from empty OCR caches: RapidOCR batches
recognition crops, and a cached crop keeps its first batch's answer, so a
warm cache would make the result depend on test order.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from pathlib import Path

import cv2
import pytest

import ocr
import screens
from perception import observe_frame
from tests.parity import observation_diff

FIXTURES = Path(__file__).parent / "fixtures"
IN_RUN = sorted(p.stem for p in FIXTURES.glob("in_run_*.png"))
# Fields the full-frame read leaves unknown that the band read fills, pinned
# as measured when P2 was planned. Anything else - in either direction -
# fails. Do not re-pin to make this pass: stop and report instead.
KNOWN_GAINS: dict[str, set[str]] = {
    "in_run_wallet_no_cutout": {"rows.attack_speed.price", "rows.attack_speed.status",
                                "rows.attack_speed.tap"},
}


def cold_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_frame_results", "_region_results", "_crop_results"):
        monkeypatch.setattr(ocr, name, OrderedDict())


@pytest.mark.parametrize("name", IN_RUN)
def test_two_band_battle_read_parses_what_the_full_frame_read_does(
    name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = cv2.imread(str(FIXTURES / f"{name}.png"), cv2.IMREAD_COLOR)
    cold_ocr(monkeypatch)
    old = observe_frame(frame, "battle")
    cold_ocr(monkeypatch)
    reads = ocr.FrameReads(frame)
    new = observe_frame(frame, "battle", reads=reads)
    assert reads._full is None, "the battle path must not fall back to the full read"
    changes, gains = observation_diff(old, new)
    assert changes == set()
    assert gains == KNOWN_GAINS.get(name, set())
