"""The OCR reader: number parsing, and a few real reads."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import cv2
import pytest

import config
import ocr
import pages
import vision

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30", 30),
        ("$10", 10),
        ("1.77K", 1770),
        ("2M", 2_000_000),
        ("1.5T", 1_500_000_000_000),
        ("0", 0),
        # Regression: float(digits) * multiplier is not always exact -
        # 2.01 * 1000 lands at 2009.9999999999998, which int() truncates to
        # 2009. round() is required to get the correct 2010.
        ("2.01K", 2010),
    ],
)
def test_parses_a_number(text, expected):
    assert ocr.parse_number(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "68 $",        # symbol on the wrong side - seen on the in-run wallet
        "Damage",
        "",
        "1.2.3",
        "50 coins",
        "x1.20",       # a stat value, not a price
        "0.00/sec",
    ],
)
def test_refuses_anything_else(text):
    """Refuse rather than guess: a partial parse of a stat value would
    report a wrong price, which is the failure this whole rule prevents."""
    assert ocr.parse_number(text) is None


def test_reads_the_workshop_labels():
    """Real engine, real fixture. Slow - kept to one test on purpose."""
    screen = cv2.imread(str(FIXTURES / "menu_workshop_attack.png"))
    found = {box.text for box in ocr.read(screen)}
    for label in ("Damage", "Critical", "Chance", "Factor", "Unlock Range Upgrades"):
        assert label in found


def test_drops_boxes_below_the_confidence_floor():
    screen = cv2.imread(str(FIXTURES / "menu_workshop_utility.png"))
    for box in ocr.read(screen):
        assert box.confidence >= config.OCR_CONFIDENCE_FLOOR


def test_a_frame_it_cannot_read_returns_empty_not_an_exception():
    assert ocr.read(None) == ()


def test_a_failed_engine_build_is_not_retried(monkeypatch):
    """A broken wheel must cost one traceback, not one per scan.

    There is no startup gate until Phase 2, so a retry-forever build would
    write a traceback into the log every couple of seconds - the same log
    the Phase 1 A/B evidence is read out of.
    """
    builds = 0

    class Broken(types.ModuleType):
        def __getattr__(self, name):
            nonlocal builds
            builds += 1
            raise ImportError("no wheel for this platform")

    # monkeypatch restores both, so a real engine built by the test above
    # survives this one.
    monkeypatch.setattr(ocr, "_engine", None)
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", Broken("rapidocr_onnxruntime"))

    assert ocr._engine_or_none() is None
    assert ocr._engine_or_none() is None
    assert ocr._engine_or_none() is None
    assert builds == 1, "construction must be attempted once, not once per scan"


# --- the startup gate ------------------------------------------------------


def test_reports_the_engine_as_unavailable_once_construction_has_failed(monkeypatch):
    """The startup gate build_shopping() checks. _FAILED is the sentinel for
    "tried and could not", so available() must read it as a hard no rather
    than retrying the build it already gave up on."""
    monkeypatch.setattr(ocr, "_engine", ocr._FAILED)
    assert ocr.available() is False


def test_reports_the_engine_as_available_once_it_is_built(monkeypatch):
    monkeypatch.setattr(ocr, "_engine", object())
    assert ocr.available() is True


# --- a number inside a region ----------------------------------------------


def box(text: str, rect: tuple[int, int, int, int]) -> ocr.TextBox:
    return ocr.TextBox(text=text, confidence=0.99, rect=config.Rect(*rect))


REGION = config.Rect(85, 152, 170, 68)


def test_reads_the_number_whose_box_sits_in_the_region():
    """The coin balance off the workshop header, as recorded from the real
    engine against menu_workshop_attack.png."""
    boxes = (box("1.77K", (90, 164, 130, 46)),)
    assert ocr.number_in(boxes, REGION) == 1770


def test_ignores_a_box_whose_centre_falls_outside_the_region():
    """The gem balance is a second number on the same header row; a coin
    region that swept it up would report gems as coins."""
    boxes = (box("40", (441, 162, 76, 50)),)
    assert ocr.number_in(boxes, REGION) is None


def test_refuses_when_the_only_box_in_the_region_is_not_a_number():
    boxes = (box("WORKSHOP", (90, 164, 130, 46)),)
    assert ocr.number_in(boxes, REGION) is None


def test_refuses_when_two_numbers_share_the_region():
    """Two candidates is ambiguity, and this module refuses rather than
    guesses - picking one would be picking a balance by luck."""
    boxes = (box("1.77K", (90, 164, 60, 46)), box("40", (200, 164, 40, 46)))
    assert ocr.number_in(boxes, REGION) is None


def test_refuses_when_nothing_was_read_at_all():
    assert ocr.number_in((), REGION) is None

# --- read_region ----------------------------------------------------------


def test_read_region_finds_a_number_a_whole_frame_read_misses() -> None:
    """The reason read_region exists.

    The engine's DETECTION stage, not its recognition, is what fails here: a
    lone "0" in the gems header produces no box anywhere in a full frame, at
    any confidence floor including 0.0. Cropped and padded it reads cleanly.
    """
    screen = cv2.imread(str(FIXTURES / "menu_main.png"), cv2.IMREAD_COLOR)
    assert screen is not None
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    reading = pages.classify_page(screen, cache)
    _coins, gems = config.HEADER_REGIONS[reading.page]
    rect = config.Rect(
        reading.top_left[0] + gems.dx, reading.top_left[1] + gems.dy, gems.w, gems.h
    )

    whole_frame = [
        box for box in ocr.read(screen, min_confidence=0.0)
        if rect.x <= box.rect.x <= rect.x + rect.w
        and rect.y <= box.rect.y <= rect.y + rect.h
    ]
    assert whole_frame == [], "premise changed: the frame read now finds it"

    assert [box.text for box in ocr.read_region(screen, rect)] == ["0"]


def test_read_region_off_screen_is_empty_not_an_error() -> None:
    """Same rule as read(): degrade the bot, never stop the scan loop."""
    screen = cv2.imread(str(FIXTURES / "menu_main.png"), cv2.IMREAD_COLOR)
    assert ocr.read_region(screen, config.Rect(-500, -500, 100, 100)) == ()
    assert ocr.read_region(screen, config.Rect(9_000, 9_000, 100, 100)) == ()
    assert ocr.read_region(None, config.Rect(0, 0, 100, 100)) == ()


def test_read_region_clamps_a_rect_that_hangs_off_the_edge() -> None:
    """Partially off-screen is readable, not refused - only wholly off is."""
    screen = cv2.imread(str(FIXTURES / "menu_main.png"), cv2.IMREAD_COLOR)
    height, width = screen.shape[:2]
    hanging = config.Rect(width - 50, height - 50, 400, 400)
    ocr.read_region(screen, hanging)  # must not raise
