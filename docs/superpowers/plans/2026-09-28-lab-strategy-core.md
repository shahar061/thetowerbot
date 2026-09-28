# Lab Strategy Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A ranked lab list with slot pins and fillers, just-in-time coin saving between labs and the Workshop, a v2 lab catalog priced from the wiki, and a new early-game template. The worker, the Labs & Gems page and the Studio all read the same plan.

**Architecture:**
- **Rules.** A new `lab_list` block and its rules live beside the existing `slot_track` model.
- **Evaluation.** `evaluate_lab_plan` dispatches to a new pure evaluator, `fleet/lab_list.py`. That evaluator picks each owned slot's target or filler and attaches a pure `SavingPlan` from `fleet/lab_saving.py` to the `LabPlan`.
- **Worker.** In `just_in_time` mode, the worker feeds `SavingPlan.reserve` into the existing `lab_coin_jar` seam. Every Workshop ceiling already subtracts that seam.

**Tech Stack:**
- Python 3.12 (uv, pytest);
- FastAPI;
- Next.js 15 / React 19 with vitest;
- JSON catalogs under `catalog/`.

**Spec:** `docs/superpowers/specs/2026-09-28-lab-strategy-core-design.md`. Read it first. Every task argues from it.

## Global Constraints

- **Unknown is never zero.** An unknown wallet, income, level, unlock fact or price never becomes 0 or "met". It becomes a skip reason or a conservative reserve.
- **Leave existing strategies alone.** Existing `slot_track` strategies and the modes `when_affordable`, `save_pct` and `labs_first` keep their exact behaviour.
- **One way into the labs lane.** A labs lane holds either exactly one `lab_list` block or `slot_track` blocks.
- **Pairing.** `just_in_time` is valid only with a `lab_list` lane.
- **Tiers.** Tiers are `S+`, `S`, `A`, `B`, `C`.
- **Saving defaults:**
  - window hours `{"S+": 72, "S": 24, "A": 12, "B": 4, "C": 0}`, each 0–168;
  - `income_margin_pct` default 75, range 50–100.
- **Filler defaults:** `enabled` true; `max_price_pct_of_wallet` 10 (range 1–100); `min_hours` 1 (range 0.25–24).
- **The list starts with Game Speed pinned to slot 1.**
- **Automation is unchanged.** `research_automated`, the route gates and `AUTOMATED` stay as they are. Only slot-1 Game Speed is started by the worker.
- **Catalog.** Values come only from the wiki's `Lab/*` and `Perk Labs/*` pages. Never invent a price. A table that can't be read stays `levels: null`.
- **Tests.** Never run the whole suite. Run only the test files named in each task, always with `-p no:allure_pytest` and a long Bash timeout (for example 600000 ms). UI tests run as `cd web/ui && npx vitest run <file>`.
- **Git.** Commit only on the feature branch. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. `docs/` is gitignored, so use `git add -f` for files under it.

## Review Focus

1. **Fresh account with no lab levels read.** Every entry except the Game Speed pin is skipped as `level unread`. Slots 2–5 get no target and no filler, and the reserve stays 0, so the Workshop is not frozen. Task 5 tests this: `test_unread_levels_skip_without_freezing_workshop`.
2. **A researching slot whose end time is unknown** (`completes_at is None`). Its target shows in the plan, stays out of the reserve, and gets `covered = None`. Task 7 tests this: `test_unknown_completion_is_not_reserved`.
3. **A wallet smaller than the sum of the targets due now.** The reserve is clamped to the wallet, the Workshop budget is 0, and the reserve is never negative. Task 7 tests this: `test_reserve_capped_at_wallet`.
4. **Labs Speed 50 running in slot 2, with Labs Speed 99 later in the list.** Slot 3 never picks Labs Speed, because it is pinned to slot 2. Slot 2's next level after 50 is 51. Task 5 tests this: `test_pinned_lab_never_leaves_its_slot`.
5. **A saved strategy with a `slot_track` lane switched to `just_in_time`.** Loading it fails with a clear message. Old documents without `saving`/`filler` rules load with the defaults. Task 3 tests this: `test_just_in_time_requires_lab_list` and `test_old_rules_load_with_defaults`.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `lab_catalog.py` | modify | Load catalog v1 or v2: unlock condition lists, non-negative seconds, optional `value`, `tier_one_wave()` |
| `catalog/labs.v2.json` | create | Priced early-game labs imported from the wiki (replaces `labs.v1.json`) |
| `catalog/labs.v1.json` | delete | Superseded |
| `tools/import_wiki_lab_tables.py` | create | Fetch `Lab/*` wikitext through the MediaWiki API, parse tables and unlocks, write v2 |
| `fleet/build_route.py` | modify | `just_in_time` mode; `LabSavingRule`, `LabFillerRule`; check that `just_in_time` has a lab list |
| `fleet/lab_list.py` | create | `lab_list` validation and the pure evaluator (pins, ranked walk, fillers) |
| `fleet/lab_saving.py` | create | Pure `income_rate`, `SavingInput`, `SavingPlan`, `saving_plan()` |
| `fleet/lab_facts.py` | create | Persisted `LabFacts` for a worker (slots, levels, best waves, income), shared by the worker and the Labs & Gems page |
| `fleet/resource_blocks.py` | modify | Dispatch to `lab_list`; extract `_slot_context` and `_capabilities`; new optional fields on `LabFacts`/`SlotPlan`/`LabPlan`; templates |
| `fleet/strategy_library.py` | modify | `labs_gems` uses `template_lab_list()` and `template_lab_list_rules()` |
| `fleet/coin_share.py` | modify | `jit_hold(saving, wallet)` |
| `fleet/reroll_progress.py` | modify | Use the just-in-time reserve in `shopping_policy`; fill income and best waves in `lab_strategy_plan` |
| `fleet/labs_view.py` | modify | Build facts with `persisted_lab_facts` |
| `web/ui/lib/labs.ts` | modify | Types for `lab_list`, rules, `SavingPlan`, slot role; `splitPreview` for `just_in_time` |
| `web/ui/app/fleet/reroll/strategies/LabListView.tsx` | create | Read-only ranked list and per-slot picks |
| `web/ui/app/fleet/reroll/strategies/StrategyStudio.tsx` | modify | Render `LabListView` for a `lab_list` lane |
| `web/ui/app/fleet/reroll/strategies/StrategyRules.tsx` | modify | Just-in-time mode, save windows and filler fields |

---

### Task 1: Catalog v2 schema in `lab_catalog.py`

**Files:**
- Modify: `lab_catalog.py`, including the `CatalogLab.unlock` type, `LabLevel.max_speed`, `_lab` and `load`
- Modify: `fleet/resource_blocks.py`, in `_choose`: the `requirement = entry.unlock.get(...)` line
- Test: `tests/test_lab_catalog_v2.py` (create)

**Interfaces:**
- **Produces:**
  - `lab_catalog.load(payload)` accepts `version` 1 or 2.
  - `CatalogLab.unlock: tuple[dict[str, Any], ...]`. Each item is `{"tier": int, "wave": int}` or `{"lab": str, "level": int}`. An empty tuple means no requirement.
  - `LabLevel(level: int, coins: int, seconds: int, max_speed: float | None, value: str | None = None)`.
  - `lab_catalog.tier_one_wave(lab_id: str) -> int | None`.
- **v1 compatibility:** a v1 `{"best_tier_1_wave": W}` loads as `({"tier": 1, "wave": W},)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lab_catalog_v2.py
"""Catalog v2: unlock condition lists, zero-second levels and optional values."""

from __future__ import annotations

import copy
import json

import pytest

import lab_catalog


def _v1() -> dict:
    return json.loads(lab_catalog.CATALOG_PATH.read_text(encoding="utf-8"))


def _payload(**lab_overrides: object) -> dict:
    payload = _v1()
    payload["version"] = 2
    lab = {"id": "labs.labs-speed", "name": "Labs Speed", "max_level": 3,
           "unlock": [{"tier": 1, "wave": 150}],
           "levels": [{"level": 1, "coins": 40, "seconds": 0, "value": "1.02"},
                      {"level": 2, "coins": 83, "seconds": 540, "value": "1.04"},
                      {"level": 3, "coins": 211, "seconds": 1320, "value": "1.06"}],
           "source_url": "https://the-tower-idle-tower-defense.fandom.com/wiki/Lab/Lab_Speed",
           "checked": "2026-09-28"}
    lab.update(lab_overrides)
    payload["labs"] = [item for item in payload["labs"] if item["id"] != "labs.labs-speed"] + [lab]
    return payload


def test_v2_loads_unlock_lists_zero_second_levels_and_values() -> None:
    catalog = lab_catalog.load(_payload())
    labs_speed = next(item for item in catalog.labs if item.id == "labs.labs-speed")
    assert labs_speed.unlock == ({"tier": 1, "wave": 150},)
    assert labs_speed.levels[0].seconds == 0
    assert labs_speed.levels[0].value == "1.02" and labs_speed.levels[0].max_speed is None


def test_v2_accepts_lab_prerequisites_that_exist() -> None:
    catalog = lab_catalog.load(_payload(unlock=[{"tier": 4, "wave": 80},
                                                {"lab": "labs.game-speed", "level": 1}]))
    labs_speed = next(item for item in catalog.labs if item.id == "labs.labs-speed")
    assert labs_speed.unlock[1] == {"lab": "labs.game-speed", "level": 1}


@pytest.mark.parametrize("unlock", [
    [{"lab": "labs.not-in-catalog", "level": 1}],
    [{"tier": 0, "wave": 10}],
    [{"tier": 1}],
    {"tier": 1, "wave": 150},
])
def test_v2_rejects_bad_unlocks(unlock: object) -> None:
    with pytest.raises(ValueError, match="unlock"):
        lab_catalog.load(_payload(unlock=unlock))


def test_v2_rejects_decreasing_seconds_or_coins() -> None:
    levels = _payload()["labs"][-1]["levels"]
    slower = copy.deepcopy(levels)
    slower[2]["seconds"] = 10
    with pytest.raises(ValueError, match="rise"):
        lab_catalog.load(_payload(levels=slower))
    cheaper = copy.deepcopy(levels)
    cheaper[2]["coins"] = 1
    with pytest.raises(ValueError, match="rise"):
        lab_catalog.load(_payload(levels=cheaper))


def test_v1_unlock_reads_as_a_tier_one_condition() -> None:
    catalog = lab_catalog.load(_v1()) if _v1()["version"] == 1 else None
    if catalog is None:
        pytest.skip("shipped catalog is already v2")
    labs_speed = next(item for item in catalog.labs if item.id == "labs.labs-speed")
    assert labs_speed.unlock == ({"tier": 1, "wave": 150},)


def test_tier_one_wave_reads_the_tier_one_condition() -> None:
    assert lab_catalog.tier_one_wave("labs.labs-speed") == 150
    assert lab_catalog.tier_one_wave("labs.game-speed") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lab_catalog_v2.py -q -p no:allure_pytest`
Expected: FAIL. The loader rejects version 2 with "unsupported lab catalog version", and `tier_one_wave` is missing.

- [ ] **Step 3: Implement**

In `lab_catalog.py`:

```python
@dataclass(frozen=True)
class LabLevel:
    level: int
    coins: int
    seconds: int
    max_speed: float | None
    value: str | None = None


@dataclass(frozen=True)
class CatalogLab:
    id: str
    name: str
    unlock: tuple[dict[str, Any], ...]
    max_level: int | None
    levels: tuple[LabLevel, ...] | None
    source_url: str
    checked: str


def _unlock(raw: object, lab_id: str, version: int) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if version == 1:
        if not isinstance(raw, dict) or set(raw) != {"best_tier_1_wave"}:
            raise ValueError(f"{lab_id}: unlock must be {{best_tier_1_wave: int}} or null")
        return ({"tier": 1, "wave": _positive(raw["best_tier_1_wave"], f"{lab_id} unlock wave")},)
    if not isinstance(raw, list):
        raise ValueError(f"{lab_id}: unlock must be a list of conditions")
    conditions: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict) and set(item) == {"tier", "wave"}:
            conditions.append({"tier": _positive(item["tier"], f"{lab_id} unlock tier"),
                               "wave": _positive(item["wave"], f"{lab_id} unlock wave")})
        elif isinstance(item, dict) and set(item) == {"lab", "level"} and isinstance(item["lab"], str):
            conditions.append({"lab": item["lab"],
                               "level": _positive(item["level"], f"{lab_id} unlock level")})
        else:
            raise ValueError(f"{lab_id}: unlock condition must be {{tier, wave}} or {{lab, level}}")
    return tuple(conditions)
```

Change `_lab(raw)` to `_lab(raw, version)`:
- Replace its unlock block with `unlock = _unlock(raw.get("unlock"), lab_id, version)`.
- Validate levels this way:

```python
    for number, entry in enumerate(levels_raw, start=1):
        if not isinstance(entry, dict) or entry.get("level") != number:
            raise ValueError(f"{lab_id}: levels must run 1 to max_level in order")
        speed = entry.get("max_speed")
        if lab_id == GAME_SPEED or version == 1:
            if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed <= 0:
                raise ValueError(f"{lab_id}: max_speed must be a positive number")
            speed = float(speed)
        elif speed is not None:
            raise ValueError(f"{lab_id}: only Game Speed has max_speed")
        value = entry.get("value")
        if value is not None and (not isinstance(value, str) or len(value) > 40):
            raise ValueError(f"{lab_id}: value must be a short string")
        seconds = entry.get("seconds")
        if version == 1:
            seconds = _positive(seconds, f"{lab_id} seconds")
        elif type(seconds) is not int or seconds < 0:
            raise ValueError(f"{lab_id} seconds must be a non-negative integer")
        levels.append(LabLevel(number, _positive(entry.get("coins"), f"{lab_id} coins"),
                               seconds, speed, value))
    if any(later.coins < earlier.coins or later.seconds < earlier.seconds
           for earlier, later in zip(levels, levels[1:])):
        raise ValueError(f"{lab_id}: level prices and times must rise")
    if lab_id == GAME_SPEED and any(later.coins <= earlier.coins or later.max_speed <= earlier.max_speed
                                    for earlier, later in zip(levels, levels[1:])):
        raise ValueError(f"{lab_id}: level prices and speeds must rise")
```

In `load`:
- Accept `payload.get("version") in (1, 2)`.
- Pass `version` into `_lab`.
- After the duplicate-id check, verify that every `{"lab": ...}` condition names an id in `ids`. Otherwise raise `ValueError(f"{entry.id}: unlock names unknown lab {name}")`.

Also add:

```python
def tier_one_wave(lab_id: str) -> int | None:
    entry = _BY_ID.get(lab_id)
    return next((item["wave"] for item in (entry.unlock if entry else ())
                 if item.get("tier") == 1), None)
```

In `fleet/resource_blocks.py` `_choose`, replace
`requirement = entry.unlock.get("best_tier_1_wave") if entry.unlock else None`
with `requirement = lab_catalog.tier_one_wave(lab_id)`.

Update the module docstring: "from catalog/labs.v2.json … every lab without a table has `levels: null`".

- [ ] **Step 4: Run the new tests and the neighbouring tests**

Run: `uv run pytest tests/test_lab_catalog_v2.py tests/test_lab_catalog.py tests/test_resource_blocks.py -q -p no:allure_pytest`
Expected: PASS. If `tests/test_lab_catalog.py` asserts the old `unlock` dict shape, update that assertion to the tuple shape. It is the same fact in its new shape.

- [ ] **Step 5: Commit**

```bash
git add lab_catalog.py fleet/resource_blocks.py tests/test_lab_catalog_v2.py tests/test_lab_catalog.py
git commit -m "Load lab catalog v2 with unlock condition lists

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Wiki importer and the shipped `catalog/labs.v2.json`

**Files:**
- Create: `tools/import_wiki_lab_tables.py`
- Create: `catalog/labs.v2.json`, the output of the tool
- Delete: `catalog/labs.v1.json`
- Modify: `lab_catalog.py`: `CATALOG_PATH` becomes `labs.v2.json`
- Modify: `tests/test_runtime_identity.py:19-21`: the parametrize entry `"catalog/labs.v1.json"` becomes `"catalog/labs.v2.json"`
- Test: `tests/test_import_wiki_lab_tables.py` (create), plus shipped-catalog assertions in `tests/test_lab_catalog_v2.py`

**Interfaces:**
- **Consumes:** the v2 loader from Task 1.
- **Produces:**
  - `tools.import_wiki_lab_tables.parse_page(text: str) -> tuple[list[dict], list[dict]]`, returning `(unlock_conditions, levels)`.
  - `parse_duration(text: str) -> int`
  - `parse_coins(text: str) -> int`
  - A shipped catalog containing every lab id the Task 8 template uses, priced whenever its page parses.

Wiki facts the parser must handle, verified on 2026-09-28 with `action=parse&prop=wikitext`:
- **Header order varies.** `Lab/Lab_Speed` lists `!Level !Time !Cost !Value`. `Lab/Game_Speed` lists `!Level !Cost !Time !Value`. Match columns by header name.
- **Time formats:**
  - `0d  0h  0m` (level 1 rounds to 0 seconds);
  - `3d 11h 19m`;
  - `9m`;
  - `2h 30m`;
  - `1d 10h 2m`.
- **Costs** look like `1,500,000` with padding spaces. Also accept the suffixes `K`, `M`, `B`, `T`.
- **Unlocks** come from prose before the table, for example "unlocked in Milestones at tier 4 wave 80". Lab prerequisites come from a fixed map in the tool, because the prose varies.
- **Transcluded tables.** Pages such as `Labs_Speed` only transclude `{{:Lab/Lab Speed}}`, so fetch the `Lab/...` page directly.

- [ ] **Step 1: Write the failing parser tests**

```python
# tests/test_import_wiki_lab_tables.py
from __future__ import annotations

import pytest

from tools.import_wiki_lab_tables import parse_coins, parse_duration, parse_page

LAB_SPEED = """Unlock [[Lab Upgrades|lab]] in [[Milestones]] at tier 1 wave 150. Has 99 levels.
{| class="mw-collapsible mw-collapsed fandom-table"
!Level
!Time
!Cost
!Value
|-
|1
|0d  0h  0m
|                                    40
|     1.02
|-
|2
|0d  0h  9m
|                                    83
|     1.04
|}"""

GAME_SPEED = """Unlocks with labs at tier 1 wave 30.
{| class="fandom-table"
!Level
!Cost
!Time
!Value
|-
|1
|300
|9m
|x2.0
|-
|4
|50,000
|1d 10h 2m
|x3.5
|}"""


@pytest.mark.parametrize(("text", "seconds"), [
    ("0d  0h  0m", 0), ("9m", 540), ("2h 30m", 9000), ("1d 10h 2m", 122520), ("3d 11h 19m", 299940),
])
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize(("text", "coins"), [
    ("                 1,500,000", 1_500_000), ("40", 40), ("2.5M", 2_500_000), ("1.2B", 1_200_000_000),
])
def test_parse_coins(text: str, coins: int) -> None:
    assert parse_coins(text) == coins


def test_parse_page_reads_rows_and_tier_unlock() -> None:
    unlock, levels = parse_page(LAB_SPEED)
    assert unlock == [{"tier": 1, "wave": 150}]
    assert levels == [{"level": 1, "coins": 40, "seconds": 0, "value": "1.02"},
                      {"level": 2, "coins": 83, "seconds": 540, "value": "1.04"}]


def test_parse_page_matches_columns_by_header() -> None:
    _, levels = parse_page(GAME_SPEED)
    assert levels[1] == {"level": 4, "coins": 50_000, "seconds": 122_520, "value": "x3.5"}


def test_parse_page_without_table_is_empty() -> None:
    assert parse_page("no table here") == ([], [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_import_wiki_lab_tables.py -q -p no:allure_pytest`
Expected: FAIL with `ModuleNotFoundError: tools.import_wiki_lab_tables`. If `tools/` has no `__init__.py`, check how the existing `tools.freshness` is imported and follow that pattern.

- [ ] **Step 3: Implement the tool**

```python
# tools/import_wiki_lab_tables.py
"""Import lab price/time tables from the Tower Fandom wiki into catalog/labs.v2.json.

Run: uv run python -m tools.import_wiki_lab_tables
The wiki blocks plain page fetches (402/403); the MediaWiki API returns wikitext.
A page that fails to fetch or parse keeps `levels: null` - never an invented price.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
V1 = ROOT / "catalog" / "labs.v1.json"
V2 = ROOT / "catalog" / "labs.v2.json"
API = "https://the-tower-idle-tower-defense.fandom.com/api.php"
WIKI = "https://the-tower-idle-tower-defense.fandom.com/wiki/"

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
_SUFFIX = {"K": 10**3, "M": 10**6, "B": 10**9, "T": 10**12}
_TIER_WAVE = re.compile(r"tier\s+(\d+)\s+wave\s+(\d+)", re.IGNORECASE)


def parse_duration(text: str) -> int:
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    parts = re.findall(r"(\d+)\s*([dhms])", text)
    if not parts:
        raise ValueError(f"unreadable duration {text!r}")
    return sum(int(amount) * units[unit] for amount, unit in parts)


def parse_coins(text: str) -> int:
    cleaned = text.strip().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMBT]?)", cleaned)
    if match is None:
        raise ValueError(f"unreadable cost {text!r}")
    return round(float(match.group(1)) * _SUFFIX.get(match.group(2), 1))


def parse_page(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    start = text.find("{|")
    if start < 0:
        return [], []
    prose, table = text[:start], text[start:text.find("|}", start)]
    unlock = [{"tier": int(t), "wave": int(w)} for t, w in _TIER_WAVE.findall(prose)[:1]]
    lines = [line.strip() for line in table.splitlines()]
    headers = [line[1:].strip().lower() for line in lines if line.startswith("!")]
    rows: list[list[str]] = []
    for line in lines:
        if line.startswith("|-"):
            rows.append([])
        elif line.startswith("|") and not line.startswith("|}") and rows:
            rows[-1].append(line[1:].strip())
    levels = []
    for row in rows:
        if len(row) != len(headers):
            continue
        cell = dict(zip(headers, row))
        levels.append({"level": int(cell["level"]), "coins": parse_coins(cell["cost"]),
                       "seconds": parse_duration(cell["time"]), "value": cell.get("value") or None})
    return unlock, levels


def fetch(page: str) -> str:
    query = urllib.parse.urlencode({"action": "parse", "page": page, "prop": "wikitext", "format": "json"})
    request = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": "Mozilla/5.0 thetowerbot"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)["parse"]["wikitext"]["*"]


def build(checked: str) -> dict[str, Any]:
    payload = json.loads(V1.read_text(encoding="utf-8"))
    payload["version"] = 2
    labs = {item["id"]: item for item in payload["labs"]}
    for lab in labs.values():  # v1 -> v2 unlock shape for untouched labs (Game Speed keeps its table)
        old = lab.get("unlock")
        lab["unlock"] = [] if old is None else [{"tier": 1, "wave": old["best_tier_1_wave"]}]
    for lab_id, (name, page) in PAGES.items():
        entry = {"id": lab_id, "name": name, "unlock": [], "max_level": None, "levels": None,
                 "source_url": WIKI + page, "checked": checked}
        try:
            unlock, levels = parse_page(fetch(page))
        except (OSError, ValueError, KeyError) as exc:
            print(f"{lab_id}: kept unpriced ({exc})", file=sys.stderr)
            unlock, levels = [], []
        entry["unlock"] = unlock + ([LAB_PREREQS[lab_id]] if lab_id in LAB_PREREQS else [])
        if levels and [row["level"] for row in levels] == list(range(1, len(levels) + 1)):
            entry["levels"], entry["max_level"] = levels, len(levels)
        elif levels:
            print(f"{lab_id}: kept unpriced (levels not 1..N)", file=sys.stderr)
        labs[lab_id] = entry
    payload["labs"] = list(labs.values())
    return payload


def main() -> None:
    V2.write_text(json.dumps(build(date.today().isoformat()), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {V2}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the parser tests**

Run: `uv run pytest tests/test_import_wiki_lab_tables.py -q -p no:allure_pytest`
Expected: PASS.

- [ ] **Step 5: Generate the catalog and switch to it**

Run: `uv run python -m tools.import_wiki_lab_tables`
Expected: `wrote .../catalog/labs.v2.json`. Any `kept unpriced` lines go to stderr. Record them for the template in Task 8.

Then:
- Set `CATALOG_PATH = Path(__file__).resolve().parent / "catalog" / "labs.v2.json"` in `lab_catalog.py`.
- `git rm catalog/labs.v1.json`.
- In `tools/import_wiki_lab_tables.py`, change `V1` handling so a re-run reads the existing `V2` whenever `V1` is missing: `source = V1 if V1.exists() else V2`. In that case skip the v1→v2 unlock conversion for labs whose `unlock` is already a list.
- Update `tests/test_runtime_identity.py` to `"catalog/labs.v2.json"`.

Changing the catalog file changes `lab_runtime._catalog_revision()`. As a result, persisted lab-level facts and slot records are re-verified on the next Labs visit. That is intended: prices changed. Don't work around it.

Add these shipped-catalog assertions to `tests/test_lab_catalog_v2.py`:

```python
TEMPLATE_LABS = ("labs.game-speed", "labs.unlock-perks", "labs.first-perk-choice", "labs.perk-option-quantity",
                 "labs.ban-perks", "labs.light-speed-shots", "labs.coins-wave", "labs.labs-speed",
                 "labs.coins-kill-bonus", "labs.cash-bonus", "labs.attack-speed", "labs.health", "labs.damage",
                 "labs.standard-perks-bonus", "labs.improve-trade-off-perks", "labs.workshop-attack-discount",
                 "labs.workshop-defense-discount", "labs.workshop-utility-discount")


def test_shipped_catalog_is_v2_and_knows_every_template_lab() -> None:
    assert json.loads(lab_catalog.CATALOG_PATH.read_text(encoding="utf-8"))["version"] == 2
    for lab_id in TEMPLATE_LABS:
        assert lab_catalog.lab(lab_id) is not None, lab_id


def test_shipped_labs_speed_matches_the_wiki() -> None:
    labs_speed = lab_catalog.lab("labs.labs-speed")
    assert labs_speed.max_level == 99 and labs_speed.unlock == ({"tier": 1, "wave": 150},)
    assert lab_catalog.level("labs.labs-speed", 99).coins == 19_360_000
```

After the switch, `test_v1_unlock_reads_as_a_tier_one_condition` skips by design. Replace it with a v1 payload built inline: take `_payload()`, set `version` to 1, and give the `labs.labs-speed` entry `{"best_tier_1_wave": 150}`, `levels: None` and `max_level: None`. The v1 path must stay tested.

- [ ] **Step 6: Run the catalog tests and the neighbouring tests**

Run: `uv run pytest tests/test_lab_catalog_v2.py tests/test_lab_catalog.py tests/test_import_wiki_lab_tables.py tests/test_runtime_identity.py tests/test_resource_blocks.py -q -p no:allure_pytest`
Expected: PASS. If `tests/test_lab_catalog.py` asserts v1-only facts (for example "only Game Speed is priced"), update those assertions to the v2 truth and mention it in the commit body.

- [ ] **Step 7: Commit**

```bash
git add tools/import_wiki_lab_tables.py catalog/labs.v2.json lab_catalog.py tests/test_import_wiki_lab_tables.py tests/test_lab_catalog_v2.py tests/test_lab_catalog.py tests/test_runtime_identity.py
git rm --cached -q catalog/labs.v1.json 2>/dev/null || true
git commit -m "Price early-game labs from the wiki in catalog v2

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Saving and filler rules in `fleet/build_route.py`

**Files:**
- Modify: `fleet/build_route.py`:
  - `LAB_SHARE_MODES` (around `:283`);
  - `LabRules` (around `:340`);
  - new `LabSavingRule` and `LabFillerRule`;
  - `RouteBaseline.from_dict` (around `:420`).
- Test: `tests/test_lab_rules.py` (create)

**Interfaces:**
- **Produces:**
  - `LAB_SHARE_MODES = ("when_affordable", "save_pct", "labs_first", "just_in_time")`
  - `TIERS = ("S+", "S", "A", "B", "C")` and `DEFAULT_WINDOW_HOURS = {"S+": 72, "S": 24, "A": 12, "B": 4, "C": 0}`
  - `LabSavingRule(income_margin_pct: int = 75, window_hours: dict[str, int] = DEFAULT_WINDOW_HOURS copy)`
  - `LabFillerRule(enabled: bool = True, max_price_pct_of_wallet: int = 10, min_hours: float = 1.0)`
  - `LabRules.saving: LabSavingRule` and `LabRules.filler: LabFillerRule`
  - `is_lab_list(labs: LabRoute) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lab_rules.py
from __future__ import annotations

import pytest

from fleet.build_route import DEFAULT_WINDOW_HOURS, RouteBaseline, RouteDocument, RouteRules

LIST = [{"id": "labs.list", "type": "lab_list", "entries": [
    {"id": "gs", "lab_id": "labs.game-speed", "to_level": 7, "tier": "S+", "pin_slot": 1}]}]


def _baseline(rules: dict, labs: dict | None = None) -> dict:
    raw = RouteDocument.compatibility().to_dict()["baseline"]
    raw["rules"] = rules
    if labs is not None:
        raw["labs"] = labs
    return raw


def test_old_rules_load_with_defaults() -> None:
    rules = RouteRules.from_dict({"coins": {"lab_share": {"mode": "save_pct", "pct": 25}}})
    assert rules.labs.saving.income_margin_pct == 75
    assert rules.labs.saving.window_hours == DEFAULT_WINDOW_HOURS
    assert (rules.labs.filler.enabled, rules.labs.filler.max_price_pct_of_wallet,
            rules.labs.filler.min_hours) == (True, 10, 1.0)


def test_saving_and_filler_round_trip() -> None:
    raw = {"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}},
           "labs": {"saving": {"income_margin_pct": 80, "window_hours": {"S+": 96, "S": 24, "A": 12, "B": 4, "C": 0}},
                    "filler": {"enabled": False, "max_price_pct_of_wallet": 5, "min_hours": 0.5}}}
    rules = RouteRules.from_dict(raw)
    assert RouteRules.from_dict(rules.to_dict()) == rules
    assert rules.labs.saving.window_hours["S+"] == 96 and rules.labs.filler.min_hours == 0.5


@pytest.mark.parametrize("labs", [
    {"saving": {"income_margin_pct": 40}},
    {"saving": {"window_hours": {"S+": 72}}},
    {"saving": {"window_hours": {"S+": 200, "S": 24, "A": 12, "B": 4, "C": 0}}},
    {"filler": {"max_price_pct_of_wallet": 0}},
    {"filler": {"min_hours": 0.1}},
    {"filler": {"enabled": "yes"}},
])
def test_rejects_out_of_range_saving_and_filler(labs: dict) -> None:
    with pytest.raises(ValueError):
        RouteRules.from_dict({"labs": labs})


def test_just_in_time_requires_lab_list() -> None:
    with pytest.raises(ValueError, match="ranked lab list"):
        RouteBaseline.from_dict(_baseline({"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}}}))


def test_just_in_time_with_lab_list_loads() -> None:
    labs = {"slot1_research": "game_speed", "steps": ["research_game_speed"], "mode": "blocks", "blocks": LIST}
    baseline = RouteBaseline.from_dict(_baseline({"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}}}, labs))
    assert baseline.rules.coins.lab_share.mode == "just_in_time"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lab_rules.py -q -p no:allure_pytest`
Expected: FAIL. There is no `DEFAULT_WINDOW_HOURS`, and `just_in_time` is an unknown mode. `test_just_in_time_with_lab_list_loads` also needs Task 4's validator. It fails with "the labs lane holds slot tracks" until Task 4 lands. Leave it failing here and mark it `@pytest.mark.xfail(reason="lab_list lands in Task 4", strict=True)`. Task 4 removes the mark.

- [ ] **Step 3: Implement**

```python
LAB_SHARE_MODES = ("when_affordable", "save_pct", "labs_first", "just_in_time")
TIERS = ("S+", "S", "A", "B", "C")
DEFAULT_WINDOW_HOURS: dict[str, int] = {"S+": 72, "S": 24, "A": 12, "B": 4, "C": 0}


def _hours(value: object, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"{name} must be a number from {low} to {high}")
    return float(value)


@dataclass(frozen=True)
class LabSavingRule:
    income_margin_pct: int = 75
    window_hours: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_WINDOW_HOURS))

    @classmethod
    def from_dict(cls, value: object) -> LabSavingRule:
        raw = _mapping(value, "saving rules")
        _keys(raw, {"income_margin_pct", "window_hours"})
        windows = raw.get("window_hours", DEFAULT_WINDOW_HOURS)
        if not isinstance(windows, Mapping) or set(windows) != set(TIERS):
            raise ValueError("window_hours must give hours for S+, S, A, B and C")
        return cls(_ranged(raw.get("income_margin_pct", 75), "income_margin_pct", 50, 100),
                   {tier: _ranged(windows[tier], f"window_hours {tier}", 0, 168) for tier in TIERS})


@dataclass(frozen=True)
class LabFillerRule:
    enabled: bool = True
    max_price_pct_of_wallet: int = 10
    min_hours: float = 1.0

    @classmethod
    def from_dict(cls, value: object) -> LabFillerRule:
        raw = _mapping(value, "filler rules")
        _keys(raw, {"enabled", "max_price_pct_of_wallet", "min_hours"})
        return cls(_flag(raw.get("enabled", True), "filler enabled"),
                   _ranged(raw.get("max_price_pct_of_wallet", 10), "filler max_price_pct_of_wallet", 1, 100),
                   _hours(raw.get("min_hours", 1.0), "filler min_hours", 0.25, 24))
```

Import `Mapping` from `typing` if it isn't imported already.

Extend `LabRules`:
- add the fields `saving: LabSavingRule = field(default_factory=LabSavingRule)` and `filler: LabFillerRule = field(default_factory=LabFillerRule)`;
- change `_keys(raw, {"auto_start", "pool", "idle_fill", "saving", "filler"})`;
- pass `LabSavingRule.from_dict(raw.get("saving", {}))` and `LabFillerRule.from_dict(raw.get("filler", {}))` to `cls(...)`.

Add the lane test next to `LabRoute`:

```python
def is_lab_list(labs: LabRoute) -> bool:
    return labs.mode == "blocks" and len(labs.blocks) == 1 and labs.blocks[0].get("type") == "lab_list"
```

In `RouteBaseline.from_dict`, right after `rules = _migrated_rules(...)`:

```python
        if rules.coins.lab_share.mode == "just_in_time" and not is_lab_list(labs):
            raise ValueError("just_in_time saving needs a ranked lab list in the labs lane")
```

- [ ] **Step 4: Run the new tests and the neighbouring tests**

Run: `uv run pytest tests/test_lab_rules.py tests/test_build_route.py tests/test_build_route_resources.py -q -p no:allure_pytest`
Expected: PASS, with one xfail. If a test compares the full `RouteRules().to_dict()` to a literal, add the two new default sub-dicts to that literal.

- [ ] **Step 5: Commit**

```bash
git add fleet/build_route.py tests/test_lab_rules.py tests/test_build_route.py
git commit -m "Add just-in-time lab saving and filler rules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `lab_list` block validation

**Files:**
- Create: `fleet/lab_list.py`, validation part
- Modify: `fleet/resource_blocks.py`: `validate_labs` (around `:150`) dispatches to it
- Modify: `tests/test_lab_rules.py`: remove the Task 3 xfail mark
- Test: `tests/test_lab_list.py` (create)

**Interfaces:**
- **Produces:**
  - `fleet.lab_list.validate_lab_list(block: Mapping[str, Any]) -> dict[str, Any]`
  - `validate_labs([lab_list_block]) -> (validated_block,)`
- **Entry shape:** `{"id", "lab_id", "to_level", "tier", "pin_slot"?, "label"?}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lab_list.py
from __future__ import annotations

from copy import deepcopy

import pytest

from fleet import resource_blocks as rb

BASE = {"id": "labs.list", "type": "lab_list", "label": "Early game", "entries": [
    {"id": "gs", "lab_id": "labs.game-speed", "to_level": 7, "tier": "S+", "pin_slot": 1},
    {"id": "ls50", "lab_id": "labs.labs-speed", "to_level": 50, "tier": "S", "pin_slot": 2},
    {"id": "ckb", "lab_id": "labs.coins-kill-bonus", "to_level": 30, "tier": "A"},
    {"id": "ls99", "lab_id": "labs.labs-speed", "to_level": 99, "tier": "A", "pin_slot": 2},
]}


def block(**changes: object) -> dict:
    result = deepcopy(BASE)
    result.update(changes)
    return result


def test_valid_list_round_trips() -> None:
    (validated,) = rb.validate_labs([block()])
    assert validated["type"] == "lab_list" and [entry["id"] for entry in validated["entries"]] == [
        "gs", "ls50", "ckb", "ls99"]


def test_list_cannot_mix_with_slot_tracks() -> None:
    with pytest.raises(ValueError, match="only block"):
        rb.validate_labs([block(), *deepcopy(list(rb.template_lab_blocks()))])


def test_first_entry_must_be_game_speed_on_slot_one() -> None:
    entries = deepcopy(BASE["entries"])
    entries[0]["pin_slot"] = 2
    with pytest.raises(ValueError, match="Game Speed pinned to slot 1"):
        rb.validate_labs([block(entries=entries)])
    with pytest.raises(ValueError, match="Game Speed pinned to slot 1"):
        rb.validate_labs([block(entries=entries[1:])])


def test_repeated_lab_needs_higher_level() -> None:
    entries = deepcopy(BASE["entries"])
    entries[3]["to_level"] = 40
    with pytest.raises(ValueError, match="higher to_level"):
        rb.validate_labs([block(entries=entries)])


@pytest.mark.parametrize(("key", "value", "match"), [
    ("tier", "D", "tier"), ("lab_id", "labs.nope", "unknown lab id"), ("pin_slot", 6, "pin_slot"),
    ("to_level", 0, "to_level"), ("extra", 1, "unknown lab list entry field"),
])
def test_rejects_bad_entries(key: str, value: object, match: str) -> None:
    entries = deepcopy(BASE["entries"])
    entries[2][key] = value
    with pytest.raises(ValueError, match=match):
        rb.validate_labs([block(entries=entries)])


def test_entry_ids_are_unique() -> None:
    entries = deepcopy(BASE["entries"])
    entries[2]["id"] = "gs"
    with pytest.raises(ValueError, match="unique"):
        rb.validate_labs([block(entries=entries)])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lab_list.py -q -p no:allure_pytest`
Expected: FAIL with "the labs lane holds slot tracks".

- [ ] **Step 3: Implement**

```python
# fleet/lab_list.py
"""The ranked lab list: one ordered list of labs every owned slot draws from.

Validation here; the pure per-slot evaluation (pins, ranked walk, fillers) is
added below it. Kept apart from resource_blocks.py, which keeps slot tracks.
"""

from __future__ import annotations

from typing import Any, Mapping

import lab_catalog
from fleet.build_route import TIERS
from fleet.strategy_blocks import MAX_BLOCKS

_ENTRY_FIELDS = {"id", "lab_id", "to_level", "tier", "pin_slot", "label"}


def validate_lab_list(raw: Mapping[str, Any]) -> dict[str, Any]:
    from fleet.resource_blocks import LAB_SLOTS, _fields, _Ids, _int, _lab_id, _max_level
    block = dict(raw)
    ids = _Ids()
    ids.claim(block)
    _fields(block, {"entries"})
    items = block.get("entries")
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= MAX_BLOCKS:
        raise ValueError(f"a lab list needs 1 to {MAX_BLOCKS} entries")
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
```

At the top of `validate_labs` in `fleet/resource_blocks.py`, before the `slot_track` loop:

```python
    if isinstance(value, (list, tuple)) and any(
            isinstance(raw, Mapping) and raw.get("type") == "lab_list" for raw in value):
        if len(value) != 1:
            raise ValueError("a lab list must be the labs lane's only block")
        from fleet.lab_list import validate_lab_list
        return (validate_lab_list(value[0]),)
```

`fleet/lab_list.py` imports `fleet.build_route`, and `build_route` imports `resource_blocks` lazily. Keep every `resource_blocks` import inside `lab_list.py` functions, so no import cycle forms at module load. Remove the xfail mark from `tests/test_lab_rules.py::test_just_in_time_with_lab_list_loads`.

- [ ] **Step 4: Run the new tests and the neighbouring tests**

Run: `uv run pytest tests/test_lab_list.py tests/test_lab_rules.py tests/test_resource_blocks.py -q -p no:allure_pytest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add fleet/lab_list.py fleet/resource_blocks.py tests/test_lab_list.py tests/test_lab_rules.py
git commit -m "Validate the ranked lab list block

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Ranked-list evaluation: pins, walk, skips, save window

**Files:**
- Modify: `fleet/resource_blocks.py`:
  - add `LabFacts.best_waves`, `LabFacts.coins_per_hour`, `SlotPlan.role`, `SlotPlan.saving_for` and `LabPlan.saving`;
  - extract `_slot_context` and `_capabilities` from `evaluate_lab_plan`;
  - dispatch to `lab_list`.
- Modify: `fleet/lab_list.py`: add `evaluate_lab_list`
- Create: `fleet/lab_saving.py` with `income_rate` only. Task 7 adds the rest.
- Test: `tests/test_lab_list_eval.py` (create)

**Interfaces:**
- **Consumes:**
  - `validate_lab_list` (Task 4);
  - `RouteRules.labs.saving` and `RouteRules.labs.filler` (Task 3);
  - `lab_catalog` v2 (Task 1).
- **Produces:**
  - `LabFacts(..., best_waves: Mapping[int, int] | None = None, coins_per_hour: float | None = None)`
  - `SlotPlan(..., role: str = "target", saving_for: SlotNext | None = None)`
  - `LabPlan(..., saving: Any = None)`
  - `resource_blocks._slot_context(facts) -> SlotContext(known: dict[str, int], running: dict[str, int], unavailable: set[str], nows: dict[int, SlotNow])`
  - `resource_blocks._capabilities(slot: int, automated: bool, now: SlotNow, facts: LabFacts, planned: bool) -> dict[str, bool]`
  - `fleet.lab_saving.income_rate(facts, rules) -> float | None`, the coins per hour after the margin
  - `fleet.lab_list.evaluate_lab_list(route, facts, *, ctx: SlotContext, gems: GemPlan) -> LabPlan`. In this task `saving=None` and there are no fillers.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lab_list_eval.py
from __future__ import annotations

from typing import Any

from fleet.build_route import RouteBaseline, RouteDocument, RouteRules
from fleet.resource_blocks import LabFacts, evaluate_lab_plan

NOW = 1_000_000.0
ENTRIES = [
    {"id": "gs", "lab_id": "labs.game-speed", "to_level": 7, "tier": "S+", "pin_slot": 1},
    {"id": "ls50", "lab_id": "labs.labs-speed", "to_level": 50, "tier": "S", "pin_slot": 2},
    {"id": "cw", "lab_id": "labs.coins-wave", "to_level": 10, "tier": "B"},
    {"id": "ckb", "lab_id": "labs.coins-kill-bonus", "to_level": 30, "tier": "A"},
    {"id": "ls99", "lab_id": "labs.labs-speed", "to_level": 99, "tier": "A", "pin_slot": 2},
]


class Route:
    """The evaluator reads .labs, .gems, .rules and .revision - like resolve_route's result."""

    def __init__(self, entries: list[dict[str, Any]] = ENTRIES, rules: dict | None = None) -> None:
        raw = RouteDocument.compatibility().to_dict()["baseline"]
        raw["labs"] = {"slot1_research": "game_speed", "steps": ["research_game_speed"], "mode": "blocks",
                       "blocks": [{"id": "labs.list", "type": "lab_list", "entries": entries}]}
        raw["rules"] = rules or {"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}},
                                 "labs": {"filler": {"enabled": False}}}
        baseline = RouteBaseline.from_dict(raw)
        self.labs, self.gems, self.rules, self.revision = baseline.labs, baseline.gems, baseline.rules, 3


def slot(state: str, research: str | None = None, level: int | None = None,
         finish: float | None = None) -> dict[str, Any]:
    return {"state": state, "research_id": research, "target_level": level, "expected_finish": finish,
            "observed_at": NOW - 5, "confirmed": True}


def facts(**changes: Any) -> LabFacts:
    base = dict(now=NOW, wallet_coins=100_000, available_coins=100_000, best_tier_1_wave=200,
                best_waves={1: 200}, coins_per_hour=10_000.0,
                slots={1: slot("idle"), 2: slot("idle"), 3: slot("locked"), 4: slot("locked"), 5: slot("locked")},
                completed_levels={"labs.game-speed": 3, "labs.labs-speed": 10, "labs.coins-wave": 2,
                                  "labs.coins-kill-bonus": 5},
                account_id="acct")
    base.update(changes)
    return LabFacts(**base)


def plan(route: Route | None = None, **changes: Any):
    return evaluate_lab_plan(route or Route(), facts(**changes))


def test_pins_hold_slots_one_and_two() -> None:
    result = plan()
    assert result.slots[0].next.lab_id == "labs.game-speed" and result.slots[0].next.level == 4
    assert result.slots[1].next.lab_id == "labs.labs-speed" and result.slots[1].next.level == 11
    assert result.slots[2].next is None and "not owned" in result.slots[2].why[-1]


def test_pin_releases_once_game_speed_is_maxed() -> None:
    result = plan(completed_levels={"labs.game-speed": 7, "labs.labs-speed": 10, "labs.coins-wave": 2,
                                    "labs.coins-kill-bonus": 5})
    assert result.slots[0].next.lab_id == "labs.coins-wave"


def test_locked_pin_falls_through_to_the_list() -> None:
    result = plan(best_tier_1_wave=100, best_waves={1: 100})
    assert result.slots[1].next.lab_id == "labs.coins-wave"
    assert any("locked" in line for line in result.slots[1].why)


def test_pinned_lab_never_leaves_its_slot() -> None:
    owned = {1: slot("researching", "labs.game-speed", 4, NOW + 3600),
             2: slot("researching", "labs.labs-speed", 50, NOW + 3600),
             3: slot("idle"), 4: slot("locked"), 5: slot("locked")}
    result = plan(slots=owned, completed_levels={"labs.game-speed": 3, "labs.labs-speed": 49,
                                                 "labs.coins-wave": 10, "labs.coins-kill-bonus": 30})
    assert result.slots[2].next is None or result.slots[2].next.lab_id != "labs.labs-speed"
    assert result.slots[1].next.lab_id == "labs.labs-speed" and result.slots[1].next.level == 51


def test_two_slots_never_pick_the_same_lab() -> None:
    owned = {1: slot("idle"), 2: slot("idle"), 3: slot("idle"), 4: slot("idle"), 5: slot("locked")}
    result = plan(slots=owned, completed_levels={"labs.game-speed": 7, "labs.labs-speed": 10,
                                                 "labs.coins-wave": 2, "labs.coins-kill-bonus": 5})
    picked = [s.next.lab_id for s in result.slots if s.next is not None]
    assert len(picked) == len(set(picked))


def test_running_slot_targets_its_next_level_at_completion() -> None:
    owned = {1: slot("researching", "labs.game-speed", 4, NOW + 7200), 2: slot("idle"),
             3: slot("locked"), 4: slot("locked"), 5: slot("locked")}
    result = plan(slots=owned)
    assert result.slots[0].next.lab_id == "labs.game-speed" and result.slots[0].next.level == 5


def test_beyond_save_window_is_skipped() -> None:
    entries = [ENTRIES[0], {"id": "c", "lab_id": "labs.coins-kill-bonus", "to_level": 30, "tier": "C"},
               {"id": "cw", "lab_id": "labs.coins-wave", "to_level": 10, "tier": "B"}]
    result = plan(Route(entries), wallet_coins=0, available_coins=0,
                  completed_levels={"labs.game-speed": 7, "labs.coins-kill-bonus": 5, "labs.coins-wave": 2})
    assert result.slots[0].next.lab_id == "labs.coins-wave"
    assert any("beyond save window" in line for line in result.slots[0].why)


def test_unknown_income_lets_only_top_tiers_wait() -> None:
    result = plan(wallet_coins=0, available_coins=0, coins_per_hour=None,
                  completed_levels={"labs.game-speed": 7, "labs.labs-speed": 10,
                                    "labs.coins-wave": 2, "labs.coins-kill-bonus": 5})
    assert result.slots[0].next is None


def test_unread_levels_skip_without_freezing_workshop() -> None:
    result = plan(completed_levels={})
    assert result.slots[0].next is None or result.slots[0].next.lab_id == "labs.game-speed"
    assert result.slots[1].next is None
    assert any("level unread" in line for line in result.slots[1].why)


def test_start_manually_note_for_unautomated_targets() -> None:
    result = plan()
    assert result.slots[1].note == "Start manually" and not result.slots[1].automated


def test_slot_track_strategies_are_untouched() -> None:
    from fleet.resource_blocks import template_lab_blocks
    raw = RouteDocument.compatibility().to_dict()["baseline"]
    raw["labs"].update(mode="blocks", blocks=list(template_lab_blocks()))
    baseline = RouteBaseline.from_dict(raw)
    result = evaluate_lab_plan(baseline, facts())
    assert result.saving is None and all(s.role == "target" for s in result.slots)
```

In `test_unread_levels_skip_without_freezing_workshop`, the Game Speed level comes from `completed_levels` or from the slot-1 record. With both empty, slot 1 has no target either. Workshop not being frozen is asserted in Task 7, once saving exists.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lab_list_eval.py -q -p no:allure_pytest`
Expected: FAIL. `LabFacts` has no `best_waves`, and `evaluate_lab_plan` has no `lab_list` branch.

- [ ] **Step 3: Refactor `evaluate_lab_plan` into shared helpers**

In `fleet/resource_blocks.py`, add the fields listed under Interfaces, with the defaults shown. Then extract the following from `evaluate_lab_plan`, keeping `slot_track` behaviour identical:

```python
@dataclass(frozen=True)
class SlotContext:
    known: dict[str, int]
    running: dict[str, int]
    unavailable: set[str]
    nows: dict[int, SlotNow]


def _slot_context(facts: LabFacts) -> SlotContext:
    """Known and running levels, busy labs and per-slot Now - shared by every lane shape."""
    known, running = _known_levels(facts.slot1)
    known.update({key: level for key, level in (facts.completed_levels or {}).items()
                  if lab_catalog.lab(key) is not None and type(level) is int and level >= 0})
    unavailable = set(facts.running_research) | set(facts.reserved_research)
    for record in (facts.slots or {}).values():
        if record.get("state") != "researching":
            continue
        lab_id = record.get("research_id")
        if isinstance(lab_id, str) and lab_catalog.lab(lab_id) is not None:
            unavailable.add(lab_id)
            target = record.get("target_level")
            if type(target) is int and target >= 1:
                running[lab_id] = target
                known[lab_id] = max(known.get(lab_id, 0), target - 1)
    if facts.slot1 and facts.slot1.get("kind") == "wait_running":
        unavailable.add(GAME_SPEED)
    later = SlotNow("unknown")
    nows = {1: _slot1_now(facts.slot1, facts.now), 2: _slot2_now(facts.slot2, facts.now),
            3: later, 4: later, 5: later}
    for slot, record in (facts.slots or {}).items():
        if slot in LAB_SLOTS and isinstance(record, Mapping):
            nows[slot] = _observed_now(record, facts.now)
    return SlotContext(known, running, unavailable, nows)


def _capabilities(slot: int, automated: bool, now: SlotNow, facts: LabFacts,
                  planned: bool) -> dict[str, bool]:
    observed = (facts.slots or {}).get(slot)
    capabilities = {"observe": True, "plan": planned,
                    "execute": automated and now.state == "idle" and not now.stale
                    and now.evidence_status == "current"
                    and observed is not None and observed.get("confirmed") is True
                    and observed.get("preview_only") is not True}
    if facts.capabilities and slot in facts.capabilities:
        capabilities = {key: capabilities[key] and facts.capabilities[slot].get(key, False)
                        for key in capabilities}
    return capabilities
```

Rewrite the top of `evaluate_lab_plan` to use `ctx = _slot_context(facts)` in place of the inline code, and `_capabilities(slot, automated, nows[slot], facts, track is not None)` in place of the inline capabilities block. Right after computing `lab_blocks`, add the dispatch:

```python
    if route.labs.mode == "blocks" and len(lab_blocks) == 1 and lab_blocks[0].get("type") == "lab_list":
        from fleet.lab_list import evaluate_lab_list
        return evaluate_lab_list(route, facts, ctx=_slot_context(facts),
                                 gems=_gem_plan(gem_blocks, facts, rules))
```

Run `uv run pytest tests/test_resource_blocks.py -q -p no:allure_pytest`. It must still pass before you continue: the refactor is behaviour-neutral.

- [ ] **Step 4: Add `income_rate`**

```python
# fleet/lab_saving.py
"""Just-in-time lab saving: how many coins Workshop must leave for upcoming labs. Pure."""

from __future__ import annotations

import math
from typing import Any


def income_rate(facts: Any, rules: Any) -> float | None:
    """Coins per hour after the safety margin; None when unread or not positive."""
    rate = getattr(facts, "coins_per_hour", None)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
        return None
    return rate * rules.labs.saving.income_margin_pct / 100
```

- [ ] **Step 5: Implement `evaluate_lab_list`, without fillers**

Append to `fleet/lab_list.py`:

```python
def _unlock_reason(lab_id: str, facts: Any, known: Mapping[str, int]) -> str | None:
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


def _candidate(entry: Mapping[str, Any], slot: int, facts: Any, known: Mapping[str, int],
               running: Mapping[str, int], busy: set[str]) -> tuple[Any | None, str]:
    """(option, reason). option is None when the entry is skipped for `reason`."""
    from fleet.resource_blocks import _next_level, _option
    lab_id = entry["lab_id"]
    level = _next_level(lab_id, known, running)
    if level is not None and level > entry["to_level"]:
        return None, "finished"
    pin = entry.get("pin_slot")
    if pin is not None and pin != slot:
        return None, "pinned elsewhere"
    if lab_id in busy:
        return None, "running or claimed elsewhere"
    locked = _unlock_reason(lab_id, facts, known)
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
    if wallet is not None and price <= wallet:
        return True
    if rate is None or wallet is None:
        return tier in ("S+", "S")
    return (price - wallet) / rate <= rules.labs.saving.window_hours[tier] + hours_until


def _hours_until(now_state: Any, now: float) -> float | None:
    if now_state.state != "researching":
        return 0.0
    if now_state.completes_at is None:
        return None
    return max(0.0, now_state.completes_at - now) / 3600


def _target(slot: int, entries: list[Mapping[str, Any]], facts: Any, ctx: Any, busy: set[str],
            wallet: int | None, rate: float | None, hours_until: float | None, rules: Any,
            why: list[str]) -> tuple[Any | None, Mapping[str, Any] | None]:
    """Step 1 (pins) then step 2 (ranked walk) of the spec."""
    for entry in entries:
        if entry.get("pin_slot") != slot:
            continue
        option, reason = _candidate(entry, slot, facts, ctx.known, ctx.running, busy)
        if option is not None:
            why.append(f"{entry['id']}: pinned to slot {slot}")
            return option, entry
        why.append(f"{entry['id']}: {reason}")
    for entry in entries:
        if entry.get("pin_slot") == slot:
            continue
        option, reason = _candidate(entry, slot, facts, ctx.known, ctx.running, busy)
        if option is None:
            why.append(f"{entry['id']}: {reason}")
            continue
        if not _within_window(option.price, entry["tier"], wallet, rate, hours_until or 0.0, rules):
            why.append(f"{entry['id']}: beyond save window ({entry['tier']})")
            continue
        why.append(f"{entry['id']}: rank {entries.index(entry) + 1}")
        return option, entry
    return None, None


def evaluate_lab_list(route: Any, facts: Any, *, ctx: Any, gems: Any) -> Any:
    """Pure: each owned slot's target (and, in Task 6, a filler). No reads, writes or clocks."""
    from fleet.lab_saving import income_rate
    from fleet.resource_blocks import LAB_SLOTS, LabPlan, SlotPlan, _capabilities, research_automated
    rules = route.rules
    entries = route.labs.blocks[0]["entries"]
    rate = income_rate(facts, rules)
    wallet = facts.available_coins if facts.available_coins is not None else facts.wallet_coins
    busy = set(ctx.unavailable)
    plans = []
    for slot in LAB_SLOTS:
        now = ctx.nows[slot]
        if now.owned is not True:
            plans.append(SlotPlan(slot, now, None, None, False, ("Slot not owned or ownership unread",),
                                  None, _capabilities(slot, False, now, facts, False)))
            continue
        why: list[str] = []
        own = now.research_id if now.state == "researching" else None
        competing = busy - {own} if own else busy
        target, _ = _target(slot, entries, facts, ctx, competing, wallet,
                            rate, _hours_until(now, facts.now), rules, why)
        if target is None:
            why.append("No entry can run in this slot now")
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
            busy.add(target.lab_id)
            if starts_now:
                wallet -= target.price
    return LabPlan(facts.wallet_coins, facts.jar, tuple(plans), gems,
                   getattr(route, "revision", 0), facts.account_id, facts.scope, facts.now)
```

- [ ] **Step 6: Run the new tests and the neighbouring tests**

Run: `uv run pytest tests/test_lab_list_eval.py tests/test_resource_blocks.py tests/test_lab_list.py -q -p no:allure_pytest`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add fleet/resource_blocks.py fleet/lab_list.py fleet/lab_saving.py tests/test_lab_list_eval.py
git commit -m "Evaluate the ranked lab list with slot pins and save windows

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Fillers: every idle slot runs a lab

**Files:**
- Modify: `fleet/lab_list.py`: `_filler` and its use in `evaluate_lab_list`
- Test: `tests/test_lab_list_eval.py`, add filler tests

**Interfaces:**
- **Consumes:** `RouteRules.labs.filler` (Task 3), plus `_candidate` and `income_rate` (Task 5).
- **Produces:**
  - A `SlotPlan` with `role="filler"` and `saving_for=<target SlotNext>`, whose `next` is the filler.
  - `fleet.lab_list.SlotSaving(slot: int, target: SlotNext, needed_at: float | None, idle_slot: bool, tier: str)`, collected into a module-level list returned alongside the plans for Task 7. Implement it as the return value of the helper `_evaluate_slots(...) -> tuple[list[SlotPlan], list[SlotSaving], int | None]`, where the int is the wallet left after the starts that happen now.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lab_list_eval.py`:

```python
FILLER_RULES = {"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}},
                "labs": {"filler": {"enabled": True, "max_price_pct_of_wallet": 10, "min_hours": 1}}}


def test_unaffordable_target_gets_shortest_cheap_filler() -> None:
    # Game Speed L4 costs 50,000; the wallet holds 20,000. Coins/Kill L6 (1h20m, 1,350 coins)
    # fits: gap = (50,000 - (20,000 - 1,350)) / 7,500 = 4.2h. Coins/Wave L3 is shorter still, if priced.
    result = plan(Route(rules=FILLER_RULES), wallet_coins=20_000, available_coins=20_000)
    slot1 = result.slots[0]
    assert slot1.role == "filler" and slot1.saving_for.lab_id == "labs.game-speed"
    assert slot1.next.lab_id in {"labs.coins-kill-bonus", "labs.coins-wave"}
    assert slot1.next.price <= 2_000 and slot1.next.seconds <= 4.2 * 3600


def test_filler_over_price_cap_is_refused() -> None:
    result = plan(Route(rules=FILLER_RULES), wallet_coins=5_000, available_coins=5_000,
                  completed_levels={"labs.game-speed": 3, "labs.labs-speed": 10,
                                    "labs.coins-wave": 9, "labs.coins-kill-bonus": 29})
    assert result.slots[0].role == "target"
    assert any("no filler fits" in line.lower() for line in result.slots[0].why)


def test_filler_longer_than_gap_is_refused() -> None:
    # Income so high the target is affordable within minutes: no filler may overrun that gap
    # beyond min_hours (1h).
    result = plan(Route(rules=FILLER_RULES), wallet_coins=49_000, available_coins=49_000,
                  coins_per_hour=10_000_000.0,
                  completed_levels={"labs.game-speed": 3, "labs.labs-speed": 10,
                                    "labs.coins-wave": 2, "labs.coins-kill-bonus": 20})
    filler = result.slots[0]
    assert filler.role == "target" or filler.next.seconds <= 3600


def test_unknown_income_caps_filler_at_min_hours() -> None:
    result = plan(Route(rules=FILLER_RULES), wallet_coins=20_000, available_coins=20_000, coins_per_hour=None)
    if result.slots[0].role == "filler":
        assert result.slots[0].next.seconds <= 3600


def test_disabled_filler_leaves_slot_waiting_on_target() -> None:
    result = plan(wallet_coins=20_000, available_coins=20_000)
    assert result.slots[0].role == "target" and result.slots[0].next.lab_id == "labs.game-speed"
    assert result.slots[0].covered is False
```

The price assertions depend on the shipped v2 tables: Coins/Kill L6 costs 1,350 coins and takes 4,800 s. If the imported numbers differ, keep each test's intent and derive its numbers from `lab_catalog.level(...)` inside the test.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lab_list_eval.py -q -p no:allure_pytest -k "filler"`
Expected: FAIL, because no slot ever has `role == "filler"`.

- [ ] **Step 3: Implement**

In `fleet/lab_list.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class SlotSaving:
    slot: int
    target: Any
    needed_at: float | None
    idle_slot: bool
    tier: str


def _filler(slot: int, entries: list[Mapping[str, Any]], facts: Any, ctx: Any, busy: set[str],
            wallet: int | None, target: Any | None, rate: float | None, rules: Any) -> Any | None:
    """Shortest cheap entry whose duration fits the gap until the target is affordable."""
    rule = rules.labs.filler
    if not rule.enabled or wallet is None:
        return None
    cap = wallet * rule.max_price_pct_of_wallet / 100
    best: tuple[tuple[int, int], Any] | None = None
    for rank, entry in enumerate(entries):
        option, _ = _candidate(entry, slot, facts, ctx.known, ctx.running, busy)
        if option is None or option.seconds is None or option.price > cap:
            continue
        if target is not None and option.lab_id == target.lab_id:
            continue
        gap = rule.min_hours
        if target is not None and rate is not None:
            gap = max(gap, (target.price - (wallet - option.price)) / rate)
        if option.seconds > gap * 3600:
            continue
        key = (option.seconds, rank)
        if best is None or key < best[0]:
            best = (key, option)
    return best[1] if best else None
```

In `evaluate_lab_list`, replace the per-slot body after the target is chosen with the following. Keep `automated`/`note` computed for whatever ends up in `next`.

```python
        target, entry = _target(...)   # as in Task 5; keep `entry` for its tier
        hours_until = _hours_until(now, facts.now)
        next_, role, saving_for, needed_at = target, "target", None, None
        idle = now.state == "idle"
        if target is not None and now.state == "researching":
            needed_at = now.completes_at
        affordable = target is not None and wallet is not None and target.price <= wallet
        if idle and not affordable:
            filler = _filler(slot, entries, facts, ctx, competing | ({target.lab_id} if target else set()),
                             wallet, target, rate, rules)
            if filler is not None:
                next_, role, saving_for = filler, "filler", target
                needed_at = facts.now + filler.seconds if target is not None else None
                why.append(f"Filler {filler.name} L{filler.level} while saving"
                           + (f" for {target.name} L{target.level}" if target else ""))
            else:
                why.append("No filler fits the price cap and the gap")
                needed_at = facts.now if target is not None else None
        starts_now = idle and next_ is not None and wallet is not None and next_.price <= wallet
        automated = next_ is not None and rules.labs.auto_start and research_automated(next_.lab_id, slot)
        note = "Start manually" if next_ is not None and not automated else None
        covered = (True if starts_now else None if wallet is None or next_ is None
                   else False if idle else None)
        plans.append(SlotPlan(slot, now, next_, covered, automated, tuple(why), note,
                              _capabilities(slot, automated, now, facts, True), role, saving_for))
        for picked in (next_, saving_for):
            if picked is not None:
                busy.add(picked.lab_id)
        if starts_now:
            wallet -= next_.price
        if target is not None and not (starts_now and role == "target"):
            savings.append(SlotSaving(slot, target, needed_at, idle, entry["tier"]))
```

Initialise `savings: list[SlotSaving] = []` before the loop. `_target` returns `(option, entry)`, so capture both. Keep `competing` as defined in Task 5.

Move the loop into `_evaluate_slots(route, facts, ctx) -> tuple[list[SlotPlan], list[SlotSaving], int | None]` so Task 7 can reuse the leftover wallet. `evaluate_lab_list` then calls it and builds the `LabPlan`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_lab_list_eval.py tests/test_resource_blocks.py -q -p no:allure_pytest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add fleet/lab_list.py tests/test_lab_list_eval.py
git commit -m "Keep every idle lab slot running with a short, cheap filler

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `SavingPlan` and coverage

**Files:**
- Modify: `fleet/lab_saving.py`: add `SavingInput`, `SavingTarget`, `SavingPlan` and `saving_plan`
- Modify: `fleet/lab_list.py`: `evaluate_lab_list` attaches `LabPlan.saving` and fills `covered` for researching and filler slots
- Test: `tests/test_lab_saving.py` (create)

**Interfaces:**
- **Consumes:** `SlotSaving` and the leftover wallet from `_evaluate_slots` (Task 6); `income_rate` (Task 5).
- **Produces:**
  - `SavingTarget(slot: int, lab_id: str, name: str, level: int | None, price: int, needed_at: float | None, ready_at: float | None, covered: bool | None)`
  - `SavingPlan(reserve: int | None, workshop_budget: int, wallet: int | None, coins_per_hour: float | None, targets: tuple[SavingTarget, ...], why: tuple[str, ...])`
  - `saving_plan(pending: Sequence[SlotSaving], *, wallet: int | None, rate: float | None, spend_limit_pct: int, now: float) -> SavingPlan`
  - `LabPlan.saving` is a `SavingPlan` for `lab_list` lanes and `None` for `slot_track` lanes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lab_saving.py
from __future__ import annotations

from fleet.lab_list import SlotSaving
from fleet.lab_saving import saving_plan
from fleet.resource_blocks import SlotNext

NOW = 1_000_000.0


def target(slot: int, price: int, needed_in_h: float | None, *, idle: bool = False,
           tier: str = "S") -> SlotSaving:
    lab = SlotNext(f"labs.x{slot}", f"Lab {slot}", 2, price, 3600)
    return SlotSaving(slot, lab, None if needed_in_h is None else NOW + needed_in_h * 3600, idle, tier)


def test_idle_unaffordable_target_without_filler_reserves_full_price() -> None:
    result = saving_plan([target(1, 50_000, 0, idle=True)], wallet=20_000, rate=1_000.0,
                         spend_limit_pct=100, now=NOW)
    assert result.reserve == 20_000 and result.workshop_budget == 0
    assert result.targets[0].covered is False
    assert result.targets[0].ready_at == NOW + 30 * 3600


def test_slot_freeing_later_reserves_only_the_uncovered_part() -> None:
    result = saving_plan([target(2, 38_000, 10)], wallet=20_000, rate=2_900.0, spend_limit_pct=100, now=NOW)
    assert result.reserve == 38_000 - 29_000
    assert result.workshop_budget == 20_000 - 9_000
    assert result.targets[0].covered is True


def test_two_targets_add_up() -> None:
    result = saving_plan([target(1, 10_000, 1), target(2, 10_000, 1)], wallet=15_000, rate=1_000.0,
                         spend_limit_pct=100, now=NOW)
    assert result.reserve == 15_000


def test_reserve_capped_at_wallet() -> None:
    result = saving_plan([target(1, 10**9, 0, idle=True)], wallet=500, rate=1.0, spend_limit_pct=100, now=NOW)
    assert result.reserve == 500 and result.workshop_budget == 0


def test_unknown_completion_is_not_reserved() -> None:
    result = saving_plan([target(1, 10_000, None)], wallet=20_000, rate=1_000.0, spend_limit_pct=100, now=NOW)
    assert result.reserve == 0 and result.targets[0].covered is None


def test_unknown_income_reserves_only_idle_top_tiers() -> None:
    pending = [target(1, 8_000, 0, idle=True, tier="S+"), target(2, 5_000, 0, idle=True, tier="A"),
               target(3, 4_000, 5, tier="S")]
    result = saving_plan(pending, wallet=20_000, rate=None, spend_limit_pct=100, now=NOW)
    assert result.reserve == 8_000 and result.targets[0].ready_at is None


def test_unread_wallet_spends_nothing() -> None:
    result = saving_plan([target(1, 1_000, 0, idle=True)], wallet=None, rate=1_000.0, spend_limit_pct=100, now=NOW)
    assert result.reserve is None and result.workshop_budget == 0


def test_spend_limit_applies_after_reserve() -> None:
    result = saving_plan([], wallet=10_000, rate=1_000.0, spend_limit_pct=50, now=NOW)
    assert (result.reserve, result.workshop_budget) == (0, 5_000)
```

Also append to `tests/test_lab_list_eval.py`:

```python
def test_plan_carries_saving_for_filler_target() -> None:
    result = plan(Route(rules=FILLER_RULES), wallet_coins=20_000, available_coins=20_000)
    assert result.saving is not None
    assert result.saving.targets and result.saving.targets[0].lab_id == "labs.game-speed"
    spent = sum(s.next.price for s in result.slots if s.now.state == "idle" and s.covered is True)
    assert result.saving.wallet == 20_000 - spent


def test_unread_levels_do_not_freeze_workshop() -> None:
    result = plan(completed_levels={})
    assert result.saving.reserve == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lab_saving.py -q -p no:allure_pytest`
Expected: FAIL with `ImportError: saving_plan`.

- [ ] **Step 3: Implement**

Append to `fleet/lab_saving.py`:

```python
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class SavingTarget:
    slot: int
    lab_id: str
    name: str
    level: int | None
    price: int
    needed_at: float | None
    ready_at: float | None
    covered: bool | None


@dataclass(frozen=True)
class SavingPlan:
    reserve: int | None
    workshop_budget: int
    wallet: int | None
    coins_per_hour: float | None
    targets: tuple[SavingTarget, ...]
    why: tuple[str, ...]


def _coins(value: float) -> str:
    return f"{value / 1000:.0f}k" if value >= 1000 else f"{value:.0f}"


def saving_plan(pending: Sequence[Any], *, wallet: int | None, rate: float | None,
                spend_limit_pct: int, now: float) -> SavingPlan:
    """How much Workshop must leave so each slot's next lab is affordable when due."""
    if wallet is None:
        targets = tuple(SavingTarget(p.slot, p.target.lab_id, p.target.name, p.target.level, p.target.price,
                                     p.needed_at, None, None) for p in pending)
        return SavingPlan(None, 0, None, rate, targets, ("Wallet unread: Workshop waits",))
    dated = sorted((p for p in pending if p.needed_at is not None), key=lambda p: p.needed_at)
    undated = [p for p in pending if p.needed_at is None]
    targets: list[SavingTarget] = []
    why: list[str] = []
    reserve, cumulative = 0.0, 0
    for p in dated:
        cumulative += p.target.price
        hours = max(0.0, p.needed_at - now) / 3600
        if rate is not None:
            reserve = max(reserve, cumulative - rate * hours)
            short = max(0, cumulative - wallet)
            ready_at = now + short / rate * 3600
            covered = ready_at <= max(now, p.needed_at)
            why.append(f"Slot {p.slot} {p.target.name} L{p.target.level}: needs {_coins(p.target.price)}"
                       + (f", due in {hours:.1f}h, income covers {_coins(min(p.target.price, rate * hours))}"
                          if hours else ", due now"))
        else:
            ready_at = None
            covered = cumulative <= wallet
            if p.idle_slot and p.tier in ("S+", "S"):
                reserve += p.target.price
            why.append(f"Slot {p.slot} {p.target.name}: income unread, "
                       + ("holding coins" if p.idle_slot and p.tier in ("S+", "S") else "not saving ahead"))
        targets.append(SavingTarget(p.slot, p.target.lab_id, p.target.name, p.target.level, p.target.price,
                                    p.needed_at, ready_at, covered))
    for p in undated:
        targets.append(SavingTarget(p.slot, p.target.lab_id, p.target.name, p.target.level, p.target.price,
                                    None, None, None))
        why.append(f"Slot {p.slot} {p.target.name}: completion time unknown, not reserved")
    held = min(wallet, max(0, math.ceil(reserve)))
    return SavingPlan(held, (wallet - held) * spend_limit_pct // 100, wallet, rate,
                      tuple(targets), tuple(why))
```

In `fleet/lab_list.py`, `evaluate_lab_list`:

```python
    plans, savings, left = _evaluate_slots(route, facts, ctx)
    saving = saving_plan(savings, wallet=left, rate=income_rate(facts, rules),
                         spend_limit_pct=rules.coins.workshop_spend_limit_pct, now=facts.now)
    by_slot = {t.slot: t for t in saving.targets}
    plans = [replace(p, covered=by_slot[p.slot].covered)
             if p.slot in by_slot and (p.now.state == "researching") else p for p in plans]
    return LabPlan(facts.wallet_coins, facts.jar, tuple(plans), gems,
                   getattr(route, "revision", 0), facts.account_id, facts.scope, facts.now, saving)
```

Here `replace` is `dataclasses.replace`, and `saving_plan` is imported from `fleet.lab_saving`. A filler slot's `covered` stays `True`, since it describes the filler starting now. The filler's target coverage lives in `saving.targets`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_lab_saving.py tests/test_lab_list_eval.py tests/test_resource_blocks.py -q -p no:allure_pytest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add fleet/lab_saving.py fleet/lab_list.py tests/test_lab_saving.py tests/test_lab_list_eval.py
git commit -m "Reserve coins just in time for each slot's next lab

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Early-game template

**Files:**
- Modify: `fleet/resource_blocks.py`: add `template_lab_list()` and `template_lab_list_rules()`. Keep `template_lab_blocks()`/`template_rules()`, which the `slot_track` tests use as fixtures.
- Modify: `fleet/strategy_library.py:43-47`: `labs_gems` uses the new pair
- Modify: `tests/test_strategy_library.py:164-175`: assert the new template
- Test: `tests/test_lab_strategy_template.py` (create)

**Interfaces:**
- **Produces:**
  - `template_lab_list() -> tuple[dict[str, Any], ...]`, a single validated `lab_list` block;
  - `template_lab_list_rules() -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lab_strategy_template.py
from __future__ import annotations

import lab_catalog
from fleet import resource_blocks as rb
from fleet.build_route import RouteBaseline, RouteDocument

EXCLUDED = {"labs.starting-cash", "labs.cash-wave", "labs.black-hole-damage"}


def test_template_validates_with_pins_and_rules() -> None:
    (block,) = rb.template_lab_list()
    entries = block["entries"]
    assert entries[0] == {"id": "labs.list.game_speed", "lab_id": "labs.game-speed", "to_level": 7,
                          "tier": "S+", "pin_slot": 1, "label": "Game Speed to max"}
    assert [e["to_level"] for e in entries if e["lab_id"] == "labs.labs-speed"] == [50, 99]
    assert all(e.get("pin_slot") == 2 for e in entries if e["lab_id"] == "labs.labs-speed")
    raw = RouteDocument.compatibility().to_dict()["baseline"]
    raw["labs"].update(mode="blocks", blocks=list(rb.template_lab_list()))
    raw["rules"] = rb.template_lab_list_rules()
    assert RouteBaseline.from_dict(raw).rules.coins.lab_share.mode == "just_in_time"


def test_template_excludes_traps_and_irreversible_labs() -> None:
    (block,) = rb.template_lab_list()
    ids = {e["lab_id"] for e in block["entries"]}
    assert not ids & EXCLUDED
    assert not any("bot" in lab_id and "cooldown" in lab_id for lab_id in ids)


def test_template_labs_are_priced() -> None:
    (block,) = rb.template_lab_list()
    for entry in block["entries"]:
        assert lab_catalog.lab(entry["lab_id"]).levels is not None, entry["lab_id"]
```

In `tests/test_strategy_library.py`, change the `labs_gems` assertions (lines 171–174) to:

```python
    assert baseline["labs"]["blocks"][0]["type"] == "lab_list"
    assert baseline["rules"]["coins"]["lab_share"] == {"mode": "just_in_time", "pct": 25}
    assert baseline["rules"]["labs"]["filler"] == {"enabled": True, "max_price_pct_of_wallet": 10, "min_hours": 1.0}
    assert baseline["rules"]["labs"]["auto_start"] and baseline["rules"]["gems"]["auto_unlock_lab_slots"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lab_strategy_template.py -q -p no:allure_pytest`
Expected: FAIL with `AttributeError: template_lab_list`.

- [ ] **Step 3: Implement**

```python
def template_lab_list() -> tuple[dict[str, Any], ...]:
    """The early-game ranked list: Game Speed on slot 1, Labs Speed on slot 2, the rest ranked.

    Order from the spec's template table (tier list v29 + the Discord Lab Progression Guide).
    """
    rows = [
        ("game_speed", "labs.game-speed", 7, "S+", 1, "Game Speed to max"),
        ("unlock_perks", "labs.unlock-perks", 1, "S+", None, "Unlock Perks"),
        ("first_perk", "labs.first-perk-choice", 1, "S+", None, "First Perk Choice"),
        ("perk_options", "labs.perk-option-quantity", 2, "S+", None, "Perk Option Quantity"),
        ("ban_perks", "labs.ban-perks", 1, "S", None, "First perk ban"),
        ("light_speed", "labs.light-speed-shots", 1, "S", None, "Light Speed Shots when affordable"),
        ("coins_wave", "labs.coins-wave", 10, "B", None, "Coins / Wave while under 100 waves"),
        ("labs_speed_50", "labs.labs-speed", 50, "S", 2, "Labs Speed to 50"),
        ("coins_kill", "labs.coins-kill-bonus", 30, "A", None, "Coins / Kill Bonus to 30"),
        ("cash_bonus", "labs.cash-bonus", 20, "A", None, "Cash Bonus to 20"),
        ("attack_speed", "labs.attack-speed", 50, "A", None, "Attack Speed to 50"),
        ("health", "labs.health", 30, "B", None, "Health: fast early levels"),
        ("damage", "labs.damage", 30, "B", None, "Damage: fast early levels"),
        ("standard_perks", "labs.standard-perks-bonus", 10, "S", None, "Standard Perks Bonus"),
        ("trade_off", "labs.improve-trade-off-perks", 5, "S", None, "Improve Trade-Off Perks"),
        ("ws_attack", "labs.workshop-attack-discount", 20, "C", None, "Workshop Attack Discount: cheap levels"),
        ("ws_defense", "labs.workshop-defense-discount", 20, "C", None, "Workshop Defense Discount: cheap levels"),
        ("ws_utility", "labs.workshop-utility-discount", 20, "C", None, "Workshop Utility Discount: cheap levels"),
        ("labs_speed_99", "labs.labs-speed", 99, "A", 2, "Labs Speed to max"),
    ]
    entries = []
    for key, lab_id, level, tier, pin, label in rows:
        entry = lab_catalog.lab(lab_id)
        if entry is None or entry.levels is None:
            continue  # Spec: a lab without a price table is left out of the template.
        item = {"id": f"labs.list.{key}", "lab_id": lab_id, "to_level": min(level, entry.max_level),
                "tier": tier, "label": label}
        if pin is not None:
            item["pin_slot"] = pin
        entries.append(item)
    return validate_labs([{"id": "labs.list", "type": "lab_list", "label": "Early game", "entries": entries}])


def template_lab_list_rules() -> dict[str, Any]:
    return {
        "coins": {"lab_share": {"mode": "just_in_time", "pct": 25}, "workshop_spend_limit_pct": 100},
        "labs": {"auto_start": True, "idle_fill": "leave_idle",
                 "pool": {"selection": "ordered", "max_price_pct_of_wallet": None, "max_seconds": None},
                 "saving": {"income_margin_pct": 75,
                            "window_hours": {"S+": 72, "S": 24, "A": 12, "B": 4, "C": 0}},
                 "filler": {"enabled": True, "max_price_pct_of_wallet": 10, "min_hours": 1}},
        "gems": {"auto_unlock_lab_slots": True, "spend_limit_pct": 100, "keep": 0},
    }
```

The `entries[0]` key order in the test must match the dict insertion order. `validate_lab_list` copies each dict, and `pin_slot` is appended after `label`. Test equality compares dicts, not their order, so no change is needed.

In `fleet/strategy_library.py` `templates()`:
- import `template_lab_list` and `template_lab_list_rules`;
- set `labs_gems["labs"].update(mode="blocks", blocks=list(template_lab_list()))`;
- set `labs_gems["rules"] = template_lab_list_rules()`;
- update the comment on line 43 to "a ranked lab list with just-in-time saving".

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_lab_strategy_template.py tests/test_strategy_library.py tests/test_auto_assign.py -q -p no:allure_pytest`
Expected: PASS. `test_auto_assign.py` rotates `labs_gems`. If it asserts `slot_track`-specific plan output, update it to the `lab_list` equivalent.

- [ ] **Step 5: Commit**

```bash
git add fleet/resource_blocks.py fleet/strategy_library.py tests/test_lab_strategy_template.py tests/test_strategy_library.py tests/test_auto_assign.py
git commit -m "Ship the early-game ranked lab template with just-in-time saving

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Persisted lab facts: income and best waves

**Files:**
- Create: `fleet/lab_facts.py`
- Modify: `fleet/labs_view.py:84-97`, where `_row` builds `LabFacts`
- Modify: `fleet/reroll_progress.py:163-184` (`lab_strategy_plan`), to fill `coins_per_hour` and `best_waves`
- Test: `tests/test_lab_facts.py` (create)

**Interfaces:**
- **Produces:**
  - `best_waves(db_path: Path) -> dict[int, int]`
  - `coins_per_hour(worker_root: Path, account_id: str) -> float | None`
  - `persisted_lab_facts(worker_root: Path, account_id: str, *, now: float, coins: int | None, gems: int | None, db_path: Path) -> LabFacts`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lab_facts.py
from __future__ import annotations

import sqlite3
from pathlib import Path

import db
from fleet import lab_facts


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "tower_bot.db"
    connection = db.connect(path) if hasattr(db, "connect") else sqlite3.connect(path)
    connection.close()
    return path


def test_best_waves_groups_finished_runs_by_tier(tmp_path: Path) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.executemany("INSERT INTO runs (tier, wave, ended_at) VALUES (?, ?, ?)",
                               [(1, 120, 1.0), (1, 180, 2.0), (2, 40, 3.0), (2, 90, None)])
    assert lab_facts.best_waves(path) == {1: 180, 2: 40}


def test_best_waves_unreadable_is_empty(tmp_path: Path) -> None:
    assert lab_facts.best_waves(tmp_path / "missing.db") == {}


def test_coins_per_hour_reads_lifetime_record(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lab_facts, "read_lifetime",
                        lambda root, account: {"recent_coins_per_hour": 12_000.0})
    assert lab_facts.coins_per_hour(tmp_path, "acct") == 12_000.0
    monkeypatch.setattr(lab_facts, "read_lifetime", lambda root, account: None)
    assert lab_facts.coins_per_hour(tmp_path, "acct") is None
```

Before writing `_db`, check how the existing tests create a worker database: `grep -rn "def .*tmp.*db\|db.connect\|db.open" tests/ | head`. Use the same helper so that the `runs` table and its required columns exist. Adjust the `INSERT` to include any `NOT NULL` columns the schema requires. The `CREATE TABLE IF NOT EXISTS runs` statement in `db.py` lists them.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lab_facts.py -q -p no:allure_pytest`
Expected: FAIL with `ModuleNotFoundError: fleet.lab_facts`.

- [ ] **Step 3: Implement**

```python
# fleet/lab_facts.py
"""Lab facts a worker can read from disk: slots, levels, best waves and coin income.

Shared by the Labs & Gems page and the worker's Workshop policy so both plan the
same way. Anything unreadable stays None/empty - never zero.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import db
from fleet.build_route_preview_facts import read_lab_slots
from fleet.coin_share import LabCoinJar
from fleet.reroll_lifetime import read_lifetime
from fleet.resource_blocks import LabFacts
from lab_plan import LabCadence


def best_waves(db_path: Path) -> dict[int, int]:
    try:
        with db.reader(db_path) as connection:
            rows = connection.execute(
                "SELECT tier, MAX(wave) FROM runs WHERE ended_at IS NOT NULL AND tier IS NOT NULL "
                "AND wave IS NOT NULL GROUP BY tier").fetchall()
    except (OSError, sqlite3.Error):
        return {}
    return {int(tier): int(wave) for tier, wave in rows}


def coins_per_hour(worker_root: Path, account_id: str) -> float | None:
    record = read_lifetime(worker_root, account_id)
    rate = record.get("recent_coins_per_hour") if record else None
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
        return None
    return float(rate)


def persisted_lab_facts(worker_root: Path, account_id: str, *, now: float, coins: int | None,
                        gems: int | None, db_path: Path) -> LabFacts:
    slot1, slot2 = LabCadence(worker_root, account_id).route_observation()
    waves = best_waves(db_path)
    return LabFacts(now, coins, gems, waves.get(1), slot1, slot2,
                    LabCoinJar(worker_root, account_id, read_only=True).amount(quiet=True),
                    slots=read_lab_slots(worker_root, account_id), available_coins=coins,
                    account_id=account_id, best_waves=waves or None,
                    coins_per_hour=coins_per_hour(worker_root, account_id))
```

If `db.reader` raises on a missing file instead of `OSError`/`sqlite3.Error`, add that exception type to the `except` clause. `test_best_waves_unreadable_is_empty` pins this.

In `fleet/labs_view.py` `_row`, replace the inline `LabCadence`/`read_lab_slots`/`LabCoinJar` facts construction with:

```python
    facts = persisted_lab_facts(worker_root, account_id, now=now, coins=coins, gems=gems,
                                db_path=registration.db_path)
    plan = evaluate_lab_plan(resolve_route(route, worker, account_id), facts)
```

Keep `_history` for `recent`. The `best` value it returns is no longer used for facts. Remove imports that become unused.

In `fleet/reroll_progress.py` `lab_strategy_plan`, extend the final `replace(facts, ...)` with:

```python
                        coins_per_hour=coins_per_hour(self.root, self.account_id),
                        best_waves=best_waves(self.root / "tower_bot.db") or None,
```

Import both from `fleet.lab_facts` at the top of the file. `read_lifetime` reads `root / "tower_bot.db"`, so `self.root` is the worker root.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_lab_facts.py tests/test_labs_view.py tests/test_reroll_progress.py -q -p no:allure_pytest`
Expected: PASS. If `tests/test_labs_view.py` or `tests/test_reroll_progress.py` doesn't exist, find the matching test files with `ls tests | grep -i "labs_view\|reroll_progress"` and run those instead.

- [ ] **Step 5: Commit**

```bash
git add fleet/lab_facts.py fleet/labs_view.py fleet/reroll_progress.py tests/test_lab_facts.py
git commit -m "Read coin income and best waves into lab facts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Worker uses the just-in-time reserve

**Files:**
- Modify: `fleet/coin_share.py`: add `jit_hold`
- Modify: `fleet/reroll_progress.py:441-446` (the jar/paused block in `shopping_policy`) and the paused message at `:490`
- Test: `tests/test_coin_share_jit.py` (create)

**Interfaces:**
- **Consumes:**
  - `persisted_lab_facts` (Task 9);
  - `evaluate_lab_plan` → `LabPlan.saving` (Task 7).
- **Produces:** `coin_share.jit_hold(saving: SavingPlan | None, wallet: int | None) -> tuple[int, bool, str | None]`, returning `(jar, paused, reason)`.

The reserve rides the existing `lab_coin_jar` seam: `build_route_eval.py:307` and `strategy_blocks.py:384` already subtract `facts.lab_coin_jar` through `workshop_ceiling`. No other consumer changes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_coin_share_jit.py
from __future__ import annotations

from fleet.coin_share import jit_hold
from fleet.lab_saving import SavingPlan, SavingTarget


def plan(reserve: int | None, wallet: int | None) -> SavingPlan:
    target = SavingTarget(1, "labs.game-speed", "Game Speed", 4, 50_000, 0.0, None, False)
    return SavingPlan(reserve, 0, wallet, 1_000.0, (target,), ("Slot 1 Game Speed L4: needs 50k, due now",))


def test_no_saving_holds_nothing() -> None:
    assert jit_hold(None, 10_000) == (0, False, None)


def test_reserve_is_held_and_full_reserve_pauses() -> None:
    assert jit_hold(plan(4_000, 10_000), 10_000) == (4_000, False, "Slot 1 Game Speed L4: needs 50k, due now")
    jar, paused, _ = jit_hold(plan(10_000, 10_000), 10_000)
    assert (jar, paused) == (10_000, True)


def test_unread_wallet_holds_everything_known() -> None:
    jar, paused, _ = jit_hold(plan(None, None), None)
    assert (jar, paused) == (0, True)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_coin_share_jit.py -q -p no:allure_pytest`
Expected: FAIL with `ImportError: jit_hold`.

- [ ] **Step 3: Implement**

In `fleet/coin_share.py`:

```python
def jit_hold(saving: Any, wallet: int | None) -> tuple[int, bool, str | None]:
    """just_in_time: the reserve rides the lab_coin_jar seam every Workshop ceiling subtracts.

    Paused when the reserve takes the whole wallet, or when the wallet is unread
    (Workshop never spends blind). No saving plan (not a lab list) holds nothing.
    """
    if saving is None:
        return 0, False, None
    reason = saving.why[0] if saving.why else None
    if saving.reserve is None or wallet is None:
        return 0, True, reason
    return saving.reserve, saving.reserve > 0 and saving.reserve >= wallet, reason
```

In `fleet/reroll_progress.py` `shopping_policy`, replace:

```python
                jar = self.coin_jar.settle(effective, lab_record, facts.wallet_coins,
                                           facts.visit_id or "", time.time())
                paused = coin_share.workshop_paused(effective, lab_record, facts.wallet_coins)
```

with:

```python
                if effective.rules.coins.lab_share.mode == "just_in_time":
                    from fleet.lab_facts import persisted_lab_facts
                    lab_plan = evaluate_lab_plan(effective, persisted_lab_facts(
                        self.root, self.account_id, now=time.time(), coins=facts.wallet_coins,
                        gems=None, db_path=registration.db_path))
                    jar, paused, saving_reason = coin_share.jit_hold(lab_plan.saving, facts.wallet_coins)
                else:
                    jar = self.coin_jar.settle(effective, lab_record, facts.wallet_coins,
                                               facts.visit_id or "", time.time())
                    paused = coin_share.workshop_paused(effective, lab_record, facts.wallet_coins)
                    saving_reason = None
```

Initialise `saving_reason: str | None = None` next to `jar = 0` and `paused = False`. In the `if paused:` branch, build the reason this way:

```python
            reason = (f"Workshop paused: saving coins for labs · {saving_reason}" if saving_reason
                      else f"Workshop paused: saving coins for the next automated lab "
                           f"({coin_share.waiting_lab_price(effective, lab_record)} coins).")
            self._publish(replace(plan, state="save_coins", upgrade_id=None, item=None, category=None,
                                  price=None, reason=reason))
```

`LabCoinJar.settle` already zeroes a stale jar whenever the mode isn't `save_pct`. Don't call it in `just_in_time` mode: the persisted jar may hold a leftover amount, and the next `save_pct` visit clears it.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_coin_share_jit.py tests/test_coin_share.py tests/test_reroll_progress.py -q -p no:allure_pytest`
Expected: PASS. Use the matching file names if they differ: `ls tests | grep -i "coin_share\|reroll_progress"`.

- [ ] **Step 5: Commit**

```bash
git add fleet/coin_share.py fleet/reroll_progress.py tests/test_coin_share_jit.py
git commit -m "Hold the just-in-time lab reserve back from Workshop

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Studio: read-only ranked list and the saving rules

**Files:**
- Modify: `web/ui/lib/labs.ts`: `LabBlock` union, `LabShareMode`, `RouteRules`, `DEFAULT_RULES`, `SlotPlan`, `LabPlan`, new `SavingPlan`, and `splitPreview`
- Create: `web/ui/app/fleet/reroll/strategies/LabListView.tsx`
- Modify: `web/ui/app/fleet/reroll/strategies/StrategyStudio.tsx:291-305`: render `LabListView` for a `lab_list` lane
- Modify: `web/ui/app/fleet/reroll/strategies/StrategyRules.tsx`: just-in-time option, windows and filler fields
- Test: `web/ui/app/fleet/reroll/strategies/LabListView.test.tsx` (create) and `StrategyRules.test.tsx` (extend)

**Interfaces:**
- **Consumes:** the API row `plan.saving`, `plan.slots[].role` and `plan.slots[].saving_for` (Tasks 5–7).
- **Produces:**
  - `isLabList(labs: BuildRouteDocument["baseline"]["labs"]): boolean`
  - the component `LabListView`
  - `splitPreview(rules, wallet, jar, price, saving?)`

- [ ] **Step 1: Write the failing tests**

```tsx
// web/ui/app/fleet/reroll/strategies/LabListView.test.tsx
import { render, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { LabsReference, LabsRow } from "@/lib/labs";
import { isLabList } from "@/lib/labs";
import { LabListView } from "./LabListView";

type Labs = BuildRouteDocument["baseline"]["labs"];
const labs: Labs = { slot1_research: "game_speed", steps: [], mode: "blocks", blocks: [
  { id: "labs.list", type: "lab_list", entries: [
    { id: "gs", lab_id: "labs.game-speed", to_level: 7, tier: "S+", pin_slot: 1 },
    { id: "ckb", lab_id: "labs.coins-kill-bonus", to_level: 30, tier: "A" }] }] };
const reference = { labs: [{ id: "labs.game-speed", name: "Game Speed", max_level: 7, priced: true },
  { id: "labs.coins-kill-bonus", name: "Coins / Kill Bonus", max_level: 99, priced: true }],
  game_speed: [], lab_slots: [], card_slots: [], card_gems: 20, labs_unlock_wave: 30, sources: [] } as LabsReference;
const now = { state: "idle", level: null, completes_at: null, overdue_seconds: null, read_at: null, stale: false } as const;
const observed = { worker: "w1", account_id: "a", strategy_name: null, read_at: null, wallet: { coins: 20000, gems: 0 },
  state: "ok", reason: null, recent: [], plan: { wallet_coins: 20000, jar: 0, gems: { wallet: 0, next: null, price: null,
    have: null, need: null, automated: false, why: [], steps: [] },
    saving: { reserve: 18650, workshop_budget: 0, wallet: 18650, coins_per_hour: 7500, why: ["Slot 1 Game Speed L4: needs 50k, due in 4.2h"],
      targets: [{ slot: 1, lab_id: "labs.game-speed", name: "Game Speed", level: 4, price: 50000, needed_at: 0, ready_at: 0, covered: true }] },
    slots: [{ slot: 1, now, covered: true, automated: false, why: [], note: "Start manually", role: "filler",
      next: { lab_id: "labs.coins-kill-bonus", name: "Coins / Kill Bonus", level: 6, price: 1350, seconds: 4800 },
      saving_for: { lab_id: "labs.game-speed", name: "Game Speed", level: 4, price: 50000, seconds: 122520 } }] } } as unknown as LabsRow;

test("detects a ranked list lane", () => {
  expect(isLabList(labs)).toBe(true);
});

test("shows ranked entries with tier and pin", () => {
  render(<LabListView labs={labs} reference={reference} observed={null} />);
  const rows = screen.getAllByRole("row");
  expect(within(rows[1]).getByText("Game Speed")).toBeInTheDocument();
  expect(within(rows[1]).getByText("Slot 1")).toBeInTheDocument();
  expect(within(rows[2]).getByText("A")).toBeInTheDocument();
  expect(screen.getByText(/editing arrives with the planner/i)).toBeInTheDocument();
});

test("shows a filler slot with what it saves for and the saving line", () => {
  render(<LabListView labs={labs} reference={reference} observed={observed} />);
  expect(screen.getByText(/Coins \/ Kill Bonus L6 · filler/)).toBeInTheDocument();
  expect(screen.getByText(/saving for Game Speed L4/)).toBeInTheDocument();
  expect(screen.getByText(/Start manually/)).toBeInTheDocument();
  expect(screen.getByText(/needs 50k, due in 4.2h/)).toBeInTheDocument();
});
```

Append to `StrategyRules.test.tsx`, following the file's existing render helper and `onChange` spy pattern:

```tsx
test("just in time shows save windows and filler fields", () => {
  const onChange = vi.fn();
  const rules = { ...DEFAULT_RULES, coins: { ...DEFAULT_RULES.coins, lab_share: { mode: "just_in_time" as const, pct: 25 } } };
  render(<StrategyRules rules={rules} locked={false} rows={[]} onChange={onChange} />);
  fireEvent.change(screen.getByLabelText("Save window S+ (hours)"), { target: { value: "96" } });
  expect(onChange).toHaveBeenLastCalledWith({ ...rules, labs: { ...rules.labs,
    saving: { ...rules.labs.saving, window_hours: { ...rules.labs.saving.window_hours, "S+": 96 } } } });
  expect(screen.getByLabelText("Filler max price (% of wallet)")).toHaveValue(10);
  expect(screen.getByLabelText("Filler minimum length (hours)")).toHaveValue(1);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd web/ui && npx vitest run app/fleet/reroll/strategies/LabListView.test.tsx app/fleet/reroll/strategies/StrategyRules.test.tsx`
Expected: FAIL. `./LabListView` can't be resolved, and the rules test can't find its labels.

- [ ] **Step 3: Implement the types in `web/ui/lib/labs.ts`**

```ts
export type LabTier = "S+" | "S" | "A" | "B" | "C";
export type LabListEntry = { id: string; lab_id: string; to_level: number; tier: LabTier; pin_slot?: number; label?: string };
// add to the LabBlock union:
//   | (Base & { type: "lab_list"; entries: LabListEntry[] })
export type LabShareMode = "when_affordable" | "save_pct" | "labs_first" | "just_in_time";
// RouteRules.labs gains:
//   saving: { income_margin_pct: number; window_hours: Record<LabTier, number> };
//   filler: { enabled: boolean; max_price_pct_of_wallet: number; min_hours: number };
// DEFAULT_RULES.labs gains (mirrors LabRules() in fleet/build_route.py):
//   saving: { income_margin_pct: 75, window_hours: { "S+": 72, S: 24, A: 12, B: 4, C: 0 } },
//   filler: { enabled: true, max_price_pct_of_wallet: 10, min_hours: 1 },
export type SavingTarget = { slot: number; lab_id: string; name: string; level: number | null; price: number;
  needed_at: number | null; ready_at: number | null; covered: boolean | null };
export type SavingPlan = { reserve: number | null; workshop_budget: number; wallet: number | null;
  coins_per_hour: number | null; targets: SavingTarget[]; why: string[] };
// SlotPlan gains: role?: "target" | "filler"; saving_for?: SlotNext | null;
// LabPlan gains:  saving?: SavingPlan | null;
export const isLabList = (labs: BuildRouteDocument["baseline"]["labs"]): boolean =>
  labs.mode === "blocks" && labs.blocks.length === 1 && labs.blocks[0]?.type === "lab_list";
```

Change `splitPreview` to take an optional fifth parameter, `saving?: SavingPlan | null`. At the top, after the `wallet === null` guard:

```ts
  if (rules.coins.lab_share.mode === "just_in_time") {
    if (!saving || saving.reserve === null) return { jar: 0, workshop: 0, price, progress: null, paused: true };
    const first = saving.targets[0]?.price ?? null;
    return { jar: saving.reserve, workshop: saving.workshop_budget, price: first,
      progress: first ? Math.min(1, saving.reserve / first) : null, paused: saving.reserve > 0 && saving.reserve >= wallet };
  }
```

Type the rest of the file (`ResourceBlocks.tsx`, `labSlotDraft.ts`) so the `LabBlock` union change compiles. Wherever they switch on `block.type`, a `lab_list` block falls through to their existing unknown or default case. Run `npx tsc --noEmit` in Step 5 to catch any gaps.

- [ ] **Step 4: Implement the components**

```tsx
// web/ui/app/fleet/reroll/strategies/LabListView.tsx
"use client";

import type { BuildRouteDocument } from "@/lib/buildRoute";
import type { LabListEntry, LabsReference, LabsRow } from "@/lib/labs";

export type LabListViewProps = {
  labs: BuildRouteDocument["baseline"]["labs"];
  reference: LabsReference | null;
  observed: LabsRow | null;
};

export function LabListView({ labs, reference, observed }: LabListViewProps): React.JSX.Element {
  const block = labs.blocks[0];
  const entries: LabListEntry[] = block?.type === "lab_list" ? block.entries : [];
  const name = (id: string): string => reference?.labs.find(lab => lab.id === id)?.name ?? id;
  const plan = observed?.plan ?? null;
  return <div className="min-w-0 space-y-4">
    <p role="note" className="text-xs text-muted-foreground">Ranked list: editing arrives with the planner. Slot pins keep a lab in its slot; every other slot takes the highest-ranked lab it can run, and runs a short, cheap filler while saving.</p>
    <table className="w-full min-w-0 table-fixed text-sm">
      <thead><tr><th className="w-10 text-left">#</th><th className="text-left">Lab</th><th className="w-14 text-left">To</th><th className="w-12 text-left">Tier</th><th className="w-16 text-left">Pin</th></tr></thead>
      <tbody>{entries.map((entry, index) => <tr key={entry.id}>
        <td>{index + 1}</td><td className="truncate">{name(entry.lab_id)}</td><td>{entry.to_level}</td>
        <td>{entry.tier}</td><td>{entry.pin_slot ? `Slot ${entry.pin_slot}` : ""}</td></tr>)}</tbody>
    </table>
    {plan && <section aria-label="Current picks" className="space-y-2">
      {plan.slots.map(slot => <div key={slot.slot} className="min-w-0 rounded-lg border border-border p-2 text-xs">
        <div className="font-semibold">Lab {slot.slot}</div>
        {slot.next ? <div>{slot.next.name} L{slot.next.level ?? "?"} · {slot.role === "filler" ? "filler" : "target"}
          {slot.saving_for && <span> · saving for {slot.saving_for.name} L{slot.saving_for.level ?? "?"}</span>}
          {slot.note && <span> · {slot.note}</span>}</div> : <div className="text-muted-foreground">{slot.why.at(-1) ?? "No pick"}</div>}
      </div>)}
      {plan.saving && <ul aria-label="Saving" className="list-disc pl-5 text-xs">{plan.saving.why.map(line => <li key={line}>{line}</li>)}</ul>}
    </section>}
  </div>;
}
```

In `StrategyStudio.tsx`, inside the `lane === "labs"` branch, keep the account-observation `<select>`. Then render one of two things:
- `isLabList(strategy.baseline.labs)`: `<LabListView labs={strategy.baseline.labs} reference={labsSnapshot?.reference ?? null} observed={observedLabs} />`;
- otherwise: the existing `<LabSlotPlanner …/>` plus the "Advanced lab blocks" `<details>`.

Import `LabListView` and `isLabList`.

In `StrategyRules.tsx`:
- Add `<option value="just_in_time">Just in time (save only what the next lab needs)</option>` to the lab share `<select>`.
- Pass the saving plan to the preview: `splitPreview(rules, row.wallet.coins, row.plan?.jar ?? 0, price, row.plan?.saving ?? null)`.
- When `coins.lab_share.mode === "just_in_time"`, render these fields in the same `field`/`input` classes as the existing ones:
  - one number input per tier, labelled `Save window ${tier} (hours)`, `min={0} max={168}`, bound to `labs.saving.window_hours[tier]`;
  - `Income safety margin (%)`, `min={50} max={100}`, bound to `labs.saving.income_margin_pct`;
  - a checkbox `Run fillers while saving`, bound to `labs.filler.enabled`;
  - `Filler max price (% of wallet)`, `min={1} max={100}`, bound to `labs.filler.max_price_pct_of_wallet`;
  - `Filler minimum length (hours)`, `min={0.25} max={24} step={0.25}`, bound to `labs.filler.min_hours`.

  Each input's `onChange` must call `set({ ...rules, labs: { ...labs, saving: {...} } })` (or `filler`), shaped exactly as the test expects.
- Mark these fields `<Tag live />`, since the worker uses the reserve (Task 10).

- [ ] **Step 5: Run the tests and type-check**

Run: `cd web/ui && npx vitest run app/fleet/reroll/strategies/LabListView.test.tsx app/fleet/reroll/strategies/StrategyRules.test.tsx app/fleet/reroll/strategies/StrategyStudio.test.tsx app/fleet/reroll/strategies/LabSlotPlanner.test.tsx && npx tsc --noEmit`
Expected: PASS, with no type errors.

- [ ] **Step 6: Commit**

```bash
git add web/ui/lib/labs.ts web/ui/app/fleet/reroll/strategies/LabListView.tsx web/ui/app/fleet/reroll/strategies/LabListView.test.tsx web/ui/app/fleet/reroll/strategies/StrategyStudio.tsx web/ui/app/fleet/reroll/strategies/StrategyRules.tsx web/ui/app/fleet/reroll/strategies/StrategyRules.test.tsx
git commit -m "Show the ranked lab list and just-in-time rules in the Studio

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Spec coverage check

| Spec section | Task |
|---|---|
| 1. Catalog v2 (tables, unlock lists, header matching, zero seconds, optional value) | 1, 2 |
| 2. `lab_list` block (entries, repeated labs rising, pins, Game Speed first) | 4 |
| 3. Evaluation (pins incl. locked fall-through, ranked walk and skips, nothing left, fillers, role/saving_for, Start manually, coins between slots, unlock facts) | 5, 6, 9 |
| 4. Saving (income margin, starts-now paid first, targets by needed_at, hours to afford, windows incl. unknown income, reserve, budget, unread wallet, covered) | 5, 7 |
| 5. Rules (`just_in_time`, `labs.saving`, `labs.filler`, pairing check) | 3 |
| 6. Template (19 rows, exclusions, unpriced labs dropped) | 8 |
| 7. Wiring (facts, worker reserve, jar untouched, lab start unchanged, preview and Labs page) | 9, 10 |
| 8. Studio read-only view and rule fields | 11 |
| Acceptance: `slot_track` strategies behave as before | 5 (`test_slot_track_strategies_are_untouched`), 3, 10 |
