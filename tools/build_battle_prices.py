"""Compile battle cash curves from pinned TheTowerSDK reference data."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx2

REVISION = "da19fefc30e091c2abb7d7cddefea5e60f3915a6"
BASE = f"https://raw.githubusercontent.com/TmRxJD/TheTowerSDK/{REVISION}"
NAMES = {
    "cash_bonus": "Cash Bonus", "cash_per_wave": "Cash / Wave",
    "coins_per_kill_bonus": "Coins / Kill Bonus", "coins_per_wave": "Coins / Wave",
    "attack_speed": "Attack Speed", "health": "Health", "defense_percent": "Defense Percent",
    "thorns": "Thorns", "orbs": "Orbs", "orb_speed": "Orb Speed",
    "knockback_force": "Knockback Force", "knockback_chance": "Knockback Chance",
    "multishot_chance": "Multishot Chance", "multishot_targets": "Multishot Targets",
    "bounce_shot_chance": "Bounce Shot Chance", "bounce_shot_targets": "Bounce Shot Targets",
    "bounce_shot_range": "Bounce Shot Range",
}


async def build() -> None:
    output = Path(__file__).resolve().parent.parent / "catalog"
    async with httpx2.AsyncClient(timeout=30) as client:
        response = await client.get(f"{BASE}/src/data/workshop/table.json")
        response.raise_for_status()
        source = response.json()
        curves = {}
        for uid, name in NAMES.items():
            levels = source[name]
            cash = [levels[str(i)]["cash"] for i in range(len(levels) - 1)]
            if any(type(n) is not int or n <= 0 for n in cash):
                raise ValueError(f"{uid}: nonpositive or noninteger cash")
            if any(b <= a for a, b in zip(cash, cash[1:])):
                raise ValueError(f"{uid}: ambiguous cash curve")
            # The source sometimes prices the terminal row. It is not proof
            # that another level exists; game MAX always wins.
            curves[uid] = cash
        data = {"version": 1, "source_revision": REVISION,
                "source_url": f"{BASE}/src/data/workshop/table.json",
                "attribution": "TheTowerSDK / TmRxJD; community reference data compiled with help from Matthew's Effective Paths spreadsheets.",
                "scope": "Cash prices indexed by battle upgrade count, not permanent Workshop level. Calibrate against the running game.",
                "curves": curves}
        (output / "battle-cash.v1.json").write_text(json.dumps(data, separators=(",", ":")) + "\n")
        for name in ("LICENSE", "NOTICE"):
            response = await client.get(f"{BASE}/{name}")
            response.raise_for_status()
            (output / f"battle-cash.{name}").write_text(response.text)
    print(f"Wrote {len(curves)} battle cash curves and source notices")


if __name__ == "__main__":
    asyncio.run(build())
