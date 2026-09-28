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


def test_event_page_is_visit_and_return_even_with_a_ready_claim():
    """Claiming Event missions belongs to main's Events walk (events_claim),
    armed from home by the Events dot. In battle the visitor only returns:
    a ready Claim on the Event page is never tapped."""
    v, d = visitor(), Device()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    icon = v.current
    boxes = (
        ocr.TextBox("EVENT - STEAMPUNK", 0.99, config.Rect(50, 50, 400, 60)),
        ocr.TextBox("Claim", 0.99, config.Rect(500, 900, 150, 60)),
        ocr.TextBox("Tap To Return To Game", 0.99, config.Rect(400, 2200, 300, 80)),
    )
    assert step(v, d, "event_page", 2, boxes) is Outcome.TAPPED
    assert d.taps[-1] == (550, 2240)                  # the footer, not (575, 930)
    assert (575, 930) not in d.taps
    assert step(v, d, "collapsed_badged", 3) is Outcome.HOLD
    assert not v.active
    assert any(isinstance(e, events.BattleMenuIconHandled) and e.icon == icon
               and e.outcome == "visited" for e in v._bus.published)


class _Recording(Device):
    """Records every input with the frame (and OCR boxes) it was issued on."""

    def __init__(self):
        super().__init__()
        self.inputs = []          # (point, frame name, screen, boxes)
        self.on = None

    def click(self, x, y):
        super().click(x, y)
        self.inputs.append(((x, y), *self.on))

    def swipe(self, *args):
        super().swipe(*args)
        self.inputs.append(((args[0], args[1]), *self.on))


def _drive(v, d, flow, start=0):
    for t, (name, boxes, screen) in enumerate(flow, start):
        screen = frame(name) if screen is None else screen
        d.on = (name, screen, boxes)
        v.observe(screen=screen, boxes=lambda b=boxes: b, device=d, policy=POLICY,
                  now=t, in_run=True)


def _inside(point, rect):
    x, y, w, h = rect
    return x <= point[0] <= x + w and y <= point[1] <= y + h


def _assert_every_input_safe(d):
    assert d.inputs
    for point, name, screen, boxes in d.inputs:
        exit_battle = battle_menu._locate(screen, TEMPLATES, "exit_battle")
        if exit_battle.status == "located":
            assert not _inside(point, exit_battle.rect), (name, point)
        for box in boxes:
            if battle_menu.is_price(box.text):
                assert not _inside(point, box.rect), (name, point, box.text)


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_never_taps_a_price_or_exit_battle(monkeypatch):
    """Every tap and swipe start, across full flows, lands outside EXIT
    BATTLE and outside every price box of the frame it was issued on."""
    reads = {name: ocr.read(frame(name)) for name in
             ("event_info_modal", "event_page", "store_top", "store_free_tiles")}

    # Event visit: hamburger, star, info modal X, return footer, close X.
    state = BattleMenuState(None)
    state.handled("cart", battle_menu.Badge("red"), now=0)
    v, d = visitor(state), _Recording()
    _drive(v, d, [("collapsed_badged", (), None), ("open_badged", (), None),
                  ("event_info_modal", reads["event_info_modal"], None),
                  ("event_page", reads["event_page"], None),
                  ("open_badged", (), None), ("collapsed_badged", (), None)])
    assert len(d.taps) == 5 and not v.active
    _assert_every_input_safe(d)

    # Store visit: two scrolls, then the return footer; then a free tile
    # (seen, never tapped) and the return footer; then close.
    state = BattleMenuState(None)
    state.handled("event", battle_menu.Badge("blue"), now=0)
    v, d = visitor(state), _Recording()
    _drive(v, d, [("collapsed_badged", (), None), ("open_badged", (), None),
                  ("store_top", reads["store_top"], None),
                  ("store_top", reads["store_top"], None),
                  ("store_top", reads["store_top"], None)])
    assert len(d.swipes) == 2
    _drive(v, d, [("open_badged", (), None), ("collapsed_badged", (), None)], start=5)
    assert not v.active
    state = BattleMenuState(None)
    state.handled("event", battle_menu.Badge("blue"), now=0)
    v2 = visitor(state)
    _drive(v2, d, [("collapsed_badged", (), None), ("open_badged", (), None),
                   ("store_free_tiles", reads["store_free_tiles"], None),
                   ("open_badged", (), None), ("collapsed_badged", (), None)])
    assert not v2.active
    free = battle_menu.free_gem_tile(frame("store_free_tiles"), reads["store_free_tiles"])
    assert free is not None and free not in d.taps
    _assert_every_input_safe(d)

    # Bail from IN_PAGE: the modal's X never reads, the step budget runs
    # out, and the way out is the footer of the page it bailed on.
    monkeypatch.setattr(battle_menu, "event_modal_close", lambda s, b: None)
    state = BattleMenuState(None)
    state.handled("cart", battle_menu.Badge("red"), now=0)
    v, d = visitor(state), _Recording()
    _drive(v, d, [("collapsed_badged", (), None), ("open_badged", (), None)]
           + [("event_info_modal", reads["event_info_modal"], None)]
           * (config.BATTLE_MENU_STEP_FRAMES + 1))
    assert not v.active and len(d.taps) == 3
    assert any(isinstance(e, events.BattleMenuIconHandled) and e.outcome == "failed"
               for e in v._bus.published)
    _assert_every_input_safe(d)


class _Refusing(Device):
    """A device that refuses (raises on) the next input, like a supervisor
    that has not yet observed a fresh frame."""

    def __init__(self):
        super().__init__()
        self.refuse = False

    def click(self, x, y):
        if self.refuse:
            raise RuntimeError("refused")
        super().click(x, y)


def test_a_refused_icon_tap_leaves_the_visit_where_it_was():
    v, d = visitor(), _Refusing()
    step(v, d, "collapsed_badged", 0)
    d.refuse = True
    with pytest.raises(RuntimeError):
        step(v, d, "open_badged", 1)
    assert v.current is None and v.active   # nothing popped, nothing entered
    d.refuse = False
    assert step(v, d, "open_badged", 2) is Outcome.TAPPED
    assert v.current is not None and v._entered == 2


def test_a_refused_bail_tap_records_nothing_and_retries(monkeypatch):
    state = BattleMenuState(None)
    v, d = visitor(state), _Refusing()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    icon = v.current
    footer = (ocr.TextBox("Tap To Return To Game", 0.99, config.Rect(400, 2200, 300, 80)),)
    monkeypatch.setattr(battle_menu, "read_page", lambda b: battle_menu.PageReading(
        "none", (550, 2240)) if b else battle_menu.PageReading("none", None))
    for t in range(config.BATTLE_MENU_STEP_FRAMES):
        step(v, d, "event_page", 2 + t, footer)          # HOLD: page unread
    d.refuse = True
    with pytest.raises(RuntimeError):
        step(v, d, "event_page", 10, footer)             # bail's tap refused
    assert v.active and v.current == icon
    assert not any(isinstance(e, events.BattleMenuIconHandled) for e in v._bus.published)
    d.refuse = False
    assert step(v, d, "event_page", 11, footer) is Outcome.TAPPED
    assert d.taps[-1] == (550, 2240) and not v.active
    failed = [e for e in v._bus.published if isinstance(e, events.BattleMenuIconHandled)]
    assert [(e.icon, e.outcome) for e in failed] == [(icon, "failed")]


def _paint_out(screen, icon):
    target = battle_menu._locate(screen, TEMPLATES, icon)
    x, y, w, h = target.rect
    screen[y:y + h, x:x + w] = 0
    return screen


def test_a_menu_missing_an_icon_still_visits_the_others():
    # No event running: the star is gone, the cart is still badged.
    open_no_event = _paint_out(frame("open_badged").copy(), "event")
    v, d = visitor(), Device()
    step(v, d, "collapsed_badged", 0)
    assert v.observe(screen=open_no_event, boxes=lambda: (), device=d, policy=POLICY,
                     now=1, in_run=True) is Outcome.TAPPED
    assert v.current == "cart"
    menu = battle_menu.read_menu(frame("open_badged"), TEMPLATES)
    assert d.taps[-1] == menu["cart"].point


def test_a_menu_of_only_missing_and_clear_icons_closes_once_and_stays_shut(monkeypatch):
    state = BattleMenuState(None)
    v, d = visitor(state), Device()
    partial = {i: battle_menu.IconReading(i, (900, 300), None) for i in ("missions", "cards")}
    monkeypatch.setattr(battle_menu, "read_menu", lambda s, t: partial)
    step(v, d, "collapsed_badged", 0)                          # open
    assert step(v, d, "open_badged", 1) is Outcome.TAPPED      # close, nothing due
    assert d.taps[-1] == battle_menu.close_point(frame("open_badged"), TEMPLATES)
    step(v, d, "collapsed_badged", 2)                          # CLOSING -> IDLE
    for t in (3, 500, 5000):
        assert step(v, d, "collapsed_badged", t) is Outcome.IDLE
    assert len(d.taps) == 2
