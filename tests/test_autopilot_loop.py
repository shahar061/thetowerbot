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
from types import SimpleNamespace
from typing import Callable

import pytest

import screens
from strategy import Claims, Shopping, ShoppingRule
from tower_bot import TowerBot


def test_same_frame_policy_refresh_honors_an_operator_pause(
    bot_in_run_on: Callable[[str], TowerBot], monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = bot_in_run_on('in_run_lit')
    bot.controls.apply({'autopilot': {'enabled': True}})
    refreshed = []
    def step(*args: object, **kwargs: object) -> bool:
        bot.controls.apply({'paused': True})
        refreshed.append(kwargs['refresh_policy'](0))
        return False
    monkeypatch.setattr(bot.autopilot, 'step', step)
    bot.run_once()
    assert len(refreshed) == 1 and not refreshed[0].enabled


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


def test_an_in_run_scan_reports_the_wave_the_autopilot_read(
    bot_in_run_on: Callable[[str], TowerBot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import events
    import tower_bot

    bot = bot_in_run_on("in_run_lit")
    bot.controls.apply({"autopilot": {"enabled": True}})
    monkeypatch.setattr(bot.autopilot, "step", lambda *args, **kwargs: False)
    # Through the real frame_combat, which returns the wave as a float.
    real = tower_bot.frame_combat
    monkeypatch.setattr(tower_bot, "frame_combat", lambda context, observation: real(
        {}, SimpleNamespace(combat={"wave": 4812})))

    bot.run_once()

    scans = [e for e in bot.bus.published if isinstance(e, events.ScanCompleted)]
    assert scans[-1].wave == 4812


def test_a_scan_off_the_run_reports_no_wave(
    bot_on_main_menu: Callable[..., TowerBot],
) -> None:
    import events

    bot = bot_on_main_menu(Shopping())
    bot._scan_wave = 4812  # left over from the run that just ended

    bot.run_once()

    scans = [e for e in bot.bus.published if isinstance(e, events.ScanCompleted)]
    assert scans[-1].wave is None
    assert bot._scan_wave is None


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

        def note_menu_wallet(self, coins):
            pass

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
        def initial_workshop_due(self) -> bool:
            return False

        def workshop_worthwhile(self, *, publish_estimate=False, detour=False):
            return worthwhile

        def stats_due(self):
            return False

        def lab_due(self):
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


@pytest.mark.parametrize(('states', 'expected'), [
    (('idle', 'researching', 'researching'), True),
    (('researching', 'researching', 'researching'), False),
])
def test_game_over_detours_for_an_idle_direct_start_lab(
    states: tuple[str, str, str], expected: bool,
) -> None:
    from tests.conftest import _shopping_bot
    from lab_plan import LabVisitOptions

    class Progress:
        def lab_visit_options(self) -> LabVisitOptions:
            return LabVisitOptions(direct_start=True)

        def lab_unlocked(self) -> bool:
            return True

        def lab_due(self) -> bool:
            return False

        def stats_due(self) -> bool:
            return False

    bot = _shopping_bot('game_over', state=screens.ScreenState.GAME_OVER,
                        policy=Shopping(), auto_navigate=True)
    bot.reroll_progress = Progress()
    snapshot = SimpleNamespace(
        slots_owned=3, observed_at=time.time(),
        slots=tuple(SimpleNamespace(state=state, confirmed=True,
            slot=index, expected_finish=time.time() + 3600)
            for index, state in enumerate(states, 1)))
    bot.lab_runtime = SimpleNamespace(snapshot=lambda: snapshot)
    bot._update_maintenance = lambda *args: None
    asked: list[bool] = []
    navigate = bot.navigator.maybe_navigate
    bot.navigator.maybe_navigate = lambda *args, **kwargs: (
        asked.append(kwargs['go_home']), navigate(*args, **kwargs))[1]
    bot.run_once()
    assert asked == [expected]
    assert bot._lab_followup_due is expected


def test_direct_lab_detour_paces_idle_and_stale_running_checks() -> None:
    from tests.conftest import _shopping_bot
    from lab_plan import LabVisitOptions

    bot = _shopping_bot('game_over', state=screens.ScreenState.GAME_OVER,
                        policy=Shopping(), auto_navigate=True)
    bot.reroll_progress = SimpleNamespace(
        lab_visit_options=lambda: LabVisitOptions(direct_start=True),
        lab_unlocked=lambda: True, route_runtime=None)
    slot = SimpleNamespace(slot=1, state='idle', confirmed=True, expected_finish=None)
    snapshot = SimpleNamespace(slots_owned=1, observed_at=1000., slots=(slot,))
    bot.lab_runtime = SimpleNamespace(snapshot=lambda: snapshot)
    assert bot._direct_lab_visit_due(1000.)
    assert not bot._direct_lab_visit_due(1001.)
    assert bot._direct_lab_visit_due(1301.)
    slot.state, slot.expected_finish = 'researching', 5000.
    snapshot.observed_at = 1301.
    assert not bot._direct_lab_visit_due(1302.)
    assert bot._direct_lab_visit_due(1602.)  # old running proof must be refreshed
    assert not bot._direct_lab_visit_due(1603.)

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
    bot._last_claim["missions"] = time.time()
    bot._last_claim["mail"] = time.time()
    bot._last_menu_badge_check_at = time.time()
    asked = _asked_go_home(bot)
    bot.run_once()
    assert asked == [owed]


def test_game_over_goes_home_when_missions_are_due() -> None:
    from tests.conftest import _shopping_bot

    bot = _shopping_bot("game_over", state=screens.ScreenState.GAME_OVER,
                        policy=Shopping(), auto_navigate=True,
                        claims=Claims(enabled=True))
    bot._ladder_tier = 1
    bot._tier_best_wave = {1: 2}
    bot._claimed_wave = {1: 2}
    asked = _asked_go_home(bot)
    bot.run_once()
    assert asked == [True]


@pytest.mark.parametrize("elapsed, expected", [(3500., False), (3601., True)])
def test_game_over_checks_menu_badges_hourly_even_without_a_known_badge(
    elapsed: float, expected: bool,
) -> None:
    from tests.conftest import _shopping_bot

    bot = _shopping_bot("game_over", state=screens.ScreenState.GAME_OVER,
                        policy=Shopping(), auto_navigate=True,
                        claims=Claims(enabled=True))
    bot._ladder_tier = 1
    bot._tier_best_wave = {1: 2}
    bot._claimed_wave = {1: 2}
    bot._last_claim["missions"] = time.time()
    bot._last_claim["mail"] = time.time()
    bot._last_menu_badge_check_at = time.time() - elapsed
    asked = _asked_go_home(bot)
    bot.run_once()
    assert asked == [expected]


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


class _BattleProgress:
    """Just the reroll_progress surface the in-run scan touches."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.battle_prices = SimpleNamespace(
            invalidate=lambda upgrade_id: self.calls.append(('invalidate', upgrade_id)))

    def speed_target(self) -> None:
        return None

    def battle_policy(self, base, rows, **kwargs):  # noqa: ANN001, ANN201 - test fake
        self.calls.append(('policy', kwargs.get('battle_tab')))
        return base

    def note_battle_levels(self, run_id, upgrade_id, levels) -> None:  # noqa: ANN001
        self.calls.append(('levels', upgrade_id, levels))

    def await_battle_receipt(self, run_id, sequence) -> None:  # noqa: ANN001
        self.calls.append(('fence', sequence))


@pytest.mark.parametrize('enabled', [True, False])
def test_a_battle_receipt_feeds_the_tally_instead_of_the_fence(
    bot_in_run_on: Callable[[str], TowerBot], monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    import config
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', enabled)
    bot = bot_in_run_on('in_run_lit')
    bot.controls.apply({'autopilot': {'enabled': True}})
    progress = _BattleProgress()
    bot.reroll_progress = progress
    monkeypatch.setattr(bot.autopilot, 'step',
                        lambda *args, **kwargs: bool(kwargs['record_receipt'](4, 'attack_speed', 3)))
    bot.run_once()
    receipts = [call for call in progress.calls if call[0] in ('levels', 'fence')]
    assert receipts == ([('levels', 'attack_speed', 3)] if enabled else [('fence', 4)])


def test_a_burst_invalidation_reaches_the_price_model(
    bot_in_run_on: Callable[[str], TowerBot], monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = bot_in_run_on('in_run_lit')
    bot.controls.apply({'autopilot': {'enabled': True}})
    progress = _BattleProgress()
    bot.reroll_progress = progress
    monkeypatch.setattr(bot.autopilot, 'step',
                        lambda *args, **kwargs: bool(kwargs['invalidate_quote']('attack_speed')))
    bot.run_once()
    assert ('invalidate', 'attack_speed') in progress.calls


@pytest.mark.parametrize('frame, tab', [('in_run_lit', 'ATTACK'), ('in_run_defense', 'DEFENSE')])
def test_the_battle_policy_learns_which_tab_is_open(
    bot_in_run_on: Callable[[str], TowerBot], monkeypatch: pytest.MonkeyPatch, frame: str, tab: str,
) -> None:
    bot = bot_in_run_on(frame)
    bot.controls.apply({'autopilot': {'enabled': True}})
    progress = _BattleProgress()
    bot.reroll_progress = progress
    monkeypatch.setattr(bot.autopilot, 'step', lambda *args, **kwargs: False)
    bot.run_once()
    assert ('policy', tab) in progress.calls
