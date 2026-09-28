"""The ranked lab list: one ordered list of labs every owned slot draws from.

Validation here; the pure per-slot evaluation (pins, ranked walk, fillers) is
added below it. Kept apart from resource_blocks.py, which keeps slot tracks.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any, Mapping

import lab_catalog
from fleet.build_route import TIERS
from fleet.lab_saving import income_rate
from fleet.strategy_blocks import MAX_BLOCKS

if TYPE_CHECKING:
    from fleet.resource_blocks import GemPlan, LabFacts, LabPlan, SlotContext, SlotNext, SlotNow

# fleet.resource_blocks imports this module in its header, so every runtime
# import from it below stays inside a function: importing it here would cycle.

# The block's own id also claims a slot against _Ids's shared MAX_BLOCKS cap,
# so the list itself may hold at most MAX_BLOCKS - 1 entries.
MAX_ENTRIES = MAX_BLOCKS - 1

_ENTRY_FIELDS = {"id", "lab_id", "to_level", "tier", "pin_slot", "label"}


def validate_lab_list(raw: Mapping[str, Any]) -> dict[str, Any]:
    from fleet.resource_blocks import LAB_SLOTS, _fields, _Ids, _int, _lab_id, _max_level
    block = dict(raw)
    ids = _Ids()
    ids.claim(block)
    _fields(block, {"entries"})
    items = block.get("entries")
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= MAX_ENTRIES:
        raise ValueError(f"a lab list needs 1 to {MAX_ENTRIES} entries")
    entries: list[dict[str, Any]] = []
    highest: dict[str, int] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("lab list entry must be an object")
        entry = dict(item)
        if set(entry) - _ENTRY_FIELDS:
            raise ValueError("unknown lab list entry field")
        ids.claim(entry)
        lab_id = _lab_id(entry.get("lab_id"))
        level = _int(entry.get("to_level"), "to_level", 1, _max_level(lab_id))
        if entry.get("tier") not in TIERS:
            raise ValueError("lab list tier must be one of S+, S, A, B, C")
        if "pin_slot" in entry:
            _int(entry["pin_slot"], "pin_slot", LAB_SLOTS[0], LAB_SLOTS[-1])
        if lab_id in highest and level <= highest[lab_id]:
            raise ValueError("a repeated lab needs a higher to_level further down the list")
        highest[lab_id] = level
        entries.append(entry)
    first = entries[0]
    if first["lab_id"] != lab_catalog.GAME_SPEED or first.get("pin_slot") != 1:
        raise ValueError("the lab list must start with Game Speed pinned to slot 1")
    block["entries"] = entries
    return block


TOP_TIERS = ("S+", "S")


def _unlock_reason(lab_id: str, facts: LabFacts, known: Mapping[str, int]) -> str | None:
    """None when every unlock condition is met; otherwise why not (never guessing)."""
    for condition in lab_catalog.lab(lab_id).unlock:
        if "tier" in condition:
            tier, wave = condition["tier"], condition["wave"]
            best = (facts.best_waves or {}).get(tier)
            if best is None and tier == 1:
                best = facts.best_tier_1_wave
            if best is None:
                return f"unlock unread: Tier {tier} best wave"
            if best < wave:
                return f"locked: needs Tier {tier} wave {wave}"
        else:
            have = known.get(condition["lab"])
            name = lab_catalog.lab(condition["lab"]).name
            if have is None:
                return f"unlock unread: {name} level"
            if have < condition["level"]:
                return f"locked: needs {name} {condition['level']}"
    return None


def _candidate(entry: Mapping[str, Any], slot: int, facts: LabFacts, ctx: SlotContext,
               elsewhere: set[str], claimed: set[str]) -> tuple[SlotNext | None, str]:
    """(option, reason). option is None when the entry is skipped for `reason`.

    `elsewhere` holds the labs running in other slots; `claimed` the labs that
    slots evaluated earlier picked. Each reason starts with its spec category.
    """
    from fleet.resource_blocks import _next_level, _option
    lab_id = entry["lab_id"]
    level = _next_level(lab_id, ctx.known, ctx.running)
    if level is not None and level > entry["to_level"]:
        return None, "finished"
    pin = entry.get("pin_slot")
    if pin is not None and pin != slot:
        return None, "pinned elsewhere"
    if lab_id in elsewhere:
        return None, "running elsewhere"
    if lab_id in claimed:
        return None, "claimed"
    locked = _unlock_reason(lab_id, facts, ctx.known)
    if locked is not None:
        return None, locked
    if level is None:
        return None, "level unread"
    option = _option(lab_id, level)
    if option.price is None:
        return None, "price unknown"
    return option, "ok"


def _within_window(price: int, tier: str, wallet: int | None, rate: float | None,
                   hours_until: float, rules: Any) -> bool:
    """Spec section 4: hours_to_afford <= the tier's window + hours until the slot needs it."""
    if wallet is not None and price <= wallet:
        return True
    if rate is None or wallet is None:
        return tier in TOP_TIERS
    return (price - wallet) / rate <= rules.labs.saving.window_hours[tier] + hours_until


def _hours_until(now_state: SlotNow, now: float) -> float | None:
    """Hours until the slot frees: 0 unless researching, None when its finish is unread."""
    if now_state.state != "researching":
        return 0.0
    if now_state.completes_at is None:
        return None
    return max(0.0, now_state.completes_at - now) / 3600


def _nothing_left(skipped: list[str]) -> str:
    """The closing why line when no entry survives: its most common skip reason."""
    remaining = [reason.partition(": ") for reason in skipped if reason != "finished"]
    if not remaining:
        return "No entry can run now: every entry is finished"
    category, count = Counter(reason for reason, _, _ in remaining).most_common(1)[0]
    detail = next(detail for reason, _, detail in remaining if reason == category)
    share = ("the one remaining entry" if len(remaining) == 1
             else f"all {count} remaining entries" if count == len(remaining)
             else f"{count} of {len(remaining)} remaining entries")
    return f"No entry can run now: {category} for {share}" + (f" ({detail})" if detail else "")


def _target(slot: int, entries: list[Mapping[str, Any]], facts: LabFacts, ctx: SlotContext,
            elsewhere: set[str], claimed: set[str], wallet: int | None, rate: float | None,
            hours_until: float | None, rules: Any,
            why: list[str]) -> tuple[SlotNext | None, Mapping[str, Any] | None]:
    """Step 1 (pins), step 2 (ranked walk) and step 3 (nothing left) of spec section 3."""
    skipped: list[str] = []

    def skip(entry: Mapping[str, Any], reason: str) -> None:
        why.append(f"{entry['id']}: {reason}")
        skipped.append(reason)

    for entry in entries:
        if entry.get("pin_slot") != slot:
            continue
        option, reason = _candidate(entry, slot, facts, ctx, elsewhere, claimed)
        if option is not None:
            why.append(f"{entry['id']}: pinned to slot {slot}")
            return option, entry
        skip(entry, reason)
        if reason != "finished":
            break  # Only the first unfinished pin holds the slot; blocked, it falls through for now.
    for rank, entry in enumerate(entries, start=1):
        if entry.get("pin_slot") == slot:
            continue
        option, reason = _candidate(entry, slot, facts, ctx, elsewhere, claimed)
        if option is None:
            skip(entry, reason)
            continue
        # A researching slot whose finish is unread is judged as if it frees now.
        if not _within_window(option.price, entry["tier"], wallet, rate, hours_until or 0.0, rules):
            skip(entry, f"beyond save window: tier {entry['tier']}")
            continue
        why.append(f"{entry['id']}: rank {rank}")
        return option, entry
    why.append(_nothing_left(skipped))
    return None, None


def evaluate_lab_list(route: Any, facts: LabFacts, *, ctx: SlotContext, gems: GemPlan) -> LabPlan:
    """Pure: each owned slot's target. No reads, writes or clocks.

    Every slot is judged against the same wallet: a slot's target takes no coins
    from the slots after it (spec section 3, "Coins between slots").
    """
    from fleet.resource_blocks import LAB_SLOTS, LabPlan, SlotPlan, _capabilities, research_automated
    rules = route.rules
    entries = route.labs.blocks[0]["entries"]
    rate = income_rate(facts, rules)
    wallet = facts.available_coins if facts.available_coins is not None else facts.wallet_coins
    claimed: set[str] = set()
    plans = []
    for slot in LAB_SLOTS:
        now = ctx.nows[slot]
        if now.owned is not True:
            plans.append(SlotPlan(slot, now, None, None, False, ("Slot not owned or ownership unread",),
                                  None, _capabilities(slot, False, now, facts, False)))
            continue
        own = now.research_id if now.state == "researching" else None
        why: list[str] = []
        target, _ = _target(slot, entries, facts, ctx, ctx.unavailable - {own}, claimed, wallet,
                            rate, _hours_until(now, facts.now), rules, why)
        automated = (target is not None and rules.labs.auto_start
                     and research_automated(target.lab_id, slot))
        note = "Start manually" if target is not None and not automated else None
        starts_now = (target is not None and now.state == "idle" and wallet is not None
                      and target.price <= wallet)
        covered = (True if starts_now else None if wallet is None or target is None
                   else False if now.state == "idle" else None)
        plans.append(SlotPlan(slot, now, target, covered, automated, tuple(why), note,
                              _capabilities(slot, automated, now, facts, True)))
        if target is not None:
            claimed.add(target.lab_id)
    return LabPlan(facts.wallet_coins, facts.jar, tuple(plans), gems,
                   getattr(route, "revision", 0), facts.account_id, facts.scope, facts.now)
