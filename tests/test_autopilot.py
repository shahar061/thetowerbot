from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import cv2
import pytest

import config
import tiles
from tests.test_perception import recorded


class Device:
    def __init__(self) -> None:
        self.actions: list[tuple] = []

    def click(self, x: int, y: int) -> None:
        self.actions.append(("tap", x, y))

    def swipe(self, x: int, y: int, x2: int, y2: int, duration: float) -> None:
        self.actions.append(("swipe", x, y, x2, y2))

    def tap_burst(self, x: int, y: int, n: int, gap_s: float) -> None:
        self.actions.append(("burst", x, y, n))


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


@pytest.mark.parametrize("status, should_navigate", [("unreadable", True), ("locked", False)])
def test_model_refresh_seeks_unreadable_target_without_waiting_for_cache_expiry(
    status: str, should_navigate: bool,
) -> None:
    from policy import UpgradeRule
    bot, device, frame, observation, policy = parts()
    target = next(row for row in observation.rows if row.upgrade_id == "attack_speed")
    bot.state.observe(replace(observation, rows=(replace(target, status=status, price=None),)))
    defense = replace(target, upgrade_id="health", category="DEFENSE")
    observation = replace(observation, observed_at=101, category="DEFENSE", rows=(defense,))
    policy = replace(policy, rules=(UpgradeRule("attack_speed"),),
                     observe_only=True, modeled_pool=True)

    bot.step(frame, device, policy, cash=100, observation=observation)

    assert bool(device.actions) is should_navigate
    if should_navigate:
        assert device.actions[0][:2] == ("tap", 180)  # Attack tab, not a purchase
    assert bot.pending is None
    assert bot.state.snapshot()["verified_purchases"] == 0


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


@pytest.mark.parametrize('category,expected_x', [
    ('ATTACK', 135), ('DEFENSE', 405), ('UTILITY', 675),
])
def test_four_cells_keep_the_first_three_categories(category: str, expected_x: int) -> None:
    """Ultimate Weapons adds a fourth cell after Tier 1 wave 60.

    The first three cells are still Attack, Defense and Utility, now a
    quarter wide; the thirds would land on the wrong cell.
    """
    import autopilot
    w, h = config.EXPECTED_RESOLUTION
    frame = _tab_bar((0, w // 4, w // 2, 3 * w // 4, w - 1))
    assert autopilot.battle_tab_point(frame, category) == (expected_x, h - 50)


def test_a_live_four_tab_bar_is_recognised() -> None:
    """A real run after the Ultimate Weapons unlock, on the Attack tab."""
    import autopilot
    import cv2

    frame = cv2.imread(str(Path(__file__).parent / 'fixtures' / 'in_run_four_tabs.png'))
    assert autopilot.battle_tab_point(frame, 'DEFENSE') == (405, 2350)


def test_a_bar_matching_both_layouts_fails_closed() -> None:
    """Borders at every third and quarter are no layout the game draws."""
    import autopilot
    w, _ = config.EXPECTED_RESOLUTION
    frame = _tab_bar((0, w // 4, w // 3, w // 2, 2 * w // 3, 3 * w // 4, w - 1))
    assert autopilot.battle_tab_point(frame, 'DEFENSE') is None


def test_a_partial_bar_fails_closed() -> None:
    """Missing one border of each layout must not read as either."""
    import autopilot
    w, _ = config.EXPECTED_RESOLUTION
    frame = _tab_bar((0, w // 3, w // 2, w - 1))
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


def test_block_purchase_waits_for_confirmed_counter_refresh() -> None:
    bot, device, frame, observation, policy = parts()
    policy = replace(policy, single_purchase=True, decision_token='route:1:run:2:count:0')
    bot.step(frame, device, policy, cash=100, observation=observation)
    changed = replace(observation, observed_at=102, rows=tuple(
        replace(row, price=12, value=4, observed_at=102) if row.upgrade_id == 'damage' else row
        for row in observation.rows))
    bot.step(frame, device, policy, cash=90, observation=changed)
    assert bot.state.snapshot()['verified_purchases'] == 1
    assert len(device.actions) == 1
    bot.step(frame, device, policy, cash=90, observation=replace(changed,observed_at=103))
    assert len(device.actions) == 1
    bot.step(frame, device, replace(policy,decision_token='route:1:run:2:count:1'),
             cash=90, observation=replace(changed,observed_at=104))
    assert len(device.actions) == 2


def test_route_price_limit_is_rechecked_against_live_row() -> None:
    bot, device, frame, observation, policy = parts()
    row = next(row for row in observation.rows if row.upgrade_id == 'damage')
    bot.step(frame,device,replace(policy,max_purchase_price=row.price-1),cash=100,observation=observation)
    assert device.actions == []


def locked_utility() -> tuple:
    from perception import parse_frame
    frame = cv2.imread(str(Path(__file__).parent / "fixtures/in_run_utility_locked.png"))
    return frame, parse_frame(frame, recorded("in_run_utility_locked"), "battle", now=100)


def test_a_tab_locked_in_the_workshop_does_not_hold_the_run() -> None:
    # A new account opened Utility for Cash Bonus, found only "Unlock utility
    # upgrades in the workshop" and waited there for the rest of every run.
    from autopilot import BattleAutopilot
    from policy import AutopilotPolicy, UpgradeRule
    frame, observation = locked_utility()
    bot, device = BattleAutopilot(), Device()
    policy = AutopilotPolicy(enabled=True, rules=(UpgradeRule("cash_bonus"), UpgradeRule("damage")))
    bot.step(frame, device, policy, cash=133, observation=observation)
    rows = bot.state.rows("battle", 100)
    assert rows["cash_bonus"]["status"] == "locked"
    assert device.actions and device.actions[-1][:2] == ("tap", 180)  # the Attack tab


def test_a_locked_row_outlives_the_sixty_second_expiry_within_its_run() -> None:
    from autopilot import AutopilotState
    from combat_context import RunIdentity
    _, observation = locked_utility()
    state, run = AutopilotState(), RunIdentity(run_id=7)
    state.observe(observation, run)
    assert state.rows("battle", 500, run)["cash_bonus"]["status"] == "locked"
    assert state.rows("battle", 500, RunIdentity(run_id=8))["cash_bonus"]["status"] == "unknown"


def test_modeled_price_buys_unreadable_price_but_never_confirms_itself() -> None:
    from policy import UpgradeRule
    bot, device, frame, observation, policy = parts()
    row = next(r for r in observation.rows if r.upgrade_id == 'attack_speed')
    quote = dict(account_id='a', run_id=7, upgrade_id='attack_speed', source='model',
                 status='available', verified=True, price=5, value=row.value,
                 wave=int(observation.combat['wave']))
    policy = replace(policy, rules=(UpgradeRule('attack_speed'),), single_purchase=True,
                     decision_token='a:1:7:0', modeled_pool=True, battle_price_quote=quote)
    unreadable = replace(row, price=None, status='unreadable', tap=None)
    obs = replace(observation, rows=(unreadable,))
    bot.step(frame, device, policy, cash=100, observation=obs, run_id=7)
    assert len(device.actions) == 1
    # A price change alone is not an independent receipt for modeled buying.
    changed = replace(obs, observed_at=101, rows=(replace(unreadable, price=7, observed_at=101),))
    bot.step(frame, device, policy, cash=95, observation=changed, run_id=7)
    assert bot.state.snapshot()['verified_purchases'] == 0
    changed = replace(obs, observed_at=102, rows=(replace(unreadable, value=row.value + .05, observed_at=102),))
    bot.step(frame, device, policy, cash=95, observation=changed, run_id=7)
    assert bot.state.snapshot()['verified_purchases'] == 1
    bot.step(frame, device, policy, cash=95, observation=replace(changed, observed_at=103), run_id=7)
    assert len(device.actions) == 1


def test_modeled_quote_needs_current_run_value_and_cash() -> None:
    from policy import UpgradeRule
    for overrides, cash in [({'run_id': 8}, 100), ({'value': -1}, 100), ({}, 4), ({}, None)]:
        bot, device, frame, obs, policy = parts()
        row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
        quote = dict(account_id='a', run_id=7, upgrade_id='attack_speed', source='model',
                     status='available', verified=True, price=5, value=row.value, wave=int(obs.combat['wave']))
        quote.update(overrides)
        policy = replace(policy, rules=(UpgradeRule('attack_speed'),), single_purchase=True,
                         decision_token='a:1:7:0', modeled_pool=True, battle_price_quote=quote)
        bot.step(frame, device, policy, cash=cash, observation=obs, run_id=7)
        assert device.actions == []


def test_model_confirms_rounded_stat_from_independent_next_price_read() -> None:
    from policy import UpgradeRule
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    quote = dict(account_id='a', run_id=7, upgrade_id='attack_speed', source='model',
                 status='available', verified=True, price=5, index=0, value=row.value,
                 wave=int(obs.combat['wave']))
    policy = replace(policy, rules=(UpgradeRule('attack_speed'),), single_purchase=True,
                     decision_token='a:1:7:0', modeled_pool=True, battle_price_quote=quote)
    bot.step(frame, device, policy, cash=100, observation=replace(obs, rows=(replace(row, price=5, raw_price='5'),)), run_id=7)
    assert len(device.actions) == 1
    receipt = replace(obs, observed_at=102, rows=(replace(row, price=7, raw_price='7', observed_at=102),))
    bot.step(frame, device, policy, cash=95, observation=receipt, run_id=7)
    assert bot.state.snapshot()['verified_purchases'] == 1


@pytest.mark.parametrize('batch_size', [1, 5])
def test_modeled_batch_reuses_confirmation_frames_and_stops_after_five(batch_size: int) -> None:
    from policy import UpgradeRule
    from fleet.battle_prices import catalog
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    curve = catalog()['curves']['attack_speed']
    def configured(index: int) -> Any:
        return replace(policy, rules=(UpgradeRule('attack_speed'),), single_purchase=True,
            decision_token=f'a:1:7:{index}', modeled_pool=True, battle_price_quote=dict(
                account_id='a', run_id=7, upgrade_id='attack_speed', source='model',
                status='available', verified=True, price=curve[index], index=index,
                value=row.value + index * .05, wave=int(obs.combat['wave']),
                sequence=index, batch_size=batch_size))
    def observed(index: int) -> Any:
        return replace(obs, observed_at=100 + index * 2, rows=(replace(row,
            price=curve[index], raw_price=str(curve[index]), value=row.value + index * .05,
            observed_at=100 + index * 2),))
    calls = []
    receipts = []
    def refresh(sequence: int | None) -> Any:
        calls.append(sequence)
        return configured(sequence + 1)
    bot.step(frame, device, configured(0), cash=1000, observation=observed(0), run_id=7,
             refresh_policy=refresh)
    for index in range(1, batch_size + 1):
        bot.step(frame, device, configured(index - 1), cash=1000,
                 observation=observed(index), run_id=7, refresh_policy=refresh,
                 record_receipt=lambda sequence, upgrade_id, levels: receipts.append(sequence))
    assert len(device.actions) == batch_size
    assert bot.state.snapshot()['verified_purchases'] == batch_size
    assert calls == list(range(batch_size - 1))
    assert receipts == list(range(batch_size))  # every level's receipt feeds the tally
    assert bot.pending is None


def test_modeled_batch_never_replays_a_receipt_while_counter_is_stale(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', False)  # pins the one-level receipt
    from policy import UpgradeRule
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    policy = replace(policy, rules=(UpgradeRule('attack_speed'),), single_purchase=True,
        decision_token='a:1:7:0', modeled_pool=True, battle_price_quote=dict(
            account_id='a', run_id=7, upgrade_id='attack_speed', source='model',
            status='available', verified=True, price=5, index=0, value=row.value,
            wave=int(obs.combat['wave']), sequence=0, batch_size=5))
    bot.step(frame, device, policy, cash=100, observation=obs, run_id=7)
    changed = replace(obs, observed_at=102, rows=(replace(row, price=7,
                      value=row.value + .05, observed_at=102),))
    bot.step(frame, device, policy, cash=95, observation=changed, run_id=7,
             refresh_policy=lambda sequence: policy)
    assert len(device.actions) == 1
    assert bot.state.snapshot()['verified_purchases'] == 1


@pytest.mark.parametrize('interruption', ['wave', 'maxed', 'pause', 'route'])
def test_modeled_batch_stops_for_changed_evidence_or_controls(
        monkeypatch: pytest.MonkeyPatch, interruption: str) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', False)  # pins the one-level receipt
    from policy import UpgradeRule
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    policy = replace(policy, rules=(UpgradeRule('attack_speed'),), single_purchase=True,
        decision_token='a:1:7:0', modeled_pool=True, battle_price_quote=dict(
            account_id='a', run_id=7, upgrade_id='attack_speed', source='model',
            status='available', verified=True, price=5, index=0, value=row.value,
            wave=int(obs.combat['wave']), sequence=0, batch_size=5))
    bot.step(frame, device, policy, cash=100, observation=obs, run_id=7)
    changed = replace(obs, observed_at=102, rows=(replace(row, price=7, value=row.value + .05,
        status='maxed' if interruption == 'maxed' else 'available', observed_at=102),))
    if interruption == 'wave':
        changed = replace(changed, combat={**changed.combat, 'wave': obs.combat['wave'] + 1})
    if interruption == 'route':
        policy = replace(policy, decision_token='a:2:7:0')
    calls = []
    def refresh(sequence: int | None) -> Any:
        calls.append(sequence)
        return replace(policy, enabled=interruption != 'pause', decision_token='a:1:7:1',
                       battle_price_quote={**policy.battle_price_quote, 'price': 7,
                                           'value': row.value + .05, 'index': 1, 'sequence': 1})
    bot.step(frame, device, policy, cash=95, observation=changed, run_id=7, refresh_policy=refresh)
    assert len(device.actions) == 1
    if interruption in {'wave', 'route'}:
        assert not calls


@pytest.mark.parametrize('enabled', [True, False])
def test_a_model_quote_from_an_earlier_wave_still_authorizes_the_tap(
        monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', enabled)
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    quote = dict(account_id='a', run_id=7, upgrade_id='attack_speed', source='model',
                 status='available', verified=True, price=5, value=row.value,
                 wave=int(obs.combat['wave']) - 1)
    policy = replace(policy, rules=(UpgradeRule('attack_speed'),), single_purchase=True,
                     decision_token='a:1:7:0', modeled_pool=True, battle_price_quote=quote)
    bot.step(frame, device, policy, cash=100, observation=obs, run_id=7)
    assert len(device.actions) == (1 if enabled else 0)


def test_a_modeled_receipt_names_the_upgrade_and_its_levels() -> None:
    from policy import UpgradeRule
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    policy = replace(policy, rules=(UpgradeRule('attack_speed'),), single_purchase=True,
        decision_token='a:1:7:0', modeled_pool=True, battle_price_quote=dict(
            account_id='a', run_id=7, upgrade_id='attack_speed', source='model',
            status='available', verified=True, price=5, value=row.value,
            wave=int(obs.combat['wave']), sequence=6))
    bot.step(frame, device, policy, cash=100, observation=obs, run_id=7)
    receipts: list[tuple] = []
    changed = replace(obs, observed_at=102, rows=(replace(row, value=row.value + .05, observed_at=102),))
    bot.step(frame, device, policy, cash=95, observation=changed, run_id=7,
             record_receipt=lambda *args: receipts.append(args))
    assert receipts == [(6, 'attack_speed', 1)]


def curve_of(upgrade_id: str) -> list[int]:
    from fleet.battle_prices import catalog
    return catalog()['curves'][upgrade_id]


def modeled(policy: Any, row: Any, index: int, *, sequence: int = 0,
            ceiling: int | None = None, wave: int = 1) -> Any:
    """A Blender-style single-purchase policy holding a verified model quote."""
    from policy import UpgradeRule
    curve = curve_of(row.upgrade_id)
    return replace(policy, rules=(UpgradeRule(row.upgrade_id),), single_purchase=True,
        decision_token=f'a:1:7:{sequence}', modeled_pool=True, burst_price_ceiling=ceiling,
        battle_price_quote=dict(account_id='a', run_id=7, upgrade_id=row.upgrade_id,
            source='model', status='available', verified=True, price=curve[index],
            index=index, value=row.value + index * .05, wave=wave, sequence=sequence,
            batch_size=5, rule_id='cheap'))


def shown(observation: Any, row: Any, index: int, at: float, **changes: Any) -> Any:
    """`observation` showing only `row`, priced at curve level `index`."""
    price = curve_of(row.upgrade_id)[index]
    fields = dict(price=price, raw_price=f'${price}', value=row.value + index * .05,
                  observed_at=at)
    return replace(observation, observed_at=at, rows=(replace(row, **{**fields, **changes}),))


def test_burst_size_stops_at_cash_ceiling_curve_end_and_the_maximum() -> None:
    from autopilot import burst_size
    curve = [5, 7, 10, 15, 21, 28, 36]
    assert burst_size(curve, 0, 100, 10_000) == 6  # 86 fits, 122 does not
    assert burst_size(curve, 0, 80, 10_000) == 5   # 58 fits, 86 does not
    assert burst_size(curve, 0, 100, 20) == 4      # 21 is over the ceiling
    assert burst_size(curve, 5, 10_000, 10_000) == 2  # only two levels left
    assert burst_size([1] * 30, 0, 10_000, 10_000) == config.BATTLE_BURST_MAX
    assert burst_size(curve, 0, 4, 10_000) == 0


@pytest.mark.parametrize('reserve, spend_pct, ceiling, taps', [
    (0, 100, 10_000, 6),   # 5+7+10+15+21+28 = 86 of 100
    (20, 100, 10_000, 5),  # the reserve applies to the whole burst
    (0, 50, 10_000, 4),    # so does the route spend limit
    (0, 100, 20, 4),       # no level above the ceiling
    (0, 100, None, 1),     # no ceiling: one tap, as today
])
def test_a_modeled_purchase_bursts_within_cash_reserve_and_ceiling(
        reserve: int, spend_pct: int, ceiling: int | None, taps: int) -> None:
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    policy = replace(modeled(policy, row, 0, ceiling=ceiling), cash_reserve=reserve,
                     cash_spend_limit_pct=spend_pct)
    bot.step(frame, device, policy, cash=100, observation=shown(obs, row, 0, at=100), run_id=7)
    assert device.actions == [('tap', *row.tap) if taps == 1 else ('burst', *row.tap, taps)]
    assert (bot._pending_index, bot._pending_k) == (0, taps)


def test_a_ceiling_below_the_next_level_saves_instead_of_tapping() -> None:
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    bot.step(frame, device, modeled(policy, row, 0, ceiling=4), cash=100,
             observation=shown(obs, row, 0, at=100), run_id=7)
    assert device.actions == []
    assert bot.state.snapshot()['phase'] == 'saving'


def test_the_kill_switch_keeps_one_tap_per_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', False)
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    bot.step(frame, device, modeled(policy, row, 0, ceiling=10_000), cash=100,
             observation=shown(obs, row, 0, at=100), run_id=7)
    assert device.actions == [('tap', *row.tap)]
    assert (bot._pending_curve, bot._pending_k) == ((), 1)


def after_burst(index: int = 0, ceiling: int = 10_000, cash: int = 100) -> tuple:
    """A bot that has just sent a burst on Attack Speed from curve level `index`."""
    from autopilot import BattleAutopilot
    _, device, frame, obs, policy = parts()
    bus = Bus()
    bot = BattleAutopilot(bus=bus)
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    bot.step(frame, device, modeled(policy, row, index, sequence=10, ceiling=ceiling),
             cash=cash, observation=shown(obs, row, index, at=100), run_id=7)
    return bot, device, frame, obs, policy, row, bus


def confirm(bot: Any, device: Any, frame: Any, policy: Any, row: Any, observation: Any,
            receipts: list, invalidated: list, index: int = 0) -> None:
    bot.step(frame, device, modeled(policy, row, index, sequence=10, ceiling=10_000),
             cash=10, observation=observation, run_id=7,
             record_receipt=lambda *args: receipts.append(args),
             invalidate_quote=invalidated.append)


def purchases(bus: Bus) -> list[tuple[str, int | None]]:
    import events
    return [(e.upgrade_id, e.price) for e in bus.published if isinstance(e, events.BattlePurchased)]


def test_a_burst_counts_the_levels_its_price_jump_proves() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst()
    assert device.actions == [('burst', *row.tap, 6)]
    receipts: list = []
    invalidated: list = []
    confirm(bot, device, frame, policy, row, shown(obs, row, 4, at=101), receipts, invalidated)
    assert purchases(bus) == [('attack_speed', price) for price in (5, 7, 10, 15)]
    assert receipts == [(13, 'attack_speed', 4)]  # the last level's sequence
    assert invalidated == []
    assert bot.state.snapshot()['verified_purchases'] == 4
    assert bot.pending is None


def test_a_jump_past_the_burst_counts_at_most_its_taps() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst(ceiling=20)
    assert device.actions == [('burst', *row.tap, 4)]
    confirm(bot, device, frame, policy, row, shown(obs, row, 6, at=101), [], [])
    assert len(purchases(bus)) == 4


def test_a_maxed_row_confirms_a_burst_that_reached_the_curve_end() -> None:
    curve = curve_of('attack_speed')
    bot, device, frame, obs, policy, row, bus = after_burst(index=len(curve) - 3, cash=30_000)
    assert device.actions == [('burst', *row.tap, 3)]
    receipts: list = []
    invalidated: list = []
    maxed = shown(obs, row, 0, at=101, status='maxed', price=None, raw_price=None)
    confirm(bot, device, frame, policy, row, maxed, receipts, invalidated, index=len(curve) - 3)
    assert purchases(bus) == [('attack_speed', price) for price in curve[-3:]]
    assert receipts == [(12, 'attack_speed', 3)] and invalidated == []


def test_a_maxed_row_short_of_the_curve_end_counts_one_level_and_reconciles() -> None:
    # The in-game maximum can sit below the curve's last level, so MAX
    # proves only that the first tap landed.
    bot, device, frame, obs, policy, row, bus = after_burst()
    assert device.actions == [('burst', *row.tap, 6)]
    receipts: list = []
    invalidated: list = []
    maxed = shown(obs, row, 0, at=101, status='maxed', price=None, raw_price=None)
    confirm(bot, device, frame, policy, row, maxed, receipts, invalidated)
    assert purchases(bus) == [('attack_speed', 5)]
    assert receipts == [(10, 'attack_speed', 1)]
    assert invalidated == ['attack_speed']  # the next read re-indexes the row
    assert bot.pending is None


@pytest.mark.parametrize('changes', [
    dict(price=None, raw_price=None, status='unreadable'),  # no price at all
    dict(price=6, raw_price='$6'),                          # matches no curve level
    dict(price=21, raw_price='$5'),                         # digits disagree with the parse
    dict(price=1000, raw_price='$1K'),                      # matches several curve levels
    dict(),                                                 # still at the starting level
])
def test_an_undecidable_price_waits_then_falls_back_to_the_value(changes: dict[str, Any]) -> None:
    bot, device, frame, obs, policy, row, bus = after_burst()
    receipts: list = []
    invalidated: list = []
    for at in (101, 104, 107.9):
        confirm(bot, device, frame, policy, row, shown(obs, row, 0, at=at, **changes),
                receipts, invalidated)
        assert bot.pending is not None and purchases(bus) == []
    confirm(bot, device, frame, policy, row,
            shown(obs, row, 0, at=108, **{**changes, 'value': row.value + .3}), receipts, invalidated)
    assert purchases(bus) == [('attack_speed', 5)]
    assert receipts == [(10, 'attack_speed', 1)]
    assert invalidated == ['attack_speed']  # the next read re-indexes the row


def test_a_burst_row_scrolled_out_of_view_waits_then_blocks() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst()
    receipts: list = []
    invalidated: list = []
    for at in (101, 104, 107.9):
        confirm(bot, device, frame, policy, row, replace(obs, observed_at=at, rows=()),
                receipts, invalidated)
        assert bot.pending is not None and purchases(bus) == []
    confirm(bot, device, frame, policy, row, replace(obs, observed_at=108, rows=()),
            receipts, invalidated)
    assert purchases(bus) == [] and receipts == [] and invalidated == []
    assert bot.pending is None and bot._blocked['attack_speed'] == 168


def test_an_unconfirmed_burst_with_an_unchanged_value_blocks_the_row() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst()
    receipts: list = []
    invalidated: list = []
    confirm(bot, device, frame, policy, row,
            shown(obs, row, 0, at=108, price=None, raw_price=None, status='unreadable'),
            receipts, invalidated)
    assert purchases(bus) == [] and receipts == [] and invalidated == []
    assert bot.pending is None and bot._blocked['attack_speed'] == 168


def test_a_price_behind_the_model_counts_nothing_and_reconciles() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst(index=3)
    assert device.actions == [('burst', *row.tap, 4)]
    receipts: list = []
    invalidated: list = []
    confirm(bot, device, frame, policy, row, shown(obs, row, 1, at=101), receipts, invalidated,
            index=3)
    assert purchases(bus) == [] and receipts == []
    assert invalidated == ['attack_speed']
    assert bot.pending is None and 'attack_speed' not in bot._blocked


def test_a_wave_change_does_not_discard_a_pending_burst() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst()
    later = shown(obs, row, 2, at=101)
    later = replace(later, combat={**later.combat, 'wave': later.combat['wave'] + 1})
    confirm(bot, device, frame, policy, row, later, [], [])
    assert purchases(bus) == [('attack_speed', 5), ('attack_speed', 7)]


def test_a_quote_for_another_row_never_sizes_a_burst() -> None:
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    policy = modeled(policy, row, 0, ceiling=10_000)
    # Defense % also starts at $5: only the quote's own row may size a burst.
    policy = replace(policy, battle_price_quote={**policy.battle_price_quote,
                                                 'upgrade_id': 'defense_percent'})
    bot.step(frame, device, policy, cash=100, observation=shown(obs, row, 0, at=100), run_id=7)
    assert device.actions == [('tap', *row.tap)]
    assert (bot._pending_curve, bot._pending_index, bot._pending_k) == ((), None, 1)


@pytest.mark.parametrize('ending', ['confirmed', 'unconfirmed', 'behind', 'identity', 'run_end'])
def test_every_way_a_pending_burst_ends_forgets_its_curve(ending: str) -> None:
    from combat_context import RunIdentity
    bot, device, frame, obs, policy, row, bus = after_burst(index=3 if ending == 'behind' else 0)
    assert bot._pending_curve and bot._pending_k > 1
    if ending == 'confirmed':
        confirm(bot, device, frame, policy, row, shown(obs, row, 4, at=101), [], [])
    elif ending == 'unconfirmed':
        confirm(bot, device, frame, policy, row,
                shown(obs, row, 0, at=108, price=None, raw_price=None, status='unreadable'), [], [])
    elif ending == 'behind':
        confirm(bot, device, frame, policy, row, shown(obs, row, 1, at=101), [], [], index=3)
    elif ending == 'identity':
        for at, run in ((101, 7), (102, 8)):  # the first only names the run it was already in
            bot.step(frame, device, modeled(policy, row, 0, sequence=10, ceiling=10_000), cash=10,
                     observation=shown(obs, row, 0, at=at), run_id=7, identity=RunIdentity(run_id=run))
    else:
        bot.suspend('Run ended', clear_battle=True)
    assert (bot.pending, bot._pending_curve, bot._pending_index, bot._pending_k) == (None, (), None, 1)


@pytest.mark.parametrize('enabled, down', [(True, True), (False, False)])
def test_seek_scrolls_toward_the_target_in_catalog_order(
        monkeypatch: pytest.MonkeyPatch, enabled: bool, down: bool) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', enabled)
    bot, device, frame, observation, policy = parts()
    # Multishot Chance is listed after every visible Attack row.
    bot.step(frame, device, replace(policy, rules=(UpgradeRule('multishot_chance'),)),
             cash=100, observation=observation)
    (kind, _, y, _, y2), = device.actions
    assert kind == 'swipe' and (y > y2) == down  # a downward scroll swipes upward


def test_seek_scrolls_up_for_a_target_listed_before_the_visible_rows(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    bot, device, frame, observation, policy = parts()
    lower = tuple(replace(r, upgrade_id=uid) for r, uid in zip(
        observation.rows, ('range', 'damage_per_meter', 'multishot_chance', 'multishot_targets')))
    bot.step(frame, device, replace(policy, rules=(UpgradeRule('damage'),)),
             cash=100, observation=replace(observation, rows=lower))
    (kind, _, y, _, y2), = device.actions
    assert kind == 'swipe' and y < y2


def test_seek_sweeps_when_catalog_order_cannot_place_the_target(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    bot, device, frame, observation, policy = parts()
    # Visible rows on both sides of Critical Chance, but not Critical Chance.
    rows = tuple(r for r in observation.rows if r.upgrade_id in ('damage', 'critical_factor'))
    bot.step(frame, device, replace(policy, rules=(UpgradeRule('critical_chance'),)),
             cash=100, observation=replace(observation, rows=rows))
    (kind, _, y, _, y2), = device.actions
    assert kind == 'swipe' and y < y2  # the sweep starts upward


def test_a_directed_seek_that_stops_moving_restarts_as_a_full_sweep(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    bot, device, frame, observation, policy = parts()
    policy = replace(policy, rules=(UpgradeRule('multishot_chance'),))
    for n in range(2):  # the panel never moves
        bot.step(frame, device, policy, cash=100, observation=replace(observation, observed_at=100 + n))
    (_, _, down_y, _, down_y2), (_, _, up_y, _, up_y2) = device.actions
    assert down_y > down_y2 and up_y < up_y2


@pytest.mark.parametrize('enabled', [True, False])
def test_a_confirmed_purchase_skips_the_post_tap_cooldown(
        monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', enabled)
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    bot.step(frame, device, modeled(policy, row, 0), cash=100,
             observation=shown(obs, row, 0, at=100), run_id=7, cooldown=1.0)
    bot.step(frame, device, modeled(policy, row, 0), cash=95,
             observation=shown(obs, row, 1, at=100.4), run_id=7, cooldown=1.0,
             refresh_policy=lambda sequence: modeled(policy, row, 1, sequence=1),
             record_receipt=lambda *args: None)
    # The confirming frame buys again only when the cooldown is skipped.
    assert len(device.actions) == (2 if enabled else 1)


def test_buying_follows_the_last_battle_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    bot, device, frame, obs, policy = parts()
    bot.step(frame, device, policy, cash=100, observation=obs)
    assert bot.buying  # tapped a purchase
    bot.suspend("Run ended", clear_battle=True)
    assert not bot.buying
    bot.step(frame, device, replace(policy, cash_reserve=95), cash=100,
             observation=replace(obs, observed_at=105))
    assert not bot.buying  # saving
    damage = next(r for r in obs.rows if r.upgrade_id == 'damage')
    bot.state.observe(replace(obs, rows=(replace(damage, upgrade_id='health', name='Health',
                                                 category='DEFENSE'),)))  # cached off-tab
    bot.step(frame, device, replace(policy, rules=(UpgradeRule('health'),)), cash=100,
             observation=replace(obs, observed_at=110))
    assert bot.buying  # opening the tab that holds the target
    bot.step(frame, device, replace(policy, rules=(UpgradeRule('health'),)), cash=100,
             observation=replace(obs, observed_at=110.3), cooldown=1.0)
    assert bot.buying  # a cooldown-skipped scan keeps the previous pace
    bot.step(frame, device, replace(policy, observe_only=True), cash=100,
             observation=replace(obs, observed_at=115))
    assert not bot.buying  # observing only


def test_a_tap_on_another_row_than_the_quoted_one_expects_no_next_price(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    bot, device, frame, obs, policy = parts()
    quoted = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    damage = next(r for r in obs.rows if r.upgrade_id == 'damage')
    other = replace(damage, upgrade_id='health', name='Health')  # has its own curve
    # The quote prices Attack Speed; the rule taps Health.
    policy = replace(modeled(policy, quoted, 0), rules=(UpgradeRule('health'),))
    bot.step(frame, device, policy, cash=100, run_id=7,
             observation=replace(obs, rows=(other, quoted)))
    assert bot.pending is not None and bot.pending[0].upgrade_id == 'health'
    assert bot._pending_next_price is None


@pytest.mark.parametrize('fixture', ['in_run_defense.png', 'in_run_defense_1920.png'])
def test_a_panel_scroll_moves_at_most_one_row(fixture: str) -> None:
    """The panel shows about 2.6 rows and a tile is only read whole. A swipe
    past one row lets a five-row tab rest only at its top and bottom, so the
    middle row (Thorns | Lifesteal on Defense) is clipped at both."""
    from autopilot import scroll_panel
    from perception import observe_frame
    frame = cv2.imread(str(Path(__file__).parent / 'fixtures' / fixture))
    observation = observe_frame(frame, 'battle')
    first, second = sorted({tile.y for tile in tiles.find_tiles(frame)})[:2]
    swipes: list[tuple[int, float]] = []

    class Recorder:
        def swipe(self, x: int, y: int, x2: int, y2: int, duration: float) -> None:
            swipes.append((y - y2, duration))

    for down in (True, False):
        scroll_panel(Recorder(), frame, observation.heading_y, down=down)
    assert [abs(distance) for distance, _ in swipes] == [abs(swipes[0][0])] * 2
    assert 0 < abs(swipes[0][0]) <= second - first
    # Slow enough that the panel stops near where the finger lifts.
    assert all(duration >= .5 for _, duration in swipes)
