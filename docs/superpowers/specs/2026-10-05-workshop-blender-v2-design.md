# Workshop Blender v2: unlock missing skills, buy by value per coin

## Goal

Make Workshop coin spending on Blender accounts (best Tier 1 wave ~400+)
follow the build's real needs:

1. Unlock the skills the strategy wants before levelling anything else,
   saving coins when an unlock is not yet affordable.
2. Spend the rest by value per coin: each skill gets a weight, and the bot
   buys the eligible skill with the lowest `price ÷ weight`.

Success: replaying worker Tiramisu64_83's recorded facts buys Unlock Free
Upgrades, then holds for and buys Unlock Knockback; level-0 skills that the
strategy weights (e.g. Crit Chance at 50 coins) are bought before 6k levels;
Defense Absolute is never bought by the Workshop lane.

## Evidence that motivated it

Saved strategy `blender` v7 (workers 82, 83) is one weighted pool of 18 ids,
every weight 1, decay 20%. Observed on 2026-10-05:

- Pool skills sit behind unlocks the pool does not list. Worker 83 (895k
  lifetime coins) never bought Unlock Knockback (5k) or Unlock Orbs (15k), so
  Knockback, Orbs and Orb Speed were never eligible.
- The draw spends every visit on ~6k levels, so 10–15k unlock tiles are
  almost never affordable at decision time.
- Cheap skills outside the pool stay at level 0: Health Regen 30, Crit
  Chance/Factor 50, Range 50, the three Free Upgrades 75–100 (worker 82),
  Unlock Free Upgrades 800 (worker 83).
- Equal draw odds ignore price, so a 16k Thorns level and a 30-coin level
  are equally likely picks.

Game facts (wiki Workshop page and Blender guide, verified 2026-10-05):
each Workshop tab's unlocks are a strict chain. Defense runs Def 75 → Thorns
500 → Lifesteal 2k → Knockback 5k → Orbs 15k → Shockwave 100k → … Utility
runs Cash 40 → Coins 100 → Free Upgrades 800 → Interest 5k → Recovery
Packages 1.5M → … Orbs instantly kill non-boss, non-elite enemies. Tier 1
elites, which are immune to orbs, begin at wave 500.

## Design

### 1. `workshop_unlocks.path_to(upgrade_id, owned)`

Returns the ordered tuple of `UnlockGroup`s still to buy on the skill's tab
before the skill is available. The tuple is empty for starters and for owned
groups. It runs from `next_group(category, owned)` through the skill's own
group, in `GROUPS` order. Pure function, no I/O.

### 2. New block type `unlock` (Workshop lane only)

```json
{"id": "...", "type": "unlock", "label": "Unlock missing skills",
 "upgrade_ids": ["knockback_chance", "orbs", "max_recovery"],
 "max_price": 20000, "hold": true}
```

- `upgrade_ids`: 1–30 levelled skill ids, validated like pool ids. Unlock-tile
  ids are rejected. Name the skill you want, not its tile.
- `max_price` (optional int > 0): unlock steps priced above it are ignored.
  The step price is the observed price, else the catalog unlock price.
- `hold` (bool, default true).

Evaluation:

1. For each listed skill, take `path_to`. A path whose first step has no
   `executable_upgrade_id` adds a trace entry `manual unlock needed: <name>`
   and is skipped. A first step over `max_price` adds `over unlock price
   limit` and is skipped.
2. The candidate set is the distinct first steps of the remaining paths, at
   most one per tab. Each candidate is passed through the shared `eligible()`
   check: Never Buy, availability, budget and prerequisites.
3. If any candidate's price is unread, observe it with the existing
   `observe_prices`.
4. If any candidate is affordable (price ≤ ceiling), buy the cheapest one.
   Ties break on tab order ATTACK, DEFENSE, UTILITY.
5. Otherwise save for the cheapest candidate. With `hold: true`, return the
   saving choice now (`save_coins`, nothing later spends). With
   `hold: false`, set `saving` the same way `save_for` does, so a
   `while_saving` sibling can run. The block then falls through.
6. If no candidates remain, fall through. Everything is unlocked, manual, or
   over the limit.

`child_lists` returns nothing for this block. `program_upgrade_ids` includes
the listed skills and the executable tiles on their paths, so price
observation and the UI see them.

### 3. Pool `selection: "value"` (Workshop lane only)

- Weights are required for every listed id, as numbers > 0 and ≤ 1000.
  `decay_pct` and `weight_floor` are rejected with this selection.
- Candidates come from the existing `pool_candidates` filters: caps, targets,
  eligibility, price cap and wallet share. Pick the minimum
  `price / weight`. Ties break on list order.
- Before choosing, if any listed item that passes every filter except its
  price is unpriced, observe those items first. An unread item might be the
  best value. This mirrors priority pools' "observe above the pick".
- Deterministic: no pending draw record and no `eligible_odds`. The trace
  lists each candidate's `price ÷ weight` so the UI can show the ranking.
- `swap_kill_bonus`: when Coins/Kill is held and swapped to Coins/Wave, the
  stand-in keeps its own weight if it is already listed. Otherwise it
  inherits Coins/Kill's weight.

### 4. Executable Interest and Recovery Packages unlocks

- `upgrades.py`: `unlock_interest` ("Unlock Interest", UTILITY,
  unlocks=("interest_per_wave",)). Also `unlock_recovery_packages`
  ("Unlock Recovery Packages", UTILITY,
  unlocks=("recovery_amount", "max_recovery", "package_chance")).
- `catalog/concepts.v1.json`: add the matching `unlocks.*` concepts.
- `workshop_unlocks.GROUPS`: set both `executable_upgrade_id`s.
- `catalog/workshop-prices.v1.json`: single prices of 5,000 and 1,500,000.
- Test: `upgrades.resolve("Unlock Interest", "UTILITY")` returns the tile,
  and `resolve("Interest", "UTILITY")` still returns `interest_per_wave`.
- Live check before rollout: worker 82 owns Free Upgrades, so its Utility tab
  shows the Interest tile. Confirm the shopper reads that row as
  `unlock_interest` (identity read only, no purchase needed).

### 5. Blender v2 Workshop program

This replaces `_BLENDER_PRIORITY` in `_blender_workshop_template`. The wave-450
condition, the else-branch templates, and the pinned built-in upgrade logic
are unchanged; only the `then` branch content changes.

1. `unlock` "Unlock missing skills": `upgrade_ids` = knockback_chance,
   knockback_force, orbs, orb_speed, free_utility_upgrade,
   free_defense_upgrade, free_attack_upgrade, max_recovery, package_chance;
   `max_price` 20000; `hold` true. The Recovery Packages path buys Interest
   (5k) and then stops at the 1.5M step.
2. `pool` "Blender value", `selection: "value"`:

   | id | weight | target |
   |---|---|---|
   | coins_per_kill_bonus | 18 | |
   | free_utility_upgrade | 3 | |
   | free_defense_upgrade | 3 | |
   | free_attack_upgrade | 2 | |
   | cash_bonus | 3 | |
   | coins_per_wave | 2 | |
   | cash_per_wave | 2 | |
   | defense_percent | 12 | |
   | health | 9 | |
   | knockback_chance | 6 | |
   | thorns | 6 | 51 |
   | lifesteal | 4 | 3.5 |
   | orb_speed | 4 | |
   | orbs | 3 | 3 |
   | knockback_force | 3 | |
   | attack_speed | 10 | |
   | damage | 4 | |
   | critical_chance | 2 | |
   | multishot_targets | 2 | 5 |
   | multishot_chance | 1 | |
   | critical_factor | 1 | |

3. `wait`.

Not listed, so never bought by this lane: defense_absolute, health_regen,
range, damage_per_meter, rapid fire, bounce shot, interest_per_wave,
shockwave and later skills. Skills already maxed have no price row, so they
drop out naturally.

Out of scope: a best-wave-500 stage that shifts weight to Health and Thorns,
and saving for Recovery Packages. Both are later strategy edits that need no
engine work.

### 6. Strategy Studio UI (web/ui)

- `lib/strategyStudio.ts`: `unlock` block type and `"value"` selection.
- `StrategyBlockInspector.tsx`:
  - unlock block: skill multi-select, max price, hold toggle
  - value pool: weight inputs (shared with weighted), plus a "next buys"
    preview ranked by `price ÷ weight` from the last facts when available
- `strategyBlocks.ts`: "Unlock missing skills" preset (group Logic), title,
  summary and guide entry. `StrategyCanvas.tsx` icon.
- Guide page: one example for each.

### 7. Rollout

- `fleet/blender_v2_update.py`, modelled on `fleet/blender_strategy_update.py`.
  - Loads the latest `blender` saved strategy and replaces its Workshop lane
    with the v2 program. It keeps the lane's other fields and leaves the
    battle lane unchanged.
  - Previews through `/api/fleet/reroll/route/preview`.
  - With `--apply`, saves a new version and assigns workers Tiramisu64_82 and
    Tiramisu64_83 to it. The default is a dry run printing the diff.
- Apply only after the live Interest-tile check and with the user's go-ahead.

## Error handling

- Missing purchase counts or stale evidence keep today's behaviour, which is
  wait with a trace reason.
- An unlock bought manually is detected through `owned_groups` (a child is
  visible or bought), and the path shortens accordingly.
- If the bot misreads an unlock tile, the shopper's `unknown_identity` skip
  still applies. The block then keeps saving, and the trace shows the
  pending step.

## Testing

Run only the touched test files, with `-p no:allure_pytest`.

- `tests/test_workshop_unlocks.py`:
  - `path_to` for starters, owned skills and multi-step paths (Lifesteal
    owned → Orbs = [Knockback, Orbs])
  - a path blocked by a non-executable tile
  - Interest and Recovery Packages now reachable
- `tests/test_strategy_blocks.py`:
  - unlock block: validation, cheapest affordable step across tabs, hold
    vs no hold, `max_price` skip, manual-unlock trace, observation of
    unread steps
  - value pool: validation, argmin `price ÷ weight`, ties, targets,
    observation of unread items, kill-bonus swap weight
  - Blender v2 template: a replay of worker 83's recorded facts
    (Free Upgrades, then Knockback)
- `tests/test_strategy_templates.py`: update Blender expectations.
- `tests/test_autopilot_policy.py` and `tests/test_concepts.py`: catalog
  counts and the new unlock concepts. A resolve test pins the "Unlock
  Interest" vs "Interest" naming.
- `tests/test_blender_v2_update.py`: dry run and apply against a fake
  coordinator.
- UI: `strategyBlocks.test.ts` and `StrategyStudio.test.tsx` cases for the
  new block and selection.
