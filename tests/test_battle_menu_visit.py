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
        self.taps, self.swipes, self.backs = [], [], 0

    def click(self, x, y):
        self.taps.append((x, y))

    def swipe(self, *args):
        self.swipes.append(args)

    def press_back(self):
        self.backs += 1


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
    for t in (3, 500):
        assert step(v, d, "collapsed_badged", t) is Outcome.IDLE
    assert len(d.taps) == 2
    assert step(v, d, "collapsed_badged", 600) is Outcome.TAPPED


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
             ("event_info_modal", "event_page", "store_top", "store_ad_ready",
              "ad_upsell", "ad_reward_claim", "store_ad_claimed")}

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

    # Store visit: two scrolls, then the return footer; then the verified
    # 20-gem ad tile, upsell close, reward claim, return footer, menu close.
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
                   ("store_ad_ready", reads["store_ad_ready"], None),
                   ("ad_upsell", reads["ad_upsell"], None),
                   ("ad_reward_claim", reads["ad_reward_claim"], None),
                   ("store_ad_claimed", reads["store_ad_claimed"], None),
                   ("open_badged", (), None), ("collapsed_badged", (), None)])
    assert not v2.active
    free = battle_menu.daily_ad_tile(frame("store_ad_ready"), reads["store_ad_ready"])
    assert free is not None and free in d.taps
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


def test_a_menu_of_only_missing_and_clear_icons_closes_then_rechecks(monkeypatch):
    state = BattleMenuState(None)
    v, d = visitor(state), Device()
    partial = {i: battle_menu.IconReading(i, (900, 300), None) for i in ("missions", "cards")}
    monkeypatch.setattr(battle_menu, "read_menu", lambda s, t: partial)
    step(v, d, "collapsed_badged", 0)                          # open
    assert step(v, d, "open_badged", 1) is Outcome.TAPPED      # close, nothing due
    assert d.taps[-1] == battle_menu.close_point(frame("open_badged"), TEMPLATES)
    step(v, d, "collapsed_badged", 2)                          # CLOSING -> IDLE
    for t in (3, 500):
        assert step(v, d, "collapsed_badged", t) is Outcome.IDLE
    assert len(d.taps) == 2
    assert step(v, d, "collapsed_badged", 600) is Outcome.TAPPED


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_daily_ad_claims_only_after_reward_screen_and_verified_balance():
    state = BattleMenuState(None)
    state.handled("event", battle_menu.Badge("blue"), now=0)
    v, d = visitor(state), Device()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    ready = ocr.read(frame("store_ad_ready"))
    assert step(v, d, "store_ad_ready", 2, ready) is Outcome.TAPPED
    assert d.taps[-1] == battle_menu.daily_ad_tile(frame("store_ad_ready"), ready)
    assert step(v, d, "ad_upsell", 38, ocr.read(frame("ad_upsell"))) is Outcome.TAPPED
    assert step(v, d, "ad_reward_claim", 40,
                ocr.read(frame("ad_reward_claim"))) is Outcome.TAPPED
    assert d.taps[-1] == battle_menu.ad_reward_claim(
        frame("ad_reward_claim"), ocr.read(frame("ad_reward_claim")))
    claimed = ocr.read(frame("store_ad_claimed"))
    assert step(v, d, "store_ad_claimed", 42, claimed) is Outcome.TAPPED
    assert any(isinstance(e, events.DailyAdGemClaimed)
               and (e.gems_before, e.gems_after, e.delta) == (8, 28, 20)
               for e in v._bus.published)
    assert d.backs == 0


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_daily_ad_timeout_backs_once_and_records_failure() -> None:
    class AdDevice(Device):
        def shell(self, command: str) -> str:
            if command.startswith("dumpsys window"):
                return ("mCurrentFocus=Window{123 u0 com.TechTreeGames.TheTower/"
                        "com.google.android.gms.ads.AdActivity}")
            if command.startswith("uiautomator dump"):
                return "<hierarchy></hierarchy>"
            raise AssertionError(command)

    state = BattleMenuState(None)
    state.handled("event", battle_menu.Badge("blue"), now=0)
    v, d = visitor(state), AdDevice()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    step(v, d, "store_ad_ready", 2, ocr.read(frame("store_ad_ready")))
    assert step(v, d, "ad_upsell", 63, ()) is Outcome.HOLD
    assert d.backs == 0
    assert step(v, d, "ad_upsell", 183, ()) is Outcome.TAPPED
    assert d.backs == 1
    assert step(v, d, "store_ad_ready", 184,
                ocr.read(frame("store_ad_ready"))) is Outcome.TAPPED
    assert any(isinstance(e, events.ClaimUncertain) and e.target == "daily_ad_gems"
               for e in v._bus.published)
    assert d.backs == 1


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_daily_ad_play_overlay_return_resumes_battle_and_settles_uncertain() -> None:
    class PlayDevice(Device):
        focus = ""

        def shell(self, command: str) -> str:
            assert command.startswith("dumpsys window")
            return self.focus

    state = BattleMenuState(None)
    state.handled("event", battle_menu.Badge("blue"), now=0)
    v, d = visitor(state), PlayDevice()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    step(v, d, "store_ad_ready", 2, ocr.read(frame("store_ad_ready")))
    ad_dir = Path(__file__).parent / "fixtures/in_game_ad"

    def ad_step(name: str, now: float, boxes: tuple[ocr.TextBox, ...] = (),
                *, in_run: bool = False) -> Outcome:
        screen = cv2.imread(str(ad_dir / f"{name}.jpg"))
        return v.observe(screen=screen, boxes=lambda: boxes, device=d,
                         policy=POLICY, now=now, in_run=in_run)

    d.focus = ("mCurrentFocus=Window{8dc4b2c u0 com.android.vending/"
               "com.google.android.finsky.transparentmainactivity.HsdpAlias}")
    assert ad_step("play_store_overlay_83", 34) is Outcome.TAPPED
    d.focus = ("mCurrentFocus=Window{123 u0 com.TechTreeGames.TheTower/"
               "com.unity3d.player.UnityPlayerActivity}")
    resume = cv2.imread(str(ad_dir / "ad_return_resume_83.jpg"))
    cloud = cv2.imread(str(ad_dir / "ad_return_cloud_83.jpg"))
    assert ad_step("ad_return_resume_83", 38, ocr.read(resume)) is Outcome.TAPPED
    assert ad_step("ad_return_cloud_83", 42, ocr.read(cloud)) is Outcome.TAPPED
    assert step(v, d, "collapsed_badged", 46) is Outcome.IDLE

    assert d.taps[-3:] == [(1000, 940), (722, 1425), (539, 1638)]
    assert d.backs == 0
    assert not v.active
    assert any(isinstance(event, events.ClaimUncertain)
               and event.target == "daily_ad_gems" for event in v._bus.published)


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_daily_ad_closes_detected_end_card_after_playing():
    state = BattleMenuState(None)
    state.handled("event", battle_menu.Badge("blue"), now=0)
    v, d = visitor(state), Device()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    step(v, d, "store_ad_ready", 2, ocr.read(frame("store_ad_ready")))
    assert step(v, d, "ad_end_card", 20, ()) is Outcome.HOLD
    assert step(v, d, "ad_end_card", 45, ()) is Outcome.TAPPED
    assert d.taps[-1] == battle_menu.ad_end_card_close(frame("ad_end_card"), TEMPLATES)
    assert d.backs == 0


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_daily_ad_uses_accessible_close_on_dark_reward_pill():
    class AdDevice(Device):
        def shell(self, command: str) -> str:
            if command.startswith("dumpsys window"):
                return ("mCurrentFocus=Window{123 u0 com.TechTreeGames.TheTower/"
                        "com.google.android.gms.ads.AdActivity}")
            if command.startswith("uiautomator dump"):
                return ('<hierarchy><node text="Reward granted" />'
                        '<node text="Close" clickable="true" '
                        'bounds="[981,33][1050,99]" /></hierarchy>')
            raise AssertionError(command)

    state = BattleMenuState(None)
    state.handled("event", battle_menu.Badge("blue"), now=0)
    visitor_state, device = visitor(state), AdDevice()
    step(visitor_state, device, "collapsed_badged", 0)
    step(visitor_state, device, "open_badged", 1)
    step(visitor_state, device, "store_ad_ready", 2, ocr.read(frame("store_ad_ready")))
    end_card = cv2.imread(str(Path(__file__).parent / "fixtures/in_game_ad"
                              / "ad_reward_granted_dark_82.jpg"))
    result = visitor_state.observe(screen=end_card, boxes=lambda: (), device=device,
                                   policy=POLICY, now=45, in_run=True)
    assert result is Outcome.TAPPED
    assert device.taps[-1] == (1015, 66)


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_daily_ad_closes_meta_skip_and_landing_then_claims():
    state = BattleMenuState(None)
    state.handled("event", battle_menu.Badge("blue"), now=0)
    v, d = visitor(state), Device()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    step(v, d, "store_ad_ready", 2, ocr.read(frame("store_ad_ready")))
    assert step(v, d, "ad_meta_complete", 45, ()) is Outcome.TAPPED
    assert d.taps[-1] == (982, 180)
    assert step(v, d, "ad_meta_landing", 50, ()) is Outcome.TAPPED
    assert d.taps[-1] == (77, 75)
    assert step(v, d, "ad_reward_claim", 51,
                ocr.read(frame("ad_reward_claim"))) is Outcome.TAPPED
    assert step(v, d, "store_ad_claimed", 52,
                ocr.read(frame("store_ad_claimed"))) is Outcome.TAPPED
    assert any(isinstance(e, events.DailyAdGemClaimed) for e in v._bus.published)
    assert d.backs == 0


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_verified_ad_reward_is_recorded_once_if_return_tap_is_refused():
    state = BattleMenuState(None)
    state.handled("event", battle_menu.Badge("blue"), now=0)
    v, d = visitor(state), _Refusing()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    step(v, d, "store_ad_ready", 2, ocr.read(frame("store_ad_ready")))
    step(v, d, "ad_reward_claim", 3, ocr.read(frame("ad_reward_claim")))
    claimed = ocr.read(frame("store_ad_claimed"))
    d.refuse = True
    with pytest.raises(RuntimeError):
        step(v, d, "store_ad_claimed", 4, claimed)
    d.refuse = False
    assert step(v, d, "store_ad_claimed", 5, claimed) is Outcome.TAPPED
    assert len([e for e in v._bus.published
                if isinstance(e, events.DailyAdGemClaimed)]) == 1


@pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")
def test_daily_ad_recovery_closes_an_ad_that_ignores_back() -> None:
    ads = FIX.parent / "in_game_ad"

    class UnityDevice(Device):
        hierarchy = (ads / "unity_playable_82.xml").read_text()

        def shell(self, command: str) -> str:
            if command.startswith("dumpsys window"):
                return (ads / "unity_focus_82.txt").read_text()
            if command.startswith("uiautomator dump"):
                return self.hierarchy
            raise AssertionError(command)

    def ad(name, now):
        return v.observe(screen=cv2.imread(str(ads / f"unity_{name}_82.jpg")),
                         boxes=lambda: (), device=d, policy=POLICY, now=now, in_run=True)

    state = BattleMenuState(None)
    state.handled("event", battle_menu.Badge("blue"), now=0)
    v, d = visitor(state), UnityDevice()
    step(v, d, "collapsed_badged", 0)
    step(v, d, "open_badged", 1)
    step(v, d, "store_ad_ready", 2, ocr.read(frame("store_ad_ready")))
    for now in (33, 36, 39):  # Skip taps the ad did not act on.
        assert ad("playable", now) is Outcome.TAPPED
    taps = len(d.taps)
    assert ad("playable", 183) is Outcome.TAPPED
    assert d.backs == 1 and len(d.taps) == taps
    assert ad("playable", 186) is Outcome.TAPPED
    assert len(d.taps) == taps + 1
    d.hierarchy = (ads / "unity_end_82.xml").read_text()
    assert ad("end", 190) is Outcome.TAPPED
    assert d.taps[-1] == (999, 105)
    claim = ocr.read(frame("ad_reward_claim"))
    assert step(v, d, "ad_reward_claim", 193, claim) is Outcome.TAPPED
    assert d.taps[-1] == battle_menu.ad_reward_claim(frame("ad_reward_claim"), claim)
    assert step(v, d, "store_ad_claimed", 195,
                ocr.read(frame("store_ad_claimed"))) is Outcome.TAPPED
    assert any(isinstance(e, events.DailyAdGemClaimed) for e in v._bus.published)
    assert d.backs == 1
