"""The scan loop shares one OCR result per frame (spec P0b, P1, P2, P3)."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable

import pytest

import config
import ocr
import screens
from strategy import Shopping
from supervisor import RecoveryState
from tests.test_perception import recorded

FIXTURES = Path(__file__).parent / "fixtures"


class _Supervisor:
    current_account = "account-a"

    def __init__(self) -> None:
        self.evidence: list[dict[str, Any]] = []

    def observe(self, **evidence: Any) -> RecoveryState:
        self.evidence.append(evidence)
        return RecoveryState.READY


def _frame_reader(bot: Any, boxes: tuple[ocr.TextBox, ...], calls: list[dict]) -> Callable[..., Any]:
    """ocr.read stand-in: the recorded boxes for the bot's own frame, nothing for crops."""
    def fake(screen: Any, **kwargs: Any) -> tuple[ocr.TextBox, ...]:
        if screen is bot._screen:
            calls.append(kwargs)
            return boxes
        return ()
    return fake


@pytest.mark.parametrize("text,flags", [
    ("Online connection required", (True, False)),
    ("No internet connection", (True, False)),
    ("Your account is logged in on another device", (False, True)),
    ("New session detected", (False, True)),
    ("Wave 12", (False, False)),
])
def test_popup_flags_name_the_recovery_modals(text: str, flags: tuple[bool, bool]) -> None:
    from tower_bot import popup_flags
    assert popup_flags((ocr.TextBox(text, .99, config.Rect(0, 0, 8, 8)),)) == flags


def test_one_scan_reads_the_full_frame_once(bot_in_run_on: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = bot_in_run_on("in_run_lit")
    bot.supervisor = _Supervisor()
    bot.controls.apply({"autopilot": {"enabled": True}})
    calls: list[dict] = []
    monkeypatch.setattr(ocr, "read", _frame_reader(bot, recorded("in_run_lit"), calls))
    bot.run_once()
    assert len(calls) == 1


def test_the_supervisor_gets_the_scans_digest(bot_in_run_on: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = bot_in_run_on("in_run_lit")
    bot.supervisor = _Supervisor()
    monkeypatch.setattr(ocr, "read", _frame_reader(bot, recorded("in_run_lit"), []))
    bot.run_once()
    assert bot.supervisor.evidence[-1]["frame_digest"] == hashlib.sha256(bot._screen.tobytes()).hexdigest()


def test_the_autopilot_gets_the_scans_reads(bot_in_run_on: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = bot_in_run_on("in_run_lit")
    bot.controls.apply({"autopilot": {"enabled": True}})
    monkeypatch.setattr(ocr, "read", _frame_reader(bot, recorded("in_run_lit"), []))
    seen: dict[str, Any] = {}
    monkeypatch.setattr(bot.autopilot, "step", lambda *args, **kwargs: bool(seen.update(kwargs)))
    bot.run_once()
    assert seen["reads"].screen is bot._screen


def test_a_failed_read_still_reaches_the_supervisor_and_readers(
    bot_on_main_menu: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review focus 2 / invariant 3."""
    bot = bot_on_main_menu(Shopping())
    bot.supervisor = _Supervisor()

    def fail(screen: Any, **kwargs: Any) -> Any:
        raise RuntimeError("OCR inference failed")

    monkeypatch.setattr(ocr, "read", fail)
    got: dict[str, Any] = {}
    original = bot.missions.scan
    monkeypatch.setattr(bot.missions, "scan",
                        lambda screen, **kwargs: (got.update(kwargs), original(screen, **kwargs))[1])
    bot.run_once()
    assert bot.supervisor.evidence[-1]["readable"] is False
    assert got["boxes"] is None
    assert bot.missions.current_evidence()["error"]


def _spy_readers(bot: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    for name, reader in (("account", bot._screen_readings), ("missions", bot.missions),
                         ("milestones", bot.milestones)):
        original = reader.scan
        monkeypatch.setattr(reader, "scan", lambda *args, _name=name, _original=original, **kwargs: (
            calls.append(_name), _original(*args, **kwargs))[1])
    return calls


def test_a_readable_battle_frame_skips_the_menu_readers(
    bot_in_run_on: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = bot_in_run_on("in_run_lit")
    monkeypatch.setattr(ocr, "read", _frame_reader(bot, recorded("in_run_lit"), []))
    calls = _spy_readers(bot, monkeypatch)
    bot.run_once()
    assert calls == []
    for reader in (bot._screen_readings, bot.missions, bot.milestones):
        assert reader.current_evidence()["scanned"] is False  # no conclusion, not "clear"


def test_a_covered_panel_runs_the_menu_readers(bot_in_run_on: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The safety net: no heading (and, after P2, no heading colour) on IN_RUN."""
    bot = bot_in_run_on("in_run_lit")
    covered = bot._screen.copy()
    covered[1640:1720] = 0  # the heading bar, text and colour both gone
    bot._screen = covered
    boxes = tuple(b for b in recorded("in_run_lit") if b.text != "ATTACKUPGRADES")
    monkeypatch.setattr(ocr, "read", _frame_reader(bot, boxes, []))
    calls = _spy_readers(bot, monkeypatch)
    bot.run_once()
    assert calls == ["account", "missions", "milestones"]


def test_menu_frames_keep_every_reader(bot_on_main_menu: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = bot_on_main_menu(Shopping())
    monkeypatch.setattr(ocr, "read", _frame_reader(bot, (), []))
    calls = _spy_readers(bot, monkeypatch)
    bot.run_once()
    assert calls == ["account", "missions", "milestones"]


def test_a_walk_in_progress_keeps_every_reader(bot_in_run_on: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = bot_in_run_on("in_run_lit")
    monkeypatch.setattr(ocr, "read", _frame_reader(bot, recorded("in_run_lit"), []))
    monkeypatch.setattr(type(bot.visit), "active", property(lambda self: True))
    monkeypatch.setattr(bot.visit, "advance", lambda **kwargs: None)
    calls = _spy_readers(bot, monkeypatch)
    bot.run_once()
    assert calls == ["account", "missions", "milestones"]
