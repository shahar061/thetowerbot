from __future__ import annotations

from fleet.lab_list import SlotSaving
from fleet.lab_saving import saving_plan
from fleet.resource_blocks import SlotNext

NOW = 1_000_000.0


def target(slot: int, price: int, needed_in_h: float | None, *, state: str = "researching",
           tier: str = "S") -> SlotSaving:
    """A pending slot target. `state` is the slot's state now: idle (waiting or running a
    filler), owned_unread, or researching (the target waits behind a running research)."""
    lab = SlotNext(f"labs.x{slot}", f"Lab {slot}", 2, price, 3600)
    needed_at = None if needed_in_h is None else NOW + needed_in_h * 3600
    return SlotSaving(slot, lab, needed_at, state == "idle", tier, researching=state == "researching")


def by_slot(result, slot: int):
    return next(t for t in result.targets if t.slot == slot)


def test_idle_unaffordable_target_without_filler_reserves_full_price() -> None:
    result = saving_plan([target(1, 50_000, 0, state="idle")], wallet=20_000, rate=1_000.0,
                         spend_limit_pct=100, now=NOW)
    assert result.reserve == 20_000 and result.workshop_budget == 0
    assert by_slot(result, 1).covered is False
    assert by_slot(result, 1).ready_at == NOW + 30 * 3600
    assert by_slot(result, 1).skipped_reason is None


def test_filler_target_is_reserved_for_the_filler_end() -> None:
    # The filler runs 30 minutes: income covers 500 of the 50,000 by then.
    result = saving_plan([target(1, 50_000, 0.5, state="idle", tier="S+")], wallet=60_000, rate=1_000.0,
                         spend_limit_pct=100, now=NOW)
    assert result.reserve == 49_500 and result.workshop_budget == 10_500
    assert by_slot(result, 1).covered is True and by_slot(result, 1).ready_at == NOW


def test_slot_freeing_later_reserves_only_the_uncovered_part() -> None:
    result = saving_plan([target(2, 38_000, 10)], wallet=20_000, rate=2_900.0, spend_limit_pct=100, now=NOW)
    assert result.reserve == 38_000 - 29_000
    assert result.workshop_budget == 20_000 - 9_000
    assert by_slot(result, 2).covered is True
    assert by_slot(result, 2).ready_at == NOW + 18_000 / 2_900 * 3600
    assert "Slot 2 frees in 10h; income covers 29k of 38k (Lab 2 L2)" in result.why


def test_two_targets_add_up() -> None:
    result = saving_plan([target(1, 10_000, 1), target(2, 10_000, 1)], wallet=15_000, rate=1_000.0,
                         spend_limit_pct=100, now=NOW)
    assert result.reserve == 15_000


def test_past_needed_at_counts_as_now() -> None:
    result = saving_plan([target(1, 10_000, -5)], wallet=20_000, rate=1_000.0, spend_limit_pct=100, now=NOW)
    assert result.reserve == 10_000
    assert by_slot(result, 1).needed_at == NOW and by_slot(result, 1).covered is True


def test_reserve_capped_at_wallet() -> None:
    result = saving_plan([target(1, 10**9, 0, state="idle")], wallet=500, rate=1.0, spend_limit_pct=100, now=NOW)
    assert result.reserve == 500 and result.workshop_budget == 0


def test_overcommitted_starts_leave_nothing_and_never_go_negative() -> None:
    # Each start was judged against the whole wallet, so together they can overspend it.
    result = saving_plan([target(1, 10_000, 0, state="idle")], wallet=-4_000, rate=1_000.0,
                         spend_limit_pct=100, now=NOW)
    assert result.reserve == 0 and result.workshop_budget == 0
    assert result.wallet == -4_000 and by_slot(result, 1).covered is False
    assert saving_plan([], wallet=-4_000, rate=None, spend_limit_pct=50, now=NOW).workshop_budget == 0


def test_unknown_completion_is_not_reserved() -> None:
    result = saving_plan([target(1, 10_000, None)], wallet=20_000, rate=1_000.0, spend_limit_pct=100, now=NOW)
    assert result.reserve == 0 and by_slot(result, 1).covered is None
    assert by_slot(result, 1).skipped_reason == "completion time unknown"


def test_unknown_income_reserves_only_idle_top_tiers_or_affordable() -> None:
    # Spec: with income unknown, reserve targets of slots not running research that are
    # affordable now or tier S+/S. Slot 2's A-tier 5,000 is affordable now, so it counts.
    pending = [target(1, 8_000, 0, state="idle", tier="S+"), target(2, 5_000, 0, state="idle", tier="A"),
               target(3, 4_000, 5, tier="S")]
    result = saving_plan(pending, wallet=20_000, rate=None, spend_limit_pct=100, now=NOW)
    assert result.reserve == 8_000 + 5_000
    assert by_slot(result, 1).ready_at == NOW and by_slot(result, 1).covered is True
    assert by_slot(result, 3).skipped_reason == "income unread: slot still researching"
    assert by_slot(result, 3).ready_at == NOW and by_slot(result, 3).covered is True


def test_unknown_income_holds_only_what_it_can_wait_for() -> None:
    # Owned-but-unread and filler slots are not running research, so they count as idle.
    pending = [target(1, 30_000, 0.5, state="idle", tier="S+"), target(2, 9_000, 0, state="owned_unread", tier="B"),
               target(3, 25_000, 0, state="idle", tier="A")]
    result = saving_plan(pending, wallet=20_000, rate=None, spend_limit_pct=100, now=NOW)
    assert result.reserve == 20_000  # 30,000 + 9,000 clamped to the wallet
    assert by_slot(result, 1).ready_at is None and by_slot(result, 1).covered is False
    assert by_slot(result, 2).skipped_reason is None and by_slot(result, 2).covered is True
    assert by_slot(result, 3).skipped_reason == "income unread: tier A waits until affordable"
    assert by_slot(result, 3).ready_at is None and by_slot(result, 3).covered is False


def test_unread_wallet_spends_nothing() -> None:
    result = saving_plan([target(1, 1_000, 0, state="idle")], wallet=None, rate=1_000.0,
                         spend_limit_pct=100, now=NOW)
    assert result.reserve is None and result.workshop_budget == 0 and result.wallet is None
    assert by_slot(result, 1).covered is None and by_slot(result, 1).skipped_reason == "wallet unread"


def test_spend_limit_applies_after_reserve() -> None:
    result = saving_plan([], wallet=10_000, rate=1_000.0, spend_limit_pct=50, now=NOW)
    assert (result.reserve, result.workshop_budget) == (0, 5_000)
    held = saving_plan([target(1, 4_000, 0, state="owned_unread")], wallet=10_000, rate=1_000.0,
                       spend_limit_pct=50, now=NOW)
    assert (held.reserve, held.workshop_budget) == (4_000, 3_000)


def test_reserve_is_the_largest_prefix_shortfall_whatever_the_input_order() -> None:
    # 10k due now + 10k due in 20h at 1k/h: the first needs 10k held, the second is fully
    # earned by then, so the reserve is 10k. Summing in the given (reversed) order would
    # wrongly hold 20k.
    later, now_ = target(2, 10_000, 20, state="idle"), target(1, 10_000, 0, state="idle")
    result = saving_plan([later, now_], wallet=50_000, rate=1_000.0, spend_limit_pct=100, now=NOW)
    assert result.reserve == 10_000 and result.workshop_budget == 40_000
