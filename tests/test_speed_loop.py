"""The speed widget as the scan loop drives it.

Two paths reach the arrows and they are tested separately because they fail
differently. A *command* is one button press in the browser and must land
exactly once, on the right screen, and never after the moment passed. A
*target* is standing policy and must keep nudging until the readout agrees.

The command tests need no mocking at all: the in-run fixture is a real frame
and the arrow point is derived from its real anchor, so "did it tap the +
button" is checked against measured pixels.
"""

from __future__ import annotations

from typing import Callable

import pytest

import config
from strategy import Shopping
from tower_bot import TowerBot

# The + and - button boxes, measured on tests/fixtures/in_run_lit.png. Any
# tap inside one of these is a tap on that arrow.
PLUS_BOX = (876, 940, 1381, 1446)
MINUS_BOX = (676, 740, 1381, 1446)


def tapped_in(box: tuple[int, int, int, int], taps: list[tuple[int, int]]) -> list:
    x0, x1, y0, y1 = box
    return [(x, y) for x, y in taps if x0 <= x <= x1 and y0 <= y <= y1]


@pytest.fixture
def in_run(bot_in_run: Callable[[Shopping], TowerBot]) -> TowerBot:
    return bot_in_run(Shopping())


def test_a_speed_up_command_taps_the_plus_arrow(in_run: TowerBot) -> None:
    in_run.controls.request("speed_up")
    in_run.run_once()

    assert tapped_in(PLUS_BOX, in_run.device.taps), (
        f"no tap landed on the + button; taps were {in_run.device.taps}"
    )


def test_speed_adjustment_defers_ocr_purchase_until_next_frame(in_run: TowerBot, monkeypatch: pytest.MonkeyPatch) -> None:
    in_run.controls.apply({"autopilot": {"enabled": True}})
    calls: list[bool] = []
    monkeypatch.setattr(in_run.speed, "settle", lambda *args, **kwargs: "up")
    monkeypatch.setattr(in_run.autopilot, "step", lambda *args, **kwargs: calls.append(True))
    in_run.run_once()
    assert calls == []


def test_a_speed_down_command_taps_the_minus_arrow(in_run: TowerBot) -> None:
    in_run.controls.request("speed_down")
    in_run.run_once()

    assert tapped_in(MINUS_BOX, in_run.device.taps)


def test_a_command_fires_on_one_scan_only(in_run: TowerBot) -> None:
    """The drain has to happen in the loop, not just in Controls. A command
    re-read every pass is one button press that keeps tapping forever."""
    in_run.controls.request("speed_up")
    in_run.run_once()
    before = len(tapped_in(PLUS_BOX, in_run.device.taps))
    in_run.run_once()

    assert len(tapped_in(PLUS_BOX, in_run.device.taps)) == before


def test_a_command_is_discarded_while_paused(in_run: TowerBot) -> None:
    """Pause means "still scanning, not tapping" - the rule the rest of the
    loop already keeps. It must also mean the command does not sit in the
    queue and fire the instant Resume is pressed, which would be a tap the
    user asked for at a moment that has passed."""
    in_run.controls.apply({"paused": True})
    in_run.controls.request("speed_up")
    in_run.run_once()
    in_run.controls.apply({"paused": False})
    in_run.run_once()

    assert not tapped_in(PLUS_BOX, in_run.device.taps)


def test_a_command_is_discarded_off_the_battle_screen(
    bot_on_workshop: Callable[[Shopping], TowerBot],
) -> None:
    """The arrow coordinates are anchored to the IN_RUN template. Off that
    screen they are just a point on a menu, and tapping it buys something."""
    bot = bot_on_workshop(Shopping())
    bot.controls.request("speed_up")
    bot.run_once()

    assert not tapped_in(PLUS_BOX, bot.device.taps)
    assert not tapped_in(MINUS_BOX, bot.device.taps)


def test_a_target_already_showing_taps_nothing(in_run: TowerBot) -> None:
    """The fixture reads x1.0. Asking for x1.0 must be a no-op, not a tap
    that overshoots and then has to come back."""
    in_run.controls.apply({"target_speed": 1.0})
    in_run.run_once()

    assert not tapped_in(PLUS_BOX, in_run.device.taps)
    assert not tapped_in(MINUS_BOX, in_run.device.taps)


def test_a_target_above_the_reading_climbs(in_run: TowerBot) -> None:
    """End to end on real pixels: the fixture genuinely reads x1.0, x1.5 is a
    genuinely harvested target, and the tap lands in the measured + button.
    Nothing is mocked."""
    in_run.controls.apply({"target_speed": 1.5})
    in_run.run_once()

    assert tapped_in(PLUS_BOX, in_run.device.taps)


def test_the_battle_ocr_label_reads_speeds_past_the_templates(
    in_run: TowerBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frame's pixels match the x1.0 template, but the battle OCR reads
    "x2.5" in the widget. The OCR wins, so a x2.0 target steps DOWN - which
    proves the scan loop hands its battle read to the speed reader."""
    import ocr

    label = ocr.TextBox("x2.5", .99, config.Rect(767, 1398, 80, 39))
    monkeypatch.setattr(ocr.FrameReads, "battle", lambda self: (label,))
    in_run.controls.apply({"target_speed": 2.0})
    in_run.run_once()

    assert tapped_in(MINUS_BOX, in_run.device.taps)
    assert not tapped_in(PLUS_BOX, in_run.device.taps)


def test_reroll_raises_speed_before_any_other_battle_action(
    in_run: TowerBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    in_run.reroll_progress = Mock()
    in_run.reroll_progress.speed_target.return_value = 1.5
    in_run.controls.apply({"target_speed": 1.0})

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("battle action ran before speed was raised")

    monkeypatch.setattr(in_run.gem, "observe", unexpected)
    monkeypatch.setattr(in_run, "find_and_click_image", unexpected)
    assert in_run.run_once()
    assert len(tapped_in(PLUS_BOX, in_run.device.taps)) == 1
    assert len(in_run.device.taps) == 1


def test_reroll_uses_account_verified_lab_speed_target(
    in_run: TowerBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    in_run.reroll_progress = Mock()
    in_run.reroll_progress.speed_target.return_value = 2.0
    targets: list[float | None] = []
    monkeypatch.setattr(in_run.speed, "settle", lambda *args, **kwargs:
                        targets.append(kwargs["target"]) or None)

    in_run._manage_speed(in_run.controls.snapshot(), (12, 1646), ())

    assert targets == [2.0]


def test_a_target_below_the_reading_descends(bot_in_run_fast: TowerBot) -> None:
    """The other direction, end to end: in_run_fast really reads x1.5, the
    target is really x1.0, and the tap has to land in the measured - button."""
    bot_in_run_fast.controls.apply({"target_speed": 1.0})
    bot_in_run_fast.run_once()

    assert tapped_in(MINUS_BOX, bot_in_run_fast.device.taps)
    assert not tapped_in(PLUS_BOX, bot_in_run_fast.device.taps)


def test_it_climbs_out_of_a_paused_game(
    bot_in_run_paused: TowerBot,
) -> None:
    """The case x0.0 exists for. A game stopped at the widget's bottom step
    must be recognised and escaped, not read as "no widget" and left frozen
    for the rest of the run."""
    bot_in_run_paused.controls.apply({"target_speed": 1.5})
    bot_in_run_paused.run_once()

    assert tapped_in(PLUS_BOX, bot_in_run_paused.device.taps)


def test_a_command_does_not_also_trigger_the_target_on_the_same_scan(
    in_run: TowerBot,
) -> None:
    """One press must be one step.

    settle() reads `self.screen`, which was captured BEFORE the command's tap
    landed - the game has not redrawn the readout yet. So with a target also
    set, the same scan would decide from a reading it already invalidated and
    tap a second time, turning one button press into two steps and
    overshooting the target it was meant to be heading for.
    """
    in_run.controls.apply({"target_speed": 1.5})
    in_run.controls.request("speed_up")
    in_run.run_once()

    assert len(tapped_in(PLUS_BOX, in_run.device.taps)) == 1


def test_the_target_resumes_on_the_scan_after_a_command(
    in_run: TowerBot,
) -> None:
    """Skipping settle() is for the one stale scan only. The target is
    standing policy: it must take over again on the next fresh frame, not be
    switched off by a manual nudge."""
    in_run.controls.apply({"target_speed": 1.5})
    in_run.controls.request("speed_up")
    in_run.run_once()
    in_run.run_once()

    assert len(tapped_in(PLUS_BOX, in_run.device.taps)) == 2


def test_no_target_leaves_the_speed_alone(bot_in_run_paused: TowerBot) -> None:
    """The default, tested on the frame with most to gain from being changed:
    the game is stopped at x0.0 and the bot could obviously improve it, but
    target_speed=None means leave the widget alone, so it does."""
    bot_in_run_paused.run_once()

    assert not tapped_in(PLUS_BOX, bot_in_run_paused.device.taps)
    assert not tapped_in(MINUS_BOX, bot_in_run_paused.device.taps)


def test_every_listed_speed_has_a_readout_template() -> None:
    """config.SPEED_TEMPLATE_VALUES and templates/speed/ have to agree: a
    value listed without its template is a FileNotFoundError out of the scan
    loop the first time the bot reads the widget. This is the check that lets
    read() call templates.get() straight, with no defensive skip."""
    assert set(config.SPEED_TEMPLATE_VALUES) <= set(config.SPEED_VALUES)
    missing = [
        value
        for value in config.SPEED_TEMPLATE_VALUES
        if not (config.TEMPLATE_DIR / config.speed_template(value)).exists()
    ]
    assert not missing, f"speeds with no readout template: {missing}"


def test_every_targetable_speed_is_one_the_bot_can_recognise() -> None:
    """TARGET_SPEEDS is a subset of SPEED_VALUES, never a list of its own. A
    target read() cannot report is one settle() would tap toward forever
    without ever matching it."""
    assert set(config.TARGET_SPEEDS) <= set(config.SPEED_VALUES)


def test_the_game_cannot_be_held_stopped() -> None:
    """x0.0 must stay readable and stay un-targetable. Readable, or a bot that
    lands on a paused game can never climb out; un-targetable, or a strategy
    can pin the game stopped and the run never ends."""
    assert 0.0 in config.SPEED_VALUES
    assert 0.0 not in config.TARGET_SPEEDS


def test_the_arrow_points_sit_inside_the_measured_buttons() -> None:
    """Guards the offsets themselves. These are derived numbers, not matched
    ones, so nothing else would notice if a region were mistyped - the bot
    would just tap empty space beside the arrow for the rest of its life."""
    anchor = (12, 1646)
    for region, box in (
        (config.SPEED_PLUS_REGION, PLUS_BOX),
        (config.SPEED_MINUS_REGION, MINUS_BOX),
    ):
        x = anchor[0] + region.dx + region.w // 2
        y = anchor[1] + region.dy + region.h // 2
        assert tapped_in(box, [(x, y)]), f"{region} centres on {(x, y)}, outside {box}"


def test_the_readout_window_does_not_overlap_either_arrow() -> None:
    """If the window reached into a button, a template cut from it would
    match the arrow's green box rather than the number - and every speed
    would read the same."""
    readout = config.SPEED_READOUT_REGION
    left = readout.dx
    right = readout.dx + readout.w
    assert left > config.SPEED_MINUS_REGION.dx + config.SPEED_MINUS_REGION.w
    assert right < config.SPEED_PLUS_REGION.dx
