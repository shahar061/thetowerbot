"""The battle context: separate observations, honest gaps, run-bound freshness."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import cv2
import pytest

from tests.test_perception import recorded

FIXTURES = Path(__file__).parent / "fixtures"


class Device:
    def __init__(self) -> None:
        self.actions: list[tuple] = []

    def click(self, x: int, y: int) -> None:
        self.actions.append(("tap", x, y))

    def swipe(self, x: int, y: int, x2: int, y2: int, duration: float) -> None:
        self.actions.append(("swipe", x, y, x2, y2))


def observed(name: str, now: float = 100.) -> Any:
    from perception import parse_frame
    frame = cv2.imread(str(FIXTURES / f"{name}.png"))
    return parse_frame(frame, recorded(name), "battle", now=now)


def parts() -> tuple:
    from autopilot import BattleAutopilot
    from policy import AutopilotPolicy, UpgradeRule
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    policy = AutopilotPolicy(enabled=True, rules=(UpgradeRule("damage"),))
    return BattleAutopilot(), Device(), frame, observed("in_run_lit"), policy


# --- separate observations ---------------------------------------------------


def test_hud_facts_are_separate_observations_with_their_own_state() -> None:
    from combat_context import CombatContext, RunIdentity
    context = CombatContext()
    context.observe(observed("in_run_early"), identity=RunIdentity(run_id=1), now=100.)
    values = {name: context.reading(name, 100.) for name in
              ("wave", "health", "max_health", "health_regen", "enemy_damage",
               "game_speed", "paused", "cash")}
    assert all(r.state == "known" for r in values.values()), values
    assert values["wave"].value == 1
    assert values["health"].value == 2 and values["max_health"].value == 10
    assert values["health_regen"].value == pytest.approx(.04)
    assert values["enemy_damage"].value == pytest.approx(1.18)
    assert values["game_speed"].value == 1.5
    assert values["paused"].value is False
    assert values["cash"].value == 101
    # Each fact carries its own stamp, so one of them ageing out cannot age
    # the rest with it.
    assert all(r.observed_at == 100. for r in values.values())


def test_paused_and_game_speed_stay_independent_readings() -> None:
    from combat_context import CombatContext, RunIdentity
    context = CombatContext()
    context.observe(observed("in_run_attack_paused"), identity=RunIdentity(run_id=1), now=100.)
    assert context.reading("game_speed", 100.).value == 0.
    assert context.reading("paused", 100.).value is True


def test_one_unreadable_value_does_not_poison_the_other_observations() -> None:
    # in_run_lit's wallet OCRs as a reversed "68 $": the frame is fine, one
    # value on it is not.
    from combat_context import CombatContext, RunIdentity
    context = CombatContext()
    context.observe(observed("in_run_lit"), identity=RunIdentity(run_id=1), now=100.)
    cash = context.reading("cash", 100.)
    assert cash.state == "unreadable"
    assert cash.value is None  # never 0: "unreadable" is not "broke"
    assert context.reading("wave", 100.).state == "known"
    assert context.reading("health", 100.).value == 5
    assert "cash" not in context.combat(100.)


def test_unseen_facts_are_unknown_rather_than_unreadable() -> None:
    from combat_context import CombatContext
    context = CombatContext()
    reading = context.reading("wave", 100.)
    assert reading.state == "unknown"
    assert reading.value is None


def test_unsupported_hud_facets_name_their_owner_instead_of_guessing() -> None:
    from combat_context import CombatContext, RunIdentity
    context = CombatContext()
    context.observe(observed("in_run_early"), identity=RunIdentity(run_id=1), now=100.)
    owners = {"tier": "F05", "perks": "F06", "loadout": "F07", "wall": "A02",
              "cards": "C01", "ultimate_weapons": "U01", "enemy_modifiers": "B08"}
    for name, owner in owners.items():
        reading = context.reading(name, 100.)
        assert reading.state == "unsupported", name
        assert reading.value is None
        assert reading.owner == owner


def test_run_purpose_and_elapsed_time_are_observations_of_the_run_not_the_hud() -> None:
    from combat_context import CombatContext, RunIdentity
    context = CombatContext()
    identity = RunIdentity(run_id=4, purpose="milestone")
    context.observe(observed("in_run_early"), identity=identity, elapsed=93.5, now=100.)
    assert context.reading("purpose", 100.).value == "milestone"
    assert context.reading("elapsed", 100.).value == pytest.approx(93.5)


def test_elapsed_time_is_unknown_while_no_run_is_open() -> None:
    from combat_context import CombatContext, RunIdentity
    from runs import RunTracker
    tracker = RunTracker()
    from screens import ScreenState
    assert tracker.elapsed(10.) is None  # not 0.0: no run is open
    tracker.transition(ScreenState.IN_RUN, 10.)
    assert tracker.elapsed(25.) == pytest.approx(15.)
    tracker.transition(ScreenState.GAME_OVER, 30.)
    assert tracker.elapsed(40.) is None
    context = CombatContext()
    context.observe(observed("in_run_early"), identity=RunIdentity(run_id=1),
                    elapsed=None, now=100.)
    assert context.reading("elapsed", 100.).state == "unknown"
    assert context.reading("elapsed", 100.).value is None


# --- freshness: a changed run or build expires what was cached ---------------


def test_a_changed_run_expires_every_cached_reading() -> None:
    from combat_context import CombatContext, RunIdentity
    context = CombatContext()
    context.observe(observed("in_run_early"), identity=RunIdentity(run_id=1), now=100.)
    context.rebind(RunIdentity(run_id=2))
    for name in ("wave", "health", "cash", "enemy_damage"):
        reading = context.reading(name, 100.)
        assert reading.state == "expired", name
        assert reading.value is None
    assert context.combat(100.) == {}


def test_a_changed_build_expires_cached_readings_within_one_run() -> None:
    # A Workshop purchase between scans changes what the same wave means.
    from combat_context import CombatContext, RunIdentity
    context = CombatContext()
    context.observe(observed("in_run_early"), identity=RunIdentity(run_id=1, build_revision=7), now=100.)
    context.rebind(RunIdentity(run_id=1, build_revision=8))
    assert context.reading("enemy_damage", 100.).state == "expired"


def test_an_unidentified_scan_does_not_pretend_the_run_changed() -> None:
    from combat_context import CombatContext, RunIdentity
    context = CombatContext()
    context.observe(observed("in_run_early"), identity=RunIdentity(run_id=1, build_revision=7), now=100.)
    # The build revision is simply not known on this scan; that is not
    # evidence of a different build.
    context.rebind(RunIdentity(run_id=1))
    assert context.reading("wave", 100.).state == "known"


def test_a_reading_that_ages_out_expires_rather_than_holding_its_value() -> None:
    from combat_context import CombatContext, RunIdentity
    context = CombatContext()
    context.observe(observed("in_run_early"), identity=RunIdentity(run_id=1), now=100.)
    assert context.reading("wave", 101.).state == "known"
    assert context.reading("wave", 130.).state == "expired"
    # Cash is spending evidence: it must come from the frame about to be tapped.
    context.observe(observed("in_run_early"), identity=RunIdentity(run_id=1), now=200.)
    assert context.reading("cash", 200.).state == "known"
    assert context.reading("cash", 200.5).state == "expired"


def test_a_reading_at_time_zero_still_expires() -> None:
    from combat_context import CombatContext, RunIdentity
    context = CombatContext()
    context.observe(observed("in_run_early", now=0.), identity=RunIdentity(run_id=1), now=0.)
    assert context.reading("wave", 2.).state == "known"
    assert context.reading("wave", 3.).state == "expired"


def test_a_retried_run_rebuilds_its_context_from_the_new_run() -> None:
    from combat_context import CombatContext, RunIdentity
    context = CombatContext()
    context.observe(observed("in_run_early"), identity=RunIdentity(run_id=1), now=100.)
    context.clear()  # the run boundary
    assert context.reading("wave", 100.).state == "expired"
    assert context.refuse("purchase", ("cash",), now=100.) is not None
    context.observe(observed("in_run_lit", now=200.), identity=RunIdentity(run_id=2), now=200.)
    assert context.reading("wave", 200.).state == "known"
    assert context.identity == RunIdentity(run_id=2)


# --- the planner ------------------------------------------------------------


def test_the_planner_expires_rows_cached_under_another_run() -> None:
    from autopilot import AutopilotState
    from combat_context import RunIdentity
    state = AutopilotState()
    state.observe(observed("in_run_early"), identity=RunIdentity(run_id=1, build_revision=7))
    same = state.rows("battle", 100., identity=RunIdentity(run_id=1, build_revision=7))
    assert same["damage"]["status"] == "available"
    assert same["damage"]["price"] is not None
    for identity in (RunIdentity(run_id=2, build_revision=7), RunIdentity(run_id=1, build_revision=8)):
        stale = state.rows("battle", 100., identity=identity)
        assert stale["damage"]["status"] == "unknown", identity
        assert stale["damage"]["price"] is None  # never 0
        assert stale["damage"]["value"] is None


def test_the_planner_records_the_identity_each_step_observed() -> None:
    from combat_context import RunIdentity
    bot, device, frame, observation, policy = parts()
    bot.step(frame, device, policy, cash=100, observation=observation,
             identity=RunIdentity(run_id=1, build_revision=7))
    assert bot.context.identity == RunIdentity(run_id=1, build_revision=7)
    stale = bot.state.rows("battle", observation.observed_at,
                           identity=RunIdentity(run_id=2, build_revision=7))
    assert stale["damage"]["status"] == "unknown"


@pytest.mark.parametrize("second", [
    (2, 7),  # restarted run
    (1, 8),  # changed build within a run
])
def test_a_changed_identity_cancels_pending_confirmation_and_cached_rows(
    second: tuple[int, int],
) -> None:
    from combat_context import RunIdentity
    bot, device, frame, observation, policy = parts()
    first = RunIdentity(run_id=1, build_revision=7)
    changed_identity = RunIdentity(run_id=second[0], build_revision=second[1])
    assert bot.step(frame, device, policy, cash=100, observation=observation, identity=first)
    assert bot.pending is not None
    changed_rows = tuple(replace(row, price=row.price + 1)
                         if row.upgrade_id == "damage" and row.price is not None else row
                         for row in observation.rows)
    changed = replace(observation, rows=changed_rows, observed_at=101.)
    assert not bot.step(frame, device, policy, cash=100, observation=changed,
                        identity=changed_identity)
    assert bot.pending is None
    assert bot.state.snapshot()["verified_purchases"] == 0
    assert bot.state.snapshot()["observations"] == []
    assert len(device.actions) == 1


def test_a_supplied_observation_from_another_frame_cannot_buy() -> None:
    bot, device, _, observation, policy = parts()
    different_frame = cv2.imread(str(FIXTURES / "in_run_early.png"))
    assert not bot.step(different_frame, device, policy, cash=100, observation=observation)
    assert device.actions == []
    assert bot.pending is None
    assert bot.state.snapshot()["phase"] == "blocked"


def test_a_missing_cash_reading_refuses_the_purchase_instead_of_reading_zero() -> None:
    bot, device, frame, observation, policy = parts()
    # No wallet argument, and in_run_lit's own wallet OCR is unreadable.
    bot.step(frame, device, policy, observation=observation)
    assert device.actions == []
    assert bot.pending is None
    view = bot.state.snapshot()
    assert view["phase"] == "blocked"
    assert "cash" in view["reason"].lower()
    assert bot.context.reading("cash", observation.observed_at).state == "unreadable"


def test_a_purchase_needs_this_frames_cash_not_the_previous_frames() -> None:
    from combat_context import RunIdentity
    bot, device, frame, observation, policy = parts()
    identity = RunIdentity(run_id=1)
    bot.step(frame, device, policy, cash=100, observation=observation, identity=identity)
    assert len(device.actions) == 1
    bot.pending = None
    later = replace(observation, observed_at=observation.observed_at + 30)
    bot.step(frame, device, policy, observation=later, identity=identity)
    assert len(device.actions) == 1  # the 30s-old wallet buys nothing


def test_the_run_boundary_clears_the_battle_context() -> None:
    from combat_context import RunIdentity
    bot, device, frame, observation, policy = parts()
    bot.step(frame, device, policy, cash=100, observation=observation,
             identity=RunIdentity(run_id=1))
    bot.suspend("Run boundary", clear_battle=True)
    assert bot.context.reading("wave", observation.observed_at).state == "expired"
    assert bot.context.combat(observation.observed_at) == {}


def test_an_unreadable_health_reading_holds_the_guide_instead_of_reading_zero() -> None:
    """Zero health would read as "about to die" and buy the wrong thing."""
    from policy import AutopilotPolicy, UpgradeRule
    bot, device, frame, observation, _ = parts()
    policy = AutopilotPolicy(enabled=True, preset="health", rules=(UpgradeRule("damage"),))
    observation = replace(observation, combat={"wave": 5})
    bot.step(frame, device, policy, cash=100, observation=observation)
    assert device.actions == []
    view = bot.state.snapshot()
    assert view["phase"] == "wait"
    assert "health" in view["reason"].lower()
    assert bot.context.reading("health", observation.observed_at).state == "unreadable"
    assert "health" not in bot.context.combat(observation.observed_at)


# --- the scan loop names what it is observing --------------------------------


def test_the_scan_loop_names_the_run_and_build_it_observes() -> None:
    from unittest.mock import MagicMock
    import events
    import screens
    import vision
    from combat_context import RunIdentity
    from tower_bot import TowerBot
    bot = TowerBot(device=MagicMock(),
                   templates=vision.TemplateCache(Path(__file__).parent.parent / "templates"),
                   bus=events.EventBus())
    settings = bot.controls.snapshot()
    # No run open yet: the identity says so rather than inventing run 0.
    assert bot.run_identity(settings) == RunIdentity(run_id=None, build_revision=None,
                                                     purpose=settings.strategy.autopilot.purpose)
    bot.runs.transition(screens.ScreenState.IN_RUN, 10.)
    assert bot.run_identity(settings).run_id == bot.runs.current_id is not None


def test_the_build_revision_is_read_through_the_public_account_snapshot() -> None:
    from types import SimpleNamespace
    from combat_context import build_revision
    assert build_revision(None) is None
    assert build_revision(SimpleNamespace(snapshot=lambda: {"revision": None})) is None
    assert build_revision(SimpleNamespace(snapshot=lambda: {"revision": {"revision_id": 12}})) == 12


# --- the frame being decided on ------------------------------------------------


def test_the_current_frame_supplies_the_wave_the_stored_context_has_let_expire() -> None:
    """A battle scan decides before the autopilot stores this frame, so the
    stored wave is the previous scan's - over two seconds old at a 2s scan
    pace, and expired. The frame on screen still names the wave."""
    from combat_context import CombatContext, RunIdentity, frame_combat
    context = CombatContext()
    context.observe(observed("in_run_early", now=100.), identity=RunIdentity(run_id=1), now=100.)
    assert "wave" not in context.combat(103.)
    combat = frame_combat(context.combat(103.), observed("in_run_early", now=103.))
    assert combat["wave"] == 1
    assert combat["enemy_damage"] == pytest.approx(1.18)


def test_a_fact_the_current_frame_did_not_read_keeps_its_stored_value() -> None:
    from combat_context import frame_combat
    frame = replace(observed("in_run_early"), combat={})
    assert frame_combat({"wave": 7.}, frame) == {"wave": 7.}
