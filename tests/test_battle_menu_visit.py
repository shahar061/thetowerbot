from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

import battle_menu
import config
import events
import ocr
from battle_menu_state import BattleMenuState
from battle_menu_visit import BattleMenuVisit, Outcome
from vision import TemplateCache

FIX = Path(__file__).parent / "fixtures" / "battle_menu"
TEMPLATES = TemplateCache(Path(__file__).resolve().parent.parent / "templates")
POLICY = SimpleNamespace(tap_jitter_px=0, tap_delay=0.0, timing_jitter=0.0)


class Device:
    def __init__(self):
        self.taps, self.swipes = [], []

    def click(self, x, y):
        self.taps.append((x, y))

    def swipe(self, *args):
        self.swipes.append(args)


class Bus:
    def __init__(self):
        self.published = []

    def publish(self, event):
        self.published.append(event)
        return event


def frame(name):
    return cv2.imread(str(FIX / f"{name}.png"))


def visitor(state=None):
    return BattleMenuVisit(Bus(), TEMPLATES, state or BattleMenuState(None),
                           sleep=lambda s: None, rng=random.Random(0))


def step(v, device, name, now, boxes=()):
    return v.observe(screen=frame(name), boxes=lambda: boxes, device=device,
                     policy=POLICY, now=now, in_run=True)


def test_idle_clear_menu_does_nothing(monkeypatch):
    v, d = visitor(), Device()
    monkeypatch.setattr(battle_menu, "collapsed",
                        lambda s, t: battle_menu.MenuButton((1015, 65), False))
    assert step(v, d, "collapsed_badged", 0) is Outcome.IDLE
    assert d.taps == [] and not v.active


def test_badged_hamburger_opens_menu():
    v, d = visitor(), Device()
    assert step(v, d, "collapsed_badged", 0) is Outcome.TAPPED
    assert v.active and len(d.taps) == 1 and d.taps[0][0] > 950


def test_open_menu_taps_a_due_icon_never_settings():
    v, d = visitor(), Device()
    step(v, d, "collapsed_badged", 0)
    assert step(v, d, "open_badged", 1) is Outcome.TAPPED
    menu = battle_menu.read_menu(frame("open_badged"), TEMPLATES)
    assert d.taps[-1] in {menu["cart"].point, menu["event"].point}


def test_not_in_run_never_starts():
    v, d = visitor(), Device()
    assert v.observe(screen=frame("collapsed_badged"), boxes=lambda: (), device=d,
                     policy=POLICY, now=0, in_run=False) is Outcome.IDLE
    assert d.taps == []


def test_settings_only_menu_closes_and_is_not_reopened(monkeypatch):
    state = BattleMenuState(None)
    v, d = visitor(state), Device()
    clear = {i: battle_menu.IconReading(i, (900, 300), None) for i in battle_menu.ICONS}
    monkeypatch.setattr(battle_menu, "read_menu", lambda s, t: clear)
    step(v, d, "collapsed_badged", 0)                 # open
    assert step(v, d, "open_badged", 1) is Outcome.TAPPED   # close
    close = battle_menu.close_point(frame("open_badged"), TEMPLATES)
    assert d.taps[-1] == close
    step(v, d, "collapsed_badged", 2)                 # CLOSING -> IDLE
    for t in (3, 500, 5000):
        assert step(v, d, "collapsed_badged", t) is Outcome.IDLE
    assert len(d.taps) == 2


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_event_visit_returns_and_cools_down():
    state = BattleMenuState(None)
    state.handled("cart", battle_menu.Badge("red"), now=0)   # only the event is due
    v, d = visitor(state), Device()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    assert v.current == "event"
    event_boxes = ocr.read(frame("event_page"))
    assert step(v, d, "event_page", 2, event_boxes) is Outcome.TAPPED
    assert d.taps[-1][1] > 2100                       # Tap To Return To Game
    assert step(v, d, "open_badged", 3) is Outcome.TAPPED   # handled, nothing due -> close
    menu = battle_menu.read_menu(frame("open_badged"), TEMPLATES)
    assert state.due(menu, now=4) == []
    assert set(state.due(menu, now=1801)) == {"event", "cart"}


def test_unreadable_page_bails_out_after_budget(monkeypatch):
    state = BattleMenuState(None)
    v, d = visitor(state), Device()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    icon = v.current
    blank = frame("open_badged").copy()
    blank[:] = 0
    for t in range(config.BATTLE_MENU_STEP_FRAMES + 1):
        v.observe(screen=blank, boxes=lambda: (), device=d, policy=POLICY, now=2 + t, in_run=False)
    assert not v.active
    assert any(isinstance(e, events.BattleMenuIconHandled) and e.outcome == "failed"
               and e.icon == icon for e in v._bus.published)
    assert state.due({icon: battle_menu.IconReading(icon, (0, 0), battle_menu.Badge("red"))},
                     now=100) == []


def test_never_taps_a_price_or_exit_battle():
    """Every tap across a full visit lands outside EXIT BATTLE and price boxes."""
    v, d = visitor(), Device()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    exit_box = battle_menu._locate(frame("open_badged"), TEMPLATES, "exit_battle").rect
    x, y, w, h = exit_box
    assert all(not (x <= tx <= x + w and y <= ty <= y + h) for tx, ty in d.taps)
