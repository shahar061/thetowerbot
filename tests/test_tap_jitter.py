"""Jitter where it meets the scan loop.

The unit contracts live in test_jitter.py. What matters here is that the
jittered point is the one the bot actually taps AND the one it reports -
config.buy_point's own comment ("computed once, up front, so the box
recorded below and the eventual tap agree") is a promise these tests keep
honest now that a random offset sits between the two.
"""

from __future__ import annotations

import random
from pathlib import Path
from unittest.mock import MagicMock

import cv2
import pytest

import config
import events
import jitter
import strategy as strategy_mod
import tower_bot as tower_bot_mod
import vision
from tower_bot import TowerBot

TEMPLATES = Path(__file__).parent.parent / "templates"
FIXTURES = Path(__file__).parent / "fixtures"


def frame(name: str):
    image = cv2.imread(str(FIXTURES / f"{name}.png"), cv2.IMREAD_COLOR)
    assert image is not None, f"missing fixture: {name}.png"
    return image


def settled_bot(monkeypatch: pytest.MonkeyPatch, **patch):
    """A bot settled on IN_RUN, with jitter seeded for repeatability."""
    monkeypatch.setattr(jitter, "_rng", random.Random(1234))
    dev = MagicMock()
    bot = TowerBot(
        device=dev, templates=vision.TemplateCache(TEMPLATES), bus=events.EventBus()
    )
    bot._screen = frame("in_run_lit")
    monkeypatch.setattr(bot, "refresh_screen", lambda: bot._screen)
    if patch:
        bot.controls.apply(patch)
    bot.run_once()
    bot.run_once()  # confirms the transition
    bot._last_click.clear()
    dev.reset_mock()
    return bot, dev


def taps(dev) -> list[tuple[int, int]]:
    return [tuple(call.args) for call in dev.click.call_args_list]


# -- the invariant that makes the device view trustworthy -------------------
def test_the_recorded_box_and_the_actual_tap_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole reason jitter is applied before the box is recorded rather
    than inside device.tap().

    If tap() jittered internally, the crosshair the device view draws would
    sit up to tap_jitter_px away from where the tap really landed, and the
    one page you open to find out why a purchase missed would be lying to
    you about the only coordinate that matters.
    """
    bot, dev = settled_bot(monkeypatch, tap_jitter_px=8.0, tap_delay=0.0)
    boxes: list[dict] = []

    for rule in bot.controls.snapshot().strategy.actions:
        bot.find_and_click_image(rule.as_action(), boxes)

    tapped = taps(dev)
    assert tapped, "expected at least one tap on the lit fixture"
    recorded = [(b["tap_x"], b["tap_y"]) for b in boxes if b["tapped"]]
    assert recorded == tapped


def test_the_tapped_event_reports_the_jittered_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same reasoning for the event stream, which is what the log, the SSE
    feed and the stored events table all read."""
    class _Sink:
        def __init__(self) -> None:
            self.seen: list[events.Event] = []

        def offer(self, event: events.Event) -> bool:
            self.seen.append(event)
            return True

    sink = _Sink()
    bot, dev = settled_bot(monkeypatch, tap_jitter_px=8.0, tap_delay=0.0)
    bot.bus.subscribe(sink)

    bot.run_once()

    reported = [(e.x, e.y) for e in sink.seen if isinstance(e, events.Tapped)]
    assert reported == taps(dev)


# -- the offset itself ------------------------------------------------------
def test_zero_jitter_taps_the_exact_buy_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-compat, asserted structurally: the box carries match.top_left as
    (x, y), so the un-jittered tap point must be exactly what
    config.buy_point derives from it."""
    bot, dev = settled_bot(monkeypatch, tap_jitter_px=0.0, tap_delay=0.0)
    boxes: list[dict] = []

    for rule in bot.controls.snapshot().strategy.actions:
        bot.find_and_click_image(rule.as_action(), boxes)

    assert boxes
    for box in boxes:
        assert (box["tap_x"], box["tap_y"]) == config.buy_point((box["x"], box["y"]))


def test_jitter_moves_the_tap_off_the_buy_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the feature: without this the bot taps the identical
    pixel on every purchase of a given upgrade, for its whole life."""
    bot, dev = settled_bot(monkeypatch, tap_jitter_px=8.0, tap_delay=0.0)
    boxes: list[dict] = []

    for _ in range(5):
        bot._last_click.clear()
        for rule in bot.controls.snapshot().strategy.actions:
            bot.find_and_click_image(rule.as_action(), boxes)

    exact = [
        (b["tap_x"], b["tap_y"]) == config.buy_point((b["x"], b["y"])) for b in boxes
    ]
    assert not all(exact), "every tap landed on the un-jittered point"


def test_a_jittered_tap_stays_within_the_radius(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tap outside the buy square buys nothing while still publishing a
    Tapped event - a silent failure, so the bound is load-bearing."""
    bot, dev = settled_bot(monkeypatch, tap_jitter_px=8.0, tap_delay=0.0)
    boxes: list[dict] = []

    for _ in range(5):
        bot._last_click.clear()
        for rule in bot.controls.snapshot().strategy.actions:
            bot.find_and_click_image(rule.as_action(), boxes)

    for box in boxes:
        anchor_x, anchor_y = config.buy_point((box["x"], box["y"]))
        assert abs(box["tap_x"] - anchor_x) <= 8
        assert abs(box["tap_y"] - anchor_y) <= 8


# -- the pre-tap pause ------------------------------------------------------
def test_the_pause_happens_before_the_tap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delay after the tap is not a reaction time, it is just a slower
    loop - and the loop already has its own interval for that."""
    bot, dev = settled_bot(monkeypatch, tap_delay=0.12)
    order: list[str] = []
    monkeypatch.setattr(
        tower_bot_mod.jitter,
        "pause",
        lambda *a, **k: (order.append("pause"), 0.0)[1],
    )
    dev.click.side_effect = lambda x, y: order.append("tap")

    bot.run_once()

    assert order[:2] == ["pause", "tap"]


def test_the_pause_uses_the_strategy_delay_and_fraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    bot, dev = settled_bot(monkeypatch, tap_delay=0.3, timing_jitter=0.2)
    monkeypatch.setattr(
        tower_bot_mod.jitter, "pause", lambda *a, **k: (calls.append(a), 0.0)[1]
    )

    bot.run_once()

    assert calls, "no pre-tap pause was taken"
    assert calls[0][0] == 0.3
    assert calls[0][1] == 0.2


def test_a_zero_delay_takes_no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    bot, dev = settled_bot(monkeypatch, tap_delay=0.0)
    monkeypatch.setattr(
        tower_bot_mod.jitter, "pause", lambda s, f, *a, **k: (slept.append(s), 0.0)[1]
    )

    bot.run_once()

    assert all(s == 0.0 for s in slept)


# -- cooldowns only ever stretch -------------------------------------------
def test_a_jittered_cooldown_never_taps_sooner_than_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason jitter.stretch exists apart from jitter.spread.

    click_cooldown is a functional minimum - config.CLICK_COOLDOWN_SECONDS
    says why: it stops a burst of taps on a button that is already
    animating. A symmetric ±50% jitter would sometimes shorten it to 0.5s
    and let exactly that burst back in. Here the clock advances to 0.99s of
    a 1.0s cooldown, which must still be gated no matter what the jitter
    rolled.
    """
    bot, dev = settled_bot(
        monkeypatch, click_cooldown=1.0, timing_jitter=0.5, tap_delay=0.0
    )
    clock = [1000.0]
    monkeypatch.setattr(tower_bot_mod.time, "monotonic", lambda: clock[0])

    bot.run_once()
    first = len(taps(dev))
    assert first, "expected the first scan to tap"

    for _ in range(40):
        clock[0] += 0.99
        bot.run_once()
        clock[0] -= 0.99  # same 0.99s gap every time, never 1.0

    assert len(taps(dev)) == first


# -- the between-scan interval ---------------------------------------------
def _recorded_waits(bot, monkeypatch: pytest.MonkeyPatch, rounds: int) -> list[float]:
    """Run the loop `rounds` times, capturing what it waited on each pass.

    run_once is stubbed out: these tests are about the between-scan wait,
    and a real scan per round costs ~0.4s of template matching that proves
    nothing here. The scan path has its own tests above.
    """
    waits: list[float] = []
    monkeypatch.setattr(bot, "run_once", lambda **_: None)

    def fake_wait(timeout: float | None = None) -> bool:
        waits.append(timeout)
        if len(waits) >= rounds:
            bot._running = False
        return False

    monkeypatch.setattr(bot._stopping, "wait", fake_wait)
    bot.run_forever()
    return waits


def test_the_scan_interval_is_jittered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The most visible pattern in the un-jittered bot: a tap every 2.000s,
    for hours."""
    bot, _dev = settled_bot(monkeypatch, interval=2.0, timing_jitter=0.15, tap_delay=0.0)

    waits = _recorded_waits(bot, monkeypatch, 8)

    assert len(set(waits)) > 1, "every wait was identical - still a metronome"
    for wait in waits:
        assert 1.7 <= wait <= 2.3


def test_the_interval_varies_in_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spread(), not stretch(): a loop that only ever waited *longer* than
    its nominal interval is still a pattern, just a slower one."""
    bot, _dev = settled_bot(monkeypatch, interval=2.0, timing_jitter=0.15, tap_delay=0.0)

    waits = _recorded_waits(bot, monkeypatch, 20)

    assert any(w < 2.0 for w in waits)
    assert any(w > 2.0 for w in waits)


def test_a_jittered_interval_never_drops_below_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MIN_INTERVAL exists because "a zero or negative interval is a busy
    loop against ADB". A dial already at the floor must not jitter under it.
    """
    bot, _dev = settled_bot(
        monkeypatch,
        interval=strategy_mod.MIN_INTERVAL,
        timing_jitter=strategy_mod.MAX_TIMING_JITTER,
        tap_delay=0.0,
    )

    waits = _recorded_waits(bot, monkeypatch, 20)

    for wait in waits:
        assert wait >= strategy_mod.MIN_INTERVAL


def test_an_explicit_interval_override_is_not_jittered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_forever's docstring promises an explicit number is "used exactly
    as given, every iteration" - that is the seam letting tests run the loop
    without sleeping, and jittering it would put the sleep back."""
    bot, _dev = settled_bot(monkeypatch, timing_jitter=0.5, tap_delay=0.0)
    waits: list[float] = []
    monkeypatch.setattr(bot, "run_once", lambda **_: None)

    def fake_wait(timeout: float | None = None) -> bool:
        waits.append(timeout)
        if len(waits) >= 5:
            bot._running = False
        return False

    monkeypatch.setattr(bot._stopping, "wait", fake_wait)
    bot.run_forever(interval=0.0)

    assert waits == [0.0] * 5


def test_stale_studio_assignment_blocks_legacy_template_purchase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dev = settled_bot(monkeypatch, tap_delay=0.0)
    execution = MagicMock()
    execution.owns_plan = True
    execution.runtime = None
    execution.authority_current.return_value = False
    bot.strategy_execution = execution
    results = [bot.find_and_click_image(rule.as_action())
        for rule in bot.controls.snapshot().strategy.actions]
    assert not any(results)
    assert execution.authority_current.called
    assert not taps(dev)
