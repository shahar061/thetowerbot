"""Lab navigation survives the game's New! and completion-count badges."""

from pathlib import Path
from unittest.mock import patch
import json

import cv2
import numpy as np
import pytest

from config import Rect
from lab_plan import LabVisitOptions
from lab_visit import LabVisit
from ocr import TextBox
from vision import TemplateCache


FIXTURES = Path(__file__).parent / "fixtures"


def frame(name: str) -> np.ndarray:
    image = cv2.imread(str(FIXTURES / name))
    assert image is not None
    return image


def visit() -> LabVisit:
    return LabVisit(TemplateCache(Path("templates")))


def test_new_research_and_finished_count_badges_keep_labs_unlocked() -> None:
    assert visit().tab_status(frame("menu_main_labs_badged.jpg")) == "unlocked"


@pytest.mark.parametrize("height", [1920, 2400])
@pytest.mark.parametrize("name", ["menu_main_labs_badged.jpg", "menu_main_labs_unlocked.png"])
def test_unlocked_tab_follows_bottom_anchor_at_supported_heights(height: int, name: str) -> None:
    image = frame(name)[2400 - height:]
    session = visit()
    assert session.tab_status(image) == "unlocked"
    point = session._unlocked_tab_match(image)
    assert point is not None and 765 <= point[0] <= 865
    assert height - 136 <= point[1] <= height - 28


def test_badged_labs_tab_can_be_opened_for_due_inspection() -> None:
    session = visit()
    assert session.request(LabVisitOptions(start_research=False))
    with patch("lab_visit.tap") as tap:
        session.advance(frame("menu_main_labs_badged.jpg"), (), object(), 10.)
    tap.assert_called_once()
    _, x, y = tap.call_args.args
    assert 765 <= x <= 865 and 2264 <= y <= 2372


@pytest.mark.parametrize(("name", "expected"), [
    ("main_menu.png", "locked"),
    ("menu_main_labs_unlocked.png", "unlocked"),
    ("menu_labs_active.png", "unlocked"),
])
def test_other_lab_tab_states_remain_distinct(name: str, expected: str) -> None:
    assert visit().tab_status(frame(name)) == expected


def test_flask_outside_labs_slot_does_not_authorize_navigation() -> None:
    image = frame("menu_main_labs_badged.jpg")
    icon = image[2264:2372, 765:865].copy()
    image[2264:2372, 765:865] = 0
    image[100:208, 100:200] = icon
    assert visit().tab_status(image) == "unknown"


def test_unreadable_or_noisy_tab_does_not_authorize_navigation() -> None:
    image = frame("menu_main_labs_badged.jpg")
    image[2250:, 750:885] = np.random.default_rng(0).integers(
        0, 256, image[2250:, 750:885].shape, dtype=np.uint8)
    assert visit().tab_status(image) == "unknown"
    image[:] = 0
    assert visit().tab_status(image) == "unknown"


def research_notice() -> tuple[TextBox, ...]:
    """Synthetic OCR layout for the observed new-research notice, not recorded OCR."""
    return (
        TextBox("NEW RESEARCHES", .99, Rect(320, 1000, 440, 50)),
        TextBox("New Researches Available", .99, Rect(270, 1100, 540, 50)),
        TextBox("OK", .99, Rect(510, 1290, 60, 50)),
    )


def test_recorded_new_research_notice_dismisses_the_visible_ok_button() -> None:
    rows = json.loads((FIXTURES / "ocr" / "menu_labs_new_researches.json").read_text())
    boxes = tuple(TextBox(row["text"], row["confidence"], Rect(*row["rect"])) for row in rows)
    observed = []
    session = LabVisit(TemplateCache(Path("templates")), slot_observer=observed.append)
    assert session.request(LabVisitOptions(start_research=False))
    with patch("lab_visit.tap") as tap:
        session.advance(frame("menu_labs_new_researches.png"), boxes, object(), 10.)
    assert observed == []
    tap.assert_called_once()
    assert tap.call_args.args[1:] == (541, 1316)
    assert session.last_tap[0] == "dismiss_new_researches"


def test_new_research_notice_is_dismissed_before_observing_covered_slots() -> None:
    observed = []
    session = LabVisit(TemplateCache(Path("templates")), slot_observer=observed.append)
    assert session.request(LabVisitOptions(start_research=False))
    with patch("lab_visit.tap") as tap:
        session.advance(frame("menu_labs_slot1_idle.png"), research_notice(), object(), 10.)
    assert observed == []
    tap.assert_called_once()
    assert tap.call_args.args[1:] == (540, 1315)
    assert session.last_tap[0] == "dismiss_new_researches"


@pytest.mark.parametrize("invalid", ["missing_body", "missing_ok", "weak_ok", "outside_ok", "duplicate_ok"])
def test_ambiguous_research_notice_is_held_without_tapping_or_observing(invalid: str) -> None:
    title, body, ok = research_notice()
    boxes = {
        "missing_body": (title, ok),
        "missing_ok": (title, body),
        "weak_ok": (title, body, TextBox("OK", .4, ok.rect)),
        "outside_ok": (title, body, TextBox("OK", .99, Rect(20, 2300, 60, 50))),
        "duplicate_ok": (title, body, ok, ok),
    }[invalid]
    observed = []
    session = LabVisit(TemplateCache(Path("templates")), slot_observer=observed.append)
    assert session.request(LabVisitOptions(start_research=False))
    with patch("lab_visit.tap") as tap:
        session.advance(frame("menu_labs_slot1_idle.png"), boxes, object(), 10.)
    assert observed == []
    tap.assert_not_called()
