"""Battle spending buys affordable upgrades without saving barriers."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fleet.blender_battle_update import configure_battle
from fleet.build_route import RouteBaseline, RouteDocument
from fleet.build_route_eval import RouteFacts
from fleet.strategy_blocks import evaluate_program, program_upgrade_ids
from upgrades import CATALOG


def evaluate(wave: int = 25, cash: int = 100, purchases: dict[str, int] | None = None,
             quotes: dict[str, Any] | None = None,
             **rows: dict[str, Any]) -> Any:
    baseline = RouteBaseline.from_dict(configure_battle(RouteDocument.compatibility().baseline.to_dict()))
    route = SimpleNamespace(revision=1, battle=baseline.battle, workshop=baseline.workshop,
                            rules=baseline.rules)
    observations = {uid: dict(value=1, observed_at=100, **row) for uid, row in rows.items()}
    facts = RouteFacts("account", "worker", "battle", 100, 101, run_id=7, wave=wave,
                       battle_cash=cash, run_purchases=purchases or {}, upgrade_rows=observations,
                       best_tier_1_wave=100, battle_price_quotes=quotes or {})
    return evaluate_program(route, facts, None, "battle")


def test_all_battle_upgrades_except_explicit_exclusions_are_eligible() -> None:
    original = RouteDocument.compatibility().baseline.to_dict()
    before = deepcopy(original)
    updated = configure_battle(original)
    expected = {upgrade.id for upgrade in CATALOG if not upgrade.unlock} - {
        "defense_absolute", "health_regen"}
    assert set(program_upgrade_ids(tuple(updated["battle"]["blocks"]))) == expected
    assert original == before
    assert {key: value for key, value in updated.items() if key != "battle"} == {
        key: value for key, value in original.items() if key != "battle"}
    assert configure_battle(updated) == updated


@pytest.mark.parametrize("wave", [1, 19, 20, 100])
def test_expensive_orbs_and_defense_do_not_block_affordable_combat(wave: int) -> None:
    result = evaluate(wave, orbs=dict(status="unaffordable", price=9000),
                      defense_absolute=dict(status="available", price=1),
                      health_regen=dict(status="available", price=1),
                      attack_speed=dict(status="available", price=10))
    assert result.decision.upgrade_id == "attack_speed"
    assert result.decision.state == "buy"


@pytest.mark.parametrize("uid", [upgrade.id for upgrade in CATALOG
                                if not upgrade.unlock and upgrade.id not in {
                                    "defense_absolute", "health_regen"}])
def test_every_allowed_upgrade_can_be_bought_without_other_prices(uid: str) -> None:
    result = evaluate(**{uid: dict(status="available", price=10)})
    assert result.decision.upgrade_id == uid
    assert result.decision.state == "buy"


@pytest.mark.parametrize("uid", ["defense_absolute", "health_regen"])
def test_excluded_upgrades_are_never_bought_or_requested(uid: str) -> None:
    result = evaluate(**{uid: dict(status="available", price=1)})
    assert result.decision is None or result.decision.upgrade_id != uid
    assert uid not in result.trace.observation_ids


@pytest.mark.parametrize("wave,expected", [(1, "cash_bonus"), (19, "cash_bonus"),
                                         (20, "attack_speed"), (100, "attack_speed")])
def test_early_economy_preference_then_cheapest_affordable(wave: int, expected: str) -> None:
    result = evaluate(wave, cash_bonus=dict(status="available", price=10),
                      attack_speed=dict(status="available", price=10))
    assert result.decision.upgrade_id == expected


def test_expensive_early_economy_falls_through_to_combat() -> None:
    result = evaluate(1, cash_bonus=dict(status="available", price=90),
                      health=dict(status="available", price=10))
    assert result.decision.upgrade_id == "health"


def test_buys_economy_after_wave_twenty_when_it_is_cheapest() -> None:
    result = evaluate(100, coins_per_kill_bonus=dict(status="available", price=5),
                      health=dict(status="available", price=10))
    assert result.decision.upgrade_id == "coins_per_kill_bonus"


@pytest.mark.parametrize("status", ["locked", "maxed", "unaffordable"])
def test_unavailable_upgrades_do_not_block_an_affordable_purchase(status: str) -> None:
    result = evaluate(orbs=dict(status=status, price=1),
                      package_chance=dict(status=status, price=1),
                      health=dict(status="available", price=10))
    assert result.decision.upgrade_id == "health"


def test_exact_wallet_balance_is_enough_to_buy() -> None:
    result = evaluate(cash=10, health=dict(status="available", price=10))
    assert result.decision.upgrade_id == "health"
    assert result.decision.price == 10


def test_stale_price_must_be_observed_again_before_purchase() -> None:
    baseline = RouteBaseline.from_dict(configure_battle(RouteDocument.compatibility().baseline.to_dict()))
    route = SimpleNamespace(revision=1, battle=baseline.battle, workshop=baseline.workshop)
    facts = RouteFacts("account", "worker", "battle", 100, 101, run_id=7, wave=25,
                      battle_cash=100, run_purchases={}, upgrade_rows={
                          "health": dict(status="available", price=10, value=1, observed_at=1)})
    result = evaluate_program(route, facts, None, "battle")
    assert result.decision is None or result.decision.state == "observe_price"


def test_no_affordable_upgrade_requests_prices_instead_of_holding_for_orbs() -> None:
    result = evaluate(cash=1, orbs=dict(status="unaffordable", price=9000),
                      health=dict(status="unaffordable", price=10))
    assert result.trace.observation_ids
    assert result.decision is not None and result.decision.state == "observe_price"


def test_discovery_reaches_other_upgrades_while_health_remains_affordable() -> None:
    result = evaluate(purchases={"health": 20}, health=dict(status="available", price=10))
    assert result.trace.observation_ids == ("defense_percent",)
    # Reading an unavailable row ends discovery and resumes spending.
    resumed = evaluate(purchases={"health": 20}, health=dict(status="available", price=10),
                       defense_percent=dict(status="locked", price=None))
    assert resumed.decision.upgrade_id == "health"
    assert resumed.decision.state == "buy"
    # A confirmed purchase advances the cursor to the next row.
    next_row = evaluate(purchases={"health": 30}, health=dict(status="available", price=10))
    assert next_row.trace.observation_ids == ("attack_speed",)


def test_failed_discovery_yields_to_affordable_purchases() -> None:
    result = evaluate(purchases={"health": 20}, health=dict(status="available", price=10),
                      defense_percent=dict(status="unknown", price=None))
    assert result.decision.upgrade_id == "health" and result.decision.state == "buy"


def quote(**changes: Any) -> dict[str, Any]:
    return dict(dict(account_id="account", run_id=7, source="model", status="available",
                     verified=True, price=5, index=0, value=1), **changes)


def test_verified_observed_choice_uses_existing_burst_engine() -> None:
    from autopilot import burst_size
    from fleet.battle_prices import catalog
    result = evaluate(cash=100, quotes={"attack_speed": quote()},
                      attack_speed=dict(status="available", price=5))
    assert result.decision.price_source == "model"
    curve = catalog()["curves"]["attack_speed"]
    assert burst_size(curve, 0, 100, result.decision.burst_price_ceiling) > 1


@pytest.mark.parametrize("changes", [dict(verified=False), dict(run_id=8), dict(account_id="other"),
                                    dict(price=7, index=1), dict(value=2), dict(index=100000),
                                    dict(status="locked")])
def test_unverified_or_mismatched_quote_keeps_observed_single_purchase(changes: dict[str, Any]) -> None:
    result = evaluate(quotes={"attack_speed": quote(**changes)},
                      attack_speed=dict(status="available", price=5))
    assert result.decision.upgrade_id == "attack_speed"
    assert result.decision.price_source == "observed"
    assert result.decision.burst_price_ceiling is None


def test_burst_discovery_does_not_require_landing_on_exact_multiple() -> None:
    result = evaluate(purchases={"health": 23}, health=dict(status="available", price=10))
    assert result.trace.observation_ids == ("defense_percent",)


def test_discovery_does_not_interrupt_each_purchase() -> None:
    result = evaluate(purchases={"health": 2}, health=dict(status="available", price=10))
    assert result.decision.state == "buy" and result.decision.upgrade_id == "health"


def test_observed_pool_policy_sends_a_real_multi_tap_command(tmp_path: Path) -> None:
    import time
    from fleet.build_route_store import BuildRouteStore
    from tests.test_strategy_blocks import _assigned_model_progress
    from tests.test_autopilot import parts, shown
    progress, _, rows, base = _assigned_model_progress(tmp_path)
    raw = progress.route_runtime.current().to_dict()
    raw["baseline"]["battle"]["blocks"][0].pop("price_source")
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), raw["revision"], "operator")
    bot, device, frame, observation, _ = parts()
    row = next(row for row in observation.rows if row.upgrade_id == "attack_speed")
    now = time.time()
    for reading in rows.values():
        reading["observed_at"] = now
    rows["attack_speed"]["value"] = row.value
    policy = progress.battle_policy(base, rows, run_id=7, wave=1, cash=100)
    assert policy.battle_price_quote is not None
    assert policy.modeled_pool
    bot.step(frame, device, policy, cash=100, run_id=7,
             observation=shown(observation, row, 0, at=now))
    assert device.actions == [("burst", *row.tap, 6)]


def test_burst_kill_switch_keeps_observed_purchase(monkeypatch: pytest.MonkeyPatch) -> None:
    import config
    monkeypatch.setattr(config, "BATTLE_BURST_ENABLED", False)
    result = evaluate(quotes={"attack_speed": quote()}, attack_speed=dict(status="available", price=5))
    assert result.decision.price_source == "observed"
    assert result.decision.burst_price_ceiling is None
