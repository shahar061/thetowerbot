"""Semantic identities for the standard battle and Workshop upgrades.

Names and aliases describe what OCR may read. Prices, coordinates and account
availability deliberately live elsewhere because all three change at runtime.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


CATEGORIES: tuple[str, ...] = ("ATTACK", "DEFENSE", "UTILITY")


@dataclass(frozen=True)
class Upgrade:
    """One stable upgrade identity shared by battle and Workshop automation."""

    id: str
    name: str
    category: str
    aliases: tuple[str, ...] = ()
    unlock: bool = False
    unlocks: tuple[str, ...] = ()

    @property
    def concept_id(self) -> str:
        """Canonical identity; the flat ID remains the execution contract."""
        return f"unlocks.{self.id.removeprefix('unlock_')}" if self.unlock else f"stats.{self.id}"


def _upgrade(
    upgrade_id: str,
    name: str,
    category: str,
    *aliases: str,
    unlock: bool = False,
    unlocks: tuple[str, ...] = (),
) -> Upgrade:
    return Upgrade(upgrade_id, name, category, tuple(aliases), unlock, unlocks)


# Order follows each game's tab from the always-visible rows through later
# unlocks. Unlock tiles are catalog entries because OCR sees and addresses them
# like ordinary Workshop rows, while ``unlock=True`` keeps spending gated.
CATALOG: tuple[Upgrade, ...] = (
    _upgrade("damage", "Damage", "ATTACK"),
    _upgrade("attack_speed", "Attack Speed", "ATTACK", "AS"),
    _upgrade("critical_chance", "Critical Chance", "ATTACK", "Crit Chance"),
    _upgrade("critical_factor", "Critical Factor", "ATTACK", "Crit Factor"),
    _upgrade(
        "unlock_range_upgrades",
        "Unlock Range Upgrades",
        "ATTACK",
        unlock=True,
        unlocks=("range", "damage_per_meter"),
    ),
    _upgrade("range", "Range", "ATTACK", "Tower Range", "Attack Range"),
    _upgrade(
        "damage_per_meter",
        "Damage / Meter",
        "ATTACK",
        "Damage Per Meter",
        "Damage/Meter",
        "DPM",
    ),
    _upgrade(
        "unlock_multishot",
        "Unlock Multishot",
        "ATTACK",
        "Unlock Multi Shot",
        "Unlock Multishot Upgrades",
        "Unlock Multi Shot Upgrades",
        unlock=True,
        unlocks=("multishot_chance", "multishot_targets"),
    ),
    _upgrade("multishot_chance", "Multishot Chance", "ATTACK", "Multi Shot Chance"),
    _upgrade("multishot_targets", "Multishot Targets", "ATTACK", "Multi Shot Targets"),
    _upgrade(
        "unlock_rapid_fire",
        "Unlock Rapid Fire",
        "ATTACK",
        "Unlock Rapidfire",
        "Unlock Rapid Fire Upgrades",
        "Unlock Rapidfire Upgrades",
        unlock=True,
        unlocks=("rapid_fire_chance", "rapid_fire_duration"),
    ),
    _upgrade("rapid_fire_chance", "Rapid Fire Chance", "ATTACK"),
    _upgrade("rapid_fire_duration", "Rapid Fire Duration", "ATTACK"),
    _upgrade("bounce_shot_chance", "Bounce Shot Chance", "ATTACK"),
    _upgrade("bounce_shot_targets", "Bounce Shot Targets", "ATTACK"),
    _upgrade("bounce_shot_range", "Bounce Shot Range", "ATTACK"),
    _upgrade("super_crit_chance", "Super Crit Chance", "ATTACK", "Super Critical Chance"),
    _upgrade(
        "super_crit_mult",
        "Super Crit Mult",
        "ATTACK",
        "Super Crit Multi",
        "Super Critical Multiplier",
    ),
    _upgrade("rend_armor_chance", "Rend Armor Chance", "ATTACK"),
    _upgrade(
        "rend_armor_mult",
        "Rend Armor Mult",
        "ATTACK",
        "Rend Armor Multi",
        "Rend Armor Multiplier",
    ),
    _upgrade("health", "Health", "DEFENSE", "HP"),
    _upgrade("health_regen", "Health Regen", "DEFENSE", "Health Regeneration", "Regen"),
    _upgrade(
        "unlock_defense_upgrades",
        "Unlock Defense Upgrades",
        "DEFENSE",
        "Unlock Defence Upgrades",
        unlock=True,
        unlocks=("defense_percent", "defense_absolute"),
    ),
    _upgrade(
        "defense_percent",
        "Defense %",
        "DEFENSE",
        "Defense Percent",
        "Defence %",
        "Defence Percent",
        "Def %",
    ),
    _upgrade(
        "defense_absolute",
        "Defense Absolute",
        "DEFENSE",
        "Defence Absolute",
        "Def Abs",
        "Absolute Defense",
    ),
    _upgrade(
        "unlock_thorns",
        "Unlock Thorns",
        "DEFENSE",
        "Unlock Thorn Damage",
        "Unlock Thorns Damage",
        "Unlock Thorn Upgrades",
        unlock=True,
        unlocks=("thorns",),
    ),
    _upgrade("thorns", "Thorns", "DEFENSE", "Thorn Damage", "Thorns Damage"),
    _upgrade(
        "unlock_lifesteal",
        "Unlock Lifesteal",
        "DEFENSE",
        "Unlock Life Steal",
        # The Workshop tile reads "Unlock Lifesteal Upgrades".
        "Unlock Lifesteal Upgrades",
        "Unlock Life Steal Upgrades",
        unlock=True,
        unlocks=("lifesteal",),
    ),
    _upgrade("lifesteal", "Lifesteal", "DEFENSE", "Life Steal"),
    _upgrade(
        "unlock_knockback",
        "Unlock Knockback",
        "DEFENSE",
        "Unlock Knock Back",
        "Unlock Knockback Upgrades",
        "Unlock Knock Back Upgrades",
        unlock=True,
        unlocks=("knockback_chance", "knockback_force"),
    ),
    _upgrade("knockback_chance", "Knockback Chance", "DEFENSE", "Knock Back Chance"),
    _upgrade("knockback_force", "Knockback Force", "DEFENSE", "Knock Back Force"),
    _upgrade(
        "unlock_orbs",
        "Unlock Orbs",
        "DEFENSE",
        "Unlock Orb Upgrades",
        "Unlock Orbs Upgrades",
        unlock=True,
        unlocks=("orb_speed", "orbs"),
    ),
    _upgrade("orb_speed", "Orb Speed", "DEFENSE", "Orbs Speed"),
    _upgrade("orbs", "Orbs", "DEFENSE", "Orb Count", "Orbs Count"),
    _upgrade("shockwave_size", "Shockwave Size", "DEFENSE", "Shock Wave Size"),
    _upgrade(
        "shockwave_frequency",
        "Shockwave Frequency",
        "DEFENSE",
        "Shock Wave Frequency",
    ),
    _upgrade("land_mine_chance", "Land Mine Chance", "DEFENSE", "Landmine Chance"),
    _upgrade("land_mine_damage", "Land Mine Damage", "DEFENSE", "Landmine Damage"),
    _upgrade("land_mine_radius", "Land Mine Radius", "DEFENSE", "Landmine Radius"),
    _upgrade("death_defy", "Death Defy", "DEFENSE"),
    _upgrade("wall_health", "Wall Health", "DEFENSE"),
    _upgrade("wall_rebuild", "Wall Rebuild", "DEFENSE", "Wall Rebuild Time"),
    _upgrade(
        "unlock_cash_bonuses",
        "Unlock Cash Bonuses",
        "UTILITY",
        "Unlock Cash Bonus",
        unlock=True,
        unlocks=("cash_bonus", "cash_per_wave"),
    ),
    _upgrade("cash_bonus", "Cash Bonus", "UTILITY"),
    _upgrade("cash_per_wave", "Cash / Wave", "UTILITY", "Cash Per Wave", "Cash/Wave"),
    _upgrade(
        "unlock_coin_bonuses",
        "Unlock Coin Bonuses",
        "UTILITY",
        "Unlock Coins Bonuses",
        "Unlock Coin Bonus",
        unlock=True,
        unlocks=("coins_per_kill_bonus", "coins_per_wave"),
    ),
    _upgrade(
        "coins_per_kill_bonus",
        "Coins / Kill Bonus",
        "UTILITY",
        "Coins Per Kill Bonus",
        "Coins/Kill Bonus",
        "Coin Per Kill Bonus",
        "Coin/Kill Bonus",
        "Coins Per Kill",
    ),
    _upgrade("coins_per_wave", "Coins / Wave", "UTILITY", "Coins Per Wave", "Coins/Wave"),
    _upgrade(
        "unlock_free_upgrades",
        "Unlock Free Upgrades",
        "UTILITY",
        "Unlock Free Upgrade",
        # What the tab actually prints: the three rows this unlock grants are
        # chances of a free upgrade, and the row is named for them.
        "Unlock Upgrade Chances",
        unlock=True,
        unlocks=(
            "free_attack_upgrade",
            "free_defense_upgrade",
            "free_utility_upgrade",
        ),
    ),
    _upgrade("free_attack_upgrade", "Free Attack Upgrade", "UTILITY"),
    _upgrade("free_defense_upgrade", "Free Defense Upgrade", "UTILITY", "Free Defence Upgrade"),
    _upgrade("free_utility_upgrade", "Free Utility Upgrade", "UTILITY"),
    _upgrade("interest_per_wave", "Interest / Wave", "UTILITY", "Interest Per Wave", "Interest"),
    _upgrade("recovery_amount", "Recovery Amount", "UTILITY", "Package Recovery Amount"),
    _upgrade("max_recovery", "Max Recovery", "UTILITY", "Maximum Recovery"),
    _upgrade("package_chance", "Package Chance", "UTILITY", "Recovery Package Chance"),
    _upgrade(
        "enemy_attack_level_skip",
        "Enemy Attack Level Skip",
        "UTILITY",
        "Enemy Attack Skip",
        "EALS",
    ),
    _upgrade(
        "enemy_health_level_skip",
        "Enemy Health Level Skip",
        "UTILITY",
        "Enemy Health Skip",
        "EHLS",
    ),
)


_BY_ID: dict[str, Upgrade] = {upgrade.id: upgrade for upgrade in CATALOG}
_DECREASING_TARGET_IDS = frozenset({"shockwave_frequency", "wall_rebuild"})


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


_BY_NAME: dict[str, tuple[Upgrade, ...]] = {}
for _item in CATALOG:
    for _label in (_item.name, _item.id, *_item.aliases):
        _key = _normalise(_label)
        _BY_NAME[_key] = (*_BY_NAME.get(_key, ()), _item)

if len(_BY_ID) != len(CATALOG):
    raise RuntimeError("upgrade catalog IDs must be unique")
if any(upgrade.category not in CATEGORIES for upgrade in CATALOG):
    raise RuntimeError("upgrade catalog contains an invalid category")
if any(upgrade.unlocks and not upgrade.unlock for upgrade in CATALOG):
    raise RuntimeError("only unlock tiles may declare unlocked upgrades")
if any(
    child not in _BY_ID
    for upgrade in CATALOG
    for child in upgrade.unlocks
):
    raise RuntimeError("upgrade catalog contains an unknown unlocked upgrade ID")


def by_id(upgrade_id: str) -> Upgrade | None:
    """Return the stable catalog record for ``upgrade_id``, if it exists."""

    return _BY_ID.get(upgrade_id)


def target_reached(upgrade_id: str, value: float, target: float) -> bool:
    """Whether a displayed value has met this upgrade's configured target.

    Most upgrades improve upward. Shockwave Frequency and Wall Rebuild are
    displayed as intervals and improve downward, so their targets are floors.
    """

    if by_id(upgrade_id) is None:
        raise ValueError(f"unknown upgrade_id {upgrade_id!r}")
    if not math.isfinite(value) or not math.isfinite(target):
        return False
    if upgrade_id in _DECREASING_TARGET_IDS:
        return value <= target
    return value >= target


def resolve(name: str, category: str | None = None) -> Upgrade | None:
    """Resolve an OCR or guide label, optionally constrained to one tab."""

    if not isinstance(name, str):
        return None
    wanted_category: str | None = None
    if category is not None:
        if not isinstance(category, str):
            return None
        wanted_category = category.upper()
        if wanted_category not in CATEGORIES:
            return None
    candidates = _BY_NAME.get(_normalise(name), ())
    matches = {upgrade.id: upgrade for upgrade in candidates
               if wanted_category is None or upgrade.category == wanted_category}
    return next(iter(matches.values())) if len(matches) == 1 else None


def catalog_payload() -> list[dict[str, Any]]:
    """Return the catalog as mutable JSON-shaped objects for the web API."""

    return [
        {
            "id": upgrade.id,
            "concept_id": upgrade.concept_id,
            "name": upgrade.name,
            "category": upgrade.category,
            "aliases": list(upgrade.aliases),
            "unlock": upgrade.unlock,
            "unlocks": list(upgrade.unlocks),
        }
        for upgrade in CATALOG
    ]
