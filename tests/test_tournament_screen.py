from __future__ import annotations

import json
from pathlib import Path
import cv2
import pytest
import config
import ocr

ROOT = Path(__file__).parent / "fixtures"


def recorded(name: str) -> tuple[object, tuple[ocr.TextBox, ...]]:
    frame = cv2.imread(str(ROOT / "tournament" / f"{name}.png"))
    boxes = tuple(ocr.TextBox(b["text"], b["confidence"], config.Rect(*b["rect"]))
                  for b in json.loads((ROOT / "ocr" / "tournament" / f"{name}.json").read_text()))
    return frame, boxes


@pytest.mark.parametrize("name,tickets", [("tournament_join_ticket_1", 1),
                                         ("tournament_leaderboard_ticket_0", 0)])
def test_real_ticket_page(name: str, tickets: int) -> None:
    from tournament_screen import scan
    frame, boxes = recorded(name)
    reading = scan(frame, boxes)
    assert reading.page is not None
    assert reading.page.tickets == tickets
    assert reading.page.league == "Copper"
    assert reading.page.entry_is_ticket
    assert reading.page.join_time_left_s > 0


def test_tournament_stats_are_not_a_running_hud() -> None:
    from tournament_screen import scan
    frame, boxes = recorded("tournament_stats")
    reading = scan(frame, boxes)
    assert reading.stats is not None
    assert (reading.stats.wave, reading.stats.rank, reading.stats.coins) == (8, 30, 103)
    assert not reading.hud_marker


def test_setup_modal_blocks_underlying_battle() -> None:
    from tournament_screen import scan
    frame, boxes = recorded("tournament_username_prompt")
    reading = scan(frame, boxes)
    assert reading.username is not None
    assert reading.blocked
    assert reading.page is None


def test_open_menu_and_tournament_hud() -> None:
    from tournament_screen import scan
    frame, boxes = recorded("menu_tournament_open")
    assert scan(frame, boxes).menu is not None
    frame, boxes = recorded("tournament_run_midway")
    assert scan(frame, boxes).hud_marker


@pytest.mark.parametrize("label", ["Buy Ticket", "10 Gems", "WATCH AD", "CONFIRM", "Purchase", "Error"])
def test_payments_and_unknown_overlays_block_entry(label: str) -> None:
    from tournament_screen import scan
    frame, boxes = recorded("tournament_join_ticket_1")
    overlay = ocr.TextBox(label, .99, config.Rect(400, 1100, 250, 50))
    reading = scan(frame, boxes + (overlay,))
    assert reading.blocked
    assert reading.page is None or reading.page.battle is None


def test_unreadable_counter_never_becomes_zero_or_one() -> None:
    from tournament_screen import scan
    frame, boxes = recorded("tournament_join_ticket_1")
    boxes = tuple(b for b in boxes if not (b.rect.x > 970 and b.rect.y < 190))
    assert scan(frame, boxes).page.tickets is None
