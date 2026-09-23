"""Spec P3: when two menu frames count as unchanged."""
from __future__ import annotations

import itertools
from pathlib import Path

import cv2
import numpy as np
import pytest

import config
import ocr

FIXTURES = Path(__file__).parent / "fixtures"
MENUS = sorted(p for p in FIXTURES.glob("*.png") if not p.stem.startswith("in_run_"))


def _image(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    assert image is not None, path
    return image


def test_every_pair_of_distinct_menu_fixtures_differs_by_more_than_the_threshold() -> None:
    """The threshold is chosen from the fixtures: measured minimum 80
    (main_menu_resume vs menu_milestones_entry), threshold 12."""
    thumbs = {path.stem: ocr.thumbnail(_image(path)) for path in MENUS}
    same_shape = [(a, b) for a, b in itertools.combinations(thumbs, 2)
                  if thumbs[a].shape == thumbs[b].shape]
    assert len(same_shape) > 100
    matched = [(a, b) for a, b in same_shape if ocr.thumbnails_match(thumbs[a], thumbs[b])]
    assert matched == []


def test_a_changed_digit_is_a_change() -> None:
    frame = _image(FIXTURES / "menu_main.png")
    before, after = frame.copy(), frame.copy()
    cv2.putText(before, "12", (500, 1200), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (255, 255, 255), 3)
    cv2.putText(after, "13", (500, 1200), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (255, 255, 255), 3)
    assert not ocr.thumbnails_match(ocr.thumbnail(before), ocr.thumbnail(after))


def test_capture_noise_is_not_a_change() -> None:
    frame = _image(FIXTURES / "menu_main.png")
    noisy = np.clip(frame.astype(np.int16) + 3, 0, 255).astype(np.uint8)
    assert ocr.thumbnails_match(ocr.thumbnail(frame), ocr.thumbnail(noisy))


def test_different_shapes_never_match() -> None:
    frame = _image(FIXTURES / "menu_main.png")
    assert not ocr.thumbnails_match(ocr.thumbnail(frame), ocr.thumbnail(frame[:1920]))


def test_the_reuse_settings_have_their_spec_defaults() -> None:
    assert config.OCR_REUSE_DIFF == 12
    assert config.OCR_REUSE_MAX_AGE == pytest.approx(10.0)
