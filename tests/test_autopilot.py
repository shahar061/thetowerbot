from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import cv2
import pytest

import config
from tests.test_perception import recorded


class Device:
    def __init__(self) -> None:
        self.actions: list[tuple] = []

    def click(self, x: int, y: int) -> None:
        self.actions.append(("tap", x, y))

    def swipe(self, x: int, y: int, x2: int, y2: int, duration: float) -> None:
        self.actions.append(("swipe", x, y, x2, y2))


def parts() -> tuple:
    from autopilot import BattleAutopilot
    from perception import parse_frame
    from policy import AutopilotPolicy, UpgradeRule
    frame = cv2.imread(str(Path(__file__).parent / "fixtures/in_run_lit.png"))
    observation = parse_frame(frame, recorded("in_run_lit"), "battle", now=100)
    policy = AutopilotPolicy(enabled=True, rules=(UpgradeRule("damage"),))
    return BattleAutopilot(), Device(), frame, observation, policy


def test_purchase_needs_new_frame_acknowledgement() -> None:
    bot, device, frame, observation, policy = parts()
    bot.step(frame, device, policy, cash=100, observation=observation)
    assert len(device.actions) == 1
    assert bot.state.snapshot()["verified_purchases"] == 0
    bot.step(frame, device, policy, cash=100, observation=replace(observation, observed_at=101))
    assert len(device.actions) == 1
    changed = replace(observation, observed_at=102, rows=tuple(
        replace(r, price=12, value=4, observed_at=102) if r.upgrade_id == "damage" else r
        for r in observation.rows))
    bot.step(frame, device, policy, cash=90, observation=changed)
    assert bot.state.snapshot()["verified_purchases"] == 1
    assert len(device.actions) == 2  # the verifying frame buys the next one


def test_missing_currency_never_authorizes_a_tap() -> None:
    bot, device, frame, observation, policy = parts()
    bot.step(frame, device, policy, observation=observation)
    assert device.actions == []
    assert "cash" in bot.state.snapshot()["reason"].lower()


def test_route_observation_mode_keeps_rows_without_tapping() -> None:
    bot, device, frame, observation, policy = parts()
    bot.step(frame, device, replace(policy, observe_only=True),
             cash=100, observation=observation)
    assert device.actions == []
    assert any(row["upgrade_id"] == "damage" for row in
               bot.state.snapshot()["observations"])


def test_route_cash_share_is_checked_again_at_tap() -> None:
    bot, device, frame, observation, policy = parts()
    row = next(row for row in observation.rows if row.upgrade_id == "damage")
    assert row.price is not None
    bot.step(frame, device, replace(policy, cash_spend_limit_pct=10),
             cash=row.price * 5, observation=observation)
    assert device.actions == []


def test_reserve_and_target_prevent_spending() -> None:
    bot, device, frame, observation, policy = parts()
    bot.step(frame, device, replace(policy, cash_reserve=95), cash=100, observation=observation)
    assert device.actions == []
    bot.step(frame, device, replace(policy, rules=(replace(policy.rules[0], target=3),)),
             cash=100, observation=observation)
    assert device.actions == []


def test_tab_switch_is_verified_before_buying() -> None:
    from policy import UpgradeRule
    bot, device, frame, observation, policy = parts()
    policy = replace(policy, rules=(UpgradeRule("health"),))
    bot.step(frame, device, policy, cash=100, observation=observation)
    assert len(device.actions) == 1
    assert device.actions[0][1] == 540
    # The game ignored navigation; it must not buy Damage in Health's place.
    for n in range(1,5):
        bot.step(frame, device, policy, cash=100, observation=replace(observation, observed_at=100+n))
    assert bot.state.snapshot()["verified_purchases"] == 0
    assert len(device.actions) <= 2


def test_unknown_search_has_a_finite_scroll_budget() -> None:
    from policy import UpgradeRule
    bot, device, frame, observation, policy = parts()
    policy = replace(policy, rules=(UpgradeRule("range"),), max_scrolls=2)
    for n in range(8):
        bot.step(frame, device, policy, cash=100, observation=replace(observation, observed_at=100+n))
    assert len(device.actions) <= 4
    observed = bot.state.snapshot()["observations"]
    row = next(r for r in observed if r["upgrade_id"] == "range")
    assert row["status"] == "unknown"


def test_run_end_discards_battle_values_and_pending_purchase() -> None:
    bot, device, frame, observation, policy = parts()
    bot.step(frame, device, policy, cash=100, observation=observation)
    bot.suspend("Run ended", clear_battle=True)
    assert bot.state.snapshot()["observations"] == []
    assert bot.state.snapshot()["next_upgrade_id"] is None


def test_manual_buy_works_once_with_automatic_policy_off() -> None:
    bot, device, frame, observation, policy = parts()
    bot.submit({"action": "buy", "upgrade_id": "damage"}, now=100)
    bot.step(frame, device, replace(policy, enabled=False), cash=100, observation=observation)
    assert len(device.actions) == 1
    for n in range(1, 12):
        bot.step(frame, device, replace(policy, enabled=False), cash=100,
                 observation=replace(observation, observed_at=100+n))
    assert len(device.actions) == 1


def test_the_frame_that_verifies_a_purchase_also_makes_the_next_one() -> None:
    bot, device, frame, observation, policy = parts()
    bot.step(frame, device, policy, cash=100, observation=observation)
    changed = replace(observation, observed_at=102, rows=tuple(
        replace(r, price=12, value=4, observed_at=102) if r.upgrade_id == "damage" else r
        for r in observation.rows))
    assert bot.step(frame, device, policy, cash=90, observation=changed)
    assert bot.state.snapshot()["verified_purchases"] == 1
    assert len(device.actions) == 2
    assert bot.pending is not None and bot.pending[0].price == 12


def test_a_verified_manual_buy_is_not_repeated_on_the_same_frame() -> None:
    bot, device, frame, observation, policy = parts()
    bot.submit({"action": "buy", "upgrade_id": "damage"}, now=100)
    bot.step(frame, device, replace(policy, enabled=False), cash=100, observation=observation)
    changed = replace(observation, observed_at=102, rows=tuple(
        replace(r, price=12, value=4, observed_at=102) if r.upgrade_id == "damage" else r
        for r in observation.rows))
    assert not bot.step(frame, device, replace(policy, enabled=False), cash=90, observation=changed)
    assert bot.state.snapshot()["verified_purchases"] == 1
    assert len(device.actions) == 1

def test_expired_manual_command_does_not_execute_in_a_later_run() -> None:
    bot, device, frame, observation, policy = parts()
    bot.submit({"action": "buy", "upgrade_id": "damage"}, now=1)
    bot.step(frame, device, replace(policy, enabled=False), cash=100, observation=observation)
    assert device.actions == []


def test_policy_edit_preserves_pending_purchase_verification() -> None:
    bot, device, frame, observation, policy = parts()
    bot.step(frame, device, policy, cash=100, observation=observation)
    bot.step(frame, device, replace(policy, cash_reserve=1), cash=100,
             observation=replace(observation, observed_at=101))
    assert len(device.actions) == 1
    assert bot.pending is not None


def test_unreadable_panel_times_out_pending_purchase() -> None:
    bot, device, frame, observation, policy = parts()
    bot.step(frame, device, policy, cash=100, observation=observation)
    bot.step(frame, device, policy, cash=100,
             observation=replace(observation, category=None, rows=(), observed_at=110))
    assert bot.pending is None
    assert len(device.actions) == 1


def test_cached_offscreen_target_search_still_has_a_scroll_bound() -> None:
    from policy import UpgradeRule
    bot, device, frame, observation, policy = parts()
    damage = next(r for r in observation.rows if r.upgrade_id == "damage")
    bot.state.observe(replace(observation, rows=(replace(damage, upgrade_id="range", name="Range"),)))
    policy = replace(policy, rules=(UpgradeRule("range"),), max_scrolls=2)
    for n in range(8):
        bot.step(frame, device, policy, cash=100, observation=replace(observation, observed_at=100+n))
    assert len(device.actions) <= 4


def test_pause_retains_pending_evidence_without_more_taps() -> None:
    bot, device, frame, observation, policy = parts()
    bot.step(frame, device, policy, cash=100, observation=observation)
    bot.suspend("Paused")
    bot.step(frame, device, policy, cash=100, observation=replace(observation, observed_at=101))
    assert len(device.actions) == 1


def test_urgent_survival_precedes_discovering_unseen_economy() -> None:
    from policy import UpgradeRule
    bot, device, frame, observation, policy = parts()
    damage = observation.rows[0]
    rows = (replace(damage, upgrade_id="defense_absolute", category="DEFENSE", value=0),
            replace(damage, upgrade_id="defense_percent", category="DEFENSE", value=0))
    observation = replace(observation, category="DEFENSE", rows=rows,
                          combat={"wave": 5, "enemy_damage": 100})
    policy = replace(policy, preset="turtle", rules=tuple(UpgradeRule(i) for i in
                     ("defense_absolute", "defense_percent", "cash_bonus")))
    bot.step(frame, device, policy, cash=100, observation=observation)
    assert bot.pending is not None
    assert bot.pending[0].upgrade_id == "defense_absolute"


# --- The in-run tab bar ------------------------------------------------------
#
# battle_tab_point is the one place in the bot that turns a category into a
# coordinate by index arithmetic rather than by reading something. That is safe
# only while the guard in front of it holds, and the guard had no test.

def _tab_bar(boundaries: tuple[int, ...]) -> Any:
    """A dark frame with a bright cell border at each given x."""
    import numpy as np
    w, h = config.EXPECTED_RESOLUTION
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    for x in boundaries:
        frame[h - 80:h - 20, max(0, x - 2):min(w, x + 3)] = 255
    return frame


@pytest.mark.parametrize('category,expected_x', [
    ('ATTACK', 180), ('DEFENSE', 540), ('UTILITY', 900),
])
def test_three_cells_give_each_category_its_own_centre(category: str, expected_x: int) -> None:
    import autopilot
    w, h = config.EXPECTED_RESOLUTION
    frame = _tab_bar((0, w // 3, 2 * w // 3, w - 1))
    assert autopilot.battle_tab_point(frame, category) == (expected_x, h - 50)


def test_a_fourth_tab_fails_closed_instead_of_tapping_the_wrong_one() -> None:
    """The failure the acceptance gate names, at the tab bar.

    With four cells the thirds are no longer borders, so the index would point
    into the middle of a cell that is not the one asked for. Returning None
    holds the action; returning a coordinate would buy on the wrong tab.
    """
    import autopilot
    w, _ = config.EXPECTED_RESOLUTION
    frame = _tab_bar((0, w // 4, w // 2, 3 * w // 4, w - 1))
    for category in ('ATTACK', 'DEFENSE', 'UTILITY'):
        assert autopilot.battle_tab_point(frame, category) is None


def test_an_unknown_category_and_a_wrong_resolution_are_both_refused() -> None:
    import autopilot
    import numpy as np
    w, h = config.EXPECTED_RESOLUTION
    good = _tab_bar((0, w // 3, 2 * w // 3, w - 1))
    assert autopilot.battle_tab_point(good, 'ULTIMATE') is None
    assert autopilot.battle_tab_point(np.zeros((h, w // 2, 3), dtype=np.uint8), 'ATTACK') is None


def test_native_1920_battle_tabs_follow_the_bottom_of_the_frame() -> None:
    import autopilot
    import cv2

    frame = cv2.imread(str(Path(__file__).parent / 'fixtures' / 'in_run_defense_1920.png'))
    assert autopilot.battle_tab_point(frame, 'DEFENSE') == (540, 1870)


def test_a_blank_tab_bar_is_refused() -> None:
    """No visible cell borders is not evidence of three cells."""
    import autopilot
    assert autopilot.battle_tab_point(_tab_bar(()), 'ATTACK') is None


def test_every_observed_row_is_drawn_and_the_bought_one_is_flagged() -> None:
    """The overlay is how you see what the planner was looking at.

    The legacy matcher recorded a box per rule "whether or not the tap
    happens, because matched but rejected is exactly what you open the
    device view to see". The autopilot owns in-run buying now, so the same
    has to hold for the rows it reads - otherwise the device view goes blank
    on exactly the frames where something is being decided.
    """
    bot, device, frame, observation, policy = parts()

    bot.step(frame, device, policy, cash=100, observation=observation)

    drawn = {box["name"]: box for box in bot.boxes}
    assert drawn.keys() == {row.name for row in observation.rows}, (
        "every row the planner read should be drawn, not only the bought one"
    )
    bought = [box for box in bot.boxes if box["tapped"]]
    assert [box["name"] for box in bought] == ["Damage"]
    assert (bought[0]["tap_x"], bought[0]["tap_y"]) == device.actions[0][1:]
    for box in bot.boxes:
        assert box["w"] > 0 and box["h"] > 0


def test_a_scan_that_buys_nothing_still_draws_the_rows() -> None:
    bot, device, frame, observation, policy = parts()
    from dataclasses import replace as _replace

    # Nothing affordable: the planner reads the panel and declines.
    bot.step(frame, device, _replace(policy, cash_reserve=10_000),
             cash=1, observation=observation)

    assert device.actions == []
    assert [box["name"] for box in bot.boxes] == [row.name for row in observation.rows]
    assert not any(box["tapped"] for box in bot.boxes)


class Bus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)


def test_a_decision_is_published_once_when_it_changes() -> None:
    """A run that stops buying leaves a row saying why, not silence."""
    import events
    _, device, frame, observation, policy = parts()
    from autopilot import BattleAutopilot
    bus = Bus()
    bot = BattleAutopilot(bus=bus)
    held = replace(policy, cash_reserve=95)
    for at in (100, 101, 102):
        bot.step(frame, device, held, cash=100,
                 observation=replace(observation, observed_at=at))
    bot.step(frame, device, policy, cash=100,
             observation=replace(observation, observed_at=103))
    decided = [e for e in bus.published if isinstance(e, events.AutopilotDecided)]
    assert [(e.phase, e.upgrade_id) for e in decided] == [
        ("wait", None), ("manual", "damage"), ("verifying", "damage")]


def test_step_checks_the_observation_against_the_scans_digest() -> None:
    import ocr
    bot, device, frame, observation, policy = parts()
    reads = ocr.FrameReads(frame)
    reads._digest = "0" * 64  # a scan digest that does not match the observation
    bot.step(frame, device, policy, cash=100, observation=observation, reads=reads)
    assert device.actions == []
    assert "does not match" in bot.state.snapshot()["reason"]


def test_reads_for_another_frame_are_ignored_by_step() -> None:
    import ocr
    bot, device, frame, observation, policy = parts()
    reads = ocr.FrameReads(frame.copy())
    reads._digest = "0" * 64
    bot.step(frame, device, policy, cash=100, observation=observation, reads=reads)
    assert len(device.actions) == 1


def test_step_hands_its_reads_to_observe_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    import autopilot
    import ocr
    bot, device, frame, observation, policy = parts()
    reads = ocr.FrameReads(frame)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(autopilot, "observe_frame",
                        lambda screen, context, **kwargs: (seen.update(kwargs), observation)[1])
    bot.step(frame, device, policy, cash=100, reads=reads)
    assert seen["reads"] is reads
