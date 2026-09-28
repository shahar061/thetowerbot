"""Just-in-time lab saving: how many coins Workshop must leave for upcoming labs. Pure."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence


def income_rate(facts: Any, rules: Any) -> float | None:
    """Coins per hour after the safety margin; None when unread or not positive."""
    rate = getattr(facts, "coins_per_hour", None)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
        return None
    return rate * rules.labs.saving.income_margin_pct / 100


TOP_TIERS = ("S+", "S")


@dataclass(frozen=True)
class SavingTarget:
    """One slot target the plan funds. ready_at: when the wallet plus income affords it
    alone (None: never, income unread). skipped_reason: why it isn't in the reserve."""
    slot: int
    lab_id: str
    name: str
    level: int | None
    price: int
    needed_at: float | None
    ready_at: float | None
    covered: bool | None
    skipped_reason: str | None = None


@dataclass(frozen=True)
class SavingPlan:
    reserve: int | None
    workshop_budget: int
    wallet: int | None
    coins_per_hour: float | None
    targets: tuple[SavingTarget, ...]
    why: tuple[str, ...]


def _coins(value: float) -> str:
    if value < 1000:
        return f"{value:.0f}"
    return f"{value / 1000:.1f}".rstrip("0").rstrip(".") + "k"


def _hours(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".") + "h"


def _label(p: Any) -> str:
    return p.target.name + (f" L{p.target.level}" if p.target.level is not None else "")


def saving_plan(pending: Sequence[Any], *, wallet: int | None, rate: float | None,
                spend_limit_pct: int, now: float) -> SavingPlan:
    """How much Workshop must leave so each slot's next lab is affordable when due (spec section 4).

    `pending` holds the slot targets not starting now (SlotSaving); `wallet` is the wallet
    left after every start happening now, which can be negative when those starts together
    overspend it. The reserve and the budget treat a negative wallet as 0.
    """
    if wallet is None:
        targets = tuple(SavingTarget(p.slot, p.target.lab_id, p.target.name, p.target.level, p.target.price,
                                     p.needed_at, None, None, "wallet unread") for p in pending)
        return SavingPlan(None, 0, None, rate, targets, ("Wallet unread: Workshop waits",))
    cap = max(0, wallet)
    dated = sorted((p for p in pending if p.needed_at is not None), key=lambda p: p.needed_at)
    targets: list[SavingTarget] = []
    why: list[str] = []
    need, cumulative = 0.0, 0
    for p in [*dated, *(p for p in pending if p.needed_at is None)]:
        price = p.target.price
        short = max(0, price - wallet)
        hours_to_afford = short / rate if rate is not None else 0.0 if short == 0 else None
        ready_at = None if hours_to_afford is None else now + hours_to_afford * 3600
        if p.needed_at is None:
            targets.append(SavingTarget(p.slot, p.target.lab_id, p.target.name, p.target.level, price,
                                        None, ready_at, None, "completion time unknown"))
            why.append(f"Slot {p.slot}: completion time unknown, not saving for {_label(p)} yet")
            continue
        needed_at = max(now, p.needed_at)
        hours = (needed_at - now) / 3600
        covered = ready_at is not None and ready_at <= needed_at
        skipped = None
        if rate is not None:
            cumulative += price
            need = max(need, cumulative - rate * hours)
            if hours:
                why.append(f"Slot {p.slot} frees in {_hours(hours)}; income covers "
                           f"{_coins(min(price, rate * hours))} of {_coins(price)} ({_label(p)})")
            else:
                why.append(f"Slot {p.slot} needs {_coins(price)} now for {_label(p)}"
                           + ("" if short == 0 else f"; ready in ~{_hours(hours_to_afford)}"))
        elif p.researching:
            skipped = "income unread: slot still researching"
            why.append(f"Slot {p.slot}: income unread, not saving ahead for {_label(p)}")
        elif short == 0 or p.tier in TOP_TIERS:
            need += price
            why.append(f"Slot {p.slot}: income unread, holding {_coins(price)} for {_label(p)}")
        else:
            skipped = f"income unread: tier {p.tier} waits until affordable"
            why.append(f"Slot {p.slot}: income unread, {_label(p)} waits until affordable")
        targets.append(SavingTarget(p.slot, p.target.lab_id, p.target.name, p.target.level, price,
                                    needed_at, ready_at, covered, skipped))
    reserve = min(cap, max(0, math.ceil(need)))
    budget = (cap - reserve) * spend_limit_pct // 100
    why.append(f"Reserve {_coins(reserve)}; Workshop may spend {_coins(budget)}")
    return SavingPlan(reserve, budget, wallet, rate, tuple(targets), tuple(why))
