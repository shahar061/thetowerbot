"""Import lab price/time tables from the Tower Fandom wiki into catalog/labs.v2.json.

Run: uv run python -m tools.import_wiki_lab_tables
The wiki blocks plain page fetches (402/403); the MediaWiki API returns wikitext.
A table that fails to fetch or read keeps `levels: null` - never an invented price.
An unlock that can't be read is never written as `[]` (no condition): the importer
keeps the lab's existing entry, uses UNLOCK_OVERRIDES, or stops with an error.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
V1 = ROOT / "catalog" / "labs.v1.json"
V2 = ROOT / "catalog" / "labs.v2.json"
API = "https://the-tower-idle-tower-defense.fandom.com/api.php"
WIKI = "https://the-tower-idle-tower-defense.fandom.com/wiki/"
SCOPE = ("Planning prices for Labs and gem purchases. Lab tables come from the wiki's Lab/* and "
         "Perk_Labs/* pages; labs with levels null have an unknown price. Observed game prices always win.")

PAGES: dict[str, tuple[str, str]] = {
    "labs.labs-speed": ("Labs Speed", "Lab/Lab_Speed"),
    "labs.attack-speed": ("Attack Speed", "Lab/Attack_Speed"),
    "labs.coins-wave": ("Coins / Wave", "Lab/Coins_Per_Wave"),
    "labs.cash-bonus": ("Cash Bonus", "Lab/Cash_Bonus"),
    "labs.coins-kill-bonus": ("Coins / Kill Bonus", "Lab/Coins_Per_Kill_Bonus"),
    "labs.starting-cash": ("Starting Cash", "Lab/Starting_Cash"),
    "labs.buy-multiplier": ("Buy Multiplier", "Lab/Buy_Multiplier"),
    "labs.workshop-attack-discount": ("Workshop Attack Discount", "Lab/Workshop_Attack_Discount"),
    "labs.workshop-defense-discount": ("Workshop Defense Discount", "Lab/Workshop_Defense_Discount"),
    "labs.workshop-utility-discount": ("Workshop Utility Discount", "Lab/Workshop_Utility_Discount"),
    "labs.labs-coin-discount": ("Labs Coin Discount", "Lab/Labs_Coin_Discount"),
    "labs.light-speed-shots": ("Light Speed Shots", "Lab/Light_Speed_Shots"),
    "labs.health": ("Health", "Lab/Health"),
    "labs.damage": ("Damage", "Lab/Damage"),
    "labs.critical-factor": ("Critical Factor", "Lab/Critical_Factor"),
    "labs.unlock-perks": ("Unlock Perks", "Perk_Labs/Unlock_Perks"),
    "labs.first-perk-choice": ("First Perk Choice", "Perk_Labs/First_Perk_Choice"),
    "labs.perk-option-quantity": ("Perk Option Quantity", "Perk_Labs/Perk_Option_Quantity"),
    "labs.ban-perks": ("Ban Perks", "Perk_Labs/Ban_Perks"),
    "labs.standard-perks-bonus": ("Standard Perks Bonus", "Perk_Labs/Standard_Perk_Bonus"),
    "labs.improve-trade-off-perks": ("Improve Trade-Off Perks", "Perk_Labs/Improve_Trade-Off_Perks"),
}
# Prose for lab prerequisites varies ("after Unlock Perk completed"), so it is fixed here.
LAB_PREREQS: dict[str, dict[str, Any]] = {
    lab_id: {"lab": "labs.unlock-perks", "level": 1}
    for lab_id in ("labs.first-perk-choice", "labs.perk-option-quantity", "labs.ban-perks",
                   "labs.standard-perks-bonus", "labs.improve-trade-off-perks")
}
# Tier/wave unlocks used when a page is silent, unreadable or unreachable. Parsed prose
# must agree with an entry here, or the import stops.
UNLOCK_OVERRIDES: dict[str, list[dict[str, int]]] = {
    "labs.labs-speed": [{"tier": 1, "wave": 150}],  # Lab/Lab_Speed: "in Milestones at tier 1 wave 150"
    # Perk_Labs/Perk_Option_Quantity: "Unlocked in Milestones at tier 4 wave 80 and after Unlock Perk completed"
    "labs.perk-option-quantity": [{"tier": 4, "wave": 80}],
    "labs.light-speed-shots": [{"tier": 7, "wave": 10}],  # Lab/Light_Speed_Shots: "at tier 7 wave 10"
    # Perk_Labs/Unlock_Perks is silent; the wiki's Perks page says "complete the Unlock Perks
    # lab in Milestones at tier 2 wave 150".
    "labs.unlock-perks": [{"tier": 2, "wave": 150}],
    # Lab/Damage: "unlock damage lab with labs in Milestones at tier 1" - no wave of its own,
    # so no condition beyond the labs milestone (catalog `labs_unlock`).
    "labs.damage": [],
}
_SUFFIX = {"K": 10**3, "M": 10**6, "B": 10**9, "T": 10**12}
_DURATION = re.compile(r"(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?")
_TIER_WAVE = re.compile(r"\btier\s+(\d+)\s+wave\s+(\d+)", re.IGNORECASE)
_UNLOCK_MENTION = re.compile(r"milestone|\btier\s+\d", re.IGNORECASE)
_STATED_LEVELS = re.compile(r"(\d+)\s+levels?\b", re.IGNORECASE)


def parse_duration(text: str) -> int:
    match = _DURATION.fullmatch(text.strip())
    if match is None or not any(match.groups()):
        raise ValueError(f"unreadable duration {text!r}")
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def parse_coins(text: str) -> int:
    cleaned = text.strip().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMBT]?)", cleaned)
    if match is None:
        raise ValueError(f"unreadable cost {text!r}")
    return round(float(match.group(1)) * _SUFFIX.get(match.group(2), 1))


def _split(text: str) -> tuple[str, str | None]:
    start = text.find("{|")
    if start < 0:
        return text, None
    end = text.find("|}", start)
    return text[:start], text[start:end if end >= 0 else len(text)]


def _parse_unlock(prose: str) -> list[dict[str, int]]:
    """Tier/wave conditions stated in the prose; raises when prose mentions one it can't read."""
    found = {(int(tier), int(wave)) for tier, wave in _TIER_WAVE.findall(prose)}
    if len(found) > 1:
        raise ValueError(f"ambiguous unlock: {sorted(found)}")
    if not found and _UNLOCK_MENTION.search(prose):
        raise ValueError("unlock mentioned but unreadable")
    return [{"tier": tier, "wave": wave} for tier, wave in found]


def _parse_levels(table: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in table.splitlines()]
    headers = [line[1:].strip().lower() for line in lines if line.startswith("!")]
    if not {"level", "cost", "time"} <= set(headers):
        raise ValueError(f"table needs Level, Cost and Time columns, has {headers}")
    rows: list[list[str]] = []
    for line in lines:
        if line.startswith("|-"):
            rows.append([])
        elif line.startswith("|") and rows:
            rows[-1].append(line[1:].strip())
    levels = []
    for row in rows:
        if len(row) != len(headers):
            raise ValueError(f"row {row} does not match headers {headers}")
        cell = dict(zip(headers, row))
        levels.append({"level": int(cell["level"]), "coins": parse_coins(cell["cost"]),
                       "seconds": parse_duration(cell["time"]), "value": cell.get("value") or None})
    return levels


def parse_page(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(tier/wave unlock conditions, level rows). Raises ValueError on anything unreadable."""
    prose, table = _split(text)
    return _parse_unlock(prose), [] if table is None else _parse_levels(table)


def _check_levels(prose: str, levels: list[dict[str, Any]]) -> None:
    """A table is only trusted whole: levels 1..N, matching the stated count, never cheaper or faster."""
    if not levels:
        raise ValueError("no table")
    if [row["level"] for row in levels] != list(range(1, len(levels) + 1)):
        raise ValueError("levels do not run 1 to N")
    stated = _STATED_LEVELS.search(prose)
    if stated is not None and int(stated.group(1)) != len(levels):
        raise ValueError(f"prose states {stated.group(1)} levels, table has {len(levels)}")
    if any(later["coins"] < earlier["coins"] or later["seconds"] < earlier["seconds"]
           for earlier, later in zip(levels, levels[1:])):
        raise ValueError("coins and seconds must never decrease")


def _tier_conditions(lab_id: str, prose: str, labs_wave: int) -> list[dict[str, int]]:
    """The lab's own tier/wave unlock. "Unlocked with labs at tier 1 wave 30" is the labs
    milestone itself (catalog `labs_unlock`), so a condition it implies is not repeated."""
    return [condition for condition in _stated_tier_conditions(lab_id, prose)
            if not (condition["tier"] == 1 and condition["wave"] <= labs_wave)]


def _stated_tier_conditions(lab_id: str, prose: str) -> list[dict[str, int]]:
    override = UNLOCK_OVERRIDES.get(lab_id)
    try:
        parsed = _parse_unlock(prose)
    except ValueError as exc:
        if override is None:
            raise ValueError(f"{lab_id}: {exc}; add it to UNLOCK_OVERRIDES with its wiki source") from exc
        return override
    if override is not None and parsed and parsed != override:
        raise ValueError(f"{lab_id}: wiki says {parsed}, UNLOCK_OVERRIDES says {override}")
    return parsed or (override or [])


def fetch(page: str) -> str:
    query = urllib.parse.urlencode({"action": "parse", "page": page, "prop": "wikitext", "format": "json"})
    request = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": "Mozilla/5.0 thetowerbot"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["parse"]["wikitext"]["*"]


def build(source: dict[str, Any], checked: str, fetch_page: Callable[[str], str] = fetch,
          pages: dict[str, tuple[str, str]] = PAGES) -> dict[str, Any]:
    payload = {**source, "version": 2, "scope": SCOPE}
    labs_wave = source["labs_unlock"]["best_tier_1_wave"]
    labs = {item["id"]: dict(item) for item in source["labs"]}
    for lab in labs.values():  # v1 -> v2 unlock shape; a re-run from v2 already has lists
        old = lab.get("unlock")
        if not isinstance(old, list):
            lab["unlock"] = [] if old is None else [{"tier": 1, "wave": old["best_tier_1_wave"]}]
    for lab_id, (name, page) in pages.items():
        prereqs = [LAB_PREREQS[lab_id]] if lab_id in LAB_PREREQS else []
        entry = {"id": lab_id, "name": name, "unlock": [], "max_level": None, "levels": None,
                 "source_url": WIKI + page, "checked": checked}
        try:
            text = fetch_page(page)
        except (OSError, ValueError, KeyError) as exc:
            if lab_id in labs:
                print(f"{lab_id}: fetch failed, kept the existing entry ({exc})", file=sys.stderr)
                continue
            if lab_id not in UNLOCK_OVERRIDES:
                raise RuntimeError(f"{lab_id}: fetch failed and its unlock is unknown ({exc})") from exc
            print(f"{lab_id}: fetch failed, kept unpriced ({exc})", file=sys.stderr)
            labs[lab_id] = {**entry, "unlock": UNLOCK_OVERRIDES[lab_id] + prereqs}
            continue
        prose, table = _split(text)
        entry["unlock"] = _tier_conditions(lab_id, prose, labs_wave) + prereqs
        try:
            levels = [] if table is None else _parse_levels(table)
            _check_levels(prose, levels)
        except ValueError as exc:
            print(f"{lab_id}: kept unpriced ({exc})", file=sys.stderr)
        else:
            entry["levels"], entry["max_level"] = levels, len(levels)
        labs[lab_id] = entry
    payload["labs"] = list(labs.values())
    return payload


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def render(payload: dict[str, Any]) -> str:
    """The catalog in the same one-entry-per-line layout as labs.v1.json."""
    blocks = []
    for key, value in payload.items():
        if key == "labs":
            entries = []
            for lab in value:
                head = ", ".join(f"{_dump(k)}: {_dump(v)}" for k, v in lab.items()
                                 if k not in ("levels", "source_url", "checked"))
                tail = f'"source_url": {_dump(lab["source_url"])}, "checked": {_dump(lab["checked"])}}}'
                if lab.get("levels") is None:
                    entries.append(f'    {{{head}, "levels": null,\n     {tail}')
                else:
                    rows = ",\n".join(f"       {_dump(row)}" for row in lab["levels"])
                    entries.append(f'    {{{head},\n     "levels": [\n{rows}\n     ],\n     {tail}')
            blocks.append(f'  "labs": [\n' + ",\n".join(entries) + "\n  ]")
        elif isinstance(value, list):
            blocks.append(f"  {_dump(key)}: [\n" + ",\n".join(f"    {_dump(item)}" for item in value) + "\n  ]")
        else:
            blocks.append(f"  {_dump(key)}: {_dump(value)}")
    return "{\n" + ",\n".join(blocks) + "\n}\n"


def main() -> None:
    import lab_catalog  # imported here: it loads the shipped catalog at import time

    source = V1 if V1.exists() else V2
    payload = build(json.loads(source.read_text(encoding="utf-8")), date.today().isoformat())
    lab_catalog.load(payload)  # never write a catalog that won't load
    V2.write_text(render(payload), encoding="utf-8")
    print(f"wrote {V2}")


if __name__ == "__main__":
    main()
