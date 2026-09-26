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


def test_settle_grows_once_per_visit_and_resets(tmp_path: Path) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 20}})
    assert jar.settle(saving, WAITING, 1000, "visit-1", 10.) == 200
    assert jar.settle(saving, WAITING, 1000, "visit-1", 11.) == 200   # same visit, no regrowth
    assert jar.settle(saving, WAITING, 1000, "visit-2", 12.) == 360
    record = json.loads(jar.path.read_text())
    assert record["account_id"] == "a1" and record["amount"] == 360
    jar.reset(13.)
    assert jar.amount() == 0
    assert jar.settle(saving, WAITING, 1000, "visit-2", 14.) == 0      # reset holds for this visit


def test_no_waiting_lab_or_another_mode_empties_the_jar(tmp_path: Path) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1")
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 20}})
    jar.settle(saving, WAITING, 1000, "visit-1", 10.)
    assert jar.settle(saving, {**WAITING, "kind": "done"}, 1000, "visit-2", 11.) == 0
    assert jar.amount() == 0
    assert coin_share.LabCoinJar(tmp_path / "fresh", "a1").settle(route(), WAITING, 1000, "v", 1.) == 0
    assert not (tmp_path / "fresh" / coin_share.JAR_FILE).exists()


def test_read_only_jar_never_writes(tmp_path: Path) -> None:
    jar = coin_share.LabCoinJar(tmp_path, "a1", read_only=True)
    saving = route(coins={"lab_share": {"mode": "save_pct", "pct": 20}})
    assert jar.settle(saving, WAITING, 1000, "visit-1", 10.) == 200
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
