# Lab strategy core: design

Date: 2026-09-28
Program: Lab strategy planner. This spec covers sub-project 1 of 3.

## Program

The goal is a complete lab strategy: a plan the bot follows (it starts labs and saves or
spends coins) and the Strategy Studio shows (timeline, projections, the reason for each
pick). The work is split into three sub-projects. Each has its own spec → plan → build
cycle and is built in this order:

1. **Lab strategy core** (this spec, backend):
   - a ranked lab list with slot pins, and the evaluator that fills slots from it;
   - just-in-time coin saving in place of the fixed-% jar;
   - catalog data for early-game labs;
   - a new default template.
2. **General lab starter** (bot):
   - find any lab by scrolling the picker and reading names (OCR), and check the
     confirmation screen (name, level, price) before tapping;
   - turn on Auto Research where the plan continues the same lab;
   - write observed prices and durations back into the catalog;
   - a dry-run mode that walks the screens without tapping Research.

   It replaces the per-lab recorded-route gate.
3. **Planner UI** (Studio):
   - a ranked-list editor with pins;
   - a per-slot timeline projected from income and durations;
   - a savings line ("saving 38k for Labs Speed · ready in ~3h");
   - a "why" line for each slot.

Target accounts are early game: labs just unlocked, 1–2 slots, Tiers 1–3. The model must
keep working as slots 3–5 are bought.

## Research summary

Sources:
- the Fandom wiki, read through its MediaWiki API, since direct fetches get 402/403;
- the community "Lab Tier List, updated for version 29" sheet:
  https://docs.google.com/spreadsheets/d/1M5WPXN3RCquT3peDUNjCqTVRPk313n9HbMe-hihzLvg
- r/TheTowerGame, through Wayback snapshots from 2025 to early 2026;
- game-vault and tower-hub;
- The Tower Discord: the pinned "Lab Progression Guide" in #beginner-guides (Shuckle,
  top-50), and #patch-notes for v29.0–29.0.5. The patch notes have no early-game lab
  changes.

Findings that shape this design:

| Finding | Consequence | Source |
|---|---|---|
| Game Speed first, to max | Pinned to slot 1 in the template | Consensus: tier list S+, Fandom Tier-Specific Guide, game-vault |
| Labs Speed is S up to level 50, then A; unlocks at T1 W150; +2%/level, ×2.98 at level 99. The Discord guide adds: "isn't as important early while all your labs are short" | Ranked below the early economy labs; one entry to 50, a second to 99 | Tier list; Fandom Labs_Speed; Discord guide |
| Unlock Perks (T2 W150), then First Perk Choice and Option Quantity, are S+. First Perk Choice is "worth grinding a few days to a week"; save for the 1st ban right after | Ranked first after Game Speed, with Ban Perks next; need `{tier, wave}` and `{lab, level}` unlocks | Tier list; Discord guide |
| The tier list rates Coins/Wave and Cash/Wave F overall. For accounts under 100 waves, the Discord guide says: coins and cash per wave, "10–20 levels at most", then Cash Bonus and Coins/Kill | Coins/Wave to 10 at tier B, then Coins/Kill and Cash Bonus; Cash/Wave left out | Tier list; Discord guide |
| Workshop discounts and Labs Coin Discount are D early ("gaining more coins beats spending less"); "early fast levels are fine" | Discounts only to level 20, at tier C | Tier list; Reddit discount threads; Discord guide |
| Light Speed Shots: "unlock as soon as you can afford it". Starting Cash: "a trap" | Light Speed Shots at tier S, high in the list; Starting Cash left out | Discord guide |
| An S+ lab is "worth saving 2–3 days of coins"; "affordable within 2 runs"; Game Speed: "save up for each level if you have to" | A per-tier save window, S+ = 72h; Game Speed pinned, so it is never skipped | Tier list; Reddit; Discord guide |
| Never leave a slot idle; switching a lab refunds its coins and keeps its progress | Idle slots pause Workshop until their lab is affordable; a filler-lab-then-switch strategy is noted as future work | Consensus; Fandom Lab_Upgrades |
| The in-game Auto Research toggle starts the next level when affordable | Sub-project 2 uses it; the scheduler here already saves for a running lab's next level | Fandom Lab_Upgrades |
| Black Hole Damage and bot-cooldown labs can't be undone | Excluded from the template | Earlier project research |

No published source gives a "spend X% on labs" rule or coin-per-hour benchmarks by
stage. The saving model below is derived from the rules above, not copied from a source.

## Scope

In scope:
- the `lab_list` block and its evaluation;
- the `just_in_time` coin mode and the `SavingPlan`;
- the per-tier save windows in Strategy Rules;
- catalog v2 with price and time tables and the new unlock kinds;
- the new default template;
- wiring the reserve into Workshop spending (worker, preview, Fleet State);
- a read-only view of `lab_list` in the Studio.

Out of scope:
- starting any lab other than slot-1 Game Speed (sub-project 2);
- the ranked-list editor and timeline UI (sub-project 3);
- filler labs with switching;
- gem-rushing;
- elite-cell boosts;
- mid- and late-game templates.

## Design

### 1. Catalog: `catalog/labs.v2.json`, loaded by `lab_catalog.py`

- Adds `levels` tables (coins and seconds per level) for these labs:
  - Labs Speed, Coins/Kill Bonus, Attack Speed;
  - Unlock Perks, First Perk Choice, Perk Option Quantity, Ban Perks, Standard Perks
    Bonus, Improve Trade-Off Perks;
  - Light Speed Shots, Health, Damage, Critical Factor;
  - Workshop Attack / Defense / Utility Discount, Labs Coin Discount;
  - Cash Bonus, Coins/Wave, Buy Multiplier, Starting Cash.

  Game Speed keeps its table.
- Tables come from the Fandom wiki through the MediaWiki API. Each lab keeps its
  `source_url` and `checked` date. A lab whose table can't be read keeps
  `levels: null`, and planning treats its price as unknown. No values are invented.
- `unlock` becomes one of:
  - `null`;
  - `{"tier": T, "wave": W}`;
  - `{"lab": id, "level": N}`.

  v1's `{"best_tier_1_wave": W}` is read as `{"tier": 1, "wave": W}`.
- Validation checks, for each lab:
  - the number of levels equals `max_level`;
  - coins and seconds never decrease;
  - lab ids referenced by unlocks exist;
  - unlocks form no cycles.

  A malformed file fails to load. This matches today's behaviour for v1.
- Wiki prices are before any Labs Coin Discount. At our stage that discount is 0 and
  the template doesn't research it, so no discount is applied.

### 2. Block: `lab_list` in `fleet/resource_blocks.py`

```json
{"id": "labs.list", "type": "lab_list", "label": "Early game",
 "entries": [
   {"id": "gs", "lab_id": "labs.game-speed", "to_level": 7, "tier": "S+", "pin_slot": 1},
   {"id": "ls50", "lab_id": "labs.labs-speed", "to_level": 50, "tier": "S"}
 ]}
```

- A labs lane holds either **one** `lab_list` or the existing `slot_track` blocks,
  never both. `validate_labs` rejects a mix.
- Entry fields:
  - `id`: a unique token;
  - `lab_id`: must be in the catalog;
  - `to_level`: 1 up to the lab's max level;
  - `tier`: one of `S+`, `S`, `A`, `B`, `C`;
  - optional `pin_slot` (1–5);
  - optional `label` (1–60 characters).
- The same `lab_id` may appear more than once, but its `to_level` values must strictly
  increase down the list (Labs Speed 50, then Labs Speed 99).
- At most one unfinished pin per slot. Only one pin is enforced as "never skipped":
  Game Speed in slot 1, which keeps today's `validate_labs` rule that slot 1 starts
  with Game Speed.
- Limits are the same as the other lanes (`MAX_BLOCKS`).

### 3. Evaluation: `evaluate_lab_plan(route, facts) -> LabPlan`

A new branch runs when the lane is a `lab_list`. The output types (`SlotPlan`, `SlotNow`,
`SlotNext`, `why`) don't change, so the Labs & Gems page, Fleet State and the worker keep
reading the same shape.

Slots are evaluated in ascending order. A slot is considered only if it is **owned**. A
slot that is idle or unread gets its target for "now". A slot that is researching gets
its target for when its current research completes (`needed_at = completes_at`).

For each slot:
1. **Pin.** If the first unfinished entry pinned to this slot exists, it is the only
   candidate. A pinned entry is finished once its `to_level` is reached; the slot then
   falls through to step 2.
2. **Ranked walk.** Go through the entries in order and skip an entry if:
   - `finished`: the known level has reached `to_level`;
   - `running elsewhere`: it is running in another slot;
   - `claimed`: another slot picked it earlier in this evaluation;
   - `pinned elsewhere`: it is pinned to another slot;
   - `locked`: its unlock condition is not met;
   - `unlock unread`: the fact behind its unlock is unknown;
   - `price unknown`: the catalog has no price for the next level;
   - `beyond save window`: `SavingPlan.hours_to_afford` is greater than the tier's window
     (section 4).

   Skipping never blocks the walk. The first entry not skipped is the slot's target.
3. **Nothing left.** If no entry survives, the slot's `next` is `None`, and `why` ends
   with the most common skip reason, for example "All remaining entries locked: need
   Tier 2 wave 150".

Other rules:
- **The level to research next** is `known + 1`, including levels already running.
  Coverage by an earlier entry of the same lab counts: the Labs Speed 99 entry starts at
  51.
- **Automation.** `automated` still comes from `research_automated(lab_id, slot)`. A
  target that can't be automated gets the note `"Start manually"`. It is still reserved
  for in the saving plan: the plan is the truth, and automation catches up later.
- **Coins between slots.** Unlike `slot_track`, a slot's target doesn't take coins away
  from slots evaluated after it. Coin coverage belongs to the `SavingPlan`, which
  considers all targets together. `covered` is taken from the `SavingPlan`: it is
  `True` when the target is funded by its `needed_at` (`ready_at ≤ needed_at`), `False`
  when it isn't, and `None` when the wallet or price is unknown.
- **Unlock facts.** `LabFacts` gains:
  - `best_waves: Mapping[int, int] | None`, the best wave per tier, from the runs table.
    `best_tier_1_wave` stays and is mirrored into `best_waves[1]`.
  - `coins_per_hour: float | None`.

  A `{lab, level}` unlock reads the same known-levels map the walk uses.

### 4. Saving: `fleet/lab_saving.py` (new), a pure function

```python
def saving_plan(plan: LabPlan, entries: Sequence[Entry], facts: LabFacts,
                rules: RouteRules) -> SavingPlan
```

Rules:
- **Income:** `r = facts.coins_per_hour × rules.labs.saving.income_margin_pct / 100`,
  with a default margin of 75. Income that is missing or not positive counts as unknown.
- **Targets:** every slot target with a known price and a `needed_at`, sorted by
  `needed_at`. A `needed_at` in the past is clamped to `now`.
- **Hours to afford a target:** `max(0, price − wallet) / r`, or infinity when `r` is
  unknown and the target isn't affordable now. This hours figure is also what step 2
  compares against the tier window: `rules.labs.saving.window_hours[tier]`.
  - With income unknown, tiers S+ and S pass the window check (they are worth waiting
    for), and A, B and C are skipped unless affordable now.
  - A pinned entry is never window-checked, because it is the slot's only candidate.
  - The comparison happens at the target's `needed_at`, as
    `hours_to_afford ≤ window + hours_until_needed`.
  - So a slot freeing in 10 hours can pick an entry that needs up to window + 10 hours
    of income.
- **Reserve, with income known:** for each prefix `k` of the sorted targets,

  `need_k = Σ price_1..k − r × hours(now → needed_at_k)`,

  and `reserve = clamp(max_k need_k, 0, wallet)`.
- **Reserve, with income unknown:** reserve only the targets of **idle** slots that are
  affordable now, or that are tier S+/S (worth waiting for). Then clamp to the wallet.
  Nothing is saved ahead for running slots.
- **Workshop budget:** `workshop_budget = (wallet − reserve) × workshop_spend_limit_pct / 100`.
  This is the same formula as `coin_share.workshop_ceiling`, with the reserve in place
  of the jar.
- **Per-target output:**
  - `ready_at = now + hours_to_afford` (the "ready in ~3h" timestamp for the UI);
  - `skipped_reason`;
  - one `why` line in plain words, for example "Slot 2 frees in 6h; income covers 29k
    of 38k".
- **Unreadable wallet:** if the wallet can't be read, the result is
  `SavingPlan(reserve=None, workshop_budget=0, …)`. Workshop doesn't spend blind. This
  is the same as today's behaviour when the wallet is unread.

Evaluation happens in two passes, with no loop:
1. The ranked walk runs with each entry's `hours_to_afford` taken from the wallet alone,
   the way step 2 describes.
2. The `SavingPlan` is computed from the targets that come out of the walk.

Each target's affordability test ignores the other targets' prices. The reserve
calculation then accounts for all of them together. The design accepts this: at most
five targets, and the reserve check prevents overspending.

### 5. Strategy rules: `fleet/build_route.py` `RouteRules`

- `coins.lab_share.mode` gains `just_in_time`. Existing modes and strategies are
  unchanged.
- A new `labs.saving`:
  - `income_margin_pct`: 50–100, default 75;
  - `window_hours`: `{"S+": 72, "S": 24, "A": 12, "B": 4, "C": 0}`, each value 0–168.
- `just_in_time` works with a `lab_list` lane only. `validate` rejects `just_in_time`
  paired with a `slot_track` lane.

### 6. Template: `fleet/strategy_library.py`

The `labs_gems` template and new strategies get `lab_list`, `just_in_time` and the
following list. The `opening` and `turtle` templates keep their current labs lane.

| # | Lab | To | Tier | Pin / unlock |
|---|---|---|---|---|
| 1 | Game Speed | 7 | S+ | pin slot 1 |
| 2 | Unlock Perks | 1 | S+ | T2 W150 |
| 3 | First Perk Choice | 1 | S+ | Unlock Perks 1 |
| 4 | Perk Option Quantity | 2 | S+ | Unlock Perks 1 |
| 5 | Ban Perks | 1 | S | Unlock Perks 1 |
| 6 | Light Speed Shots | 1 | S | |
| 7 | Coins / Wave | 10 | B | |
| 8 | Labs Speed | 50 | S | T1 W150 |
| 9 | Coins / Kill Bonus | 30 | A | |
| 10 | Cash Bonus | 20 | A | |
| 11 | Attack Speed | 50 | A | |
| 12 | Health | 30 | B | |
| 13 | Damage | 30 | B | |
| 14 | Standard Perks Bonus | 10 | S | Unlock Perks 1 |
| 15 | Improve Trade-Off Perks | 5 | S | Unlock Perks 1 |
| 16 | Workshop Attack Discount | 20 | C | |
| 17 | Workshop Defense Discount | 20 | C | |
| 18 | Workshop Utility Discount | 20 | C | |
| 19 | Labs Speed | 99 | A | |

The unlock conditions live in the catalog; the table repeats them for the reader.

Not in the template:
- Cash/Wave, and Coins/Wave past level 10;
- Starting Cash ("a trap");
- Black Hole Damage and bot-cooldown labs (they can't be undone);
- Reroll Shards, which the Discord guide calls "very important" but which needs
  modules (around Tier 4). It belongs in a mid-game template.

Any lab without a price table in v2 is left out of the template.

### 7. Wiring

- **Facts.** `AccountState.lab_facts` (`fleet/account_state.py`) fills:
  - `coins_per_hour`, from `recent_coins_per_hour` in `reroll-lifetime.json`;
  - `best_waves`, from the runs table.
- **Worker.** In `fleet/reroll_progress.py` `shopping_policy`, when the mode is
  `just_in_time`, the Workshop ceiling is `SavingPlan.workshop_budget` rather than
  `coin_share.workshop_ceiling(route, wallet, jar)`. The other caps (plan price, starter
  cap, filler share, utility cap) still apply on top.
- **Jar.** The jar is not grown or reset in this mode. `LabCoinJar.settle` already
  zeroes itself for modes other than `save_pct`.
- **Lab start.** `choose_lab_action` is unchanged. It still starts only automated,
  confirmed-idle, affordable targets, which today means slot-1 Game Speed.
- **Preview and Fleet State.** The Studio preview (`previewBuildRoute`) and the Fleet
  State Labs section get the `SavingPlan` from the same function, along with the
  `LabPlan`.
  - `GET /api/fleet/labs` adds `saving: {reserve, workshop_budget, targets[], why[]}`
    for each account.

### 8. Studio: minimal read-only view

`LabSlotPlanner.tsx` detects a `lab_list` lane and shows it read-only:
- the ranked entries (lab, to-level, tier, pin);
- a notice: "Ranked list: editing arrives with the planner".

The rules panel shows the `just_in_time` mode and the save windows as editable fields,
since they're simple form fields. Strategies that use `slot_track` keep the current
editor.

## Error handling

- **Unknown fact.** An unknown wallet, income, level, unlock fact or price is never
  treated as 0 or as "not met". It produces a skip reason, or a conservative reserve.
- **Catalog.** A catalog that fails to load keeps today's behaviour: the loader raises,
  and routes that reference labs fail validation.
- **Stale strategy.** A stale or invalid strategy fails validation at save time, as
  today, so the worker never evaluates an invalid list.

## Testing

New test files:
- `tests/test_lab_list.py`, covering validation:
  - no mixing with `slot_track`;
  - duplicate labs must have increasing levels;
  - the slot-1 Game Speed rule;
  - unknown lab ids and tiers.
- `tests/test_lab_list_eval.py`, covering evaluation:
  - a pin holding slot 1, then releasing once maxed;
  - each skip reason;
  - two slots never picking the same lab;
  - a running slot targeting at `completes_at`;
  - the Labs Speed 99 entry starting at 51;
  - the unowned slots 3–5 being ignored;
  - the "Start manually" note.
- `tests/test_lab_saving.py`, covering saving:
  - an idle unaffordable target reserves its full price;
  - a slot freeing in 10h reserves only the uncovered part;
  - two targets add up;
  - the window skip happens at `needed_at`;
  - unknown income;
  - an unread wallet;
  - the reserve is capped at the wallet.
- `tests/test_lab_catalog_v2.py`, covering the catalog:
  - the shipped file loads;
  - levels are monotonic and their count matches;
  - the v1 unlock shape is read correctly;
  - unlock references are valid.
- `tests/test_lab_strategy_template.py`, covering the template:
  - it validates;
  - it contains no excluded labs.

Regression, running only the files next to the change:
- the existing `resource_blocks` tests;
- the existing `coin_share` tests;
- the existing `reroll_progress` tests;
- the existing `build_route` tests;
- the existing `strategy_library` tests;
- the `LabSlotPlanner` and `StrategyRules` vitest files.

No full-suite runs.

## Acceptance

- A strategy made from the `labs_gems` template shows the 19-entry ranked list in the
  Studio. The Labs & Gems page shows each owned slot's target, with the "Start manually"
  note on anything other than Game Speed.
- With slot 1 idle and Game Speed L4 unaffordable, Workshop spends nothing until the
  wallet covers L4. Once it is affordable, the worker starts it as today.
- With slot 1 running Game Speed and 10h left, Workshop keeps spending, holding back only
  `price − 0.75 × income × 10h`.
- Existing `slot_track` strategies behave exactly as before.
