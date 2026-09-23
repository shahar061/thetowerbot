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
