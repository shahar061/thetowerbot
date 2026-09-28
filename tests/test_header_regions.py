"""The menu header's coin and gem balances, against committed captures.

Replaces the glyph-atlas tests these regions used to have. The regions
themselves are unchanged and still production config - only what reads them
changed, from a harvested atlas to OCR (see shopping.header_numbers). The
coverage worth keeping was never about the atlas: it is that each page's two
regions land on that page's two numbers, anchored to that page's own anchor
rather than to absolute pixels, because the three pages put their anchors in
three different places.

The expected balances are the same hand-verified values the atlas tests
asserted, so a regression in the regions still fails here.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest

import config
import digits
import pages
import shopping
import vision

FIXTURES = Path(__file__).parent / "fixtures"

# fixture -> page, coins, gems. Hand-counted off each capture.
HEADER_CASES: dict[str, tuple[str, int, int]] = {
    "menu_main": ("MAIN_MENU", 78, 0),
    "menu_workshop_attack": ("WORKSHOP", 1770, 40),
    "menu_workshop_defense": ("WORKSHOP", 1770, 40),
    "menu_workshop_utility": ("WORKSHOP", 1770, 40),
    # Spending the whole wallet leaves a lone "0", which reads below .9.
    "menu_workshop_zero_coins": ("WORKSHOP", 0, 54),
    "menu_cards": ("CARDS", 78, 40),
}


@pytest.fixture(scope="module")
def cache() -> vision.TemplateCache:
    return vision.TemplateCache(config.TEMPLATE_DIR)


def frame(name: str):
    img = cv2.imread(str(FIXTURES / f"{name}.png"), cv2.IMREAD_COLOR)
    assert img is not None, f"missing fixture: {name}.png"
    return img


@pytest.mark.parametrize("fixture,case", HEADER_CASES.items())
def test_header_regions_land_on_their_page_s_numbers(
    fixture: str, case: tuple[str, int, int], cache: vision.TemplateCache
) -> None:
    page, expected_coins, expected_gems = case
    screen = frame(fixture)
    _, top_left = vision.best_score(screen, cache.get(config.PAGE_ANCHORS[page]))

    assert shopping.header_numbers(screen, page, top_left) == (
        expected_coins,
        expected_gems,
    )


def test_every_page_with_a_header_is_covered() -> None:
    """A page that grows header regions without a case here would read
    nothing and abort every visit to it, silently."""
    assert set(config.HEADER_REGIONS) == {page for page, _, _ in HEADER_CASES.values()}


def test_the_regions_stay_inside_the_frame(cache: vision.TemplateCache) -> None:
    """digits.crop returns None for a region that hangs off the capture, and
    ocr.read_region returns no boxes for one - both silent."""
    for fixture, (page, _, _) in HEADER_CASES.items():
        screen = frame(fixture)
        _, top_left = vision.best_score(screen, cache.get(config.PAGE_ANCHORS[page]))
        for region in config.HEADER_REGIONS[page]:
            assert digits.crop(screen, region, top_left) is not None, (
                f"{fixture}: a header region fell outside the frame"
            )


# --- the binarisation threshold hook --------------------------------------


def test_every_size_class_now_takes_the_shared_default() -> None:
    """`header` was the only class that ever overrode it, and it is gone.

    Kept as a test rather than deleted with it: the hook is still wired into
    digits.read and build_atlas.dump_glyphs, and an override added without
    measuring it is exactly the mistake the 140 default protects against.
    """
    assert config.DIGIT_BINARY_THRESHOLDS == {}
    for size_class in digits.ALL_SIZE_CLASSES + ("nonsense",):
        assert digits.threshold_for(size_class) == config.DIGIT_BINARY_THRESHOLD


def test_the_header_atlas_class_is_gone() -> None:
    """Guards against it being reintroduced by half - a class in the tuple
    with no atlas on disk reads None for every balance."""
    assert "header" not in digits.ALL_SIZE_CLASSES
    assert not (config.ATLAS_DIR / "header").exists()
