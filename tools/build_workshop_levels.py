"""Regenerate catalog/workshop-levels.v1.json from pinned TowerSmith tables.

Each upgrade keeps its full per-level ladder: the value the Workshop row
displays at level L (`values[L]`, level 0 is the base) and the coin price of
buying L to L+1 (`next_coins[L]`, null at max). Values are stored in the unit
the game shows, so a read of "30.50m" or "0.65s" compares directly.

Needs network access; run from the repository root:

    python -m tools.build_workshop_levels
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

REVISION = "da361120e28de87b33ffc340d25962b966dc6150"
BASE = f"https://raw.githubusercontent.com/AngryBrit/tower-smith/{REVISION}/tables/workshop"
OUTPUT = Path(__file__).resolve().parent.parent / "catalog" / "workshop-levels.v1.json"

# upgrade_id -> TowerSmith table path, and the factor that turns the table's
# stored value into the displayed one (Range is stored in micro-metres, the
# timers in 1e-21 seconds).
TABLES: dict[str, tuple[str, float]] = {
    "damage": ("attack/damage", 1), "attack_speed": ("attack/attack-speed", 1),
    "critical_chance": ("attack/critical-chance", 1), "critical_factor": ("attack/critical-factor", 1),
    "range": ("attack/range", 1e-6), "damage_per_meter": ("attack/damage-meter", 1),
    "multishot_chance": ("attack/multishot-chance", 1), "multishot_targets": ("attack/multishot-targets", 1),
    "rapid_fire_chance": ("attack/rapid-fire-chance", 1),
    "rapid_fire_duration": ("attack/rapid-fire-duration", 1e-21),
    "bounce_shot_chance": ("attack/bounce-shot-chance", 1),
    "bounce_shot_targets": ("attack/bounce-shot-targets", 1),
    "bounce_shot_range": ("attack/bounce-shot-range", 1e-6),
    "super_crit_chance": ("attack/super-crit-chance", 1), "super_crit_mult": ("attack/super-crit-mult", 1),
    "rend_armor_chance": ("attack/rend-armor-chance", 1), "rend_armor_mult": ("attack/rend-armor-mult", 1),
    "health": ("defense/health", 1), "health_regen": ("defense/health-regen", 1),
    "defense_percent": ("defense/defense-percent", 1), "defense_absolute": ("defense/defense-absolute", 1),
    "thorns": ("defense/thorns", 1), "lifesteal": ("defense/lifesteal", 1),
    "knockback_chance": ("defense/knockback-chance", 1), "knockback_force": ("defense/knockback-force", 1),
    "orb_speed": ("defense/orb-speed", 1), "orbs": ("defense/orbs", 1),
    "shockwave_size": ("defense/shockwave-size", 1),
    "shockwave_frequency": ("defense/shockwave-frequency", 1e-21),
    "land_mine_chance": ("defense/land-mine-chance", 1), "land_mine_damage": ("defense/land-mine-damage", 1),
    "land_mine_radius": ("defense/land-mine-radius", 1), "death_defy": ("defense/death-defy", 1),
    "wall_health": ("defense/wall-health", 1), "wall_rebuild": ("defense/wall-rebuild", 1e-21),
    "cash_bonus": ("utility/cash-bonus", 1), "cash_per_wave": ("utility/cash-wave", 1),
    "coins_per_kill_bonus": ("utility/coins-kill-bonus", 1), "coins_per_wave": ("utility/coins-wave", 1),
    "free_attack_upgrade": ("utility/free-attack-upgrade", 1),
    "free_defense_upgrade": ("utility/free-defense-upgrade", 1),
    "free_utility_upgrade": ("utility/free-utility-upgrade", 1),
    "interest_per_wave": ("utility/interest-wave", 1), "recovery_amount": ("utility/recovery-amount", 1),
    "max_recovery": ("utility/max-recovery", 1), "package_chance": ("utility/package-chance", 1),
    "enemy_attack_level_skip": ("utility/enemy-attack-level-skip", 1),
    "enemy_health_level_skip": ("utility/enemy-health-level-skip", 1),
}


def _significant(value: float) -> float:
    return float(f"{value:.7g}")


def build() -> dict:
    upgrades = {}
    for upgrade_id, (path, scale) in TABLES.items():
        with urllib.request.urlopen(f"{BASE}/{path}.json", timeout=30) as response:
            table = json.load(response)
        levels = sorted(table["levels"], key=lambda row: row["level"])
        if [row["level"] for row in levels] != list(range(table["maxLevel"] + 1)):
            raise ValueError(f"{path}: levels are not 0..maxLevel")
        upgrades[upgrade_id] = {
            "max_level": table["maxLevel"],
            "values": [_significant(row["value"] * scale) for row in levels],
            # A few tables price the max level at 0 instead of flagging it.
            "next_coins": [None if row["level"] == table["maxLevel"] or row["nextCoins"].get("maxed")
                           else round(row["nextCoins"]["coins"]) for row in levels],
        }
    return {
        "version": 1,
        "source_revision": REVISION,
        "attribution": "Workshop level ladders compiled from AngryBrit/tower-smith tables/workshop. "
                       "No calculator code copied.",
        "source_license": "CC BY-NC-SA 4.0",
        "source_license_url": f"https://raw.githubusercontent.com/AngryBrit/tower-smith/{REVISION}/LICENCE",
        "scope": "values[L] is the displayed Workshop value at level L (0 = base); "
                 "next_coins[L] is the coin cost to buy L to L+1, null at max.",
        "upgrades": upgrades,
    }


if __name__ == "__main__":
    OUTPUT.write_text(json.dumps(build(), separators=(",", ":")) + "\n")
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size // 1024} KiB)")
