"""The OCR autopilot as the scan loop drives it.

test_autopilot.py hands `step()` a frame directly, so it can prove what the
executor decides but never that anything calls it. That gap is exactly where
this bug lived: the loop reached `step()` on the ATTACK tab and nowhere else,
so the executor's own tests all passed while the bot sat on the UTILITY tab
doing nothing for the rest of the run.

The tab is the whole variable. `screens/in_run.png` is the ATTACK header
crop, so a DEFENSE or UTILITY frame is IN_RUN by its cash counter alone and
carries no panel anchor - and the autopilot's first act under any economy
preset is to open UTILITY, which is a tab it cannot come back from if the
loop stops stepping it there.
"""

from __future__ import annotations

import time
from typing import Callable

import pytest

import screens
from strategy import Claims, Shopping, ShoppingRule
from tower_bot import TowerBot


@pytest.mark.parametrize("frame", ["in_run_lit", "in_run_defense", "in_run_utility"])
def test_the_autopilot_steps_on_every_upgrade_tab(
    bot_in_run_on: Callable[[str], TowerBot],
    monkeypatch: pytest.MonkeyPatch,
    frame: str,
) -> None:
    bot = bot_in_run_on(frame)
    bot.controls.apply({"autopilot": {"enabled": True}})
    stepped: list[str] = []
    monkeypatch.setattr(
        bot.autopilot, "step", lambda *args, **kwargs: bool(stepped.append(frame))
    )

    bot.run_once()

    assert stepped == [frame], (
        f"the scan loop never stepped the autopilot on {frame}; a tab whose "
        "header the ATTACK template does not match is still a live run"
    )


def test_the_overlay_shows_what_the_autopilot_read(
    bot_in_run_on: Callable[[str], TowerBot],
) -> None:
    """`boxes` is a local list the legacy matcher fills as it goes, and
    run_once() hands it to the frame buffer at the end of every pass. The
    autopilot path never touched it, so taking over in-run buying meant
    set_boxes([]) blanked the device view on every scan.
    """
    from frames import FrameBuffer

    bot = bot_in_run_on("in_run_lit")
    bot.frames = FrameBuffer()
    bot.controls.apply({"autopilot": {"enabled": True}})

    bot.run_once()

    assert bot.frames.boxes(), "the device view went blank while the autopilot was deciding"


def test_reroll_collects_stats_once_from_a_clear_main_menu(
    bot_on_main_menu: Callable[..., TowerBot],
) -> None:
    class Progress:
        requested = False

        def shopping_policy(self, policy):
            return policy

        def stats_due(self):
            return not self.requested

        def note_stats_requested(self):
            self.requested = True

    bot = bot_on_main_menu(Shopping())
    progress = Progress()
    bot.reroll_progress = progress
    bot.run_once()
    assert progress.requested
    assert bot.collection.active



@pytest.mark.parametrize("worthwhile", [True, False])
def test_reroll_game_over_only_goes_home_when_the_target_may_be_affordable(
    worthwhile: bool,
) -> None:
    from tests.conftest import _shopping_bot
    import screens

    class Progress:
        def workshop_worthwhile(self):
            return worthwhile

        def stats_due(self):
            return False

    bot = _shopping_bot(
        "game_over", state=screens.ScreenState.GAME_OVER,
        policy=Shopping(enabled=True, workshop=(ShoppingRule("Damage", "ATTACK"),)),
        auto_navigate=True)
    bot.runs.completed = 1
    bot.reroll_progress = Progress()
    asked: list[bool] = []
    navigate = bot.navigator.maybe_navigate
    bot.navigator.maybe_navigate = lambda *args, **kwargs: (
        asked.append(kwargs["go_home"]), navigate(*args, **kwargs))[1]
    bot.run_once()
    assert asked == [worthwhile]

# -- Claim cadence in the loop ---------------------------------------------
def test_a_due_claim_is_armed_from_the_main_menu(bot_on_main_menu: Callable[..., TowerBot]) -> None:
    """The same frame shopping.begin() reserves, and only when it declined.
    A never-claimed account with a read best wave owes a milestones claim."""
    bot = bot_on_main_menu(Shopping(), claims=Claims(enabled=True))  # shopping disabled by default
    bot._best_wave = 137  # a read best wave, as prepare_store() would seed it
    bot.run_once()
    assert bot.milestones_claim.active


def test_a_still_active_walk_is_not_re_armed_on_the_next_frame(
    bot_on_main_menu: Callable[..., TowerBot]
) -> None:
    """request() returning False is the normal case, not an error: the walk
    from the previous frame is still holding the menus.

    Covers the still-active case specifically: on frame 2 the walk is still
    `.active`, so run_once()'s frame guard (the "actions held" early return)
    never even reaches _offer_claim. That structural guard is a different,
    weaker invariant than the one _claimed_best_wave protects - see
    test_a_completed_walk_is_not_re_armed_for_the_same_best below, which
    proves the real thing by letting the walk go idle first.
    """
    bot = bot_on_main_menu(Shopping(), claims=Claims(enabled=True))
    bot._best_wave = 137
    bot.run_once()
    first = bot.milestones_claim.snapshot()["requested_at"]
    bot.run_once()
    assert bot.milestones_claim.snapshot()["requested_at"] == first


def test_a_completed_walk_is_not_re_armed_for_the_same_best(
    bot_on_main_menu: Callable[..., TowerBot]
) -> None:
    """A completed claim keeps its best wave and must not immediately re-arm."""
    bot = bot_on_main_menu(Shopping(), claims=Claims(enabled=True))
    bot._best_wave = 137
    # Keep missions out of the way so the only thing under test is the
    # milestones cadence: an unset last_missions is immediately due on its
    # own and would otherwise arm bot.claim on the second scan below,
    # muddying what this test is checking.
    bot._last_claim["missions"] = time.time()
    bot._last_claim["mail"] = time.time()

    bot.run_once()
    assert bot.milestones_claim.active

    bot.milestones_claim._finish("completed", "claimed", "Returned home.", time.time())
    assert not bot.milestones_claim.active

    bot.run_once()
    assert bot._claimed_wave[None] == 137
    assert not bot.milestones_claim.active, (
        "a completed walk was re-armed for a best wave it already checked"
    )


def test_failed_milestones_walk_restores_unclaimed_wave(
    bot_on_main_menu: Callable[..., TowerBot]
) -> None:
    bot = bot_on_main_menu(Shopping(), claims=Claims(enabled=True))
    bot._best_wave = 137
    bot._claimed_wave = {None: 90}
    bot._last_claim["missions"] = time.time()
    bot._last_claim["mail"] = time.time()

    bot.run_once()
    assert bot.milestones_claim.active
    bot.milestones_claim.cancel("milestones_control_absent", "The button was not found.")
    bot.run_once()
    assert bot._claimed_wave[None] == 90
    assert not bot.milestones_claim.active

    bot._milestones_retry_at = 0.
    bot.run_once()
    assert bot.milestones_claim.active


def test_a_disabled_cadence_arms_nothing(bot_on_main_menu: Callable[..., TowerBot]) -> None:
    bot = bot_on_main_menu(Shopping())  # claims disabled (the fixture's default)
    bot._best_wave = 137
    bot.run_once()
    assert not bot.claim.active
    assert not bot.milestones_claim.active


def test_a_shopping_visit_keeps_the_frame_from_a_due_claim(
    bot_on_main_menu: Callable[..., TowerBot]
) -> None:
    """One maintenance walk at a time. A visit that took this frame means the
    claim waits - and it must not be armed only to be refused."""
    bot = bot_on_main_menu(
        Shopping(enabled=True, workshop=(ShoppingRule(name="Damage", category="ATTACK"),)),
        claims=Claims(enabled=True),
    )  # BOTH claims and a due shopping visit
    bot._best_wave = 137
    bot.run_once()
    assert bot.shopping.active
    assert not bot.claim.active
    assert not bot.milestones_claim.active


def test_battle_is_not_tapped_on_the_frame_a_claim_arms(
    bot_on_main_menu: Callable[..., TowerBot]
) -> None:
    """A claim walk is a maintenance walk exactly like a shopping visit, and
    the auto-navigate gate already suppresses BATTLE for `self.shopping.active`
    (`visiting` at run_once()'s top) - but had no equivalent term for
    `self.claim.active` / `self.milestones_claim.active`. So a claim armed by
    _offer_claim() this same frame fell straight through into the nav block
    below it: BATTLE got tapped, a run started, and the walk lost its next
    frame mid-errand to a live run it can never recognise - burning
    STEP_FRAME_BUDGET frames with actions held and autopilot suspended before
    failing outright. Worse, `_last_claim`/`_claimed_best_wave` were already
    recorded before any of that, so the missed claim reads as a taken one.
    """
    bot = bot_on_main_menu(Shopping(), claims=Claims(enabled=True))
    bot._best_wave = 137  # a read best wave, as prepare_store() would seed it

    bot.run_once()

    assert bot.milestones_claim.active, "the claim should have armed this frame"
    navigated = [e.target for e in bot.bus.published if e.type == "Navigated"]
    assert navigated == [], f"BATTLE was tapped on the frame the claim armed: {navigated}"


# -- Milestones: which ladder, and leaving the death screen for it ----------
def _asked_go_home(bot: TowerBot) -> list[bool]:
    asked: list[bool] = []
    navigate = bot.navigator.maybe_navigate
    bot.navigator.maybe_navigate = lambda *args, **kwargs: (
        asked.append(kwargs["go_home"]), navigate(*args, **kwargs))[1]
    return asked


@pytest.mark.parametrize("best, claimed, owed", [(30, 21, True), (29, 21, False)])
def test_game_over_goes_home_only_for_a_crossed_ladder_row(
    best: int, claimed: int, owed: bool,
) -> None:
    """RETRY never passes the main menu, the only screen a claim is offered
    on - so a crossed row has to take HOME, and a best that crossed nothing
    must not spend the trip."""
    from tests.conftest import _shopping_bot

    bot = _shopping_bot("game_over", state=screens.ScreenState.GAME_OVER,
                        policy=Shopping(), auto_navigate=True,
                        claims=Claims(enabled=True))
    bot._ladder_tier = 1
    bot._tier_best_wave = {1: best}
    bot._claimed_wave = {1: claimed}
    asked = _asked_go_home(bot)
    bot.run_once()
    assert asked == [owed]


def test_each_tier_has_its_own_ladder() -> None:
    """Tier 2 wave 50 owes Tier 2's rows though Tier 1's best is far higher."""
    from tests.conftest import _shopping_bot

    bot = _shopping_bot("game_over", state=screens.ScreenState.GAME_OVER,
                        policy=Shopping(), auto_navigate=True,
                        claims=Claims(enabled=True))
    bot._best_wave = 137
    bot._claimed_wave = {None: 137, 1: 137, 2: 40}
    bot._ladder_tier = 2
    bot._tier_best_wave = {1: 137, 2: 50}
    asked = _asked_go_home(bot)
    bot.run_once()
    assert asked == [True]


@pytest.mark.parametrize("frame, armed", [("menu_main_bluestacks_1920", True),
                                           ("menu_milestones_entry", False)])
def test_the_milestones_badge_arms_a_claim_no_wave_explains(frame: str, armed: bool) -> None:
    """Best == claimed-at, so nothing but the badge can make the ladder due."""
    from tests.conftest import _shopping_bot

    bot = _shopping_bot(frame, state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=True,
                        claims=Claims(enabled=True))
    bot.runs.completed = 1
    bot._best_wave = 21
    bot._claimed_wave = {None: 21}
    bot._last_claim["missions"] = time.time()
    # menu_main_bluestacks_1920 also carries the Cards tab's "new" arrow,
    # which would arm the first Cards visit ahead of the claim. A visit just
    # run keeps it from being offered again for a while.
    bot.cards_intro.request()
    bot.cards_intro.cancel("test", "The Cards arrow is not under test here.")
    bot.run_once()
    assert bot.milestones_claim.active is armed


def test_game_over_returns_home_for_a_previously_seen_badge() -> None:
    from tests.conftest import _shopping_bot

    bot = _shopping_bot("game_over", state=screens.ScreenState.GAME_OVER,
                        policy=Shopping(), auto_navigate=True,
                        claims=Claims(enabled=True))
    bot._best_wave = 21
    bot._claimed_wave = {None: 21}
    bot._milestones_badge = True
    bot._last_claim["missions"] = time.time()
    bot._last_claim["mail"] = time.time()
    asked = _asked_go_home(bot)
    bot.run_once()
    assert asked == [True]


def test_failed_badge_claim_is_retried_without_waiting_an_hour() -> None:
    """A failed menu lookup must leave the red badge eligible after a short backoff."""
    from tests.conftest import _shopping_bot

    bot = _shopping_bot("menu_main_bluestacks_1920",
                        state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=True,
                        claims=Claims(enabled=True))
    bot.runs.completed = 1
    bot._best_wave = 21
    bot._claimed_wave = {None: 21}
    bot._last_claim["missions"] = time.time()
    bot._last_claim["mail"] = time.time()
    bot.cards_intro.request()
    bot.cards_intro.cancel("test", "The Cards arrow is not under test here.")

    bot.run_once()
    assert bot.milestones_claim.active
    bot.milestones_claim.cancel("milestones_control_absent", "The button was not found.")
    bot.run_once()
    assert not bot.milestones_claim.active
    assert bot._milestones_badge
    assert "milestones" not in bot._last_claim
    assert bot._milestones_retry_at > time.time()

    bot._milestones_retry_at = 0.
    bot.run_once()
    assert bot.milestones_claim.active
