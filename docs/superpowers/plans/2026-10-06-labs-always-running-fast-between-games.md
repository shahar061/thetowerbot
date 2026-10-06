# Labs Always Running + Fast Between Games — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every owned lab slot always has research running (saving for Game Speed with cheap fillers meanwhile), and the time from a run ending to the next run starting drops from a median of ~70 s to 20–30 s.

**Architecture:**
- The ranked `lab_list` planner (`fleet/lab_list.py`) gains a last-resort filler, so an idle slot is never left uncovered while any eligible lab is affordable.
- A migration tool converts every fleet strategy to a ranked lab list with just-in-time saving and `direct_start`.
- In the bot loop (`tower_bot.py`), a single `_lab_start_due` predicate drives the HOME-vs-RETRY decision, the Labs-before-Workshop order on the main menu, and the end of the busy-slot visit loop.
- `LabVisit` fills every idle slot in one visit.
- A between-games timing profile shortens the menu scan interval, the screen-confirmation gap and the navigation cooldown, without touching battle timing.

**Tech Stack:** Python 3 (uv), pytest, SQLite (read-only for the report), the coordinator HTTP API on 127.0.0.1:8765 (stdlib `urllib`).

**Spec:** `docs/superpowers/specs/2026-10-06-labs-always-running-fast-between-games-design.md`

## Global Constraints

- **Running tests:**
  - Run tests only per file or per test: `uv run pytest tests/<file>.py -q -p no:allure_pytest` (add `-k <name>` when useful).
  - **Never** run the whole suite or a whole directory.
  - The repo has no CI, so the per-file runs listed in each task are the gate.
- **Commits:**
  - Commit per task.
  - End every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - `docs/` is gitignored, so add files under it with `git add -f`.
- **Code style:** type hints on all new functions; match the surrounding code's comment density and naming.
- **Labs and saving:**
  - Labs are filled before the Workshop spends.
  - S+ labs save; others don't.
  - A slot stays idle **only** if no eligible lab is affordable.
  - Never Black Hole Damage, never the bot-cooldown labs. Neither is in the template list; the migration must not add them.
- **Lab starts:**
  - Use `direct_start: true`.
  - Every per-start safety check stays: two identical reads, title/price match on the confirm dialog, the Rush-proximity refusal, the journal intent before the tap, a proven debit and research state, and `lab_start_uncertain` halting.
- **Timing:**
  - Battle timing is unchanged.
  - The between-games values live in `config.py`: `MENU_FAST_PROFILE = True`, `MENU_FAST_SCAN_SECONDS = 0.6`, `MENU_CONFIRM_GAP_SECONDS = 0.3`, `MENU_FAST_NAV_COOLDOWN_SECONDS = 1.0`, `MENU_IDLE_WATCHDOG_SECONDS = 20.0`.
- **Picker skip:** after 2 picker failures for the same lab, skip that lab for **1800 s**.
- **Live fleet:** the coordinator API and worker DBs are live. Tasks never write to them, except Task 10, which needs the user's explicit go-ahead first.

## Review Focus

1. **A restart mid-visit, after the first of two starts.** The journal must reconcile the first start, and the second slot must be planned fresh, never double-tapped. *Test: Task 5, `test_second_start_waits_for_first_proof`.*
2. **The wallet is unread at GAME_OVER (`wallet_coins=None`).** The last-resort filler must not fire, and `_lab_start_due` must still answer from slot state alone. *Tests: Task 1, `test_last_resort_needs_a_read_wallet`; Task 3, `test_lab_start_due_ignores_wallet`.*
3. **A slot timer is unknown (`expected_finish is None`) while researching.** It is treated as due, so the bot looks once, then is paced by the 300 s check rather than looping every frame. *Test: Task 3, `test_unknown_finish_is_due_once_then_paced`.*
4. **A migrated strategy fails server validation.** For example, a repeated lab with a lower-or-equal level, or a lab without a price table. The tool must report and skip that strategy, not crash halfway with later strategies unsaved. *Test: Task 2, `test_flatten_drops_non_increasing_repeats_and_unpriced_labs`.*
5. **The between-games profile leaks into battle** (e.g. an UNKNOWN frame with cash at top-left during a run). The battle interval must be used. *Test: Task 7, `test_fast_profile_never_applies_in_battle_context`.*

---

### Task 1: Last-resort filler in the lab-list planner

**Files:**
- Modify: `fleet/lab_list.py` (`_evaluate_slots` lines ~258-292; new helper next to `_filler` at ~203-227)
- Test: `tests/test_lab_list_eval.py`

**Interfaces:**
- Produces: `_last_resort(slot, entries, facts, ctx, elsewhere, claimed, wallet, target) -> tuple[SlotNext | None, int | None]`, returning the pick and the cheapest price seen.
- Produces: a plan with `role == "filler"`, `covered is True` and `why` containing `"last-resort filler: keeps the slot busy"`. No change is needed in `choose_lab_action`, because a `role != "target"` keeps `covered` from line 288.

**Background:**
- In `_evaluate_slots`, an idle slot whose target is unaffordable runs step 4 (`_filler`).
- When `_filler` returns None, the `why` gets `"No filler fits the price cap and the gap"`, `next_` stays the unaffordable target, and `covered=False`, so the slot stays idle.
- Step 5 runs in exactly that case, and also when `target is None` on an idle slot.
- Step 5 requires `rules.labs.filler.enabled` and `wallet is not None`.
- The test helpers `Route`, `facts`, `evaluate`, `FILLER_RULES` and `NOW` already exist in `tests/test_lab_list_eval.py` (lines 10-60, 238-246).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_lab_list_eval.py`)

```python
def test_last_resort_filler_takes_cheapest_affordable_when_no_filler_fits() -> None:
    # Wallet 49,000: Game Speed L4 (50,000) is unaffordable. The 10% cap is 4,900, but every
    # capped entry is longer than the gap, so step 4 finds nothing. Step 5 must pick the
    # cheapest affordable entry instead of leaving slot 1 idle.
    rules = {"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}},
             "labs": {"filler": {"enabled": True, "max_price_pct_of_wallet": 10, "min_hours": 0.001}}}
    plans, savings, _ = evaluate(Route(rules=rules), wallet_coins=49_000, available_coins=49_000,
                                 coins_per_hour=10_000_000.0)
    slot1 = plans[0]
    assert slot1.role == "filler" and slot1.covered is True
    assert slot1.saving_for is not None and slot1.saving_for.lab_id == "labs.game-speed"
    assert "last-resort filler: keeps the slot busy" in slot1.why
    cheapest = min(
        (lab_catalog.level(lab, lvl + 1).coins, lab)
        for lab, lvl in (("labs.coins-wave", 2), ("labs.coins-kill-bonus", 5)))
    assert slot1.next.lab_id == cheapest[1]
    assert any(s.slot == 1 and s.target.lab_id == "labs.game-speed" for s in savings)


def test_last_resort_reports_cheapest_price_when_nothing_affordable() -> None:
    # Game Speed is maxed and the 2,000 wallet buys nothing on the list.
    plans, _, _ = evaluate(Route(rules=FILLER_RULES), wallet_coins=100, available_coins=100,
                           completed_levels={"labs.game-speed": 7, "labs.labs-speed": 10,
                                             "labs.coins-wave": 9, "labs.coins-kill-bonus": 5})
    slot1 = plans[0]
    assert slot1.covered is not True
    assert any(line.startswith("Nothing affordable: cheapest is ") for line in slot1.why)


def test_last_resort_needs_a_read_wallet() -> None:
    plans, _, _ = evaluate(Route(rules=FILLER_RULES), wallet_coins=None, available_coins=None)
    assert all("last-resort filler: keeps the slot busy" not in p.why for p in plans)


def test_last_resort_respects_fillers_off() -> None:
    rules = {"coins": {"lab_share": {"mode": "just_in_time", "pct": 25}},
             "labs": {"filler": {"enabled": False}}}
    plans, _, _ = evaluate(Route(rules=rules), wallet_coins=49_000, available_coins=49_000)
    assert plans[0].covered is False and "Fillers off" in plans[0].why
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lab_list_eval.py -q -p no:allure_pytest -k last_resort`
Expected: the first two tests FAIL, because the `why` line is missing or `covered` is False. The other two may pass already; that's fine, they are guards.

- [ ] **Step 3: Implement**

Add next to `_filler` in `fleet/lab_list.py`:

```python
def _last_resort(slot: int, entries: list[Mapping[str, Any]], facts: LabFacts, ctx: SlotContext,
                 elsewhere: set[str], claimed: set[str], wallet: int,
                 target: SlotNext | None) -> tuple[SlotNext | None, int | None]:
    """Step 5: the cheapest affordable entry, so an idle slot never waits empty.

    Ties go to the shorter research, then to rank. Returns the pick and the cheapest
    price seen among eligible entries (for the "nothing affordable" line).
    """
    best: tuple[tuple[int, float, int], SlotNext] | None = None
    cheapest: int | None = None
    for rank, entry in enumerate(entries):
        if target is not None and entry["lab_id"] == target.lab_id:
            continue
        option, _ = _candidate(entry, slot, facts, ctx, elsewhere, claimed)
        if option is None:
            continue
        cheapest = option.price if cheapest is None else min(cheapest, option.price)
        if option.price > wallet:
            continue
        key = (option.price, float("inf") if option.seconds is None else option.seconds, rank)
        if best is None or key < best[0]:
            best = (key, option)
    return (best[1] if best else None), cheapest
```

In `_evaluate_slots`, replace the step-4 block (lines ~265-280) with:

```python
        if idle and not affordable:
            filler = None
            if not rules.labs.filler.enabled:
                why.append("Fillers off")
            elif wallet is None:
                why.append("No filler: wallet unread")
            else:
                filler = _filler(slot, entries, facts, ctx, elsewhere, claimed, wallet, target,
                                 rate, rules.labs.filler)
                if filler is None:
                    why.append("No filler fits the price cap and the gap")
                    filler, cheapest = _last_resort(slot, entries, facts, ctx, elsewhere, claimed,
                                                    wallet, target)
                    if filler is not None:
                        why.append("last-resort filler: keeps the slot busy")
                    elif cheapest is not None:
                        why.append(f"Nothing affordable: cheapest is {cheapest:,} coins")
                    else:
                        why.append("Nothing affordable: no eligible lab")
            if filler is not None:
                next_, role, saving_for = filler, "filler", target
                needed_at = facts.now + (filler.seconds or 0)
                why.append(f"Filler {filler.name} L{filler.level} while saving"
                           + (f" for {target.name} L{target.level}" if target else ""))
```

- [ ] **Step 4: Run the tests to verify they pass, then re-run the whole file**

Run: `uv run pytest tests/test_lab_list_eval.py -q -p no:allure_pytest`
Expected: all pass.

Some existing tests assert that `why` *ends* with `"No filler fits the price cap and the gap"` or `"Fillers off"`: `test_filler_longer_than_gap_is_refused` (277-289), `test_slot_without_a_target_says_no_filler_fits` (307-316), `test_unknown_income_caps_filler_at_min_hours` (292-304) and `test_no_survivor_names_the_most_common_skip_reason` (210-222).
- Update those assertions to `in plan.why` membership checks.
- Where step 5 now picks a filler (e.g. the `fast` case at 277-289), assert the new last-resort outcome instead.
- Do **not** weaken the price-cap and gap assertions of step 4 itself.

Then run: `uv run pytest tests/test_lab_saving.py tests/test_labs_view.py -q -p no:allure_pytest`. Expected: pass. Fix assertions only where they encoded "idle slot stays idle".

- [ ] **Step 5: Commit**

```bash
git add fleet/lab_list.py tests/test_lab_list_eval.py tests/test_lab_saving.py tests/test_labs_view.py
git commit -m "Never leave an idle lab slot empty: last-resort cheapest filler

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Strategy migration tool and the `direct_start` template default

**Files:**
- Modify: `fleet/resource_blocks.py:432-442` (`template_lab_list_rules`: add `"direct_start": True` to `"labs"`)
- Create: `tools/migrate_lab_lists.py`
- Test: `tests/test_migrate_lab_lists.py`

**Interfaces:**
- Produces:
  - `flatten_slot_tracks(blocks: list[dict]) -> tuple[list[dict], list[str]]`, returning (lab_list entries, notes on what was dropped)
  - `migrate_baseline(baseline: dict) -> tuple[dict, list[str]]`, returning (new baseline, notes)
  - `main(argv) -> int`, CLI: `uv run python tools/migrate_lab_lists.py [--dry-run] [--port 8765]`

**Background (verified against the code):**
- `GET /api/fleet/reroll/strategies` returns `{"revision", "templates", "strategies": [{id, name, version, source_template, baseline, ...}]}`.
- `POST /api/fleet/reroll/strategies` takes the strict body `{expected_revision: int, name, source_template, baseline, strategy_id}`.
  - Each save returns the new full library, **including the new `revision`**. Chain it into the next save; reusing the first revision gives 409 from the second save on.
  - Validation errors return 422 with the message.
  - Saving does **not** publish to workers. Assignment is separate (Task 10).
- `RouteBaseline.from_dict` rejects `lab_share.mode == "just_in_time"` unless labs is a single `lab_list` block. `validate_lab_list`, at `fleet/lab_list.py:31-63`, enforces:
  - the first entry is Game Speed with `pin_slot: 1`
  - unique ids
  - tier in S+/S/A/B/C
  - `to_level` within 1..max
  - a repeated lab needs a strictly higher `to_level` further down
- The current fleet strategies:
  - Steps-mode: `blender`, Turtle Discord Guide, Turtle Eco Wall, Turtle Opening. These get `template_lab_list()`.
  - Already a lab_list: McBlue Practical Guide. Rules only.
  - `slot_track` blocks: Blender Road, Survivor Ladder, Health & Damage Road, Eco Farmer. These get flattened.
- Slot-track block shapes:
  - `{"type": "slot_track", "slots": [1], "children": [...]}`
  - children `{"type": "research", "lab_id", "to_level"}`
  - children `{"type": "lab_pool", "lab_ids": [...], "caps": {lab: level}?, "max_seconds"?}`
  - children `{"type": "condition", "then": [...], "else": [...]}`
- Leave `coins.workshop_spend_limit_pct` and `gems.*` untouched.

**Flattening rules:**
1. Walk the tracks in block order. For a track with exactly one slot `s`, each `research` child (also inside a condition's `then`/`else`) becomes `{lab_id, to_level, pin_slot: s}`. In a multi-slot track, research children are unpinned.
2. Each `lab_pool` lab becomes an unpinned entry with `to_level = caps.get(lab, catalog max level)`.
3. Order:
   - (a) every pinned research for slot 1, in order (Game Speed first)
   - (b) pinned research for slot 2
   - (c) then all other entries in the order first seen
4. Tier: use the tier of the same `lab_id` in `template_lab_list()`, else `"B"`. Game Speed is always `"S+"`.
5. Repeats:
   - Keep the first occurrence of `(lab_id)`.
   - A later occurrence is kept only if its `to_level` is strictly higher **and** it is not pinned to a different slot than an earlier one. Otherwise drop it, with a note.
6. Drop, with a note, any lab without a price table (`lab_catalog.lab(id) is None or .levels is None`), and any lab id containing `black-hole` or `cooldown`.
7. Ids: `f"migrated.{lab_id.removeprefix('labs.')}.{to_level}"`.
8. If Game Speed is not first after ordering, insert `{lab_id: labs.game-speed, to_level: 7, tier: S+, pin_slot: 1}` at the front.
9. Wrap the result as `[{"id": "labs.list", "type": "lab_list", "label": "Migrated ranked list", "entries": entries}]` and pass it through `fleet.lab_list.validate_lab_list`.

**`migrate_baseline` sets:**
- `labs.mode = "blocks"`
- `labs.blocks`:
  - steps → `list(template_lab_list())`
  - slot_track → flattened
  - lab_list → unchanged
- `rules.coins.lab_share = {"mode": "just_in_time", "pct": 25}`
- `rules.labs.filler.enabled = True`
- `rules.labs.direct_start = True`
- `rules.labs.auto_start = True`

- [ ] **Step 1: Write the failing tests** (`tests/test_migrate_lab_lists.py`)

```python
from __future__ import annotations

import copy
from typing import Any

from fleet.build_route import RouteBaseline, RouteDocument
from fleet.resource_blocks import template_lab_list
from tools import migrate_lab_lists as m


def _baseline(labs: dict[str, Any]) -> dict[str, Any]:
    raw = RouteDocument.compatibility().to_dict()["baseline"]
    raw["labs"] = labs
    return raw


STEPS = {"slot1_research": "game_speed", "steps": ["research_game_speed"], "mode": "steps", "blocks": []}

TRACKS = [
    {"id": "t1", "type": "slot_track", "slots": [1], "on_blocked": "skip", "children": [
        {"id": "t1.gs", "type": "research", "lab_id": "labs.game-speed", "to_level": 7},
        {"id": "t1.pool", "type": "lab_pool", "lab_ids": ["labs.coins-kill-bonus", "labs.cash-bonus"],
         "selection": "cheapest", "caps": {"labs.coins-kill-bonus": 30}},
        {"id": "t1.c", "type": "condition", "field": "game_speed_maxed", "cmp": "eq", "value": 1,
         "then": [{"id": "t1.as", "type": "research", "lab_id": "labs.attack-speed", "to_level": 50}],
         "else": []}]},
    {"id": "t2", "type": "slot_track", "slots": [2], "on_blocked": "skip", "children": [
        {"id": "t2.ls", "type": "research", "lab_id": "labs.labs-speed", "to_level": 99}]},
    {"id": "t3", "type": "slot_track", "slots": [3, 4], "on_blocked": "skip", "children": [
        {"id": "t3.cw", "type": "research", "lab_id": "labs.coins-wave", "to_level": 20},
        {"id": "t3.dup", "type": "research", "lab_id": "labs.coins-kill-bonus", "to_level": 10}]},
]


def test_steps_strategy_gets_template_list_and_rules() -> None:
    new, notes = m.migrate_baseline(_baseline(STEPS))
    assert new["labs"]["mode"] == "blocks"
    assert new["labs"]["blocks"] == list(template_lab_list())
    assert new["rules"]["coins"]["lab_share"] == {"mode": "just_in_time", "pct": 25}
    assert new["rules"]["labs"]["direct_start"] is True
    assert new["rules"]["labs"]["filler"]["enabled"] is True
    RouteBaseline.from_dict(new)  # passes server-side validation


def test_slot_tracks_flatten_with_pins_order_and_caps() -> None:
    entries, notes = m.flatten_slot_tracks(copy.deepcopy(TRACKS))
    ids = [(e["lab_id"], e["to_level"], e.get("pin_slot")) for e in entries]
    assert ids[:3] == [("labs.game-speed", 7, 1), ("labs.attack-speed", 50, 1), ("labs.labs-speed", 99, 2)]
    assert ("labs.coins-kill-bonus", 30, None) in ids
    assert ("labs.coins-wave", 20, None) in ids
    # Coins/Kill L10 after L30 is not a higher level: dropped with a note.
    assert ("labs.coins-kill-bonus", 10, None) not in ids
    assert any("coins-kill-bonus" in n for n in notes)
    assert entries[0]["tier"] == "S+"


def test_slot_track_strategy_migrates_and_validates() -> None:
    new, _ = m.migrate_baseline(_baseline({"slot1_research": "game_speed", "steps": ["research_game_speed"],
                                           "mode": "blocks", "blocks": copy.deepcopy(TRACKS)}))
    assert new["labs"]["blocks"][0]["type"] == "lab_list"
    RouteBaseline.from_dict(new)


def test_existing_lab_list_keeps_entries() -> None:
    blocks = list(template_lab_list())
    new, _ = m.migrate_baseline(_baseline({"slot1_research": "game_speed", "steps": ["research_game_speed"],
                                           "mode": "blocks", "blocks": copy.deepcopy(blocks)}))
    assert new["labs"]["blocks"] == blocks
    assert new["rules"]["labs"]["direct_start"] is True


def test_flatten_drops_non_increasing_repeats_and_unpriced_labs(monkeypatch) -> None:
    from fleet import resource_blocks
    tracks = copy.deepcopy(TRACKS)
    tracks[2]["children"].append({"id": "t3.bh", "type": "research", "lab_id": "labs.black-hole-damage",
                                  "to_level": 1})
    entries, notes = m.flatten_slot_tracks(tracks)
    assert all("black-hole" not in e["lab_id"] for e in entries)
    assert any("black-hole" in n for n in notes)


def test_rules_leave_workshop_and_gem_limits_alone() -> None:
    base = _baseline(STEPS)
    base["rules"]["coins"]["workshop_spend_limit_pct"] = 40
    new, _ = m.migrate_baseline(base)
    assert new["rules"]["coins"]["workshop_spend_limit_pct"] == 40
    assert new["rules"]["gems"] == base["rules"]["gems"]


def test_template_rules_default_to_direct_start() -> None:
    from fleet.resource_blocks import template_lab_list_rules
    assert template_lab_list_rules()["labs"]["direct_start"] is True
```

Note: if `labs.black-hole-damage` is not a catalog id, check `catalog/labs.v2.json` for the real Black Hole Damage id (`grep -i "black" catalog/labs.v2.json`) and use it in the test.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_migrate_lab_lists.py -q -p no:allure_pytest`
Expected: FAIL (`ModuleNotFoundError: tools.migrate_lab_lists`).

- [ ] **Step 3: Implement `tools/migrate_lab_lists.py`**

```python
"""Convert every fleet strategy to a ranked lab list with direct, never-idle lab starts.

Usage:
    uv run python tools/migrate_lab_lists.py --dry-run   # print what would change
    uv run python tools/migrate_lab_lists.py             # save each strategy (new version)

Saving creates a new strategy version only; assigning it to workers is a separate step.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab_catalog  # noqa: E402
from fleet.lab_list import validate_lab_list  # noqa: E402
from fleet.resource_blocks import template_lab_list  # noqa: E402

GAME_SPEED = "labs.game-speed"
BANNED = ("black-hole", "cooldown")


def _template_tiers() -> dict[str, str]:
    tiers: dict[str, str] = {}
    for entry in template_lab_list()[0]["entries"]:
        tiers.setdefault(entry["lab_id"], entry["tier"])
    return tiers


def _walk(children: list[dict[str, Any]], pin: int | None,
          pinned: dict[int, list[tuple[str, int]]], rest: list[tuple[str, int]]) -> None:
    for child in children:
        kind = child.get("type")
        if kind == "research":
            item = (child["lab_id"], int(child["to_level"]))
            (pinned.setdefault(pin, []) if pin is not None else rest).append(item)
        elif kind == "lab_pool":
            caps = child.get("caps") or {}
            for lab_id in child.get("lab_ids", []):
                entry = lab_catalog.lab(lab_id)
                top = entry.max_level if entry is not None else 1
                rest.append((lab_id, int(caps.get(lab_id, top))))
        elif kind == "condition":
            _walk(list(child.get("then", [])) + list(child.get("else", [])), pin, pinned, rest)


def flatten_slot_tracks(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Slot tracks to ranked lab-list entries: slot-1 pins, slot-2 pins, then the rest in order."""
    pinned: dict[int, list[tuple[str, int]]] = {}
    rest: list[tuple[str, int]] = []
    for block in blocks:
        if block.get("type") != "slot_track":
            continue
        slots = block.get("slots") or []
        _walk(block.get("children", []), slots[0] if len(slots) == 1 else None, pinned, rest)
    ordered = ([(lab, lvl, 1) for lab, lvl in pinned.get(1, [])]
               + [(lab, lvl, 2) for lab, lvl in pinned.get(2, [])]
               + [(lab, lvl, s) for s in sorted(k for k in pinned if k not in (1, 2))
                  for lab, lvl in pinned[s]]
               + [(lab, lvl, None) for lab, lvl in rest])
    tiers, notes = _template_tiers(), []
    entries: list[dict[str, Any]] = []
    highest: dict[str, tuple[int, int | None]] = {}
    for lab_id, level, pin in ordered:
        if any(word in lab_id for word in BANNED):
            notes.append(f"dropped {lab_id}: never researched (banned)")
            continue
        entry = lab_catalog.lab(lab_id)
        if entry is None or entry.levels is None:
            notes.append(f"dropped {lab_id}: no price table")
            continue
        level = min(level, entry.max_level)
        if lab_id in highest:
            top, top_pin = highest[lab_id]
            if level <= top or (pin is not None and top_pin is not None and pin != top_pin):
                notes.append(f"dropped {lab_id} L{level}: repeats L{top}")
                continue
        highest[lab_id] = (level, pin)
        item = {"id": f"migrated.{lab_id.removeprefix('labs.')}.{level}", "lab_id": lab_id,
                "to_level": level, "tier": "S+" if lab_id == GAME_SPEED else tiers.get(lab_id, "B"),
                "label": entry.name}
        if pin is not None:
            item["pin_slot"] = pin
        entries.append(item)
    if not entries or entries[0]["lab_id"] != GAME_SPEED or entries[0].get("pin_slot") != 1:
        entries = [e for e in entries if e["lab_id"] != GAME_SPEED]
        entries.insert(0, {"id": "migrated.game-speed.7", "lab_id": GAME_SPEED, "to_level": 7,
                           "tier": "S+", "pin_slot": 1, "label": "Game Speed to max"})
        notes.append("put Game Speed first, pinned to slot 1")
    return entries, notes


def migrate_baseline(baseline: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    new = copy.deepcopy(baseline)
    labs, notes = new["labs"], []
    blocks = labs.get("blocks") or []
    if labs.get("mode") != "blocks":
        labs["blocks"] = list(template_lab_list())
        notes.append("steps mode: replaced with the built-in early-game ranked list")
    elif not (len(blocks) == 1 and blocks[0].get("type") == "lab_list"):
        entries, notes = flatten_slot_tracks(blocks)
        labs["blocks"] = [validate_lab_list({"id": "labs.list", "type": "lab_list",
                                             "label": "Migrated ranked list", "entries": entries})]
        notes.insert(0, f"slot tracks: flattened to {len(entries)} ranked entries")
    labs["mode"] = "blocks"
    rules = new["rules"]
    rules["coins"]["lab_share"] = {"mode": "just_in_time", "pct": 25}
    rules["labs"]["filler"]["enabled"] = True
    rules["labs"]["direct_start"] = True
    rules["labs"]["auto_start"] = True
    return new, notes


def _request(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    url = f"http://127.0.0.1:{args.port}/api/fleet/reroll/strategies"
    library = _request(url)
    revision, failed = library["revision"], 0
    for row in library["strategies"]:
        try:
            new, notes = migrate_baseline(row["baseline"])
        except ValueError as exc:
            print(f"SKIP {row['name']}: {exc}")
            failed += 1
            continue
        old_labs, new_labs = row["baseline"]["labs"], new["labs"]
        print(f"== {row['name']} (v{row['version']}): "
              + ("; ".join(notes) or "rules only"))
        if old_labs != new_labs:
            for entry in new_labs["blocks"][0]["entries"]:
                pin = f" [slot {entry['pin_slot']}]" if "pin_slot" in entry else ""
                print(f"   {entry['tier']:>2} {entry['lab_id']} → L{entry['to_level']}{pin}")
        if args.dry_run:
            continue
        try:
            library = _request(url, {"expected_revision": revision, "name": row["name"],
                                     "source_template": row["source_template"],
                                     "baseline": new, "strategy_id": row["id"]})
            revision = library["revision"]
            print(f"   saved (library revision {revision})")
        except urllib.error.HTTPError as exc:
            print(f"   SAVE FAILED {exc.code}: {exc.read().decode()[:300]}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

Also edit `template_lab_list_rules()` in `fleet/resource_blocks.py`: in the `"labs"` dict, add `"direct_start": True,` after `"auto_start": True,`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_migrate_lab_lists.py tests/test_strategy_library.py tests/test_lab_strategy_template.py tests/test_lab_rules.py tests/test_labs_view.py -q -p no:allure_pytest`
Expected: all pass. If `test_labs_view.py` cases change because the template now has direct start (no rehearse), update only those expectations, and state why in the commit body.

- [ ] **Step 5: Read-only smoke test against the live coordinator**

Run: `uv run python tools/migrate_lab_lists.py --dry-run`
Expected:
- One block per strategy.
- `blender` shows the template list.
- The four slot-track strategies show flattened lists starting `S+ labs.game-speed → L7 [slot 1]`.
- No `SKIP` lines.

Paste the output into the task report. **Do not run without `--dry-run`.**

- [ ] **Step 6: Commit**

```bash
git add tools/migrate_lab_lists.py tests/test_migrate_lab_lists.py fleet/resource_blocks.py
git commit -m "Add lab-list migration tool; template strategies start labs directly

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `_lab_start_due` — one predicate for "a lab start is due"

**Files:**
- Modify: `tower_bot.py` (`_direct_lab_visit_due` at ~1607; GAME_OVER `go_home` at ~3053-3081; the main-menu lab arm at ~2996-3000)
- Modify: `fleet/reroll_progress.py:148` (split out `lab_unlock_due`)
- Test: `tests/test_autopilot_loop.py`, `tests/test_reroll_progress.py`

**Interfaces:**
- Produces:
  - `TowerBot._lab_start_due(now: float) -> bool`, which replaces `_direct_lab_visit_due`. Keep `_direct_lab_visit_due = _lab_start_due` as an alias for one release, so other call sites and tests keep working.
  - `RerollProgress.lab_unlock_due(now: float | None = None, *, wallet_gems: int | None = None) -> bool`
- Consumes: `self.lab_runtime.snapshot()` (`slots_owned`, `observed_at`, `slots[*].{slot, state, confirmed, expected_finish}`) and `LAB_DIRECT_CHECK_SECONDS = 300.`

**Behaviour:** `_lab_start_due` is the current `_direct_lab_visit_due` body with two changes:
1. It requires `options.start_research`, but **not** `options.direct_start`.
2. The stale-strip refresh threshold becomes `LAB_STRIP_REFRESH_SECONDS = 6 * 3600.` (spec §3.5 d).
   - The 300 s pacing still applies to the "unknown / not confirmed / finish unknown" cases through `_lab_direct_check`, so a slot with an unknown timer is looked at once, then at most every 300 s.

In `lab_due`, only the research part moves out. Add:

```python
    def lab_unlock_due(self, now: float | None = None, *, wallet_gems: int | None = None) -> bool:
        moment = time.time() if now is None else now
        if not self.lab_unlocked() or self.lab_cadence.backing_off(moment):
            return False
        rules = self.resource_rules()
        slot = self.next_unlock_slot() if rules.gems.auto_unlock_lab_slots else None
        price = lab_catalog.lab_slot_gems(slot) if slot is not None else None
        return price is not None and self.lab_cadence.slot_due(slot, moment, wallet_gems,
                                                                min_gems=price + rules.gems.keep)
```

and make `lab_due` return `self.lab_unlock_due(moment, wallet_gems=wallet_gems) or research`, with `research` unchanged. It stays for legacy steps-mode routes.

**At GAME_OVER** (`go_home=` expression), replace the two lab terms

```python
or (state is screens.ScreenState.GAME_OVER and self.reroll_progress is not None and self.reroll_progress.lab_due())
or lab_direct_due
```

with

```python
or lab_start_due
or (state is screens.ScreenState.GAME_OVER and self.reroll_progress is not None
    and self.reroll_progress.lab_unlock_due())
```

where `lab_start_due = state is screens.ScreenState.GAME_OVER and self._lab_start_due(time.time())`.

**On the main menu**, the `_request_planned_lab_visit(time.time(), lab_notice_due or self.reroll_progress.lab_due(...))` call becomes:

```python
self._request_planned_lab_visit(time.time(), lab_notice_due or self._lab_start_due(time.time())
                                or self.reroll_progress.lab_unlock_due(wallet_gems=menu_gems))
```

This removes the legacy cadence's `wait_running` re-visits from the lab-list path.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_autopilot_loop.py`, next to `test_game_over_detours_for_an_idle_direct_start_lab`)

```python
def _due_bot(states, finishes, *, direct_start=False, observed_at=None):
    from tests.conftest import _shopping_bot
    from lab_plan import LabVisitOptions

    class Progress:
        def lab_visit_options(self) -> LabVisitOptions:
            return LabVisitOptions(direct_start=direct_start)
        def lab_unlocked(self) -> bool:
            return True
        def lab_due(self, *a, **k) -> bool:
            return False
        def lab_unlock_due(self, *a, **k) -> bool:
            return False
        def stats_due(self) -> bool:
            return False

    bot = _shopping_bot('game_over', state=screens.ScreenState.GAME_OVER,
                        policy=Shopping(), auto_navigate=True)
    bot.reroll_progress = Progress()
    now = time.time()
    snapshot = SimpleNamespace(
        slots_owned=len(states), observed_at=now if observed_at is None else observed_at,
        slots=tuple(SimpleNamespace(state=s, confirmed=True, slot=i, expected_finish=f)
                    for i, (s, f) in enumerate(zip(states, finishes), 1)))
    bot.lab_runtime = SimpleNamespace(snapshot=lambda: snapshot)
    return bot, now


def test_lab_start_due_without_direct_start_for_an_idle_slot() -> None:
    bot, now = _due_bot(('researching', 'idle'), (time.time() + 3600, None))
    assert bot._lab_start_due(now) is True


def test_lab_start_due_false_while_every_slot_runs_with_known_finish() -> None:
    bot, now = _due_bot(('researching', 'researching'), (time.time() + 3600, time.time() + 7200))
    assert bot._lab_start_due(now) is False


def test_lab_start_due_when_a_timer_has_ended() -> None:
    bot, now = _due_bot(('researching', 'researching'), (time.time() - 1, time.time() + 7200))
    assert bot._lab_start_due(now) is True


def test_unknown_finish_is_due_once_then_paced() -> None:
    bot, now = _due_bot(('researching',), (None,))
    assert bot._lab_start_due(now) is True
    assert bot._lab_start_due(now + 10) is False
    assert bot._lab_start_due(now + 301) is True


def test_lab_start_due_ignores_wallet() -> None:
    # No wallet input at all: the answer comes from slot state only.
    bot, now = _due_bot(('idle',), (None,))
    assert bot._lab_start_due(now) is True


def test_stale_strip_refreshes_after_six_hours_not_five_minutes() -> None:
    old = time.time() - 600
    bot, now = _due_bot(('researching',), (time.time() + 86400,), observed_at=old)
    assert bot._lab_start_due(now) is False
    bot2, now2 = _due_bot(('researching',), (time.time() + 86400,), observed_at=time.time() - 7 * 3600)
    assert bot2._lab_start_due(now2) is True
```

Add to `tests/test_reroll_progress.py` a test that `lab_unlock_due` is True when the next slot's gem price is affordable and `slot_due` fires, and False when `auto_unlock_lab_slots` is off. Copy the setup from the existing `lab_due` unlock tests in that file (`grep -n "def test.*lab_due" tests/test_reroll_progress.py`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_autopilot_loop.py tests/test_reroll_progress.py -q -p no:allure_pytest -k "lab_start_due or unknown_finish or stale_strip or lab_unlock_due"`
Expected: FAIL (`AttributeError: _lab_start_due` / `lab_unlock_due`).

- [ ] **Step 3: Implement** as described in Behaviour above. Rename the method body. Add `LAB_STRIP_REFRESH_SECONDS = 6 * 3600.` next to `LAB_DIRECT_CHECK_SECONDS` (`tower_bot.py:133`). Keep the alias.

- [ ] **Step 4: Run the touched files**

Run: `uv run pytest tests/test_autopilot_loop.py tests/test_reroll_progress.py tests/test_lab_towerbot.py tests/test_shopping_loop.py -q -p no:allure_pytest`
Expected: all pass.
- `test_direct_lab_detour_paces_idle_and_stale_running_checks` (`test_autopilot_loop.py:223`) asserts the 300 s stale refresh. Update its stale case to 6 h; the comment must say spec §3.5(d).
- Tests that stub `Progress` without `lab_unlock_due` need the stub method added.

- [ ] **Step 5: Commit**

```bash
git add tower_bot.py fleet/reroll_progress.py tests/test_autopilot_loop.py tests/test_reroll_progress.py tests/test_lab_towerbot.py tests/test_shopping_loop.py
git commit -m "One lab-start-due predicate for game over and the main menu

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Labs before claims and the Workshop on the main menu

**Files:**
- Modify: `tower_bot.py` main-menu chain (~2988-3007)
- Test: `tests/test_shopping_loop.py`

**Interfaces:**
- Consumes: `self._lab_start_due(now)` (Task 3) and `self._request_planned_lab_visit(now, due)`.

**Behaviour:**
- In the reroll branch (`elif self.lab_visit is not None and self.reroll_progress is not None:`), compute the tab status and `lab_due_now = self._lab_start_due(now) or lab_notice_due or self.reroll_progress.lab_unlock_due(wallet_gems=menu_gems)` **first**.
- If the labs tab is `"unlocked"` and `lab_due_now`, try `_request_planned_lab_visit(now, True)`.
- Only when that does not arm a visit, fall back to today's order: `_offer_claim`, then lab (non-due), then Workshop.
- Missions verification, the maintenance inspection, the first Workshop visit and the cards intro keep their earlier places.

Target code shape:

```python
            elif self.lab_visit is not None and self.reroll_progress is not None:
                now = time.time()
                labs_tab_status = self.lab_visit.tab_status(self.screen)
                if labs_tab_status == "unlocked":
                    self.reroll_progress.note_lab_unlocked("labs_tab")
                elif labs_tab_status == "locked":
                    self.reroll_progress.note_lab_locked("labs_tab")
                lab_notice_due = self._notifications.eligible("labs", now)
                lab_due_now = (labs_tab_status == "unlocked" and (
                    lab_notice_due or self._lab_start_due(now)
                    or self.reroll_progress.lab_unlock_due(wallet_gems=menu_gems)))
                if lab_due_now and self._request_planned_lab_visit(now, True):
                    self._arm_lab_visit_bookkeeping(lab_notice_due)
                else:
                    armed = self._offer_claim(settings)
                    if armed is not None:
                        logger.info("Armed a %s claim from the main menu.", armed)
                    elif self._menu_tab_unlocked("workshop"):
                        self.shopping.begin(shopping_policy, self.runs.completed)
```

Extract the three existing bookkeeping lines into `_arm_lab_visit_bookkeeping(self, lab_notice_due: bool) -> None`: setting `_lab_visit_revision`, `_notifications.begin("labs", ...)`, and the "Armed Labs check…" log.

- [ ] **Step 1: Write the failing test** (in `tests/test_shopping_loop.py`, next to `test_confirmed_lab_dot_arms_visit_before_periodic_lab_due` at ~123; copy its fixture setup)

```python
def test_due_lab_arms_before_an_owed_claim_and_the_workshop(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(Shopping(enabled=True))
    # Copy the reroll_progress / lab_visit stubs from
    # test_confirmed_lab_dot_arms_visit_before_periodic_lab_due, then:
    bot._lab_start_due = lambda now: True
    offered: list[str] = []
    bot._offer_claim = lambda settings: offered.append("claim") or "missions"
    armed: list[bool] = []
    bot._request_planned_lab_visit = lambda now, due: armed.append(due) or True
    bot.run_once()
    assert armed == [True]
    assert offered == []          # the claim waits for the next frame
    assert not bot.shopping.visit_in_progress


def test_claim_still_arms_when_no_lab_is_due(bot_on_main_menu) -> None:
    bot = bot_on_main_menu(Shopping(enabled=True))
    bot._lab_start_due = lambda now: False
    offered: list[str] = []
    bot._offer_claim = lambda settings: offered.append("claim") or "missions"
    bot.run_once()
    assert offered == ["claim"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_shopping_loop.py -q -p no:allure_pytest -k "due_lab_arms or claim_still_arms"`
Expected: the first test FAILS (`offered == ["claim"]`).

- [ ] **Step 3: Implement** the reordering above.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_shopping_loop.py tests/test_autopilot_loop.py tests/test_badge_claim_loop.py tests/test_lab_towerbot.py -q -p no:allure_pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tower_bot.py tests/test_shopping_loop.py
git commit -m "Arm a due lab visit before claims and the Workshop

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Fill every idle slot in one Labs visit

**Files:**
- Modify: `lab_visit.py` (`LabVisitResult` at 41-55; `_recover` BOUGHT branch at ~505-518; the visit budget at ~1048-1076; a new `_next_start` helper)
- Modify: `tower_bot.py` `_settle_planned_lab_attempt` (~1575-1593)
- Test: `tests/test_lab_execution.py`, `tests/test_lab_transactions.py`

**Interfaces:**
- Produces: `LabVisitResult.started_slots: tuple[int, ...] = ()`, the slots proven started in this visit, in order.
- Produces: `LabVisit._next_start(outcome: LabVisitResult) -> None`.
  - When `self._options.direct_start` and `self.plan_action is not None`, it clears the per-start state, keeps `outcome` as the visit's pending outcome (accumulating `started_slots`) and sets `self._state = "home"`. Otherwise it calls `self._return(outcome)`.

**Background:**
- Today a proven start, in `_recover`'s `Verdict.BOUGHT`/`FREE` branch for `lab_start`, calls `self._return(LabVisitResult('started', ...))`, which sets `_state = "return"` (tap Battle).
- The `plan_action` hook in `advance` (1110-1127) fires only when `selected is None` and `_state in {'open','home'}`.
- The per-start state to reset is the subset of `request()` (216-261) that describes one start:
  - `selected_action`, `pending_action`, `_slot`, `_purchase`, `_search`, `_search_frames`
  - `_picker_signature`, `_picker_reads`, `_dialog_signature`, `_dialog_reads`
  - `_unavailable_signature`, `_unavailable_reads`, `_picker_name`, `_picker_seconds`
  - `_start_tap`, `_start_scans`, `_start_frames`, `_rehearsing`
- **Do not** reset `_scans`, `_started_at`, `_home_seen`, `_unlock_done`, `_options`, or the journal.
- One transaction at a time is preserved, because `_next_start` only runs after the journal verdict for the previous start.

**Budget:** in `advance`, define `extra = len(self._outcome.started_slots) if self._outcome is not None else 0` and `scan_cap, time_cap = (64, 120.) if searching else (48, 90.)`, then add `48 * extra` and `90. * extra` respectively.

**Outcome merge in `_next_start`:**

```python
    def _next_start(self, outcome: LabVisitResult) -> None:
        slots = (self._outcome.started_slots if self._outcome is not None else ()) + (
            (outcome.confirmed_job.slot,) if outcome.confirmed_job is not None else ())
        outcome = replace(outcome, started_slots=slots)
        if not (self._options.direct_start and self.plan_action is not None and self._options.start_research):
            self._return(outcome)
            return
        self._outcome = outcome            # a later stage timeout still reports "started"
        for name in self._PER_START_FIELDS:
            setattr(self, name, self._PER_START_DEFAULTS[name])
        self._state = "home"
```

- Define `_PER_START_FIELDS` and `_PER_START_DEFAULTS` as class attributes, using the same default values `request()` assigns. Copy them from `request()` and keep the two in sync.
- Refactor `request()` to call the same reset, so there is one source of truth.

When the `home` state's planner returns no action (`decide(home, None)` path, nothing to start), the visit must go to `return` with the accumulated `self._outcome`. Check the `home` branch after 1209-1214: where it would finish or return with a fresh outcome, use `self._outcome or <fresh>` so the accumulated "started" result wins. The stage-timeout code at 1186-1191 already prefers `self._outcome`.

In the BOUGHT branch, replace `self._return(LabVisitResult('started', ...))` with `self._next_start(LabVisitResult('started', ...))`. Leave the refuted branch (`_return` at ~532) unchanged.

In `tower_bot._settle_planned_lab_attempt`, the `result.status == 'started'` branch already resets the backoff and sets `_lab_followup_due` under direct start. No change is needed beyond logging `started_slots` in the "Lab N visit ended" line (`tower_bot.py:1704`): `"Lab visit ended: %s (%s), started slots %s"`.

- [ ] **Step 1: Write the failing tests** (`tests/test_lab_execution.py`, using `LabHarness` from `tests/test_lab_transactions.py:21-57` and the `action()` helper at line 17)

```python
def test_direct_start_visit_replans_after_a_proven_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    planned = [action(), None]
    h.visit.plan_action = lambda at: planned.pop(0) if planned else None
    assert h.visit.request(None, options=LabVisitOptions(direct_start=True))
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation')
    for _ in range(3):
        h.scan('menu_labs_game_speed_running')
    # Proven: instead of returning to battle, the visit is back on the slot strip.
    assert h.visit.active and h.visit._state == 'home'
    assert h.visit._outcome.status == 'started' and h.visit._outcome.started_slots == (1,)
    assert h.visit.selected_action is None
    # The planner has nothing more: the visit returns with the accumulated result.
    h.scan('menu_labs_game_speed_running')
    h.scan('menu_labs_game_speed_running')
    h.time += 1
    result = h.visit.advance(frame('menu_main_labs_unlocked'), (), h.device, h.time,
                             observed_at=h.time, capture_scope=h.scope)
    assert not h.visit.active
    assert h.visit.last_result.started_slots == (1,)   # use the attribute the visit exposes for the finished outcome


def test_without_direct_start_a_proven_start_still_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    assert h.visit.request(action(), options=LabVisitOptions())
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation')
    for _ in range(3):
        h.scan('menu_labs_game_speed_running')
    assert h.visit._state == 'return'


def test_second_start_waits_for_first_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = LabHarness(tmp_path, monkeypatch)
    h.visit.cancel('new request')
    calls: list[float] = []
    h.visit.plan_action = lambda at: calls.append(at) or action()
    assert h.visit.request(None, options=LabVisitOptions(direct_start=True))
    h.confirmation()
    h.scan('menu_labs_game_speed_confirmation')
    planned_before_proof = len(calls)
    h.scan('menu_labs_game_speed_running')   # first post-tap scan: still pending
    assert len(calls) == planned_before_proof
    assert len(h.journal.open_transactions()) <= 1
```

- Before writing the assertions, check which attribute holds the finished outcome: `grep -n "_outcome\|last_result\|def result" lab_visit.py | head`. Adapt the `last_result` line to it; the finished `LabVisitResult` may be the return value of `advance`.
- The re-plan path needs a fresh complete strip read whose `observed_at == capture_at` (1209-1214). If `menu_labs_game_speed_running` doesn't produce a complete strip in `LabRuntime`, use the frame the existing `plan_action` tests in `tests/test_lab_visit.py:112-158` use for the strip.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_lab_execution.py -q -p no:allure_pytest -k "replans or still_returns or waits_for_first"`
Expected: `replans` FAILS (`_state == 'return'`); the other two pass as guards.

- [ ] **Step 3: Implement** `started_slots`, `_next_start`, the shared reset, the budget extension and the BOUGHT-branch change.

- [ ] **Step 4: Run all lab visit test files**

Run: `uv run pytest tests/test_lab_execution.py tests/test_lab_transactions.py tests/test_lab_visit.py tests/test_lab_start_any.py tests/test_lab_towerbot.py tests/test_lab_repeat.py tests/test_lab_slot_unlock.py -q -p no:allure_pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add lab_visit.py tower_bot.py tests/test_lab_execution.py
git commit -m "Fill every idle lab slot in one visit under direct start

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Picker failures — root-cause fix and a 30-minute per-lab skip

**Files:**
- Modify: `tower_bot.py` (`_settle_planned_lab_attempt` ~1575-1593; `_lab_unavailable` at 251)
- Modify: `lab_picker.py` (`PickerSearch`: upward phase), `lab_visit.py` (picker stage ~1250-1309: search when the card is missing; evidence on timeout)
- Test: `tests/test_lab_towerbot.py`, `tests/test_lab_picker_search.py` (new), `tests/test_lab_execution.py`

**Interfaces:**
- Produces: `LAB_PICKER_SKIP_SECONDS = 1800.` and `LAB_PICKER_FAILURES_TO_SKIP = 2` in `tower_bot.py`, and `self._lab_picker_failures: dict[tuple[str | None, int | None, str], int]`.
- Consumes: `self._lab_unavailable[(account_id, revision, research)] = expiry`. `_excluded_lab_research` (1599-1605) already feeds it into the planner as `excluded_research`.

**Skip behaviour:** in `_settle_planned_lab_attempt`, when `selected is not None` and `result.reason in {'research_not_found', 'picker_stage_timeout'}`:
- increment `_lab_picker_failures[key]`
- when it reaches 2, set `_lab_unavailable[key] = time.time() + LAB_PICKER_SKIP_SECONDS`, reset the counter, set `_lab_followup_due = True`, and log `"Skipping %s for 30 min after 2 picker failures"`
- on `result.status == 'started'` for the same key, reset the counter

- [ ] **Step 1: Write the failing tests** (`tests/test_lab_towerbot.py`; copy the bot/visit stub setup from the `_lab_unavailable` tests at 99-115)

```python
def test_two_picker_failures_skip_the_lab_for_thirty_minutes(...) -> None:
    # Setup copied from the research_unavailable test at lines 99-115.
    for _ in range(2):
        bot._settle_planned_lab_attempt(failed_result('picker_stage_timeout'))
    key = (bot._lab_account_id(), 7, 'labs.game-speed')
    assert bot._lab_unavailable[key] == pytest.approx(time.time() + 1800, abs=5)
    assert 'labs.game-speed' in bot._excluded_lab_research(time.time())


def test_one_picker_failure_does_not_skip(...) -> None:
    bot._settle_planned_lab_attempt(failed_result('research_not_found'))
    assert 'labs.game-speed' not in bot._excluded_lab_research(time.time())
```

- [ ] **Step 2: Run to verify failure.** Run: `uv run pytest tests/test_lab_towerbot.py -q -p no:allure_pytest -k picker`

- [ ] **Step 3: Implement the skip.**

- [ ] **Step 4: Root-cause fix.** Follow the Root cause section below. Write its regression test first, watch it fail, then fix.

- [ ] **Step 5: Run** `uv run pytest tests/test_lab_towerbot.py tests/test_lab_picker.py tests/test_lab_visit.py -q -p no:allure_pytest`. The `tests/test_lab_picker.py` file may not exist; drop it if it doesn't.

- [ ] **Step 6: Commit**

```bash
git add tower_bot.py lab_picker.py lab_visit.py tests/test_lab_towerbot.py tests/test_lab_picker_search.py tests/test_lab_execution.py
git commit -m "Fix lab picker timeouts; skip a lab for 30 min after 2 picker failures

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

#### Root cause (picker_stage_timeout, 2026-10-05)

**What happened** (high confidence on the code path, medium on the trigger):
- Every failing visit opened Lab 1's picker, read the SELECT RESEARCH page and found **no Game Speed card**. The visit went `unknown`, waited 8 scans and timed out (`lab_visit.py:1181-1190`).
- Game Speed is the first card at the top of the list. The game keeps the picker's scroll position (or Search text) while the app runs. Each failure window followed a picker scrolled further down, by a Lab 2/3 search or by hand, and only an app relaunch cleared it.
- The bot assumes the picker always opens at the top (`lab_picker.py:3-4`). Game Speed never goes through `PickerSearch` (the `general` shortcut at `lab_visit.py:1253` / `1128-1130`). Even when it does, `PickerSearch` only swipes **down**.

**Fix:**
1. **`lab_picker.PickerSearch` searches upward first.** On the first frame where the target card is not fully visible, swipe *up* (reverse direction: from `top + 10%` to `top + 10% + distance`) until the list stops moving (signature unchanged after a swipe) or `MAX_UP_SWIPES = 8`. Then run today's downward search from the top. Reset `_after_swipe` between phases; count up-swipes separately from `MAX_SWIPES`.
2. **`lab_visit.py`: every target goes through `PickerSearch` when its card is missing, Game Speed included.**
   - In the picker stage, when the selected/target research card is not on the page, start `self._search = PickerSearch(target, width)`, whatever the `general` flag says.
   - The existing `general=False` fast path stays only for the case where the card *is* visible.
   - A search that ends `not_found` gives `research_not_found` (counted by the Task 6 skip), never an endless `unknown`.
3. **Save the picker frames on any `*_stage_timeout` in the picker stage**, with `self._save_evidence(f"lab-picker-timeout-{research}", self._capture_at, frames)`, the same as `_note_miss` (`lab_visit.py:948-956`). Also log at INFO, once per stage, why the scan read `unknown` (`card missing` / `price unread`).

**Regression tests:**
- These are pure `PickerSearch` unit tests with fake pages. No incident frame exists.
- Put them in `tests/test_lab_picker_search.py` (create it, or add to the existing picker test file if `grep -ln PickerSearch tests/` finds one).

```python
from dataclasses import dataclass
from lab_picker import PickerSearch


@dataclass
class Card:
    fully_visible: bool = True


class Page:
    """A fake picker list: `offset` cards scrolled; the target is card `at`."""
    def __init__(self, offset: int, at: int, size: int = 30) -> None:
        self.offset, self.at, self.size = offset, at, size
        self.open, self.viewport = True, (300, 1500)
    def card(self, lab_id: str):
        return Card() if self.offset <= self.at < self.offset + 6 else None
    def signature(self):
        return ("page", self.offset)


def drive(search: PickerSearch, offset: int, at: int, limit: int = 40) -> tuple[str, int]:
    for _ in range(limit):
        step = search.step(Page(offset, at))
        if step.kind != "swipe":
            return step.kind, offset
        x1, y1, x2, y2 = step.swipe
        offset = max(0, offset - 5) if y2 > y1 else min(24, offset + 5)  # up swipe: y grows
    return "limit", offset


def test_scrolled_picker_scrolls_up_to_game_speed() -> None:
    # The incident: the list was left 20 cards down; Game Speed is card 0.
    assert drive(PickerSearch("labs.game-speed", 1080), offset=20, at=0) == ("found", 0)


def test_target_below_still_found_after_reaching_top() -> None:
    assert drive(PickerSearch("labs.coins-wave", 1080), offset=10, at=18)[0] == "found"


def test_absent_target_ends_not_found_not_waiting() -> None:
    assert drive(PickerSearch("labs.missing", 1080), offset=10, at=999)[0] == "not_found"


def test_visible_target_needs_no_swipe() -> None:
    assert PickerSearch("labs.game-speed", 1080).step(Page(0, 0)).kind == "found"
```

**Visit-level test:** add to `tests/test_lab_execution.py`.
- Monkeypatch `lab_screen.read_picker_page` (or the reader `lab_visit` calls; find it with `grep -n "read_picker_page\|read_selected_picker" lab_visit.py`) so the first picker frame reports no Game Speed card.
- Assert the visit taps a swipe (`h.device` records swipes; check the `Device` helper) instead of timing out after 8 scans.
- Assert that it never yields `picker_stage_timeout` within 12 scans.

**Follow-up (not in this branch):** capture a real scrolled-picker frame (`tests/fixtures/menu_labs_picker_scrolled.png`) during the Task 10 live run, for a frame-based test.

---

### Task 7: Between-games timing profile

**Files:**
- Modify: `config.py` (add the constants)
- Modify: `screens.py` (`ScreenTracker`: add a `pending` property)
- Modify: `navigate.py` (`Navigator.maybe_navigate`: optional `cooldown` override)
- Modify: `tower_bot.py` (track `_last_scan_between_games` next to `_last_scan_in_battle` at ~1816-1820; the `run_forever` sleep at ~3227-3254; the `maybe_navigate` call at ~3055)
- Test: `tests/test_cli.py` (near `test_run_forever_uses_the_menu_interval_outside_battle` at 1042), `tests/test_screen_tracker.py`, `tests/test_navigate.py`

**Interfaces:**
- Produces:
  - `config.MENU_FAST_PROFILE: bool = True`
  - `MENU_FAST_SCAN_SECONDS: float = 0.6`
  - `MENU_CONFIRM_GAP_SECONDS: float = 0.3`
  - `MENU_FAST_NAV_COOLDOWN_SECONDS: float = 1.0`
  - `MENU_IDLE_WATCHDOG_SECONDS: float = 20.0` (used in Task 9)
- Produces: `ScreenTracker.pending -> bool`, which is True while an unconfirmed state streak is building (`self._pending is not None`).
- Produces: `Navigator.maybe_navigate(..., cooldown: float | None = None)`. When given, it replaces `self._cooldown` for this call only; jitter still applies.
- Produces: `TowerBot._last_scan_between_games: bool`, which is True when the reading is GAME_OVER, MAIN_MENU, or UNKNOWN **without** `cash_top_left`.

**Sleep rule** (in `run_forever`, after the existing `current_interval` computation and before `maintenance.wait_seconds`):

```python
        if interval is None and config.MENU_FAST_PROFILE and self._last_scan_between_games:
            current_interval = min(current_interval, max(MIN_INTERVAL, jitter.spread(
                config.MENU_FAST_SCAN_SECONDS, live.timing_jitter)))
            if self.tracker.pending:
                current_interval = min(current_interval, config.MENU_CONFIRM_GAP_SECONDS)
```

Set the flag where `_last_scan_in_battle` is set:

```python
        in_run = (reading.state is screens.ScreenState.IN_RUN
                  or (reading.state is screens.ScreenState.UNKNOWN and reading.cash_top_left is not None))
        self._last_scan_between_games = not in_run
```

(Initialise `self._last_scan_between_games = False` in `__init__`.) The existing `battle_context` stays as it is; GAME_OVER is in both.

At the `maybe_navigate` call, pass `cooldown=config.MENU_FAST_NAV_COOLDOWN_SECONDS if config.MENU_FAST_PROFILE and self._last_scan_between_games else None`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`, copy the structure of `test_run_forever_uses_the_menu_interval_outside_battle`:

```python
def test_run_forever_uses_fast_menu_interval_between_games(monkeypatch) -> None:
    bot = TowerBot(device=MagicMock(), templates=vision.TemplateCache(Path("templates")), bus=events.EventBus())
    bot.controls.apply({"interval": 5.0, "menu_interval": 2.0, "timing_jitter": 0.0})
    flags = iter([(True, False), (False, True)])   # (in_battle, between_games)
    def fake_run_once():
        bot._last_scan_in_battle, bot._last_scan_between_games = next(flags)
    monkeypatch.setattr(bot, "run_once", fake_run_once)
    waits: list[float] = []
    def fake_wait(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            bot.stop()
        return False
    monkeypatch.setattr(bot._stopping, "wait", fake_wait)
    bot.run_forever()
    assert waits == [5.0, 0.6]


def test_fast_profile_never_applies_in_battle_context(monkeypatch) -> None:
    # Same setup; flags (True, False) twice. Expected: waits == [5.0, 5.0].
    ...


def test_pending_confirmation_uses_the_short_gap(monkeypatch) -> None:
    # Flags (False, True); bot.tracker._pending = screens.ScreenState.MAIN_MENU. Expected: waits == [0.3].
    ...


def test_kill_switch_restores_menu_interval(monkeypatch) -> None:
    monkeypatch.setattr(config, "MENU_FAST_PROFILE", False)
    # Flags (False, True). Expected: waits == [2.0].
    ...
```

Write the three `...` bodies out in full, following the first test; they differ only in the flags, the setup line and the expected list. Match the existing test's monkeypatch style; check whether `MagicMock`, `vision`, `events` and `Path` are already imported in `tests/test_cli.py`.

In `tests/test_screen_tracker.py`:

```python
def test_pending_is_true_only_while_a_streak_builds() -> None:
    tracker = screens.ScreenTracker(confirmations=2)
    assert tracker.pending is False
    tracker.observe(reading(screens.ScreenState.MAIN_MENU))
    assert tracker.pending is True
    tracker.observe(reading(screens.ScreenState.MAIN_MENU))
    assert tracker.pending is False
```

Use the file's existing `reading` helper (`grep -n "def " tests/test_screen_tracker.py`).

In `tests/test_navigate.py`, add a test that `maybe_navigate(..., cooldown=0.0)` taps again immediately after a tap, where the default 3.0 s would refuse. Copy the setup of the existing cooldown test there.

- [ ] **Step 2: Run to verify failure.** Run: `uv run pytest tests/test_cli.py tests/test_screen_tracker.py tests/test_navigate.py -q -p no:allure_pytest -k "fast or pending or kill_switch or cooldown"`

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run** `uv run pytest tests/test_cli.py tests/test_screen_tracker.py tests/test_navigate.py tests/test_strategy.py tests/test_maintenance_schedule.py -q -p no:allure_pytest`. Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add config.py screens.py navigate.py tower_bot.py tests/test_cli.py tests/test_screen_tracker.py tests/test_navigate.py
git commit -m "Scan faster between games: 0.6 s menus, 0.3 s confirmations, 1 s nav cooldown

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Workshop worthwhile only above the lab savings

**Files:**
- Modify: `fleet/reroll_progress.py:964-1027` (`workshop_worthwhile`)
- Test: `tests/test_reroll_progress.py` (next to the `workshop_worthwhile` tests at 528-621)

**Interfaces:**
- Consumes: `fleet.coin_share.spendable_wallet(wallet: int, jar: int) -> int` and `self._route_jar` (set at `fleet/reroll_progress.py:569-584`; may be None or 0).

**Behaviour:** in the final gate, compare the price against `spendable = coin_share.spendable_wallet(plan.wallet_coins, self._route_jar or 0)` instead of the raw `plan.wallet_coins`:

```python
        spendable = (None if plan.wallet_coins is None
                     else coin_share.spendable_wallet(plan.wallet_coins, getattr(self, "_route_jar", 0) or 0))
        worthwhile = (plan.upgrade_id is not None and
                      (spendable is None or spendable > 0) and
                      (plan.price is None or spendable is None or spendable >= plan.price))
```

When it returns False during a visit decision, log once per call site at INFO: `"Skipping the Workshop: %s coins above lab savings, cheapest planned %s"`.

Batched Workshop buys (spec §4.4) are covered by Task 7's faster interval. Each purchase stays one-in-flight with row-change proof; see "Spec deviations" at the end.

- [ ] **Step 1: Failing test.** Copy the setup of the nearest existing `workshop_worthwhile` test, then:

```python
def test_workshop_not_worthwhile_when_only_lab_savings_cover_the_price(...) -> None:
    # Plan: next upgrade costs 5,000; wallet 6,000; jar (lab savings) 4,000 → spendable 2,000.
    progress._route_jar = 4_000
    assert progress.workshop_worthwhile() is False


def test_workshop_worthwhile_when_spendable_covers_the_price(...) -> None:
    progress._route_jar = 500
    assert progress.workshop_worthwhile() is True
```

- [ ] **Step 2: Run to verify failure.** Run: `uv run pytest tests/test_reroll_progress.py -q -p no:allure_pytest -k workshop_worthwhile`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_reroll_progress.py tests/test_autopilot_loop.py tests/test_shopping_loop.py -q -p no:allure_pytest`.
- [ ] **Step 5: Commit**

```bash
git add fleet/reroll_progress.py tests/test_reroll_progress.py
git commit -m "Visit the Workshop only when coins above lab savings buy something

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Home-screen idle watchdog

**Files:**
- Modify: `tower_bot.py` (in `run_once`, after the navigation block ~3051-3095)
- Test: `tests/test_autopilot_loop.py`

**Interfaces:**
- Consumes: `config.MENU_IDLE_WATCHDOG_SECONDS` (Task 7) and `self.navigator.maybe_navigate(..., go_home=False, cooldown=0.0)`.
- Produces: `TowerBot._menu_idle_since: float | None` and the log line `between_games_idle_watchdog`.

**Behaviour:**
- On a confirmed MAIN_MENU frame where nothing is active, set `_menu_idle_since` if it is None. "Nothing active" means none of `shopping.visit_in_progress`, `lab_visit.active`, `claim.active`, `milestones_claim.active`, `cards_intro.active`, `card_runtime.active`, `visit.active`, and not paused, and not run-cap.
- On any other state, or when anything is active, reset it to None.
- If `time.monotonic() - _menu_idle_since >= MENU_IDLE_WATCHDOG_SECONDS`:
  - log WARNING `"between_games_idle_watchdog: %.0f s idle on the main menu; starting the next run"`
  - call `maybe_navigate(self.screen, state, self.device, now=time.monotonic(), tuning=settings.strategy, go_home=False, cooldown=0.0)`
  - reset `_menu_idle_since` to None
- **Tier selection (`_advance_tier`) counts as active.** Do not fire while it returns True.

- [ ] **Step 1: Failing tests**

```python
def test_watchdog_taps_battle_after_twenty_idle_seconds(bot_on_main_menu, monkeypatch) -> None:
    bot = bot_on_main_menu(Shopping(enabled=False))
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    calls: list[dict] = []
    bot.navigator.maybe_navigate = lambda *a, **k: calls.append(k)
    bot.run_once()
    clock[0] += 21
    bot.run_once()
    assert any(k.get("cooldown") == 0.0 and k.get("go_home") is False for k in calls)


def test_watchdog_quiet_while_a_visit_is_active(bot_on_main_menu, monkeypatch) -> None:
    bot = bot_on_main_menu(Shopping(enabled=True))
    monkeypatch.setattr(type(bot.shopping), "visit_in_progress", property(lambda self: True))
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    bot.run_once(); clock[0] += 60; bot.run_once()
    assert bot._menu_idle_since is None
```

- [ ] **Step 2: Run to verify failure.** Run: `uv run pytest tests/test_autopilot_loop.py -q -p no:allure_pytest -k watchdog`
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_autopilot_loop.py tests/test_shopping_loop.py -q -p no:allure_pytest`.
- [ ] **Step 5: Commit**

```bash
git add tower_bot.py tests/test_autopilot_loop.py
git commit -m "Start the next run after 20 s idle on the main menu

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Between-games report tool, then the gated live rollout

**Files:**
- Create: `tools/between_games_report.py`
- Test: `tests/test_between_games_report.py`

**Interfaces:**
- Produces:
  - `gaps(rows: list[tuple[float, str]]) -> list[float]`, which takes `(ts, type)` rows sorted by time and returns the seconds from each `RunEnded` to the next `RunStarted`, skipping a `RunEnded` followed by another `RunEnded`
  - `summary(values: list[float]) -> dict[str, float]`, with keys `n`, `median`, `p90`, `mean`
  - `main(argv) -> int`, CLI: `uv run python tools/between_games_report.py <worker> [--hours 24]`. It reads `~/.local/share/thetowerbot/fleet/workers/<worker>/tower_bot.db` read-only (`sqlite3.connect(f"file:{path}?mode=ro", uri=True)`) and prints the gap summary and lab starts per hour (`LabResearchStarted` count / hours).

- [ ] **Step 1: Failing tests**

```python
from tools import between_games_report as r


def test_gaps_pairs_run_end_with_next_start() -> None:
    rows = [(0., "RunStarted"), (100., "RunEnded"), (130., "Tapped"), (160., "RunStarted"),
            (900., "RunEnded"), (950., "RunEnded"), (980., "RunStarted")]
    assert r.gaps(rows) == [60.0, 30.0]


def test_summary_median_and_p90() -> None:
    s = r.summary([10., 20., 30., 40., 50., 60., 70., 80., 90., 100.])
    assert s["n"] == 10 and s["median"] == 55.0 and 90.0 <= s["p90"] <= 100.0
```

- [ ] **Step 2: Run to verify failure.** Run: `uv run pytest tests/test_between_games_report.py -q -p no:allure_pytest`

- [ ] **Step 3: Implement** with stdlib only (`sqlite3`, `statistics.quantiles(values, n=10)[8]` for p90 when n ≥ 2), with the same `main` and argparse style as `tools/migrate_lab_lists.py`.

- [ ] **Step 4: Run the test, then a read-only baseline:** `uv run python tools/between_games_report.py Tiramisu64_82 --hours 48`. Paste the output into the task report.

- [ ] **Step 5: Commit**

```bash
git add tools/between_games_report.py tests/test_between_games_report.py
git commit -m "Add between-games timing report

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: STOP. Live rollout needs the user's explicit go-ahead.** The controller (not a subagent) asks the user before each of these:
  1. `uv run python tools/migrate_lab_lists.py` (no `--dry-run`), which saves new strategy versions.
  2. Assign the new `blender` version to Tiramisu64_82 only, via `POST /api/fleet/reroll/strategies/assign` (`expected_revision, strategy_id, strategy_version, workers:[{worker, account_id}]`), and restart that worker on this branch's code.
  3. After 3+ hours, run `tools/between_games_report.py Tiramisu64_82 --hours 3`. Check:
     - lab starts > 0
     - every owned slot is researching (`GET /api/fleet/labs`)
     - median gap ≤ 30 s
     - no `lab_start_uncertain` / `LabStarterHalted` events
  4. If those pass, roll out to the rest of the fleet.

---

## Spec deviations (decided while planning)

- **§4.4 batched Workshop buys are dropped.**
  - Shopping proves each purchase by a row change on the next frame, and replans a single-row policy after each one (`shopping.py:1154`, `942-946`). Buying several rows before re-reading would bypass that proof.
  - With the 0.6 s menu interval (Task 7), a purchase cycle of tap, proof frame and replan frame takes about 1.8 s instead of about 6 s, which meets the spec's 1–1.5 s-per-buy intent closely without weakening proof.
- **§3.5 `lab_start_due` reuses `_direct_lab_visit_due`**, which already implements (a)–(c) and a 300 s pacing for unknown or unconfirmed slots. (d) moves from 300 s to 6 h.
