"""How a shopping visit sits inside the scan loop.

Three collaborators read the same frame - the screen tracker, the run tracker
and the shopping session - and the interactions between them are where this
feature can break something that already works. Each one gets a test.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import cv2

import config
import digits
import events
import tower_bot
import vision
from shopping import ShoppingSession
from lab_plan import LabDecision
from lab_visit import LabVisit, LabVisitResult
from labs import LabJob, LabsReading, LabsState
from strategy import Shopping, ShoppingRule

_FIXTURES = Path(__file__).parent / "fixtures"


def frame(name: str):
    image = cv2.imread(str(_FIXTURES / f"{name}.png"), cv2.IMREAD_COLOR)
    assert image is not None, f"missing fixture: {name}.png"
    return image


def a_policy(**over) -> Shopping:
    base = dict(
        enabled=True, armed=False,
        workshop=(ShoppingRule(name="Damage", category="ATTACK"),),
    )
    return Shopping(**{**base, **over})


def navigated(bus) -> list[str]:
    """The targets Navigator actually tapped. Navigator has no
    `last_target` attribute and must not grow one just so a test can read
    it - events.Navigated is the public record of the thing being tested."""
    return [e.target for e in bus.published if e.type == "Navigated"]


def test_navigation_is_suppressed_while_a_visit_is_live(bot_on_main_menu) -> None:
    """Navigator taps BATTLE on MAIN_MENU. Left alone it starts a run in the
    middle of an errand."""
    bot = bot_on_main_menu(a_policy())
    bot.shopping.begin(a_policy(), run_count=1)
    assert bot.shopping.active
    bot.run_once()
    assert navigated(bot.bus) == [], "BATTLE was tapped mid-visit"


def test_navigation_resumes_once_the_visit_ends(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(a_policy(enabled=False))
    assert not bot.shopping.active
    bot.run_once()
    assert "BATTLE" in navigated(bot.bus)


def test_due_visit_starts_before_battle_navigation(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(a_policy())
    bot.runs.completed = 1
    bot.run_once()
    assert bot.shopping.active
    assert navigated(bot.bus) == []


def test_reroll_lab_check_arms_before_workshop(bot_on_main_menu) -> None:
    from tests.conftest import _shopping_bot

    bot = _shopping_bot("menu_main_labs_unlocked", state=tower_bot.screens.ScreenState.MAIN_MENU,
                        policy=a_policy(), auto_navigate=True)
    progress = Mock()
    progress.shopping_policy.return_value = a_policy()
    progress.stats_due.return_value = False
    progress.lab_due.return_value = True
    progress.initial_workshop_due.return_value = False
    bot.reroll_progress = progress
    bot.lab_visit = LabVisit(bot.templates)

    bot.run_once()

    assert bot.lab_visit.active
    progress.note_lab_unlocked.assert_called_once_with("labs_tab")
    assert not bot.shopping.active
    assert navigated(bot.bus) == []


def test_reroll_does_not_open_labs_without_a_visible_unlocked_tab(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(a_policy())
    progress = Mock()
    progress.shopping_policy.return_value = a_policy()
    progress.stats_due.return_value = False
    progress.initial_workshop_due.return_value = False
    progress.lab_due.return_value = True
    bot.reroll_progress = progress
    bot.lab_visit = LabVisit(bot.templates)

    bot.run_once()

    assert not bot.lab_visit.active
    progress.note_lab_unlocked.assert_not_called()
    progress.note_lab_locked.assert_called_once_with("labs_tab")
    progress.lab_due.assert_not_called()


def test_reroll_does_not_open_labs_when_the_tab_is_unreadable(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(a_policy())
    bot._screen[:] = 0
    progress = Mock()
    progress.shopping_policy.return_value = a_policy()
    progress.stats_due.return_value = False
    progress.initial_workshop_due.return_value = False
    progress.lab_due.return_value = True
    bot.reroll_progress = progress
    bot.lab_visit = LabVisit(bot.templates)

    bot.run_once()

    assert not bot.lab_visit.active
    progress.note_lab_unlocked.assert_not_called()
    progress.note_lab_locked.assert_not_called()
    progress.lab_due.assert_not_called()


def test_reroll_workshop_resumes_when_lab_check_not_due(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(a_policy())
    progress = Mock()
    progress.shopping_policy.return_value = a_policy()
    progress.stats_due.return_value = False
    progress.lab_due.return_value = False
    bot.reroll_progress = progress
    bot.lab_visit = LabVisit(bot.templates)

    bot.run_once()

    assert bot.shopping.active
    assert not bot.lab_visit.active


def test_single_emulator_does_not_construct_a_lab_visit(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(a_policy())
    assert bot.lab_visit is None


def test_confirmed_lab_start_records_one_job_and_one_coin_debit(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(a_policy())
    bot.reroll_progress = Mock()
    account = Mock()
    bot.lab_state = LabsState(account)
    job = LabJob(1, "labs.game-speed", "Game Speed Lv.1", 5000., 4000.,
                 None, "unknown", "researching", 1., (100, 300, 200, 50))
    frames = tuple(LabsReading(1000. + index, 1080, 2400, f"frame-{index}",
                               1, "observed", (), (job,)) for index in range(2))
    result = LabVisitResult("started", "game_speed_confirmed",
                            LabDecision("start", price=300, wallet_coins=400),
                            job, 300, frames)

    bot._finish_lab_visit(result)
    bot._finish_lab_visit(result)

    account.record_labs.assert_called_once()
    started = [event for event in bot.bus.published
               if isinstance(event, events.LabResearchStarted)]
    assert len(started) == 1
    assert (started[0].coins_before, started[0].coins_after) == (400, 100)


def test_failed_lab_start_does_not_record_a_spend(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(a_policy())
    bot.reroll_progress = Mock()
    bot.lab_state = LabsState(Mock())
    bot._finish_lab_visit(LabVisitResult(
        "failed", "purchase_unconfirmed", LabDecision("unknown")))
    assert not [event for event in bot.bus.published
                if isinstance(event, events.LabResearchStarted)]


def test_pause_cancels_a_live_lab_visit_without_a_tap(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(a_policy())
    bot.reroll_progress = Mock()
    bot.lab_visit = LabVisit(bot.templates)
    assert bot.lab_visit.request()
    bot.controls.apply({"paused": True})

    bot.run_once()

    assert not bot.lab_visit.active
    assert not [event for event in bot.bus.published
                if isinstance(event, events.Tapped) and event.action.startswith("lab:")]


def test_unknown_snapshots_are_suppressed_while_a_visit_is_live(bot_on_workshop) -> None:
    """A workshop page reads UNKNOWN to the screen tracker by design. Without
    this, every visit fills unknown/ with pictures of the workshop and evicts
    the genuine mysteries the directory exists to hold."""
    bot = bot_on_workshop(a_policy())
    bot.shopping.begin(a_policy(), run_count=1)
    for _ in range(4):
        bot.run_once()
    assert bot.snapshots.written == []


def test_unknown_snapshots_still_happen_outside_a_visit(bot_on_workshop) -> None:
    """The suppression must be scoped, not switched off.

    The frame is noise rather than the workshop this fixture supplies, and
    that swap is the point. This test's own assertion message says what it
    is about - "an unmodelled screen" - and a workshop page never was one:
    pages.classify_page names it at 1.000. Standing it in for a mystery is
    what let the real bug hide behind a green test, because suppression
    scoped to a live visit and suppression scoped to an unnamed frame agree
    on every frame except this one. Noise is a screen nobody modelled, so
    the trail is the correct behaviour on it, whatever else changes.
    """
    import numpy as np

    bot = bot_on_workshop(a_policy(enabled=False))
    rng = np.random.default_rng(0)
    bot._screen = rng.integers(0, 256, bot._screen.shape, dtype=np.uint8)

    for _ in range(4):
        bot.run_once()

    assert bot.snapshots.written, "an unmodelled screen must still leave a trail"


def test_a_visit_does_not_block_a_run_from_opening_or_closing(bot_on_main_menu) -> None:
    """Run boundaries come from the screen tracker's own confirmations alone
    - shopping must never suppress or substitute for them.

    bot_on_main_menu's frame never changes on its own (that is what makes it
    useful for the other tests in this file), so an assertion that merely
    watched `runs.current_id` sit still while feeding it the same frame over
    and over would pass against a stub that did nothing: RunTracker.observe()
    short-circuits on every repeat of an already-confirmed reading, so
    RunTracker.transition() is never even called. Proving the property needs
    a REAL transition: feed a genuine in-run frame until the tracker confirms
    IN_RUN, then a menu frame until it confirms leaving it, and check that a
    run actually opens and closes on schedule.

    The visit itself does not survive the in-run frame - a fight page is
    UNKNOWN to pages.classify_page (see pages.py), so it self-aborts within
    two scans, the same off-page-streak safety valve any other unrecognised
    page would trip. That is expected, and is not what this test is about:
    the point is that the run opens and closes correctly regardless of
    whatever shopping happens to be doing at the time.
    """
    bot = bot_on_main_menu(a_policy())
    bot.shopping.begin(a_policy(), run_count=1)
    assert bot.shopping.active
    before_completed = bot.runs.completed

    bot._screen = frame("in_run_lit")
    for _ in range(2):  # SCREEN_CONFIRMATIONS consecutive readings to confirm
        bot.run_once()
    assert bot.runs.current_id is not None, "a run must still open normally"

    bot._screen = frame("main_menu")
    for _ in range(2):
        bot.run_once()
    assert bot.runs.current_id is None, "a run must still close normally"
    assert bot.runs.completed == before_completed + 1


def test_no_visit_starts_while_a_run_is_live(bot_in_run) -> None:
    """A visit begins from MAIN_MENU only. Starting one mid-fight would tap
    the workshop tab over a live run."""
    bot = bot_in_run(a_policy())
    for _ in range(3):
        bot.run_once()
    assert not bot.shopping.active


def test_a_paused_bot_does_not_shop(bot_on_main_menu) -> None:
    """Pause means 'still scanning, not tapping', and that must cover the one
    tap path that costs money - including a visit already under way, not
    just one that has not started yet. Pausing BEFORE begin() is not enough
    to prove this: that only exercises begin()'s own paused guard, which was
    never the gap. The visit is started first, live, and paused only once
    active - the shape that let a real bug through: advance() had no paused
    check at all and kept tapping mid-errand regardless."""
    bot = bot_on_main_menu(a_policy(armed=True))
    bot.shopping.begin(a_policy(armed=True), run_count=1)
    assert bot.shopping.active
    bot.controls.apply({"paused": True})
    for _ in range(4):
        bot.run_once()
    assert bot.device.taps == []
    assert bot.shopping.active, "pause must freeze the visit, not end it"


def test_reaching_the_run_cap_does_not_start_a_visit(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(a_policy())
    for _ in range(3):
        bot.run_once(max_runs=1)
    assert not bot.shopping.active


def test_shopping_is_disabled_when_the_ocr_engine_will_not_load(monkeypatch) -> None:
    """No reader means no balance and no price, so no purchase can ever be
    approved - say so once at startup rather than failing silently on every
    visit. Spec §10's startup gate.

    build_shopping() always returns a session, never None - a disabled one
    carries a non-empty `disabled_reason` and declines every begin()."""
    monkeypatch.setattr(tower_bot.ocr, "available", lambda: False)
    session = tower_bot.build_shopping(bus=None, templates=None)
    assert session.disabled_reason, "an engine that will not load must disable shopping"
    assert session.begin(a_policy(enabled=True), run_count=1) is False


def test_an_unbuilt_glyph_atlas_no_longer_disables_shopping(monkeypatch) -> None:
    """The header used to be read glyph by glyph, so shopping was gated on
    an atlas only a manual harvesting session could complete. The header is
    read with OCR now; the atlas has no say."""
    monkeypatch.setattr(tower_bot.ocr, "available", lambda: True)
    monkeypatch.setattr(digits.AtlasCache, "get", lambda self, name: None)
    session = tower_bot.build_shopping(bus=None, templates=None)
    assert session.disabled_reason is None


# --- Getting to the Workshop at all ---------------------------------------
# begin() is only ever called on MAIN_MENU, and the navigator taps RETRY on
# GAME_OVER - which starts the next run straight from the death screen. Left
# alone the bot loops IN_RUN -> GAME_OVER -> IN_RUN and never once offers
# begin() a frame to say yes to, so an enabled, armed policy buys nothing.


def test_game_over_goes_home_when_a_visit_is_due(bot_on_game_over) -> None:
    bot = bot_on_game_over(a_policy())
    bot.run_once()
    assert navigated(bot.bus) == ["HOME"], "RETRY skipped the Workshop detour"


def test_game_over_retries_when_shopping_is_off(bot_on_game_over) -> None:
    """The detour costs a trip through the menu; a bot with nothing to buy
    must not pay it."""
    bot = bot_on_game_over(a_policy(enabled=False))
    bot.run_once()
    assert navigated(bot.bus) == ["RETRY"]


def test_game_over_retries_on_a_non_visiting_run(bot_on_game_over) -> None:
    """visit_every_n_runs=2 means every other run goes straight back in."""
    bot = bot_on_game_over(a_policy(visit_every_n_runs=2))
    bot.shopping._last_run_count = bot.runs.completed
    bot.run_once()
    assert navigated(bot.bus) == ["RETRY"]


def test_due_agrees_with_begin(bot_on_game_over) -> None:
    """due() is begin()'s gate without the side effects. If they ever drift,
    the bot detours home and then declines to shop - a wasted trip every run."""
    policy = a_policy()
    session = bot_on_game_over(policy).shopping

    assert session.due(policy, run_count=1) is True
    assert session.begin(policy, run_count=1) is True
    # The cadence has now been spent for this run count.
    assert session.due(policy, run_count=1) is False
    assert session.begin(policy, run_count=1) is False


def test_due_is_false_when_the_session_is_disabled() -> None:
    session = ShoppingSession(
        vision.TemplateCache(config.TEMPLATE_DIR), events.EventBus(),
        digits.NumberReader(), disabled_reason="the OCR engine will not load",
    )
    assert session.due(a_policy(), run_count=1) is False
