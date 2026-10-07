"""Workshop and Labs share one wallet; the jar is the only thing between them."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fleet import coin_share
from fleet import strategy_blocks as blocks
from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_eval import RouteFacts, evaluate_workshop


def route(**rules: Any):
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["workshop"].update(mode="priorities", priority_ids=["attack_speed", "damage"])
    for section, values in rules.items():
        raw["baseline"]["rules"][section] = {**raw["baseline"]["rules"][section], **values}
    # R1: every rules-aware writer must write both the old field and the rule
    # to the same value, or the old field (present in this compatibility doc)
    # wins on load and silently reverts the rule.
    raw["baseline"]["workshop"]["coin_spend_limit_pct"] = \
        raw["baseline"]["rules"]["coins"]["workshop_spend_limit_pct"]
    return resolve_route(RouteDocument.from_dict(raw), "Air_38", "a1")


WAITING = {"kind": "wait_coins", "price": 2500, "game_speed_level": 2, "observed_at": 1.}


def test_jar_grows_by_the_share_of_spare_coins_and_caps_at_the_price() -> None:
    assert coin_share.grow_jar(0, 1000, 2500, 20) == 200
    assert coin_share.grow_jar(200, 1000, 2500, 20) == 360
    assert coin_share.grow_jar(2400, 10_000, 2500, 50) == 2500


def test_jar_grows_only_by_the_share_of_coins_earned() -> None:
    assert coin_share.grow_jar(200, 1000, 2500, 20, earned=0) == 200
    assert coin_share.grow_jar(200, 1500, 2500, 20, earned=500) == 300
    assert coin_share.grow_jar(200, 1000, 2500, 20, earned=-300) == 200   # a lab spend: no growth
    assert coin_share.grow_jar(900, 1000, 2500, 20, earned=5000) == 920   # never past the spare coins


def test_ceiling_never_goes_negative_and_jar_never_exceeds_price() -> None:
    assert coin_share.workshop_ceiling(route(), 300, 500) == 0
    assert coin_share.grow_jar(500, 300, 2500, 20) == 500   # wallet below the jar: no growth
    assert coin_share.grow_jar(900, 5000, 400, 20) == 400   # a cheaper next lab caps the jar


def test_ceiling_for_each_limit() -> None:
    assert coin_share.workshop_ceiling(route(), 1000, 200) == 800
    assert coin_share.workshop_ceiling(route(coins={"workshop_spend_limit_pct": 50}), 1000, 200) == 400
    legacy = SimpleNamespace(workshop=SimpleNamespace(coin_spend_limit_pct=50))
    assert coin_share.workshop_ceiling(legacy, 1000, 0) == 500


def test_only_an_automated_lab_waiting_for_coins_holds_coins() -> None:
    assert coin_share.waiting_lab_price(route(), WAITING) == 2500
    assert coin_share.waiting_lab_price(route(labs={"auto_start": False}), WAITING) is None
    assert coin_share.waiting_lab_price(route(), {**WAITING, "kind": "done"}) is None
    assert coin_share.waiting_lab_price(route(), None) is None
    assert coin_share.workshop_paused(route(coins={"lab_share": {"mode": "labs_first"}}), WAITING)
    assert not coin_share.workshop_paused(route(coins={"lab_share": {"mode": "save_pct"}}), WAITING)
    assert not coin_share.workshop_paused(route(coins={"lab_share": {"mode": "labs_first"}}), None)


def test_workshop_paused_agrees_with_the_wallet_not_just_the_lab_state() -> None:
    """The worker's pause must match the UI's splitPreview: paused only when
    the live wallet cannot cover the waiting lab's price, not merely because
    the lab record says wait_coins."""
    labs_first = route(coins={"lab_share": {"mode": "labs_first"}})
    assert not coin_share.workshop_paused(labs_first, WAITING, wallet=2500)  # wallet covers price exactly
    assert not coin_share.workshop_paused(labs_first, WAITING, wallet=3000)  # wallet covers price with room
    assert coin_share.workshop_paused(labs_first, WAITING, wallet=2499)      # wallet short of price
    assert coin_share.workshop_paused(labs_first, WAITING, wallet=None)      # wallet unknown: fail closed


GS4 = coin_share.SaveTarget("labs.game-speed", 4, 50_000)


def test_settle_grows_once_per_visit(tmp_path: Path) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 20}})
    target = coin_share.cadence_target(saving, WAITING)
    assert target == coin_share.SaveTarget("labs.game-speed", 2, 2500)
    assert jar.settle(saving, target, 1000, "visit-1", 10.) == 200
    assert jar.settle(saving, target, 1000, "visit-1", 11.) == 200   # same visit, no regrowth
    assert jar.settle(saving, target, 1000, "visit-2", 12.) == 200   # nothing earned since
    assert jar.settle(saving, target, 1500, "visit-3", 13.) == 300   # 20% of the 500 earned
    record = json.loads(jar.path.read_text())
    assert record["account_id"] == "a1" and record["amount"] == 300 and record["wallet"] == 1500
    assert record["target"] == {"lab_id": "labs.game-speed", "level": 2}


def test_save_pct_keeps_a_share_of_each_run_across_runs(tmp_path: Path) -> None:
    """Each run's first menu visit moves pct% of the coins above the jar into it."""
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 25}})
    assert jar.settle(saving, GS4, 10_000, "after-run:1", 10.) == 2_500
    assert coin_share.workshop_ceiling(saving, 10_000, 2_500) == 7_500
    # Workshop spent its 7,500 (the next menu scan sees it); the next run earned 10,000 more.
    assert jar.settle(saving, GS4, 2_500, "after-run:1", 10.5) == 2_500
    assert jar.settle(saving, GS4, 12_500, "after-run:2", 11.) == 5_000
    assert coin_share.workshop_ceiling(saving, 12_500, 5_000) == 7_500


def test_workshop_savings_are_not_taxed_again_each_run(tmp_path: Path) -> None:
    """A Workshop saving for a dear upgrade keeps its coins: only new income is shared."""
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 25}})
    jar.settle(saving, GS4, 10_000, "after-run:1", 10.)                  # 2,500 jar, 7,500 saved
    assert jar.settle(saving, GS4, 20_000, "after-run:2", 11.) == 5_000  # +25% of 10,000 earned
    assert jar.settle(saving, GS4, 30_000, "after-run:3", 12.) == 7_500
    assert coin_share.workshop_ceiling(saving, 30_000, 7_500) == 22_500  # 75% of all income


def test_an_older_jar_without_a_wallet_starts_counting_income_from_now(tmp_path: Path) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 25}})
    jar.path.write_text(json.dumps({"account_id": "a1", "amount": 60_000, "visit_key": "after-run:1",
                                    "target": {"lab_id": "labs.game-speed", "level": 5}}))
    gs5 = coin_share.SaveTarget("labs.game-speed", 5, 150_000)
    assert jar.settle(saving, gs5, 80_000, "after-run:2", 10.) == 60_000  # unknown income: no growth
    assert jar.settle(saving, gs5, 90_000, "after-run:3", 11.) == 62_500


def test_a_lab_debit_takes_only_its_price_from_the_jar(tmp_path: Path) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 25}})
    jar.settle(saving, GS4, 20_000, "after-run:1", 10.)
    assert jar.spend(71, 11.) == 4_929        # a cheap filler keeps the Game Speed savings
    assert jar.settle(saving, GS4, 19_929, "after-run:1", 12.) == 4_929  # same run: no regrowth
    assert jar.spend(50_000, 13.) == 0        # never below empty
    assert jar.amount() == 0
    assert coin_share.LabCoinJar(tmp_path / "none", "a1").spend(71, 14.) == 0


def test_the_jar_never_exceeds_the_wallet_or_the_target_price(tmp_path: Path) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 50}})
    assert jar.settle(saving, GS4, 200_000, "after-run:1", 10.) == 50_000   # capped at the price
    assert jar.settle(saving, GS4, 1_000, "after-run:1", 11.) == 1_000      # a known wallet clamps it
    assert jar.settle(saving, None, 400, "after-run:1", 12.) == 400         # with no target too
    assert jar.amount() == 50_000                                          # ...but only in effect


def test_a_misread_wallet_never_shrinks_the_stored_jar(tmp_path: Path) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 25}})
    jar.path.write_text(json.dumps({"account_id": "a1", "amount": 20_000, "visit_key": "after-run:1",
                                    "target": {"lab_id": "labs.game-speed", "level": 4}}))
    assert jar.settle(saving, GS4, 100, "after-run:1", 10.) == 100
    assert jar.amount() == 20_000
    assert jar.settle(saving, GS4, 30_000, "after-run:1", 11.) == 20_000


def test_an_unknown_target_keeps_the_jar_and_another_mode_empties_it(tmp_path: Path) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 20}})
    jar.settle(saving, GS4, 10_000, "after-run:1", 10.)
    # A transient unknown/inspect cadence or unread plan names no target: keep, never grow.
    assert coin_share.cadence_target(saving, {**WAITING, "kind": "unknown"}) is None
    assert jar.settle(saving, None, 10_000, "after-run:2", 11.) == 2_000
    assert jar.amount() == 2_000
    assert jar.settle(route(), GS4, 10_000, "after-run:3", 12.) == 0
    assert jar.amount() == 0
    assert coin_share.LabCoinJar(tmp_path / "fresh", "a1").settle(route(), GS4, 1000, "v", 1.) == 0
    assert coin_share.LabCoinJar(tmp_path / "fresh", "a1").settle(saving, None, 1000, "v", 1.) == 0
    assert not (tmp_path / "fresh" / coin_share.JAR_FILE).exists()


def test_the_jar_empties_once_its_target_is_researching_or_finished(tmp_path: Path) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 25}})
    jar.settle(saving, GS4, 20_000, "after-run:1", 10.)
    gs5 = coin_share.SaveTarget("labs.game-speed", 5, 150_000)
    started = {("labs.game-speed", 4)}
    # The plan moved on but L4 is neither running nor done: the savings carry over.
    assert jar.settle(saving, gs5, 20_000, "after-run:1", 11., retired=lambda lab, level: False) == 5_000
    jar.settle(saving, GS4, 20_000, "after-run:1", 12.)
    # L4 started (or finished): its savings are spent, saving for L5 starts from zero
    # and takes its share of the 10,000 earned since.
    assert jar.settle(saving, gs5, 30_000, "after-run:2", 13.,
                      retired=lambda lab, level: (lab, level) in started) == 2_500
    assert json.loads(jar.path.read_text())["target"] == {"lab_id": "labs.game-speed", "level": 5}


def test_read_only_jar_never_writes(tmp_path: Path) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1", read_only=True)
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 20}})
    assert jar.settle(saving, coin_share.cadence_target(saving, WAITING), 1000, "visit-1", 10.) == 200
    assert not jar.path.exists()


@pytest.mark.parametrize("content", [None, "{bad", json.dumps({"account_id": "b2", "amount": 900})])
def test_missing_corrupt_or_foreign_jar_is_zero_and_logged(
        tmp_path: Path, content: str | None, caplog: pytest.LogCaptureFixture) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    if content is not None:
        jar.path.write_text(content)
    with caplog.at_level(logging.INFO, logger="fleet.coin_share"):
        assert jar.amount() == 0
    assert "lab coin jar" in caplog.text.lower()


def test_evaluate_workshop_ceiling_leaves_the_jar_alone() -> None:
    facts = RouteFacts("a1", "Air_38", "main_menu", 100., 101., best_tier_1_wave=1,
                       wallet_coins=1000, prices={"damage": 10, "attack_speed": 12},
                       utility_spent_coins=0, visit_id="v", lab_coin_jar=200)
    assert evaluate_workshop(route(), facts, None).trace.spend_ceiling == 800
    assert evaluate_workshop(route(), replace(facts, lab_coin_jar=0), None).trace.spend_ceiling == 1000


def test_block_programs_use_the_same_ceiling() -> None:
    workshop = SimpleNamespace(id="workshop.default", mode="blocks",
                               blocks=({"id": "cheap", "type": "pool", "upgrade_ids": ["damage", "attack_speed"],
                                        "selection": "priority"},),
                               banned_upgrade_ids=frozenset(), coin_spend_limit_pct=100)
    program = SimpleNamespace(revision=1, workshop=workshop, battle=SimpleNamespace(mode="blocks", blocks=()))
    facts = RouteFacts("account", "Air_38", "workshop", 100, 101, best_tier_1_wave=25, wallet_coins=100,
                       prices={"damage": 80, "attack_speed": 81}, purchases={"unlock_defense_upgrades": 1},
                       confirmed_purchases={}, utility_spent_coins=400, visit_id="visit",
                       lab_coin_jar=30)
    result = blocks.evaluate_program(program, facts, None, "workshop")
    assert result.trace.spend_ceiling == 70
    assert getattr(result.decision, "upgrade_id", None) != "damage"
