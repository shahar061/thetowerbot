"""Pure, account-bound next-purchase decisions for the manual reroll goal."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from typing import Mapping

import upgrades


@dataclass(frozen=True)
class RerollFacts:
    account_id: str
    best_tier_1_wave: int | None = None
    purchases: Mapping[str, int] = field(default_factory=dict)
    values: Mapping[str, float] = field(default_factory=dict)
    wallet_coins: int | None = None
    lifetime_coins: int | None = None
    prices: Mapping[str, int] = field(default_factory=dict)
    spend_fraction: float | None = None


@dataclass(frozen=True)
class RerollDecision:
    account_id: str
    stage: str
    goal: str
    state: str
    upgrade_id: str | None
    item: str | None
    category: str | None
    price: int | None
    wallet_coins: int | None
    lifetime_coins: int | None
    reason: str


# Weights choose a repeatable, diminishing-return rotation. They are
# progression preferences, not purchase permission; the existing buyer still
# validates the account, screen, row, balance, price, and transaction.
_OPENING = (
    ("damage", 6), ("attack_speed", 6),
    ("unlock_coin_bonuses", 5), ("coins_per_kill_bonus", 5),
    ("coins_per_wave", 3),
)
_TURTLE = (
    ("unlock_defense_upgrades", 10), ("unlock_thorns", 9),
    ("defense_absolute", 10), ("thorns", 9),
    ("defense_percent", 5), ("coins_per_kill_bonus", 2),
)
_PREREQUISITES = {
    "coins_per_kill_bonus": "unlock_coin_bonuses",
    "coins_per_wave": "unlock_coin_bonuses",
    "defense_absolute": "unlock_defense_upgrades",
    "defense_percent": "unlock_defense_upgrades",
    "thorns": "unlock_thorns",
}
_TARGETS = {"thorns": 51., "defense_percent": 50.}


def choose_next(facts: RerollFacts) -> RerollDecision:
    if not facts.account_id:
        raise ValueError("reroll account identity required")
    if facts.best_tier_1_wave is not None and facts.best_tier_1_wave >= 60:
        return RerollDecision(facts.account_id, "stones", "Earn stones for the first Ultimate Weapon",
                              "needs_operator", None, None, None, None, facts.wallet_coins,
                              facts.lifetime_coins,
                              "Tier 1 Wave 60 was verified; Ultimate Weapon choice stays with the operator.")
    stage = "turtle" if (facts.best_tier_1_wave or 0) >= 20 else "opening"
    candidates = _TURTLE if stage == "turtle" else _OPENING
    ranked: list[tuple[float, int, str]] = []
    for order, (upgrade_id, weight) in enumerate(candidates):
        if (upgrade_id.startswith("unlock_") and facts.purchases.get(upgrade_id, 0) > 0):
            continue
        prerequisite = _PREREQUISITES.get(upgrade_id)
        if prerequisite and facts.purchases.get(prerequisite, 0) < 1:
            continue
        target = _TARGETS.get(upgrade_id)
        if target is not None and facts.values.get(upgrade_id, 0) >= target:
            continue
        if not any(item.id == upgrade_id for item in upgrades.CATALOG):
            continue
        ranked.append((weight / (1 + max(0, facts.purchases.get(upgrade_id, 0))), -order, upgrade_id))
    if not ranked:
        return RerollDecision(facts.account_id, stage, "Review account progression",
                              "needs_operator", None, None, None, None, facts.wallet_coins,
                              facts.lifetime_coins, "No supported next purchase has verified prerequisites.")
    upgrade_id = max(ranked)[2]
    upgrade = next(item for item in upgrades.CATALOG if item.id == upgrade_id)
    price = facts.prices.get(upgrade_id)
    if price is not None and price < 0:
        price = None
    if price is None:
        state = "observe_price"
        reason = "Read a fresh Workshop price before deciding whether to buy or save."
    elif facts.wallet_coins is None:
        state = "observe_balance"
        reason = "Read the current Workshop coin balance before spending."
    elif facts.wallet_coins < price:
        state = "save_coins"
        reason = f"Save {price - facts.wallet_coins} more coins for {upgrade.name}."
    elif (facts.spend_fraction is not None and 0 < facts.spend_fraction < 1
          and price > int(facts.wallet_coins * facts.spend_fraction)):
        state = "save_coins"
        needed = ceil(price / facts.spend_fraction) - facts.wallet_coins
        reason = f"Save {needed} more coins to keep this purchase within the Workshop spend limit."
    else:
        state = "buy"
        reason = f"{upgrade.name} is affordable with the observed Workshop balance."
    if (price is not None and facts.lifetime_coins is not None
            and facts.lifetime_coins > 0):
        reason += f" Its price is {price / facts.lifetime_coins:.0%} of verified lifetime coins."
    goal = "Reach Tier 1 Wave 20" if stage == "opening" else "Reach Tier 1 Wave 60"
    return RerollDecision(facts.account_id, stage, goal, state, upgrade_id,
                          upgrade.name, upgrade.category, price, facts.wallet_coins,
                          facts.lifetime_coins, reason)
