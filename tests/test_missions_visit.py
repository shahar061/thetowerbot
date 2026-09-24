"""The read-only Missions visit: Home -> Missions -> read -> Home.

Every case drives the real scan loop (`TowerBot.run_once`) over committed
1080x2400 captures, so what is proven here is the wiring as much as the
transaction: the taps it issues, the frames it refuses to act on, and the
events the loop publishes for them.

Two main-menu captures matter and they are not interchangeable.
`menu_main.png` shows the MISSIONS control (`nav/missions.png` matches it at
1.0000); `main_menu.png` does not (.3258, against a .9 threshold). The second
is not a broken fixture - it is a recorded main menu without that control on
it, and it is what proves the transaction waits rather than tapping where the
button usually is.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import pytest

import config
import events
import missions_visit
import ocr
from strategy import Shopping

FIXTURES = Path(__file__).parent / 'fixtures'

# Measured by template match on the captures named above.
MISSIONS_CONTROL = (987, 351)
RETURN_CONTROL = (540, 2293)


def recorded(name: str = 'menu_missions') -> tuple[ocr.TextBox, ...]:
    return tuple(ocr.TextBox(b['text'], b['confidence'], config.Rect(*b['rect']))
                 for b in json.loads((FIXTURES / 'ocr' / f'{name}.json').read_text()))


def image(name: str) -> Any:
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    assert frame is not None, f'missing fixture: {name}.png'
    return frame


def home(name: str = 'menu_main') -> tuple[str, tuple[ocr.TextBox, ...]]:
    """A main menu frame with no OCR on it: no panel, no missions title."""
    return (name, ())


def missions() -> tuple[str, tuple[ocr.TextBox, ...]]:
    return ('menu_missions', recorded())


def drive(bot: Any, monkeypatch: pytest.MonkeyPatch,
          script: list[tuple[str, tuple[ocr.TextBox, ...]]]) -> None:
    """One scan per (fixture, OCR boxes) pair."""
    for name, boxes in script:
        bot._screen = image(name)
        monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: boxes)
        bot.run_once()


def published(bot: Any, kind: type) -> list[Any]:
    """Every event of one kind the bot published; the test bus keeps them."""
    return [e for e in bot.bus.published if isinstance(e, kind)]


def taps(bot: Any) -> list[tuple[int, int]]:
    return [(int(x), int(y)) for x, y in bot.device.taps]


def visit_taps(bot: Any) -> list[tuple[int, int]]:
    """Only the taps THIS transaction made, by the events it published.

    Not `bot.device.taps`: once a visit ends the guard releases and the bot
    goes back to its ordinary navigation, so the device log holds taps that
    are nothing to do with the walk.
    """
    return [(e.x, e.y) for e in published(bot, events.Tapped)
            if e.action.startswith('missions_visit:')]


@pytest.fixture
def visiting(bot_on_main_menu: Any) -> Any:
    """An armed visit with jitter off, so the tap coordinates are exact."""
    bot = bot_on_main_menu(Shopping(enabled=False))
    bot.controls.apply({'tap_jitter_px': 0, 'tap_delay': 0})
    bot.visit.request(now=100.)
    # menu_main.png also carries the Cards tab's "new" arrow, which would arm
    # the first Cards visit the moment this one hands the menu back. A visit
    # just run keeps it from being offered again for a while.
    bot.cards_intro.request()
    bot.cards_intro.cancel('test', 'The Cards arrow is not under test here.')
    return bot


# --- the walk -------------------------------------------------------------

def test_the_visit_walks_home_missions_and_home_reading_only(
        visiting: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Success: two located taps, one per step, and a recorded reading."""
    drive(visiting, monkeypatch, [
        home(),        # the MISSIONS control is tapped from this frame
        missions(),    # the page is identified and read: tap the return control
        # Two home frames: the tracker debounces, so the first frame back is
        # not yet a confirmed main menu.
        home(), home(),
    ])
    snapshot = visiting.visit.snapshot()
    assert snapshot['status'] == 'completed'
    assert snapshot['result']['reason'] == 'visited'
    assert snapshot['result']['screen_id'] == 'missions.daily'
    assert snapshot['trail'] == ['open_missions', 'read', 'confirm_home']
    assert visit_taps(visiting) == [MISSIONS_CONTROL, RETURN_CONTROL]
    # The reading the walk existed to take outlives the page: by the last
    # frame the bot is back on the menu and `current_screen_id` is None.
    snapshot_after = visiting.missions.snapshot()
    assert snapshot_after['latest']['shown'] == 2
    assert snapshot_after['current_screen_id'] is None
    # And the guard released: the bot is navigating for itself again on the
    # last frame, which is what makes this a visit rather than a dead end.
    assert taps(visiting)[-1] not in (MISSIONS_CONTROL, RETURN_CONTROL)


def test_every_tap_the_visit_makes_is_published_and_drawn(
        visiting: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tap nobody can find afterwards is the failure this guards against.

    Both halves are checked, and the overlay half needs a real FrameBuffer:
    without one the loop skips drawing entirely, and a crosshair labelled
    with the wrong transaction would go unnoticed.
    """
    from frames import FrameBuffer
    visiting.frames = FrameBuffer()
    drive(visiting, monkeypatch, [home()])
    # Measured on menu_main.png: the MISSIONS control matches at 1.0 over
    # (912, 332, 150, 38), whose centre is the tap.
    assert visiting.frames.boxes() == [{
        'name': 'missions_visit:missions_control', 'x': 912, 'y': 332, 'w': 150, 'h': 38,
        'tap_x': 987, 'tap_y': 351, 'score': pytest.approx(1., abs=.01), 'tapped': True,
    }]
    drive(visiting, monkeypatch, [missions()])
    assert visiting.frames.boxes() == [{
        'name': 'missions_visit:return_control', 'x': 236, 'y': 2266, 'w': 608, 'h': 54,
        'tap_x': 540, 'tap_y': 2293, 'score': pytest.approx(1., abs=.01), 'tapped': True,
    }]
    drive(visiting, monkeypatch, [home(), home()])
    tapped = published(visiting, events.Tapped)
    assert [e.action for e in tapped] == [
        'missions_visit:open_missions', 'missions_visit:read']
    assert [(e.x, e.y) for e in tapped] == [MISSIONS_CONTROL, RETURN_CONTROL]


# --- refusals -------------------------------------------------------------

def test_a_main_menu_without_the_control_is_waited_out_never_guessed(
        visiting: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """`main_menu.png` is a recorded main menu with no MISSIONS control on
    it. The button's usual coordinate is known and must not be used."""
    drive(visiting, monkeypatch, [home('main_menu')] * 8)
    result = visiting.visit.snapshot()['result']
    assert result['status'] == 'failed'
    assert result['reason'] == 'missions_control_absent'
    assert visit_taps(visiting) == []


def test_the_visit_never_taps_before_the_main_menu_is_confirmed(
        visiting: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control is on this frame, but the frame is the missions page
    itself - the anchor for home is not on it."""
    drive(visiting, monkeypatch, [missions()] * 8)
    result = visiting.visit.snapshot()['result']
    assert result['reason'] == 'home_not_confirmed'
    assert visit_taps(visiting) == []


def test_a_page_that_never_arrives_stops_the_visit_without_a_second_tap(
        visiting: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    drive(visiting, monkeypatch, [home()] + [home('menu_cards')] * 8)
    result = visiting.visit.snapshot()['result']
    assert result['reason'] == 'missions_not_reached'
    assert result['screen_id'] is None
    assert visit_taps(visiting) == [MISSIONS_CONTROL]


def test_a_missions_page_we_cannot_read_is_not_recorded_as_visited(
        visiting: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The title is there, the reading is not. The visit stops with that
    reason rather than tapping its way home off an unread page."""
    boxes = tuple(b for b in recorded() if b.text != '2/8 Missions')
    drive(visiting, monkeypatch, [home()] + [('menu_missions', boxes)] * 8)
    result = visiting.visit.snapshot()['result']
    assert result['reason'] == 'missions_unreadable'
    assert result['screen_id'] is None
    assert visit_taps(visiting) == [MISSIONS_CONTROL]


def test_pausing_the_bot_ends_a_walk_already_in_flight(
        visiting: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    drive(visiting, monkeypatch, [home()])
    visiting.controls.apply({'paused': True})
    drive(visiting, monkeypatch, [missions()])
    result = visiting.visit.snapshot()['result']
    assert result['status'] == 'failed' and result['reason'] == 'paused'
    assert visit_taps(visiting) == [MISSIONS_CONTROL]


def test_a_visit_is_never_queued_behind_the_one_already_walking(
        visiting: Any) -> None:
    assert visiting.visit.request(now=101.) is False


# --- the passive guard ----------------------------------------------------

def test_the_missions_page_holds_actions_even_with_no_visit_armed(
        bot_on_main_menu: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bot has no verified target on this page, so nothing may tap while
    it is up - including the navigation that would otherwise start a run."""
    bot = bot_on_main_menu(Shopping(enabled=False))
    drive(bot, monkeypatch, [missions()])
    skipped = published(bot, events.Skipped)
    assert skipped and skipped[-1].reason == 'missions_screen_guard'
    assert taps(bot) == []
    assert bot.missions.current_evidence()['screen_id'] == 'missions.daily'


def test_an_ordinary_menu_frame_is_not_held_by_the_missions_guard(
        bot_on_main_menu: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard has to be the missions page and not "any menu", or it would
    quietly stop the bot everywhere."""
    bot = bot_on_main_menu(Shopping(enabled=False))
    drive(bot, monkeypatch, [home()])
    assert not [e for e in published(bot, events.Skipped)
                if e.reason == 'missions_screen_guard']


def test_the_visit_exposes_no_field_a_tap_could_be_read_from_after_the_fact(
        visiting: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The snapshot is a report, not a source of coordinates for a later
    frame: every tap is located again on the frame it is made from."""
    drive(visiting, monkeypatch, [home(), missions(), home(), home()])
    payload = json.dumps(visiting.visit.snapshot())
    assert not any(key in payload for key in ('"x"', '"y"', '"point', '"rect'))


def test_the_module_offers_no_way_to_spend_anything() -> None:
    source = Path(missions_visit.__file__).read_text()
    assert 'price' not in source and 'purchase' not in source and 'buy' not in source


class _Evidence:
    """A reader stub that reports one fixed answer about the last frame."""

    def __init__(self, **evidence: Any) -> None:
        self._evidence = {'screen_id': None, 'error': None, 'scanned': True, **evidence}

    def current_evidence(self) -> dict[str, Any]:
        return dict(self._evidence)


def test_the_anchor_alone_cannot_confirm_home_while_the_page_is_still_up() -> None:
    """The screen tracker is debounced (config.SCREEN_CONFIRMATIONS is 2), so
    it keeps reporting MAIN_MENU for a scan after the missions page appears.
    Home off that anchor alone would let a visit tap - or report a return -
    from the very page it is meant to be away from. The missions reader
    looked at the same frame and is asked too.

    Driven directly rather than through run_once: the loop's own ordering
    means CONFIRM_HOME never sees the first frame of the page, so this state
    is real in the game and not reachable from the harness.
    """
    visit = missions_visit.MissionsVisit()
    visit.request(now=100.)
    action = visit.advance(
        screen=image('menu_missions'), device=None, templates=None,
        readings=_Evidence(),                                  # no panel: clear
        missions=_Evidence(screen_id='missions.daily'),        # but the page IS up
        state='MAIN_MENU',                                     # and the anchor still says home
        now=101.,
    )
    assert action is None
    assert visit.snapshot()['step'] == 'open_missions'
    # It waits rather than failing outright, and never leaves the step.
    for frame in range(102, 110):
        visit.advance(screen=image('menu_missions'), device=None, templates=None,
                      readings=_Evidence(), missions=_Evidence(screen_id='missions.daily'),
                      state='MAIN_MENU', now=float(frame))
    assert visit.snapshot()['result']['reason'] == 'home_not_confirmed'


def test_a_reader_that_reached_no_conclusion_is_not_a_clear_menu() -> None:
    """`scanned` False is the wrong geometry or a failed engine. It is not
    evidence that the missions page is absent, so it cannot confirm home."""
    visit = missions_visit.MissionsVisit()
    visit.request(now=100.)
    for frame in range(101, 110):
        visit.advance(screen=image('menu_main'), device=None, templates=None,
                      readings=_Evidence(), missions=_Evidence(scanned=False),
                      state='MAIN_MENU', now=float(frame))
    assert visit.snapshot()['result']['reason'] == 'home_not_confirmed'
