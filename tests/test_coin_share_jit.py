from __future__ import annotations

from types import SimpleNamespace

import pytest

from fleet.coin_share import jit_hold, workshop_ceiling
from fleet.lab_saving import SavingPlan, SavingTarget, saving_plan


def plan(reserve: int | None, wallet: int | None) -> SavingPlan:
    target = SavingTarget(1, "labs.game-speed", "Game Speed", 4, 50_000, 0.0, None, False)
    return SavingPlan(reserve, 0, wallet, 1_000.0, (target,),
                      ("Slot 1 Game Speed L4: needs 50k, due now", "Reserve 4k; Workshop may spend 0"))


def route(limit_pct: int) -> SimpleNamespace:
    return SimpleNamespace(rules=SimpleNamespace(coins=SimpleNamespace(workshop_spend_limit_pct=limit_pct)))


def test_no_saving_holds_nothing() -> None:
    assert jit_hold(None, 10_000) == (0, False, None)


def test_reserve_is_held_and_full_reserve_pauses() -> None:
    assert jit_hold(plan(4_000, 10_000), 10_000) == (4_000, False, "Reserve 4k; Workshop may spend 0")
    jar, paused, _ = jit_hold(plan(10_000, 10_000), 10_000)
    assert (jar, paused) == (10_000, True)


def test_unread_wallet_pauses_workshop() -> None:
    jar, paused, _ = jit_hold(plan(None, None), None)
    assert (jar, paused) == (0, True)


def test_unread_plan_wallet_holds_the_whole_read_wallet() -> None:
    assert jit_hold(plan(None, None), 7_000)[:2] == (7_000, True)
    assert workshop_ceiling(route(100), 7_000, jit_hold(plan(None, None), 7_000)[0]) == 0


def test_labs_starting_now_are_held_as_well_as_the_reserve() -> None:
    # 3,000 coins; 2,500 go to a lab starting now, so the plan's wallet is 500.
    jar, paused, _ = jit_hold(plan(0, 500), 3_000)
    assert (jar, paused) == (2_500, False)
    jar, paused, _ = jit_hold(plan(0, -400), 3_000)
    assert (jar, paused) == (3_000, True)


@pytest.mark.parametrize("wallet,starts_now,limit,pending_price", [
    (3_000, 2_500, 100, None), (10_000, 0, 60, 4_000), (10_000, 3_000, 35, 9_000),
    (1_000, 2_500, 100, None), (5_000, 0, 100, 5_000), (9_999, 1, 7, 333), (0, 0, 100, 10),
])
def test_worker_ceiling_equals_the_saving_plans_workshop_budget(
        wallet: int, starts_now: int, limit: int, pending_price: int | None) -> None:
    pending = [] if pending_price is None else [SimpleNamespace(
        slot=2, needed_at=0.0, researching=False, tier="S+",
        target=SimpleNamespace(lab_id="labs.labs-speed", name="Labs Speed", level=11, price=pending_price))]
    saving = saving_plan(pending, wallet=wallet - starts_now, rate=None, spend_limit_pct=limit, now=0.0)
    jar, _, _ = jit_hold(saving, wallet)
    assert workshop_ceiling(route(limit), wallet, jar) == saving.workshop_budget


def test_paused_reason_is_the_plans_summary_not_one_slots_line() -> None:
    pending = [SimpleNamespace(slot=slot, needed_at=0.0, researching=False, tier="S+",
                               target=SimpleNamespace(lab_id=lab, name=name, level=level, price=price))
               for slot, lab, name, level, price in ((1, "labs.game-speed", "Game Speed", 4, 50_000),
                                                     (2, "labs.labs-speed", "Labs Speed", 11, 16_710))]
    saving = saving_plan(pending, wallet=20_000, rate=None, spend_limit_pct=100, now=0.0)
    jar, paused, reason = jit_hold(saving, 20_000)
    assert (jar, paused) == (20_000, True)
    assert reason == saving.why[-1] and reason.startswith("Reserve ") and "Workshop may spend" in reason
    assert jit_hold(saving_plan(pending, wallet=None, rate=None, spend_limit_pct=100, now=0.0),
                    None)[2] == "Wallet unread: Workshop waits"
