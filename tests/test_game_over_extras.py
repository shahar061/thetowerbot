"""Killed By, ad coins and OCR'd coins earned from the game-over popup, persisted per run."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

import config
import events
import game_over
import ocr
import tower_bot

FIXTURES = Path(__file__).parent / "fixtures"


def recorded(name: str) -> tuple[ocr.TextBox, ...]:
    return tuple(ocr.TextBox(b["text"], b["confidence"], config.Rect(*b["rect"]))
                 for b in json.loads((FIXTURES / "ocr" / f"{name}.json").read_text()))


def parsed(name: str) -> game_over.GameOverReading | None:
    return game_over.parse_frame(cv2.imread(str(FIXTURES / f"{name}.png")), recorded(name), now=1_700_000_000.0)


@pytest.mark.parametrize(("name", "expected"), [
    ("game_over", ("Basic", None, None)),    # ad coins column not drawn
    ("game_over_newhigh", ("Basic", 0, None)),  # coins OCR as "21 (" - refused
])
def test_run_extras_from_recorded_popups(name: str, expected: tuple[str | None, int | None, int | None]) -> None:
    assert game_over.run_extras(parsed(name)) == expected


def test_run_extras_is_empty_without_a_reading() -> None:
    assert game_over.run_extras(None) == (None, None, None)


def test_run_extras_never_reports_an_unreadable_field() -> None:
    field = game_over.ResultField("killed_by", "Killed By", None, "unreadable", 0.0, None)
    reading = game_over.GameOverReading("game_over.result", 0.0, 1080, 2400, "d", (field,))
    assert game_over.run_extras(reading) == (None, None, None)


def test_modal_extras_swallow_ocr_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> tuple[ocr.TextBox, ...]:
        raise RuntimeError("onnx went away")
    monkeypatch.setattr(ocr, "read", boom)
    assert tower_bot.TowerBot._read_modal_extras(SimpleNamespace(screen=None)) == (None, None, None)


def test_read_modal_stats_puts_killed_by_and_ad_coins_onto_run_ended() -> None:
    """The seam between the glyph-read reader and the OCR'd extras: wave,
    coins and tier come from `reader`, killed_by and ad_coins from
    `_read_modal_extras` - both must land on the returned RunEnded."""
    reader = SimpleNamespace(
        read=lambda *a, **k: 11,
        read_at_caption=lambda *a, **k: 80,
    )
    fake_self = SimpleNamespace(
        screen=None, reader=reader,
        _read_modal_extras=lambda: ("Tank", 7, 99),
    )
    ended = events.RunEnded(run_id=1, duration=60.0)

    result = tower_bot.TowerBot._read_modal_stats(fake_self, ended, (0, 0))

    assert (result.wave, result.coins, result.tier) == (11, 80, 80)
    assert (result.killed_by, result.ad_coins) == ("Tank", 7)


def test_run_extras_reads_thousands_coins_earned() -> None:
    """The glyph atlas has no "." or "K", so a four-digit run's coins only
    survive through OCR - "1.21K" must come back as 1210, not None."""
    field = game_over.ResultField("coins_earned", "Coins earned", "1.21K", "observed", 0.9, None)
    reading = game_over.GameOverReading("game_over.result", 0.0, 1080, 2400, "d", (field,))
    assert game_over.run_extras(reading) == (None, None, 1210)


def test_read_modal_stats_falls_back_to_ocr_coins_when_glyphs_refuse() -> None:
    """A 1.21K coins line has glyphs the modal atlas cannot match, so the
    glyph reader returns None; the OCR'd count must fill the run's coins."""
    reader = SimpleNamespace(
        read=lambda *a, **k: 64,
        read_at_caption=lambda screen, caption, *a, **k: None if caption == config.MODAL_COINS_CAPTION else 1,
    )
    fake_self = SimpleNamespace(
        screen=None, reader=reader,
        _read_modal_extras=lambda: ("Basic", 0, 1210),
    )
    ended = events.RunEnded(run_id=1, duration=60.0)

    result = tower_bot.TowerBot._read_modal_stats(fake_self, ended, (0, 0))

    assert (result.wave, result.coins, result.tier) == (64, 1210, 1)



def test_thousands_coins_earned_from_a_recorded_popup() -> None:
    """The fleet's BlueStacks popup reads its coins as a clean count ("5").
    The same popup at 1.21K coins must yield 1210 - the value the glyph
    reader cannot produce."""
    boxes = tuple(ocr.TextBox("1.21K", b.confidence, b.rect) if b.text == "5" else b
                  for b in recorded("game_over_bluestacks_1920"))
    frame = cv2.imread(str(FIXTURES / "game_over_bluestacks_1920.png"))
    assert game_over.run_extras(game_over.parse_frame(frame, boxes, now=1_700_000_000.0))[2] == 1210
