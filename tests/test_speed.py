"""The in-battle speed widget: reading it, and deciding which arrow to tap.

Split deliberately into two halves that do not share a dependency. `read()`
needs real pixels, so its tests run against the checked-in in-run fixture.
`step()` is pure arithmetic over a value list, so its tests need no image at
all - which is what lets the whole settle policy be covered before a single
readout template beyond x1.0 exists (see tools/harvest_speed_glyphs.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import cv2
import pytest

import config
import events
import ocr
import speed
import vision

FIXTURES = Path(__file__).parent / "fixtures"
TEMPLATES = Path(__file__).parent.parent / "templates"


def frame(name: str):
    return cv2.imread(str(FIXTURES / f"{name}.png"), cv2.IMREAD_COLOR)


class _RecordingBus:
    """Local rather than imported: conftest's is private, and this one only
    needs to keep what it is handed."""

    def __init__(self) -> None:
        self.published: list[events.Event] = []

    def publish(self, event: events.Event) -> events.Event:
        self.published.append(event)
        return event


@pytest.fixture
def templates() -> vision.TemplateCache:
    return vision.TemplateCache(TEMPLATES)


def test_reads_the_speed_off_a_real_in_run_frame(templates: vision.TemplateCache) -> None:
    assert speed.read(frame("in_run_lit"), templates, anchor=(12, 1646)) == 1.0


def test_reads_the_paused_speed(templates: vision.TemplateCache) -> None:
    """x0.0 is a real reading, not an absent one: the widget's bottom step
    stops the game dead. It has to be recognisable, because a bot that read
    None here could never decide to tap its way back up - decide() refuses to
    act on an unreadable widget, so an unreadable x0.0 is a frozen game the
    bot cannot escape."""
    assert speed.read(frame("in_run_paused"), templates, anchor=(12, 1646)) == 0.0


def test_climbs_out_of_a_paused_game() -> None:
    assert speed.SpeedController().decide(current=0.0, target=1.5) == "up"


def test_reads_the_ceiling_speed(templates: vision.TemplateCache) -> None:
    """x1.5 against a frame captured at x1.5. Together with the x1.0 and x0.0
    cases this covers every harvested template against real pixels - which is
    what stops a mislabelled crop (x1.5's template saved as x1.0's name) from
    going unnoticed."""
    assert speed.read(frame("in_run_fast"), templates, anchor=(12, 1646)) == 1.5


def test_reads_nothing_when_the_widget_is_not_on_screen(
    templates: vision.TemplateCache,
) -> None:
    """The main menu has no speed widget. Reading the same offsets there must
    answer None, not the nearest-looking number - `settle()` taps an arrow on
    the strength of this answer, and a confident wrong read taps the game's
    speed away from where the strategy asked for it."""
    assert speed.read(frame("main_menu"), templates, anchor=(12, 1646)) is None


def test_reads_nothing_when_the_anchor_puts_the_window_off_screen(
    templates: vision.TemplateCache,
) -> None:
    """A bad anchor must be a None, not an IndexError out of the scan loop.
    numpy slices past an edge silently return a smaller - or empty - array,
    so this is the case that reaches matchTemplate as a 0-row image."""
    assert speed.read(frame("in_run_lit"), templates, anchor=(5000, 5000)) is None


# -- decide() ---------------------------------------------------------------
# Pure over floats, so none of these need a frame. That is the point of the
# split: the settle policy is fully covered while x1.0 is still the only
# value with a captured template.


def test_decides_to_tap_up_when_below_the_target() -> None:
    assert speed.SpeedController().decide(current=1.0, target=2.0) == "up"


def test_decides_to_tap_down_when_above_the_target() -> None:
    assert speed.SpeedController().decide(current=3.0, target=2.0) == "down"


def test_decides_nothing_once_the_target_is_showing() -> None:
    assert speed.SpeedController().decide(current=2.0, target=2.0) is None


def test_decides_nothing_when_no_target_is_set() -> None:
    """`target_speed` is None by default and means "leave the speed alone" -
    the bot must not drag a hand-set speed back to some implicit default."""
    assert speed.SpeedController().decide(current=3.0, target=None) is None


def test_decides_nothing_when_the_readout_could_not_be_read() -> None:
    """No reading is not "assume 1.0". Tapping on a failed read is how the
    bot walks the speed to a random value while the widget is mid-animation."""
    assert speed.SpeedController().decide(current=None, target=2.0) is None


def test_gives_up_after_the_readout_refuses_to_move() -> None:
    """A target the game will not reach - above the account's unlocked cap -
    must not tap forever. After `patience` taps that leave the reading where
    it was, this controller stops asking."""
    controller = speed.SpeedController(patience=3)
    assert [controller.decide(current=1.0, target=9.0) for _ in range(5)] == [
        "up", "up", "up", None, None,
    ]


def test_a_reading_that_moves_resets_the_patience() -> None:
    """Progress is progress. Only a stuck readout counts against the budget,
    so a long climb through many steps never exhausts it."""
    controller = speed.SpeedController(patience=2)
    assert controller.decide(current=1.0, target=4.0) == "up"
    assert controller.decide(current=1.0, target=4.0) == "up"
    assert controller.decide(current=1.0, target=4.0) is None
    assert controller.decide(current=2.0, target=4.0) == "up"


def test_giving_up_is_forgotten_when_the_target_changes() -> None:
    """Editing the target on the Strategy page is a fresh instruction, not a
    retry of the one that failed - it must not inherit the old give-up."""
    controller = speed.SpeedController(patience=1)
    assert controller.decide(current=1.0, target=9.0) == "up"
    assert controller.decide(current=1.0, target=9.0) is None
    assert controller.decide(current=1.0, target=2.0) == "up"


# -- settle() ---------------------------------------------------------------
# The thin part: read the frame, ask decide(), tap the arrow it named. Only
# the wiring is tested here - the policy is covered above.

ANCHOR = (12, 1646)


def test_settle_taps_nothing_when_the_speed_already_matches(
    templates: vision.TemplateCache,
) -> None:
    device = MagicMock()
    controller = speed.SpeedController()

    assert controller.settle(
        frame("in_run_lit"), device, templates, target=1.0, anchor=ANCHOR
    ) is None
    device.click.assert_not_called()


def test_settle_taps_the_plus_arrow_to_climb(templates: vision.TemplateCache) -> None:
    """The fixture reads x1.0, so a target above it must land a tap inside
    the `+` button - not merely 'a tap somewhere'. The measured button box is
    x876-940, y1381-1446 on this frame."""
    device = MagicMock()
    controller = speed.SpeedController()

    assert controller.settle(
        frame("in_run_lit"), device, templates, target=2.0, anchor=ANCHOR
    ) == "up"

    (x, y), _ = device.click.call_args
    assert 876 <= x <= 940, f"tap x={x} missed the + button"
    assert 1381 <= y <= 1446, f"tap y={y} missed the + button"


def test_settle_announces_the_adjustment(templates: vision.TemplateCache) -> None:
    """The feed has to show speed moving, or a bot quietly tapping `+` for a
    whole run looks identical to one that never touched the widget."""
    bus = _RecordingBus()
    controller = speed.SpeedController(bus=bus)

    controller.settle(frame("in_run_lit"), MagicMock(), templates, target=2.0, anchor=ANCHOR)

    adjusted = [e for e in bus.published if isinstance(e, events.SpeedAdjusted)]
    assert len(adjusted) == 1
    assert (adjusted[0].direction, adjusted[0].reading, adjusted[0].target) == ("up", 1.0, 2.0)


# -- the OCR readout ----------------------------------------------------------
# Templates exist only for the speeds harvested so far. The label the battle
# OCR already reads covers every step up to x5.0 without a crop per level.

def _box(text: str, rect: tuple[int, int, int, int] = (767, 1398, 80, 39),
         confidence: float = .99) -> ocr.TextBox:
    """Defaults to where in_run_lit.json reads its "x1.0" label."""
    return ocr.TextBox(text, confidence, config.Rect(*rect))


@pytest.mark.parametrize(("text", "value"), [("x2.5", 2.5), ("x5.0", 5.0), ("X3.5", 3.5)])
def test_reads_a_speed_with_no_template_from_the_ocr_label(
    templates: vision.TemplateCache, text: str, value: float,
) -> None:
    assert speed.read(frame("main_menu"), templates, anchor=ANCHOR,
                      boxes=(_box(text),)) == value


@pytest.mark.parametrize("box", [
    _box("x1.20", rect=(861, 1995, 110, 45)),  # critical factor, below the widget
    _box("x1.00", rect=(429, 1486, 88, 35)),   # coins multiplier, left of it
    _box("x2.6"),                               # not a step the widget can show
    _box("x2.5", confidence=.5),
])
def test_ignores_labels_that_are_not_a_confident_widget_reading(
    templates: vision.TemplateCache, box: ocr.TextBox,
) -> None:
    assert speed.read(frame("main_menu"), templates, anchor=ANCHOR, boxes=(box,)) is None


def test_template_still_reads_when_ocr_missed_the_label(
    templates: vision.TemplateCache,
) -> None:
    assert speed.read(frame("in_run_lit"), templates, anchor=ANCHOR, boxes=()) == 1.0


def test_settle_climbs_from_an_ocr_only_speed(templates: vision.TemplateCache) -> None:
    controller = speed.SpeedController()

    assert controller.settle(frame("in_run_lit"), MagicMock(), templates, target=3.0,
                             anchor=ANCHOR, boxes=(_box("x2.5"),)) == "up"
    assert controller.settle(frame("in_run_lit"), MagicMock(), templates, target=2.5,
                             anchor=ANCHOR, boxes=(_box("x2.5"),)) is None
