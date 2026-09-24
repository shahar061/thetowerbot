"""The OCR reader: number parsing, and a few real reads."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import cv2
import numpy as np
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


# --- exact-bytes caches ----------------------------------------------------


def _fake_engine(calls: list[int]):
    def engine(image):
        calls.append(int(image[0, 0, 0]))
        return [([[0, 0], [100, 0], [100, 40], [0, 40]], "READ", .99)], None
    return engine


def test_a_frame_read_twice_runs_the_engine_once(monkeypatch) -> None:
    """A tick hands one frame to several readers; only the first pays."""
    calls: list[int] = []
    monkeypatch.setattr(ocr, "_engine_or_none", lambda engine=_fake_engine(calls): engine)
    monkeypatch.setattr(ocr, "_frame_results", type(ocr._frame_results)())
    monkeypatch.setattr(ocr, "_region_results", type(ocr._frame_results)())
    frame = np.full((50, 50, 3), 7, np.uint8)

    assert [b.text for b in ocr.read(frame)] == ["READ"]
    assert [b.text for b in ocr.read(frame.copy())] == ["READ"]
    assert calls == [7]

    ocr.read(np.full((50, 50, 3), 8, np.uint8))
    assert calls == [7, 8], "different pixels must reach the engine"


def test_a_swapped_engine_does_not_serve_the_old_engines_answer(monkeypatch) -> None:
    monkeypatch.setattr(ocr, "_frame_results", type(ocr._frame_results)())
    monkeypatch.setattr(ocr, "_region_results", type(ocr._frame_results)())
    frame = np.full((50, 50, 3), 7, np.uint8)
    first: list[int] = []
    second: list[int] = []
    monkeypatch.setattr(ocr, "_engine_or_none", lambda engine=_fake_engine(first): engine)
    ocr.read(frame)
    monkeypatch.setattr(ocr, "_engine_or_none", lambda engine=_fake_engine(second): engine)
    ocr.read(frame)
    assert first == [7] and second == [7]


def test_recognition_only_runs_on_crops_it_has_not_seen(monkeypatch) -> None:
    monkeypatch.setattr(ocr, "_crop_results", type(ocr._crop_results)())
    seen: list[list[int]] = []

    def recognize(crops, return_word_box=False):
        seen.append([int(c[0, 0]) for c in crops])
        return [(f"t{int(c[0, 0])}", .99) for c in crops], 0.1

    engine = types.SimpleNamespace(text_rec=recognize)
    ocr._cache_recognition(engine)
    crop = lambda value: np.full((8, 8), value, np.uint8)  # noqa: E731

    assert engine.text_rec([crop(1), crop(2)])[0] == [("t1", .99), ("t2", .99)]
    results, _ = engine.text_rec([crop(2), crop(3), crop(1)])
    assert results == [("t2", .99), ("t3", .99), ("t1", .99)], "input order kept"
    assert seen == [[1, 2], [3]]


def test_a_batch_larger_than_the_crop_cache_still_answers_every_crop(monkeypatch) -> None:
    """Eviction mid-call must not lose a crop this same call still returns."""
    monkeypatch.setattr(ocr, "_crop_results", type(ocr._crop_results)())
    monkeypatch.setattr(config, "OCR_CROP_CACHE", 2)
    engine = types.SimpleNamespace(
        text_rec=lambda crops, return_word_box=False: ([(str(int(c[0, 0])), 1.) for c in crops], 0.))
    ocr._cache_recognition(engine)

    results, _ = engine.text_rec([np.full((4, 4), v, np.uint8) for v in range(5)])
    assert [text for text, _ in results] == ["0", "1", "2", "3", "4"]
    assert len(ocr._crop_results) == 2


# --- detector upscaling (spec P0a) -------------------------------------------


def _det_engine(seen: list[str]):
    """A fake engine with RapidOCR's detector attribute; records its mode."""
    detector = types.SimpleNamespace(limit_type="min")

    def engine(image):
        seen.append(detector.limit_type)
        return [([[0, 0], [100, 0], [100, 40], [0, 40]], "READ", .99)], None

    engine.text_det = detector
    return engine


def test_a_read_that_opts_out_runs_the_detector_without_upscaling(monkeypatch) -> None:
    seen: list[str] = []
    engine = _det_engine(seen)
    monkeypatch.setattr(ocr, "_engine_or_none", lambda: engine)
    monkeypatch.setattr(ocr, "_frame_results", type(ocr._frame_results)())
    monkeypatch.setattr(ocr, "_region_results", type(ocr._frame_results)())
    monkeypatch.setattr(config, "OCR_DET_UPSCALE", False)
    ocr.read(np.full((50, 50, 3), 3, np.uint8), upscale=False)
    ocr.read(np.full((50, 50, 3), 4, np.uint8))
    assert seen == ["max", "min"]


def test_each_detector_mode_is_its_own_cache_entry(monkeypatch) -> None:
    """Review focus 1: a result detected one way is never served for the other."""
    seen: list[str] = []
    engine = _det_engine(seen)
    monkeypatch.setattr(ocr, "_engine_or_none", lambda: engine)
    monkeypatch.setattr(ocr, "_frame_results", type(ocr._frame_results)())
    monkeypatch.setattr(ocr, "_region_results", type(ocr._frame_results)())
    monkeypatch.setattr(config, "OCR_DET_UPSCALE", False)
    frame = np.full((50, 50, 3), 5, np.uint8)
    ocr.read(frame, upscale=False)
    ocr.read(frame)
    ocr.read(frame, upscale=False)
    ocr.read(frame)
    assert seen == ["max", "min"]


def test_the_config_switch_restores_upscaling_for_every_read(monkeypatch) -> None:
    seen: list[str] = []
    engine = _det_engine(seen)
    monkeypatch.setattr(ocr, "_engine_or_none", lambda: engine)
    monkeypatch.setattr(ocr, "_frame_results", type(ocr._frame_results)())
    monkeypatch.setattr(ocr, "_region_results", type(ocr._frame_results)())
    monkeypatch.setattr(config, "OCR_DET_UPSCALE", True)
    ocr.read(np.full((50, 50, 3), 6, np.uint8), upscale=False)
    assert seen == ["min"]


def test_crop_reads_cannot_evict_a_full_frame(monkeypatch) -> None:
    """Spec P0c: a scan's title, cash and re-read crops left the full frame
    to be detected again. Crops now live in their own LRU."""
    calls: list[int] = []
    monkeypatch.setattr(ocr, "_engine_or_none", lambda engine=_fake_engine(calls): engine)
    monkeypatch.setattr(ocr, "_frame_results", type(ocr._frame_results)())
    monkeypatch.setattr(ocr, "_region_results", type(ocr._frame_results)())
    frame = np.full((2400, 1080, 3), 1, np.uint8)
    ocr.read(frame)
    for value in range(2, 2 + config.OCR_FRAME_CACHE * 3):
        ocr.read(np.full((150, 410, 3), value, np.uint8))
    ocr.read(frame)
    assert calls.count(1) == 1
    assert len(ocr._frame_results) == 1
