"""Killed By and ad coins from the game-over popup, persisted per run."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

import config
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
    ("game_over", ("Basic", None)),          # ad coins column not drawn
    ("game_over_newhigh", ("Basic", 0)),
])
def test_run_extras_from_recorded_popups(name: str, expected: tuple[str | None, int | None]) -> None:
    assert game_over.run_extras(parsed(name)) == expected


def test_run_extras_is_empty_without_a_reading() -> None:
    assert game_over.run_extras(None) == (None, None)


def test_run_extras_never_reports_an_unreadable_field() -> None:
    field = game_over.ResultField("killed_by", "Killed By", None, "unreadable", 0.0, None)
    reading = game_over.GameOverReading("game_over.result", 0.0, 1080, 2400, "d", (field,))
    assert game_over.run_extras(reading) == (None, None)


def test_modal_extras_swallow_ocr_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> tuple[ocr.TextBox, ...]:
        raise RuntimeError("onnx went away")
    monkeypatch.setattr(ocr, "read", boom)
    assert tower_bot.TowerBot._read_modal_extras(SimpleNamespace(screen=None)) == (None, None)
