"""The battle tab from its heading bar's colour, no OCR (spec P2)."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

import battle_tab
import config

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED = {
    "in_run_attack_paused": "ATTACK", "in_run_damage_single_digit": "ATTACK",
    "in_run_defense": "DEFENSE", "in_run_defense_1920": "DEFENSE", "in_run_early": "ATTACK",
    "in_run_fast": "ATTACK", "in_run_lit": "ATTACK", "in_run_paused": "ATTACK",
    "in_run_utility": "UTILITY", "in_run_wallet": "ATTACK", "in_run_wallet_no_cutout": "ATTACK",
}
MENUS = sorted(p.stem for p in FIXTURES.glob("*.png") if not p.stem.startswith("in_run_"))


def _image(name: str) -> np.ndarray:
    image = cv2.imread(str(FIXTURES / f"{name}.png"), cv2.IMREAD_COLOR)
    assert image is not None, name
    return image


def test_every_in_run_fixture_is_named() -> None:
    assert sorted(EXPECTED) == sorted(p.stem for p in FIXTURES.glob("in_run_*.png"))


@pytest.mark.parametrize("name,tab", sorted(EXPECTED.items()))
def test_every_battle_fixture_names_its_tab(name: str, tab: str) -> None:
    assert battle_tab.classify_frame(_image(name)) == tab


@pytest.mark.parametrize("name", MENUS)
def test_no_menu_fixture_has_a_tab(name: str) -> None:
    assert battle_tab.classify_frame(_image(name)) is None


def _band(hues: list[int]) -> np.ndarray:
    """A 1080x80 HSV band split evenly between `hues`, saturated and bright, as BGR."""
    hsv = np.full((80, 1080, 3), 255, np.uint8)
    for i, hue in enumerate(hues):
        hsv[:, i * 1080 // len(hues):(i + 1) * 1080 // len(hues), 0] = hue
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def test_a_red_band_straddling_hue_zero_is_defense() -> None:
    """Review focus 4: a plain median of 178s and 2s is 90, which is not red."""
    band = _band([178, 2])
    assert battle_tab.classify(band, config.Rect(0, 0, 1080, 80)) == "DEFENSE"


def test_a_band_with_too_few_saturated_pixels_has_no_tab() -> None:
    band = _band([97])
    band[:, : int(1080 * 0.6)] = 40  # dark grey: unsaturated
    assert battle_tab.classify(band, config.Rect(0, 0, 1080, 80)) is None


def test_a_hue_outside_every_range_has_no_tab() -> None:
    assert battle_tab.classify(_band([60]), config.Rect(0, 0, 1080, 80)) is None


def test_an_unmeasured_frame_size_has_no_tab() -> None:
    """Review focus 5."""
    frame = cv2.resize(_image("in_run_lit"), (1080, 2340))
    assert battle_tab.classify_frame(frame) is None
