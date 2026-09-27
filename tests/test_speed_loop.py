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

from pathlib import Path
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


def test_reroll_raises_speed_before_any_other_battle_action(
    in_run: TowerBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    in_run.reroll_progress = Mock()
    in_run.reroll_progress.speed_target.return_value = 1.5
    in_run.controls.apply({"target_speed": None, "auto_fastest": True})

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
    """config.SPEED_VALUES and templates/speed/ have to agree: a value listed
    without its template is a FileNotFoundError out of the scan loop the
    first time the bot reads the widget. This is the check that lets read()
    call templates.get() straight, with no defensive skip."""
    missing = [
        value
        for value in config.SPEED_VALUES
        if not (config.TEMPLATE_DIR / config.speed_template(value)).exists()
    ]
    assert not missing, f"speeds with no readout template: {missing}"


def test_every_targetable_speed_is_one_the_bot_can_recognise() -> None:
    """TARGET_SPEEDS is a subset of SPEED_VALUES, never a list of its own. A
    target with no readout template is one settle() would tap toward forever
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

@pytest.fixture
def deadline_bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TowerBot, list[float], list[str]]:
    from control import Controls
    from strategy import Strategy, ActionRule
    from tests.test_lab_transactions import authority, Device
    from tests.test_maintenance_schedule import running
    from tests.test_lab_screen import frame
    from tests.conftest import _RecordingBus
    from shopping import ShoppingSession
    import digits
    import vision
    import screens
    import ocr
    account, journal, scope = authority(tmp_path)
    runtime = running(tmp_path, finish=110.)
    clock = [109.]
    picture = ['in_run_lit']
    monkeypatch.setattr('tower_bot.time.time', lambda: clock[0])
    monkeypatch.setattr('tower_bot.capture_screen', lambda _device: frame(picture[0]))
    monkeypatch.setattr('speed.tap', lambda device, x, y: device.taps.append((x, y)))
    monkeypatch.setattr(ocr.FrameReads, 'full', lambda _reads: ())
    templates = vision.TemplateCache(config.TEMPLATE_DIR)
    bus = _RecordingBus()
    bot = TowerBot(Device(), templates, bus, account_state=account,
        shopping=ShoppingSession(templates, bus, digits.NumberReader(), journal=journal),
        controls=Controls(strategy=Strategy(name='deadline',
            actions=(ActionRule(name='Damage', template='upgrade_damage.png', enabled=False),),
            auto_fastest=True, tap_delay=0., tap_jitter_px=0.)))
    # Persisted jobs are historical after construction; fresh Lab observations
    # are the only way to introduce a new deadline into this process.
    bot.lab_runtime = runtime
    bot.lab_visit.runtime = bot.lab_runtime
    bot.tracker.state = screens.ScreenState.IN_RUN
    bot.tracker._confirmed = True
    return bot, clock, picture


def test_deadline_real_bot_taps_once_then_persists_widget_capability(deadline_bot: tuple[TowerBot, list[float], list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    bot, clock, picture = deadline_bot
    bot.run_once()
    assert not tapped_in(PLUS_BOX, bot.device.taps)
    clock[0] = 110.
    bot.run_once()
    assert len(tapped_in(PLUS_BOX, bot.device.taps)) == 1
    picture[0] = 'in_run_fast'
    clock[0] = 111.
    bot.run_once()
    clock[0] = 112.
    bot.run_once()
    assert bot.lab_runtime.snapshot().verified_speed == 1.5
    assert bot.lab_runtime.snapshot().slots[0].state == 'researching'
    from unittest.mock import Mock
    bot.reroll_progress = Mock()
    bot.reroll_progress.speed_target.return_value = 1.
    targets = []
    monkeypatch.setattr(bot.speed, 'settle', lambda *args, **kw: targets.append(kw['target']))
    bot._manage_speed(bot.controls.snapshot(), (12, 1646), ())
    assert targets[0] >= 1.5
    assert not tapped_in(MINUS_BOX, bot.device.taps)


def test_deadline_pending_purchase_and_pause_preserve_generation(deadline_bot: tuple[TowerBot, list[float], list[str]]) -> None:
    from tests.test_lab_transactions import prepared
    bot, clock, _ = deadline_bot
    bot.run_once()
    txn = prepared(bot.shopping.journal, bot.account_state.verified_scope)
    clock[0] = 110.
    bot.run_once()
    assert not tapped_in(PLUS_BOX, bot.device.taps)
    assert bot.shopping.journal.open_transactions()[0].key == txn.key
    assert bot.maintenance_status == 'pending_purchase'
    assert any(a.kind == 'speed_check' for a in bot.maintenance.due(110.))
    bot.controls.apply({'paused': True})
    clock[0] = 111.
    bot.run_once()
    assert bot.maintenance_status == 'paused'
    assert not tapped_in(PLUS_BOX, bot.device.taps)


def test_deadline_manual_target_never_raised_and_new_generation_rearms(deadline_bot: tuple[TowerBot, list[float], list[str]]) -> None:
    bot, clock, _ = deadline_bot
    bot.controls.apply({'target_speed': 1.})
    clock[0] = 110.
    bot.run_once()
    assert not tapped_in(PLUS_BOX, bot.device.taps)
    bot.controls.apply({'target_speed': 2.})
    for stamp in range(111, 119):
        clock[0] = float(stamp)
        bot.run_once()
    assert len(tapped_in(PLUS_BOX, bot.device.taps)) == 4
    from dataclasses import replace
    snapshot = bot.lab_runtime.snapshot()
    bot.lab_runtime._snapshot = replace(snapshot, slots=(replace(snapshot.slots[0],
        generation='new-job', expected_finish=120.), *snapshot.slots[1:]))
    clock[0] = 120.
    bot.run_once()
    assert len(tapped_in(PLUS_BOX, bot.device.taps)) == 5


def test_manual_target_overrides_reroll(in_run: TowerBot, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock
    in_run.reroll_progress = Mock()
    in_run.reroll_progress.speed_target.return_value = 2.
    in_run.controls.apply({'target_speed': 1., 'auto_fastest': True})
    targets = []
    monkeypatch.setattr(in_run.speed, 'settle', lambda *args, **kw: targets.append(kw['target']))
    in_run._manage_speed(in_run.controls.snapshot(), (12, 1646), ())
    assert targets == [1.]


def test_suspended_pending_battle_purchase_blocks_due_speed(deadline_bot: tuple[TowerBot, list[float], list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    bot, clock, _ = deadline_bot
    bot.autopilot.pending = (object(), 100.)
    bot.autopilot.suspend('waiting on purchase evidence')
    clock[0] = 110.
    # The purchase owner keeps its pending outcome through this scan.
    monkeypatch.setattr(bot.autopilot, 'step', lambda *args, **kwargs: None)
    bot.run_once()
    assert bot.autopilot.pending is not None
    assert bot.maintenance_status == 'pending_purchase'
    assert not tapped_in(PLUS_BOX, bot.device.taps)
    assert any(a.kind == 'speed_check' for a in bot.maintenance.due(110.))
    bot.autopilot.pending = None
    clock[0] = 111.
    bot.run_once()
    assert len(tapped_in(PLUS_BOX, bot.device.taps)) == 1


def test_due_speed_stays_visible_on_unsafe_screen(deadline_bot: tuple[TowerBot, list[float], list[str]]) -> None:
    bot, clock, picture = deadline_bot
    picture[0] = 'main_menu'
    clock[0] = 110.
    bot.run_once()
    assert bot.maintenance_status == 'unsafe_screen'
    assert not tapped_in(PLUS_BOX, bot.device.taps)
    assert any(a.kind == 'speed_check' for a in bot.maintenance.due(110.))


def test_corrected_deadline_rearms_exhausted_real_loop_once(deadline_bot: tuple[TowerBot, list[float], list[str]]) -> None:
    from dataclasses import replace
    bot, clock, _ = deadline_bot
    for stamp in range(110, 117):
        clock[0] = float(stamp)
        bot.run_once()
    assert len(tapped_in(PLUS_BOX, bot.device.taps)) == 4
    assert bot.maintenance_status == 'speed_check_exhausted'
    snapshot = bot.lab_runtime.snapshot()
    job = replace(snapshot.slots[0], expected_finish=130., observed_at=120.)
    bot.lab_runtime._snapshot = replace(snapshot, slots=(job, *snapshot.slots[1:]))
    clock[0] = 129.
    bot.run_once()
    assert len(tapped_in(PLUS_BOX, bot.device.taps)) == 4
    for stamp in range(130, 138):
        clock[0] = float(stamp)
        bot.run_once()
    assert len(tapped_in(PLUS_BOX, bot.device.taps)) == 8
    assert bot.lab_runtime.snapshot().slots[0].generation == job.generation
    assert bot.lab_runtime.snapshot().slots[0].state == 'researching'


def test_loop_wakes_at_deadline_before_normal_scan(deadline_bot: tuple[TowerBot, list[float], list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    bot, clock, _ = deadline_bot
    clock[0] = 109.
    waits = []
    def stop_after_wait(timeout: float | None = None) -> bool:
        waits.append(timeout)
        bot._running = False
        return False
    monkeypatch.setattr(bot._stopping, 'wait', stop_after_wait)
    bot.run_forever(interval=30.)
    assert waits == [1.]


def test_restart_does_not_rearm_consumed_window(deadline_bot: tuple[TowerBot, list[float], list[str]]) -> None:
    from maintenance_schedule import MaintenanceSchedule
    import speed
    bot, clock, _ = deadline_bot
    for stamp in range(110, 117):
        clock[0] = float(stamp)
        bot.run_once()
    assert len(tapped_in(PLUS_BOX, bot.device.taps)) == 4
    before = bot.lab_runtime.snapshot()
    bot.speed = speed.SpeedController()
    bot.maintenance = MaintenanceSchedule(bot.lab_runtime.root, before.scope.account_id)
    bot._deadline_speed = None
    clock[0] = 117.
    bot.run_once()
    assert len(tapped_in(PLUS_BOX, bot.device.taps)) == 4
    assert before.slots[0] == bot.lab_runtime.snapshot().slots[0]


def test_safe_home_arms_read_only_deadline_inspection_without_acknowledging(deadline_bot: tuple[TowerBot, list[float], list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    import screens
    bot, clock, picture = deadline_bot
    picture[0] = 'menu_main_labs_unlocked'
    clock[0] = 110.
    bot.tracker.state = screens.ScreenState.MAIN_MENU
    bot.tracker._confirmed = True
    monkeypatch.setattr(bot._notifications, 'verification_due', lambda now: False)
    monkeypatch.setattr(bot, '_offer_cards_intro', lambda: False)
    monkeypatch.setattr(bot, '_offer_claim', lambda _settings: None)
    bot.run_once()
    assert bot.lab_visit.active
    assert any(a.kind == 'inspect_labs' for a in bot.maintenance.due(110.))
    assert bot.shopping.journal.open_transactions() == ()
    assert not bot.device.taps


def test_runtime_callback_requires_current_complete_two_capture_strip(deadline_bot: tuple[TowerBot, list[float], list[str]]) -> None:
    from dataclasses import replace
    from tests.test_lab_runtime import observation
    bot, clock, _ = deadline_bot
    reading = observation(100.)
    reading = replace(reading, slots_owned=1, jobs=(reading.jobs[0],))
    def capture(stamp: float, *, complete: bool) -> None:
        clock[0] = stamp
        bot._screen_captured_at = stamp
        bot._screen_fact_scope = bot.account_state.verified_scope
        current = replace(reading, observed_at=stamp)
        if not complete:
            current = replace(current, slots_owned=None, slots_status='unknown')
            assert not current.strip_read()
        bot._observe_lab_runtime(current)
    capture(100., complete=True)
    capture(101., complete=True)
    before = bot.lab_runtime.snapshot().slots[0]
    assert bot.lab_runtime.snapshot().slots_owned == 1
    bot.maintenance.request('inspect_labs', 'partial-regression', 110., reason='test-inspection')
    capture(120., complete=False)
    capture(121., complete=False)
    snapshot = bot.lab_runtime.snapshot()
    assert snapshot.slots_owned == 1  # Historical ownership is preserved.
    assert snapshot.slots[0].observed_at == 121.
    assert snapshot.slots[0].generation == before.generation
    assert snapshot.slots[0].state == 'researching'
    assert any(a.generation == 'partial-regression' for a in bot.maintenance.due(121.))
    capture(122., complete=True)  # One complete capture is not a pair.
    capture(122., complete=True)  # Replayed timestamp is not a second capture.
    assert any(a.generation == 'partial-regression' for a in bot.maintenance.due(122.))
    capture(123., complete=True)
    assert not any(a.generation == 'partial-regression' for a in bot.maintenance.due(123.))


@pytest.mark.parametrize('reason', ['timer_correction_limit', 'restart'])
def test_unacknowledged_inspection_never_rearms_every_menu_pass(
        deadline_bot, monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    """A due inspection a visit cannot acknowledge is paced; BATTLE proceeds."""
    import screens
    bot, clock, picture = deadline_bot
    bot.maintenance.request('inspect_labs', f'{reason}:x', 50., reason=reason, slot=1)
    picture[0] = 'menu_main_labs_unlocked'
    bot.tracker.state = screens.ScreenState.MAIN_MENU
    bot.tracker._confirmed = True
    monkeypatch.setattr(bot._notifications, 'verification_due', lambda now: False)
    monkeypatch.setattr(bot, '_offer_cards_intro', lambda: False)
    monkeypatch.setattr(bot, '_offer_claim', lambda _settings: None)
    armed = []
    for stamp in (110., 200., 300., 400., 500.):
        clock[0] = stamp
        bot.run_once()
        if bot.lab_visit.active:
            armed.append(stamp)
            bot.lab_visit.cancel('visit_ended_without_acknowledgment')
    # Exponential spacing (120 s, then 240 s): most menu passes reach BATTLE.
    assert armed == [110., 300.], armed
    if reason == 'restart':
        assert any(a.reason == reason for a in bot.maintenance.due(500.))  # Still due, visible.
