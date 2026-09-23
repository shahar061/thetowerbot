"""ocr.FrameReads: one scan's OCR, shared by every reader (spec P0b-P3)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest

import config
import ocr

MENU = cv2.imread(str(Path(__file__).parent / "fixtures" / "menu_main.png"), cv2.IMREAD_COLOR)


def _counting_reader(calls: list[dict], boxes=()):
    def fake(screen, **kwargs):
        calls.append({"shape": screen.shape, **kwargs})
        return boxes
    return fake


def test_full_reads_the_frame_once_and_strictly(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    box = ocr.TextBox("Wave 1", .99, config.Rect(1, 2, 3, 4))
    monkeypatch.setattr(ocr, "read", _counting_reader(calls, (box,)))
    reads = ocr.FrameReads(np.zeros((2400, 1080, 3), np.uint8))
    assert reads.full() == (box,)
    assert reads.full() == (box,)
    assert len(calls) == 1 and calls[0]["strict"] is True


def test_a_failed_full_read_raises_to_every_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review focus 2 / invariant 3: the failure is remembered, not swallowed."""
    calls: list[int] = []

    def fail(screen, **kwargs):
        calls.append(1)
        raise RuntimeError("OCR inference failed")

    monkeypatch.setattr(ocr, "read", fail)
    reads = ocr.FrameReads(np.zeros((2400, 1080, 3), np.uint8))
    for _ in range(3):
        with pytest.raises(RuntimeError):
            reads.full()
    assert calls == [1]


def test_digest_is_the_frames_sha256_computed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = np.arange(2400 * 1080 * 3, dtype=np.uint32).astype(np.uint8).reshape(2400, 1080, 3)
    expected = hashlib.sha256(frame.tobytes()).hexdigest()
    reads = ocr.FrameReads(frame)
    assert reads.digest == expected
    monkeypatch.setattr(ocr.hashlib, "sha256", lambda *_: pytest.fail("hashed twice"))
    assert reads.digest == expected


def test_a_band_box_lands_at_its_full_frame_position() -> None:
    box = ocr.TextBox("x1.5", .99, config.Rect(100, 100, 30, 40))
    band = config.Rect(0, 1320, 1080, 1080)
    mapped = ocr.band_box_to_frame(box, band, 1080 / 896, 1080 / 896)
    assert mapped == ocr.TextBox("x1.5", .99, config.Rect(121, 1441, 36, 48))


def _expected_band_box(box: ocr.TextBox, band: config.Rect) -> ocr.TextBox:
    """Where `box`, read off `band` resized by BATTLE_OCR_SCALE, belongs in
    the frame - worked from the scale, not from the current band sizes."""
    small_w = round(band.w * config.BATTLE_OCR_SCALE)
    small_h = round(band.h * config.BATTLE_OCR_SCALE)
    sx, sy = band.w / small_w, band.h / small_h
    r = box.rect
    return ocr.TextBox(box.text, box.confidence, config.Rect(
        band.x + round(r.x * sx), band.y + round(r.y * sy), round(r.w * sx), round(r.h * sy)))


def test_battle_reads_both_bands_small_and_maps_them_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    box = ocr.TextBox("T", .99, config.Rect(100, 100, 30, 40))
    monkeypatch.setattr(ocr, "read", _counting_reader(calls, (box,)))
    reads = ocr.FrameReads(np.zeros((2400, 1080, 3), np.uint8))
    bands = config.BATTLE_BANDS[(1080, 2400)]
    assert reads.battle() == (_expected_band_box(box, bands.top),
                              _expected_band_box(box, bands.panel))
    assert reads.battle() is reads.battle()
    assert [c["shape"] for c in calls] == [
        (round(b.h * config.BATTLE_OCR_SCALE), round(b.w * config.BATTLE_OCR_SCALE), 3)
        for b in (bands.top, bands.panel)]
    assert all(c["strict"] is True and c["upscale"] is False for c in calls)


def test_battle_maps_band_boxes_back_whatever_the_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mapping follows the scale math, not one lucky band size."""
    box = ocr.TextBox("T", .99, config.Rect(57, 211, 83, 19))
    monkeypatch.setattr(ocr, "read", _counting_reader([], (box,)))
    for scale in (0.5, 0.71, 0.83):
        monkeypatch.setattr(config, "BATTLE_OCR_SCALE", scale)
        bands = config.BATTLE_BANDS[(1080, 1920)]
        reads = ocr.FrameReads(np.zeros((1920, 1080, 3), np.uint8))
        assert reads.battle() == (_expected_band_box(box, bands.top),
                                  _expected_band_box(box, bands.panel))


def test_an_unmeasured_frame_size_falls_back_to_the_full_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review focus 5."""
    calls: list[dict] = []
    box = ocr.TextBox("T", .99, config.Rect(100, 100, 30, 40))
    monkeypatch.setattr(ocr, "read", _counting_reader(calls, (box,)))
    reads = ocr.FrameReads(np.zeros((2340, 1080, 3), np.uint8))
    assert reads.battle() == reads.full() == (box,)
    assert [c["shape"] for c in calls] == [(2340, 1080, 3)]


def test_a_failed_band_read_is_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review focus 2."""
    calls: list[int] = []

    def fail(screen, **kwargs):
        calls.append(1)
        raise RuntimeError("OCR inference failed")

    monkeypatch.setattr(ocr, "read", fail)
    reads = ocr.FrameReads(np.zeros((2400, 1080, 3), np.uint8))
    for _ in range(2):
        with pytest.raises(RuntimeError):
            reads.battle()
    assert calls == [1]


def _full(frame, now: float, reuse: bool = True):
    return ocr.FrameReads(frame, reuse=reuse, clock=lambda: now).full()


def test_an_unchanged_menu_frame_reuses_the_last_read(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(ocr, "read", _counting_reader(calls, ("boxes",)))
    assert _full(MENU, 100.0) == ("boxes",)
    assert _full(MENU.copy(), 105.0) == ("boxes",)
    assert len(calls) == 1


def test_a_popup_pasted_over_one_region_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(ocr, "read", _counting_reader(calls))
    _full(MENU, 100.0)
    popup = MENU.copy()
    popup[1000:1100, 400:600] = 255
    _full(popup, 101.0)
    assert len(calls) == 2


def test_a_stored_read_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(ocr, "read", _counting_reader(calls))
    _full(MENU, 100.0)
    _full(MENU, 100.0 + config.OCR_REUSE_MAX_AGE)
    assert len(calls) == 2


def test_a_different_shape_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(ocr, "read", _counting_reader(calls))
    _full(MENU, 100.0)
    _full(cv2.resize(MENU, (1080, 1920)), 101.0)
    assert len(calls) == 2


def test_a_different_reader_never_reuses_a_stored_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review focus 1: a replaced ocr.read (another test, another engine) starts fresh."""
    first: list[dict] = []
    second: list[dict] = []
    monkeypatch.setattr(ocr, "read", _counting_reader(first, ("old",)))
    _full(MENU, 100.0)
    monkeypatch.setattr(ocr, "read", _counting_reader(second, ("new",)))
    assert _full(MENU, 101.0) == ("new",)


def test_battle_frames_never_store_or_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(ocr, "read", _counting_reader(calls))
    _full(MENU, 100.0, reuse=False)
    _full(MENU, 101.0, reuse=False)
    assert len(calls) == 2 and ocr._last_full is None


def test_a_failed_read_is_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(screen, **kwargs):
        raise RuntimeError("OCR inference failed")

    monkeypatch.setattr(ocr, "read", fail)
    with pytest.raises(RuntimeError):
        _full(MENU, 100.0)
    assert ocr._last_full is None
