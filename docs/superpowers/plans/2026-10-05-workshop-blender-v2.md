# Workshop Blender v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Blender accounts unlock the Workshop skills their strategy needs before levelling anything else, then spend coins by value per coin (`price ÷ weight`).

**Architecture:** Add an `unlock` block and a `value` pool selection to the Workshop block engine in `fleet/strategy_blocks.py`. A small `path_to` helper in `workshop_unlocks.py` drives the unlock block. Two more unlock tiles (Interest, Recovery Packages) become executable catalog entries. The built-in Blender branch and the saved `blender` strategy switch to the new program. Strategy Studio (Next.js, `web/ui`) gets editor support.

**Tech Stack:** Python 3.12 (uv, pytest), Next.js + TypeScript (vitest, Testing Library).

**Spec:** `docs/superpowers/specs/2026-10-05-workshop-blender-v2-design.md`

## Global Constraints

- Run only the test files a task touches: `uv run pytest <file> -q -p no:allure_pytest`. Never run `pytest tests/` or a directory.
- UI tests: `cd web/ui && npx vitest run <file>`. If `web/ui/node_modules` is missing in this worktree, run `npm ci` in `web/ui` first.
- Commit steps need the user's go-ahead (user CLAUDE.md: never commit automatically). Batch them if the user approves commits for this plan. `docs/` is gitignored, so docs files need `git add -f`.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Never push to `main`. Never edit fleet data under `~/.local/share/thetowerbot/` by hand. Strategy changes go through the coordinator API (`127.0.0.1:8765`), only in Task 8, and only after the user approves.
- Workshop-only features: the `unlock` block and `selection: "value"` are rejected for the battle lane.
- Unlock steps cost at most `max_price`. Blender v2 uses `max_price` 20000 and `hold` true.
- Value-pool weights are integers 1..100000, the existing pool weight rule. (The spec's "≤ 1000" is superseded by the existing validator range.)

Blender v2 weights (sum 100) and targets, copied from the spec:

| id | weight | target |
|---|---|---|
| coins_per_kill_bonus | 18 | |
| defense_percent | 12 | |
| attack_speed | 10 | |
| health | 9 | |
| knockback_chance | 6 | |
| thorns | 6 | 51 |
| damage | 4 | |
| lifesteal | 4 | 3.5 |
| orb_speed | 4 | |
| free_utility_upgrade | 3 | |
| free_defense_upgrade | 3 | |
| cash_bonus | 3 | |
| orbs | 3 | 3 |
| knockback_force | 3 | |
| free_attack_upgrade | 2 | |
| coins_per_wave | 2 | |
| cash_per_wave | 2 | |
| critical_chance | 2 | |
| multishot_targets | 2 | 5 |
| multishot_chance | 1 | |
| critical_factor | 1 | |

## Deviations from the spec (decided while planning)

1. **Unlock steps are not observed just for ranking.** An unlock has one fixed price, so the catalog cost (`UnlockGroup.cost`) stands in when no price was read. The block only observes a step when it is about to buy it and the price is unread. Buy decisions need a real price, and `coin_budget` uses it.
2. **The unlock list covers every gated skill in the value pool**, plus `max_recovery` and `package_chance`. The spec listed only Knockback, Orbs and Free Upgrades, which would leave Multishot (Range → Multishot) unreachable on a fresh account.
3. **No coordinator preview call in the update script.** Workshop previews report "Account evidence is stale" most of the time. The script's dry run prints the planned change instead, matching `fleet/blender_strategy_update.py`.
4. **The value-pool UI preview shows each skill's coin share at balance** (`weight ÷ total`), not a ranking from live facts. The inspector has no facts to rank with.
5. **The live Interest-tile check happens after rollout (Task 8).** It is safe because the shopper re-reads every row's identity before tapping, and a misread row is skipped as `unknown_identity`.

## Review Focus

- **Unlock evidence with gaps.** A later group (Orbs) is proven owned, but an earlier tile (Knockback) is not. The unlock block should buy the missing earlier tile, not stall. Test: `test_unlock_block_fills_an_evidence_gap_in_the_chain` (Task 4).
- **Coins/Kill held at low best waves inside a value pool.** Coins/Wave must inherit Coins/Kill's weight, and the pick must still be valid. Test: `test_value_pool_kill_bonus_stand_in_inherits_weight` (Task 3).
- **A cheaper item whose price was never read.** The value pool must read it before buying a more expensive known item. Test: `test_value_pool_reads_unpriced_items_before_buying` (Task 3).
- **Defense Absolute is cheap but not in the Blender pool.** The program must wait, never fall through to it. Test: `test_blender_v2_never_buys_defense_absolute` (Task 5).
- **A weighted pool switched to Value in the editor.** It still carries `decay_pct`/`weight_floor`, which the server rejects for value pools. The inspector must drop them. Test: `"switching a weighted pool to value drops decay settings"` (Task 6).

---

### Task 1: Make Interest and Recovery Packages unlock tiles executable

**Files:**
- Modify: `upgrades.py` (insert before `_upgrade("interest_per_wave", ...)`, currently line 255, and before `_upgrade("recovery_amount", ...)`)
- Modify: `catalog/concepts.v1.json` (insert before the `stats.interest_per_wave` line, currently 64, and before `stats.recovery_amount`)
- Modify: `workshop_unlocks.py` (the two `GROUPS` rows)
- Modify: `catalog/workshop-prices.v1.json` (after the `unlock_thorns` entry)
- Test: `tests/test_workshop_unlocks.py`, `tests/test_concepts.py`, `tests/test_autopilot_policy.py`

**Interfaces:**
- Produces: catalog ids `unlock_interest` and `unlock_recovery_packages`. Both are `unlock=True` and UTILITY. `workshop_unlocks.GROUPS` entries `unlock_interest` / `unlock_recovery_packages` get `executable_upgrade_id` equal to their id.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workshop_unlocks.py`:

```python
def test_interest_and_recovery_unlock_tiles_are_executable_and_named_apart() -> None:
    assert upgrades.resolve("Unlock Interest", "UTILITY").id == "unlock_interest"
    assert upgrades.resolve("Interest", "UTILITY").id == "interest_per_wave"
    assert upgrades.resolve("Unlock Recovery Packages", "UTILITY").id == "unlock_recovery_packages"
    owned = owned_groups(purchased_ids={"unlock_cash_bonuses", "unlock_coin_bonuses", "unlock_free_upgrades"})
    assert available("unlock_interest", owned)
    owned |= owned_groups(purchased_ids={"unlock_interest"})
    assert available("unlock_recovery_packages", owned)
    assert gate_for("max_recovery").executable_upgrade_id == "unlock_recovery_packages"
```

In the same file, change `assert len(upgrades.CATALOG) == 59` to `assert len(upgrades.CATALOG) == 62`. It is already stale at 60 since the Bounce Shot commit.

In `tests/test_concepts.py`, change both `== 60` assertions (lines 19–20) to `== 62`.

In `tests/test_autopilot_policy.py`:
- change `assert len(by_category["UTILITY"]) == 16` to `== 18`
- add `"unlock_interest",` and `"unlock_recovery_packages",` to the unlock-id set literal
- add `("Unlock Interest", "UTILITY", "unlock_interest"),` to the resolve parametrize list that holds `("Unlock Bounce Shot Upgrades", "ATTACK", "unlock_bounce_shot")`
- add `("unlock_interest", ("interest_per_wave",)),` and `("unlock_recovery_packages", ("recovery_amount", "max_recovery", "package_chance")),` to the unlock-children parametrize list that holds `("unlock_bounce_shot", (...))`

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_workshop_unlocks.py tests/test_concepts.py tests/test_autopilot_policy.py -q -p no:allure_pytest`
Expected: FAIL. `resolve("Unlock Interest")` is None, and the counts are off.

- [ ] **Step 3: Add the catalog entries**

In `upgrades.py`, directly before `_upgrade("interest_per_wave", "Interest / Wave", ...)`:

```python
    _upgrade(
        "unlock_interest",
        "Unlock Interest",
        "UTILITY",
        unlock=True,
        unlocks=("interest_per_wave",),
    ),
```

Directly before `_upgrade("recovery_amount", "Recovery Amount", ...)`:

```python
    _upgrade(
        "unlock_recovery_packages",
        "Unlock Recovery Packages",
        "UTILITY",
        unlock=True,
        unlocks=("recovery_amount", "max_recovery", "package_chance"),
    ),
```

In `catalog/concepts.v1.json`, insert directly before the `stats.interest_per_wave` line (keep the trailing comma style of neighbouring lines):

```json
    {"concept_id": "unlocks.interest", "name": "Unlock Interest", "domain": "unlocks", "kind": "unlock", "aliases": [], "legacy_id": "unlock_interest", "unit": null, "caps": null, "prerequisites": null, "source_refs": ["legacy-upgrades"], "source_url": null, "validity": {"status": "unknown", "game_version_min": null, "game_version_max": null}, "definition_verified": false, "rule_verified": false, "execution_scopes": ["battle", "workshop"], "unlocks": ["stats.interest_per_wave"]},
```

Directly before the `stats.recovery_amount` line:

```json
    {"concept_id": "unlocks.recovery_packages", "name": "Unlock Recovery Packages", "domain": "unlocks", "kind": "unlock", "aliases": [], "legacy_id": "unlock_recovery_packages", "unit": null, "caps": null, "prerequisites": null, "source_refs": ["legacy-upgrades"], "source_url": null, "validity": {"status": "unknown", "game_version_min": null, "game_version_max": null}, "definition_verified": false, "rule_verified": false, "execution_scopes": ["battle", "workshop"], "unlocks": ["stats.recovery_amount", "stats.max_recovery", "stats.package_chance"]},
```

In `workshop_unlocks.py`, set the executable ids:

```python
    UnlockGroup("unlock_interest", "UTILITY", "Unlock Interest",
                ("interest_per_wave",), "unlock_interest", 5000),
    UnlockGroup("unlock_recovery_packages", "UTILITY", "Unlock Recovery Packages",
                ("recovery_amount", "max_recovery", "package_chance"), "unlock_recovery_packages", 1_500_000),
```

In `catalog/workshop-prices.v1.json`, after the `"unlock_thorns": {...}` entry (add a comma after its closing brace):

```json
    "unlock_interest": {
      "source_url": "https://the-tower-idle-tower-defense.game-vault.net/wiki/Workshop",
      "next_coins": [
        5000
      ]
    },
    "unlock_recovery_packages": {
      "source_url": "https://the-tower-idle-tower-defense.game-vault.net/wiki/Workshop",
      "next_coins": [
        1500000
      ]
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_workshop_unlocks.py tests/test_concepts.py tests/test_autopilot_policy.py tests/test_workshop_prices.py -q -p no:allure_pytest`
Expected: PASS. If `tests/test_workshop_prices.py` asserts an exact set of unlock price keys, add the two new ids to it.

- [ ] **Step 5: Commit**

```bash
git add upgrades.py catalog/concepts.v1.json catalog/workshop-prices.v1.json workshop_unlocks.py tests/test_workshop_unlocks.py tests/test_concepts.py tests/test_autopilot_policy.py
git commit -m "Make Interest and Recovery Packages unlocks executable

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `workshop_unlocks.path_to`

**Files:**
- Modify: `workshop_unlocks.py` (add a function after `next_group`)
- Test: `tests/test_workshop_unlocks.py`

**Interfaces:**
- Produces: `path_to(upgrade_id: str, owned: set[str]) -> tuple[UnlockGroup, ...]`. The result is the unlock groups still missing before `upgrade_id` is available, in tab order and including the skill's own group. It is `()` for starters, unknown ids and owned groups.

- [ ] **Step 1: Write the failing test**

Add `path_to` to the import list at the top of `tests/test_workshop_unlocks.py`, then append:

```python
def test_path_to_lists_missing_unlocks_in_tab_order() -> None:
    assert path_to("damage", set()) == ()
    assert path_to("unknown", set()) == ()
    assert [group.id for group in path_to("cash_bonus", set())] == ["unlock_cash_bonuses"]
    owned = owned_groups(purchased_ids={"unlock_defense_upgrades", "unlock_thorns", "unlock_lifesteal"})
    assert path_to("lifesteal", owned) == ()
    assert [group.id for group in path_to("orbs", owned)] == ["unlock_knockback", "unlock_orbs"]
    assert [group.id for group in path_to("land_mine_chance", owned)] == [
        "unlock_knockback", "unlock_orbs", "unlock_shockwave", "unlock_land_mines"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_workshop_unlocks.py -q -p no:allure_pytest`
Expected: FAIL with `ImportError: cannot import name 'path_to'`.

- [ ] **Step 3: Implement**

In `workshop_unlocks.py`, after `next_group`:

```python
def path_to(upgrade_id: str, owned: set[str]) -> tuple[UnlockGroup, ...]:
    """Unlock groups still to buy, in tab order, before ``upgrade_id`` is available."""
    target = gate_for(upgrade_id)
    if target is None or target.id in owned:
        return ()
    chain = [group for group in GROUPS if group.category == target.category]
    return tuple(group for group in chain[:chain.index(target) + 1] if group.id not in owned)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_workshop_unlocks.py -q -p no:allure_pytest`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add workshop_unlocks.py tests/test_workshop_unlocks.py
git commit -m "Add Workshop unlock path helper

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Pool `selection: "value"`

**Files:**
- Modify: `fleet/strategy_blocks.py`. Edit `validate_program`'s pool branch (selection check near line 89, weights loop near line 117) and its `save_for` branch (near line 212). Edit `evaluate_program`'s pool branch (the priority-observe block near line 1023 and the selection chain near line 1065).
- Test: `tests/test_strategy_blocks.py`

**Interfaces:**
- Produces: a pool `selection` value `"value"`, Workshop only. It requires `weights` for every listed id and rejects `decay_pct`/`weight_floor`. It cannot be a `save_for` goal. At runtime it picks the minimum `price / weight` (ties go to list order) and adds the trace line `"<block id>: value ranking <id> <ratio>, ..."`. There is no pending draw and `eligible_odds` stays empty.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_strategy_blocks.py`:

```python
def value_pool(**extra: Any) -> dict[str, Any]:
    return pool(selection='value', **{'weights': {'damage': 1, 'attack_speed': 4}, **extra})


def test_value_pool_buys_lowest_price_per_weight() -> None:
    result = blocks.evaluate_program(route([value_pool()]), facts(), None, 'workshop')
    assert result.decision.upgrade_id == 'attack_speed'  # 81 / 4 beats 80 / 1
    assert any('value ranking' in item for item in result.trace.rejected)
    assert not result.trace.eligible_odds


def test_value_pool_ties_follow_list_order() -> None:
    sample = replace(facts(), prices={'damage': 80, 'attack_speed': 80})
    program = [value_pool(weights={'damage': 2, 'attack_speed': 2})]
    assert blocks.evaluate_program(route(program), sample, None, 'workshop').decision.upgrade_id == 'damage'


def test_value_pool_skips_unaffordable_and_reached_targets() -> None:
    sample = replace(facts(), prices={'damage': 80, 'attack_speed': 400}, values={'damage': 50.0})
    result = blocks.evaluate_program(route([value_pool(targets={'damage': 50})]), sample, None, 'workshop')
    assert result.decision is None


def test_value_pool_reads_unpriced_items_before_buying() -> None:
    program = [pool(upgrade_ids=['damage', 'health'], selection='value', weights={'damage': 1, 'health': 1})]
    result = blocks.evaluate_program(route(program), facts(), None, 'workshop')
    assert (result.decision.state, result.decision.upgrade_id) == ('observe_price', 'health')


def test_value_pool_kill_bonus_stand_in_inherits_weight() -> None:
    held = route([pool(upgrade_ids=['coins_per_kill_bonus', 'damage'], selection='value',
                       weights={'coins_per_kill_bonus': 18, 'damage': 1})])
    held.rules = RouteRules(coins=CoinRules(kill_bonus_min_best_wave=500))
    sample = replace(facts(), prices={'damage': 80, 'coins_per_wave': 90},
                     purchases={'unlock_cash_bonuses': 1, 'unlock_coin_bonuses': 1})
    result = blocks.evaluate_program(held, sample, None, 'workshop')
    assert result.decision.upgrade_id == 'coins_per_wave'  # 90 / 18 beats 80 / 1


@pytest.mark.parametrize(('extra', 'message'), [
    ({'weights': {'damage': 1}}, 'weight for every upgrade'),
    ({'weights': {'damage': 1, 'attack_speed': 1}, 'decay_pct': 10}, 'decay or weight floor'),
    ({'weights': {'damage': 1, 'attack_speed': 1}, 'weight_floor': 1}, 'decay or weight floor'),
])
def test_value_pool_validation(extra: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        blocks.validate_program([pool(selection='value', **extra)], 'workshop')


def test_value_selection_is_workshop_only_and_not_a_saving_goal() -> None:
    good = pool(selection='value', weights={'damage': 1, 'attack_speed': 1})
    blocks.validate_program([good], 'workshop')
    with pytest.raises(ValueError, match='only available in the Workshop'):
        blocks.validate_program([good], 'battle')
    with pytest.raises(ValueError, match='value pool'):
        blocks.validate_program([{'id': 'save', 'type': 'save_for', 'goal': [good]}], 'workshop')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_strategy_blocks.py -q -p no:allure_pytest -k value`
Expected: FAIL with `ValueError: unknown pool selection`.

- [ ] **Step 3: Implement validation**

In `validate_program`'s pool branch, replace:

```python
                if block.get('selection', 'priority') not in {'priority', 'weighted', 'cheapest'}:
                    raise ValueError('unknown pool selection')
```

with:

```python
                if block.get('selection', 'priority') not in {'priority', 'weighted', 'cheapest', 'value'}:
                    raise ValueError('unknown pool selection')
                if block.get('selection') == 'value' and lane != 'workshop':
                    raise ValueError('value selection is only available in the Workshop')
```

Directly after the existing loop:

```python
                for weight in weights.values():
                    number(weight, 'weight', 1, 100000)
```

add:

```python
                if block.get('selection') == 'value':
                    if set(weights) != set(ids):
                        raise ValueError('value selection needs a weight for every upgrade')
                    if 'decay_pct' in block or 'weight_floor' in block:
                        raise ValueError('value selection does not use decay or weight floor')
```

In the `save_for` branch, after the price-comparison check, add:

```python
                if block['goal'][0].get('selection') == 'value':
                    raise ValueError('a saving goal cannot be a value pool')
```

- [ ] **Step 4: Implement evaluation**

In `evaluate_program`'s pool branch, directly after the existing block that starts `if lane == 'workshop' and block.get('selection', 'priority') == 'priority':` and ends `return observation`, add:

```python
                if lane == 'workshop' and block.get('selection') == 'value':
                    # The cheapest value may be an item whose price is unread.
                    if observation := observe_prices(
                            identity, [uid for uid in unpriced[before_unpriced:]
                                       if uid in block['upgrade_ids'] and price_for(uid) is None]):
                        return observation
```

In the selection chain, between the `if block.get('selection', 'priority') == 'cheapest':` branch and `elif block.get('selection', 'priority') == 'weighted':`, insert:

```python
                elif block.get('selection') == 'value':
                    ranking = sorted(candidates, key=lambda uid: (
                        price_for(uid) / candidates[uid], block['upgrade_ids'].index(uid)))
                    chosen = ranking[0]
                    rejected.append(f'{identity}: value ranking ' + ', '.join(
                        f'{uid} {price_for(uid) / candidates[uid]:.1f}' for uid in ranking))
```

(`candidates[uid]` equals the configured weight, because value pools have no decay and the floor defaults to 1.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_strategy_blocks.py -q -p no:allure_pytest`
Expected: PASS (whole file, since existing pool tests share the edited code).

- [ ] **Step 6: Commit**

```bash
git add fleet/strategy_blocks.py tests/test_strategy_blocks.py
git commit -m "Add value-per-coin Workshop pool selection

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `unlock` block

**Files:**
- Modify: `fleet/strategy_blocks.py`. Add a `validate_program` branch before `elif kind == 'condition':`. Extend `program_upgrade_ids`. Extend the `evaluate_program` import line `from workshop_unlocks import available`. Add an evaluate branch before `elif kind == 'while_saving':`.
- Test: `tests/test_strategy_blocks.py`

**Interfaces:**
- Consumes: `workshop_unlocks.path_to` (Task 2).
- Produces: block `{"type": "unlock", "upgrade_ids": [levelled skill ids, 1..30], "max_price"?: int 1..1e12, "hold"?: bool (default true)}`, Workshop only. Decisions:
  - buy the cheapest affordable tile → `state 'buy'`
  - an unread affordable tile → `'observe_price'`
  - otherwise, with hold on → `'save_coins'` for the cheapest tile
  - with hold off → it records `saving` (as `save_for` does) and falls through

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_strategy_blocks.py`:

```python
def unlock(**extra: Any) -> dict[str, Any]:
    return {'id': 'unlock', 'type': 'unlock', 'upgrade_ids': ['lifesteal'], **extra}


def test_unlock_block_validation() -> None:
    blocks.validate_program([unlock(max_price=20000, hold=False)], 'workshop')
    for bad in (unlock(upgrade_ids=[]), unlock(upgrade_ids=['unlock_lifesteal']), unlock(hold='yes'),
                unlock(max_price=0), unlock(upgrade_ids=['lifesteal', 'lifesteal'])):
        with pytest.raises(ValueError):
            blocks.validate_program([bad], 'workshop')
    with pytest.raises(ValueError, match='only available in the Workshop'):
        blocks.validate_program([unlock()], 'battle')


def test_unlock_block_buys_the_cheapest_affordable_next_unlock_across_tabs() -> None:
    sample = replace(facts(), prices={**facts().prices, 'unlock_lifesteal': 90, 'unlock_range_upgrades': 50})
    result = blocks.evaluate_program(route([unlock(upgrade_ids=['lifesteal', 'range'])]), sample, None, 'workshop')
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'unlock_range_upgrades')


def test_unlock_block_walks_the_tab_chain_one_step_at_a_time() -> None:
    sample = replace(facts(), prices={**facts().prices, 'unlock_lifesteal': 90})
    result = blocks.evaluate_program(route([unlock(upgrade_ids=['orbs'])]), sample, None, 'workshop')
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'unlock_lifesteal')


def test_unlock_block_fills_an_evidence_gap_in_the_chain() -> None:
    gap = replace(facts(), purchases={'unlock_defense_upgrades': 1, 'unlock_thorns': 1,
                                      'unlock_lifesteal': 1, 'unlock_orbs': 1},
                  prices={**facts().prices, 'unlock_knockback': 90})
    result = blocks.evaluate_program(route([unlock(upgrade_ids=['knockback_chance', 'orbs'])]), gap, None, 'workshop')
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'unlock_knockback')


def test_unlock_block_holds_coins_by_default() -> None:
    sample = replace(facts(), prices={**facts().prices, 'unlock_lifesteal': 2000})
    held = blocks.evaluate_program(route([unlock(), pool()]), sample, None, 'workshop')
    assert (held.decision.state, held.decision.upgrade_id) == ('save_coins', 'unlock_lifesteal')
    spending = blocks.evaluate_program(route([unlock(hold=False), pool()]), sample, None, 'workshop')
    assert (spending.decision.state, spending.decision.upgrade_id) == ('buy', 'damage')


def test_unlock_block_without_hold_feeds_while_saving() -> None:
    sample = replace(facts(), prices={**facts().prices, 'unlock_lifesteal': 2000})
    program = [unlock(hold=False),
               {'id': 'ws', 'type': 'while_saving', 'upgrade_id': 'unlock_lifesteal',
                'blocks': [{'id': 'hold', 'type': 'wait'}]},
               pool()]
    result = blocks.evaluate_program(route(program), sample, None, 'workshop')
    assert result.decision is None
    assert result.trace.matched_rule_id == 'hold'


def test_unlock_block_respects_max_price_and_reports_manual_unlocks() -> None:
    sample = replace(facts(), prices={**facts().prices, 'unlock_lifesteal': 2000})
    capped = blocks.evaluate_program(route([unlock(max_price=1000), pool()]), sample, None, 'workshop')
    assert capped.decision.upgrade_id == 'damage'
    assert any('over unlock price limit' in item for item in capped.trace.rejected)
    late = replace(facts(), purchases={uid: 1 for uid in ('unlock_defense_upgrades', 'unlock_thorns',
                                                          'unlock_lifesteal', 'unlock_knockback', 'unlock_orbs')})
    manual = blocks.evaluate_program(route([unlock(upgrade_ids=['land_mine_chance']), pool()]),
                                     late, None, 'workshop')
    assert manual.decision.upgrade_id == 'damage'
    assert any('manual unlock needed: Unlock Shockwave' in item for item in manual.trace.rejected)


def test_unlock_block_reads_an_unpriced_affordable_unlock() -> None:
    sample = replace(facts(), purchases={})
    result = blocks.evaluate_program(route([unlock(upgrade_ids=['cash_bonus']), pool()]), sample, None, 'workshop')
    assert (result.decision.state, result.decision.upgrade_id) == ('observe_price', 'unlock_cash_bonuses')


def test_unlock_block_passes_when_every_skill_is_reachable_or_banned() -> None:
    reachable = blocks.evaluate_program(route([unlock(upgrade_ids=['thorns', 'damage']), pool()]),
                                        facts(), None, 'workshop')
    assert reachable.decision.upgrade_id == 'damage'
    sample = replace(facts(), prices={**facts().prices, 'unlock_lifesteal': 90})
    banned = blocks.evaluate_program(route([unlock(), pool()], bans=('unlock_lifesteal',)), sample, None, 'workshop')
    assert banned.decision.upgrade_id == 'damage'


def test_unlock_block_exposes_skills_and_their_unlock_tiles() -> None:
    program = blocks.validate_program([unlock(upgrade_ids=['orbs'])], 'workshop')
    assert {'orbs', 'unlock_defense_upgrades', 'unlock_knockback', 'unlock_orbs'} <= set(
        blocks.program_upgrade_ids(program))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_strategy_blocks.py -q -p no:allure_pytest -k unlock_block`
Expected: FAIL with `ValueError: unknown strategy block type`.

- [ ] **Step 3: Implement validation**

In `validate_program`, add before `elif kind == 'condition':`:

```python
            elif kind == 'unlock':
                allowed |= {'upgrade_ids', 'max_price', 'hold'}
                if lane != 'workshop':
                    raise ValueError('unlock blocks are only available in the Workshop')
                ids = block.get('upgrade_ids')
                if not isinstance(ids, (tuple, list)) or not ids or len(ids) > 30:
                    raise ValueError('unlock block requires 1 to 30 skills')
                ids = [uid(item) for item in ids]
                if len(set(ids)) != len(ids):
                    raise ValueError('duplicate unlock skill')
                if any(upgrades.by_id(item).unlock for item in ids):
                    raise ValueError('name the skill to unlock, not its unlock tile')
                block['upgrade_ids'] = ids
                if 'max_price' in block:
                    number(block['max_price'], 'max_price', 1, 1000000000000)
                if 'hold' in block and type(block['hold']) is not bool:
                    raise ValueError('hold must be true or false')
```

- [ ] **Step 4: Expose the tiles to observers**

At the top of `fleet/strategy_blocks.py`, add after `import upgrades`:

```python
from workshop_unlocks import path_to
```

In `program_upgrade_ids`, add a branch after the `elif kind == "pool":` branch:

```python
        elif kind == "unlock":
            result.extend(block["upgrade_ids"])
            for skill in block["upgrade_ids"]:
                result.extend(group.executable_upgrade_id for group in path_to(skill, set())
                              if group.executable_upgrade_id)
```

- [ ] **Step 5: Implement evaluation**

In `evaluate_program`, the existing `from workshop_unlocks import available` stays (the module-level `path_to` import covers the new name). Add before `elif kind == 'while_saving':`:

```python
            elif kind == 'unlock':
                steps: dict[str, tuple[Any, int]] = {}
                for skill in block['upgrade_ids']:
                    path = path_to(skill, workshop_owned)
                    if not path:
                        continue
                    group = path[0]
                    tile = group.executable_upgrade_id
                    if tile is None:
                        rejected.append(f'{identity}: manual unlock needed: {group.name} (for {skill})')
                        continue
                    if tile in excluded:
                        rejected.append(f'{tile}: blocked by Never Buy')
                        continue
                    if not available(tile, workshop_owned):
                        continue
                    # An unlock has one fixed price; the catalog figure ranks it until read.
                    price = price_for(tile)
                    price = group.cost if price is None else price
                    if price is None:
                        continue
                    if 'max_price' in block and price > block['max_price']:
                        rejected.append(f'{identity}: {group.name} over unlock price limit')
                        continue
                    steps.setdefault(tile, (group, price))
                if not steps:
                    continue
                tab_order = {'ATTACK': 0, 'DEFENSE': 1, 'UTILITY': 2}
                ranked = sorted(steps, key=lambda tile: (steps[tile][1], tab_order[steps[tile][0].category]))
                for tile in ranked:
                    group, price = steps[tile]
                    if price > ceiling or (budget_room is not None and price > budget_room):
                        continue
                    if price_for(tile) is None:
                        if observation := observe_prices(identity, [tile]):
                            return observation
                        continue
                    return _Choice(identity, tile, f'Unlock {group.name} for skills in this strategy')
                group, price = steps[ranked[0]]
                goal = _Choice(identity, ranked[0], f'Saving for {group.name} ({wallet}/{price} coins)',
                               wait=True, save_price=price)
                if block.get('hold', True):
                    return goal
                if saving is None:
                    saving = goal
```

(`saving` is already declared `nonlocal` at the top of `evaluate`.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_strategy_blocks.py tests/test_workshop_unlocks.py -q -p no:allure_pytest`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add fleet/strategy_blocks.py tests/test_strategy_blocks.py
git commit -m "Add Workshop unlock block that saves for missing skills

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Blender v2 built-in program

**Files:**
- Modify: `fleet/strategy_blocks.py`. Replace `_BLENDER_PRIORITY` (near line 246) and `_blender_workshop_template` (near line 322).
- Modify: `fleet/blender_strategy_update.py` (`add_blender_gate`)
- Test: `tests/test_strategy_blocks.py`, `tests/test_blender_strategy_update.py`

**Interfaces:**
- Consumes: the `unlock` block (Task 4), `selection: "value"` (Task 3), and `workshop_unlocks.gate_for`.
- Produces: `template_program(policy, 'workshop')[0]['then']` = `[{id: f'{policy}.blender.unlocks', type: 'unlock', ...}, {id: f'{policy}.blender.value', type: 'pool', selection: 'value', ...}, {id: f'{policy}.blender.wait', type: 'wait'}]`. Module constants `_BLENDER_WEIGHTS`, `_BLENDER_TARGETS`, `_BLENDER_UNLOCKS`. Task 7 copies this branch.

- [ ] **Step 1: Write the failing tests and retire the v1 ones**

In `tests/test_strategy_blocks.py`, delete these three tests. They pin the old priority list (attack_speed first, Bounce Shot unlock):
- `test_blender_uses_lower_affordable_priority_then_reaches_bounce_unlock`
- `test_blender_prefers_attack_speed_and_never_falls_back_to_def_abs`
- `test_blender_prefers_knockback_force_then_multishot_unlock`

Keep `test_wave_450_switches_workshop_from_def_abs_to_blender_unlocks`, `test_blender_unlocks_the_defense_path_in_order`, `test_pinned_legacy_builtin_uses_blender_after_wave_450` and `test_older_saved_builtin_shape_uses_blender_after_wave_450`. Their expectations still hold: a 40-coin Defense unlock beats the 40-coin catalog Cash unlock on tab order, and beats the 50-coin Range unlock on price.

Append:

```python
WORKER_83_PRICES = {
    'attack_speed': 6490, 'cash_bonus': 6540, 'cash_per_wave': 6050, 'coins_per_kill_bonus': 5860,
    'coins_per_wave': 4410, 'critical_chance': 50, 'critical_factor': 50, 'damage': 235,
    'damage_per_meter': 50, 'defense_absolute': 11790, 'defense_percent': 7360, 'health': 4130,
    'health_regen': 30, 'lifesteal': 547, 'multishot_chance': 5950, 'multishot_targets': 450,
    'range': 50, 'rapid_fire_chance': 120, 'rapid_fire_duration': 120, 'thorns': 16760,
    'unlock_bounce_shot': 10000, 'unlock_free_upgrades': 800, 'unlock_knockback': 5000}
WORKER_83_PURCHASES = {
    'attack_speed': 27, 'cash_bonus': 28, 'cash_per_wave': 27, 'coins_per_kill_bonus': 24,
    'coins_per_wave': 21, 'damage': 3, 'defense_absolute': 43, 'defense_percent': 29, 'health': 26,
    'multishot_chance': 23, 'thorns': 44, 'unlock_cash_bonuses': 1, 'unlock_coin_bonuses': 1,
    'unlock_defense_upgrades': 1, 'unlock_lifesteal': 1, 'unlock_multishot': 1,
    'unlock_range_upgrades': 1, 'unlock_rapid_fire': 1, 'unlock_thorns': 1}


def blender_v2() -> list[dict[str, Any]]:
    return list(blocks.template_program('turtle', 'workshop')[0]['then'])


def worker_83(**changes: Any) -> RouteFacts:
    """Tiramisu64_83's recorded Workshop facts on 2026-10-05 (blender v7)."""
    return replace(facts(), best_tier_1_wave=415, wallet_coins=1230, prices=dict(WORKER_83_PRICES),
                   purchases=dict(WORKER_83_PURCHASES), confirmed_purchases=dict(WORKER_83_PURCHASES),
                   **changes)


def test_blender_v2_replay_buys_free_upgrades_then_saves_for_knockback() -> None:
    first = blocks.evaluate_program(route(blender_v2()), worker_83(), None, 'workshop')
    assert (first.decision.state, first.decision.upgrade_id) == ('buy', 'unlock_free_upgrades')
    owned = {**WORKER_83_PURCHASES, 'unlock_free_upgrades': 1}
    second = blocks.evaluate_program(route(blender_v2()),
        worker_83(wallet_coins=430, purchases=owned, confirmed_purchases=owned), None, 'workshop')
    assert (second.decision.state, second.decision.upgrade_id) == ('save_coins', 'unlock_knockback')


def test_blender_v2_spends_by_value_once_unlocks_are_done() -> None:
    owned = {**WORKER_83_PURCHASES, **{uid: 1 for uid in (
        'unlock_free_upgrades', 'unlock_knockback', 'unlock_orbs', 'unlock_interest')}}
    prices = {**WORKER_83_PRICES, 'knockback_chance': 500, 'knockback_force': 500, 'orb_speed': 500,
              'orbs': 20000, 'free_attack_upgrade': 75, 'free_defense_upgrade': 90,
              'free_utility_upgrade': 100}
    result = blocks.evaluate_program(route(blender_v2()),
        worker_83(wallet_coins=10000, prices=prices, purchases=owned, confirmed_purchases=owned), None, 'workshop')
    # Crit Chance 50 / 2 = 25 beats Free Defense 90 / 3 = 30; Regen is cheaper but not in the pool.
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'critical_chance')
    assert any('manual unlock needed' in item or 'over unlock price limit' in item
               for item in result.trace.rejected)


def test_blender_v2_never_buys_defense_absolute() -> None:
    owned = {**WORKER_83_PURCHASES, **{uid: 1 for uid in (
        'unlock_free_upgrades', 'unlock_knockback', 'unlock_orbs', 'unlock_interest')}}
    prices = {uid: 10**9 for uid in WORKER_83_PRICES} | {'defense_absolute': 1, 'health_regen': 1}
    result = blocks.evaluate_program(route(blender_v2()),
        worker_83(wallet_coins=10000, prices=prices, purchases=owned, confirmed_purchases=owned), None, 'workshop')
    assert result.decision is None or result.decision.upgrade_id not in {'defense_absolute', 'health_regen'}


def test_blender_v2_weights_sum_to_100_and_unlock_every_gated_pool_skill() -> None:
    unlocks, value, stop = blender_v2()
    assert sum(value['weights'].values()) == 100
    assert value['selection'] == 'value' and stop['type'] == 'wait'
    gated = {uid for uid in value['upgrade_ids'] if blocks.gate_for(uid) is not None}
    assert gated <= set(unlocks['upgrade_ids'])
    assert {'max_recovery', 'package_chance'} <= set(unlocks['upgrade_ids'])
    assert (unlocks['max_price'], unlocks['hold']) == (20000, True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_strategy_blocks.py -q -p no:allure_pytest -k "blender or wave_450"`
Expected: FAIL. `blender_v2()` unpacks into two blocks, and `blocks.gate_for` is missing.

- [ ] **Step 3: Implement the template**

In `fleet/strategy_blocks.py`, change the module import added in Task 4 to:

```python
from workshop_unlocks import gate_for, path_to
```

Replace the whole `_BLENDER_PRIORITY = (...)` tuple with:

```python
# Blender v2: coin weights for value-per-coin buying (sum 100) and stop targets.
_BLENDER_WEIGHTS = {
    'coins_per_kill_bonus': 18, 'defense_percent': 12, 'attack_speed': 10, 'health': 9,
    'knockback_chance': 6, 'thorns': 6, 'damage': 4, 'lifesteal': 4, 'orb_speed': 4,
    'free_utility_upgrade': 3, 'free_defense_upgrade': 3, 'cash_bonus': 3, 'orbs': 3,
    'knockback_force': 3, 'free_attack_upgrade': 2, 'coins_per_wave': 2, 'cash_per_wave': 2,
    'critical_chance': 2, 'multishot_targets': 2, 'multishot_chance': 1, 'critical_factor': 1,
}
_BLENDER_TARGETS = {'thorns': 51, 'lifesteal': 3.5, 'orbs': 3, 'multishot_targets': 5}
# Every gated pool skill, plus Recovery Packages (whose path buys Interest).
_BLENDER_UNLOCKS = (*(uid for uid in _BLENDER_WEIGHTS if gate_for(uid) is not None),
                    'max_recovery', 'package_chance')
```

Replace `_blender_workshop_template` with:

```python
def _blender_workshop_template(policy: str) -> dict[str, Any]:
    return {'id': f'{policy}.blender.wave450', 'type': 'condition',
            'label': 'Blender after best wave 450',
            'field': 'best_tier_1_wave', 'op': 'gte', 'value': 450,
            'then': [
                {'id': f'{policy}.blender.unlocks', 'type': 'unlock',
                 'label': 'Unlock missing skills', 'upgrade_ids': list(_BLENDER_UNLOCKS),
                 'max_price': 20000, 'hold': True},
                {'id': f'{policy}.blender.value', 'type': 'pool', 'label': 'Blender value per coin',
                 'upgrade_ids': list(_BLENDER_WEIGHTS), 'selection': 'value',
                 'weights': dict(_BLENDER_WEIGHTS), 'targets': dict(_BLENDER_TARGETS)},
                {'id': f'{policy}.blender.wait', 'type': 'wait',
                 'label': 'Wait for an affordable Blender upgrade'},
            ], 'else': _workshop_template(policy)}
```

- [ ] **Step 4: Keep `add_blender_gate` working with three blocks**

In `fleet/blender_strategy_update.py`, replace:

```python
    gate["then"][0]["id"] = f"{prefix}.blender.priorities"
    gate["then"][1]["id"] = f"{prefix}.blender.wait"
```

with:

```python
    for child in gate["then"]:
        child["id"] = child["id"].replace("turtle.blender.", f"{prefix}.blender.", 1)
```

and replace:

```python
    priorities = set(gate["then"][0]["upgrade_ids"])
    if _ban_closure(frozenset(workshop["banned_upgrade_ids"])) & priorities:
        raise ValueError("a Blender priority is still banned")
```

with:

```python
    priorities = {uid for child in gate["then"] for uid in child.get("upgrade_ids", ())}
    if _ban_closure(frozenset(workshop["banned_upgrade_ids"])) & priorities:
        raise ValueError("a Blender priority is still banned")
```

In `tests/test_blender_strategy_update.py`, replace in `test_blender_gate_precedes_old_rules_and_is_idempotent`:

```python
    priorities, stop = gate["then"]
    assert priorities["upgrade_ids"][0] == "attack_speed"
    assert {"unlock_orbs", "knockback_force", "knockback_chance", "orbs",
            "unlock_multishot", "unlock_bounce_shot"} <= set(priorities["upgrade_ids"])
    assert "defense_absolute" not in priorities["upgrade_ids"]
    assert stop["type"] == "wait"
```

with:

```python
    unlocks, priorities, stop = gate["then"]
    assert [block["id"] for block in gate["then"]] == [
        f"{prefix}.blender.unlocks", f"{prefix}.blender.value", f"{prefix}.blender.wait"]
    assert unlocks["type"] == "unlock" and {"knockback_chance", "orbs"} <= set(unlocks["upgrade_ids"])
    assert priorities["selection"] == "value"
    assert "defense_absolute" not in priorities["upgrade_ids"]
    assert stop["type"] == "wait"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_strategy_blocks.py tests/test_blender_strategy_update.py tests/test_strategy_templates.py -q -p no:allure_pytest`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add fleet/strategy_blocks.py fleet/blender_strategy_update.py tests/test_strategy_blocks.py tests/test_blender_strategy_update.py
git commit -m "Switch built-in Blender to unlock-first value buying

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Strategy Studio support

**Files:**
- Modify: `web/ui/lib/strategyStudio.ts` (the `StrategyBlock` union)
- Modify: `web/ui/app/fleet/reroll/strategies/strategyBlocks.ts` (presets, `presetsForLane`, `makeBlock`, `blockTitle`, `blockDetail`, `GuideBlockType`, `GUIDE_BLOCKS`)
- Modify: `web/ui/app/fleet/reroll/strategies/StrategyBlockInspector.tsx` (selection options, weight inputs, unlock section, value preview)
- Modify: `web/ui/app/fleet/reroll/strategies/StrategyCanvas.tsx` (icon and kind)
- Modify: `web/ui/app/fleet/reroll/strategies/guide/diagrams.tsx` (`OUTCOMES`), `guide/page.tsx` (`EXAMPLES`)
- Test: `web/ui/app/fleet/reroll/strategies/strategyBlocks.test.ts`, `StrategyStudio.test.tsx`

**Interfaces:**
- Consumes: the server shapes from Tasks 3–4.
- Produces: the TS block `{ type: "unlock"; upgrade_ids: string[]; max_price?: number; hold?: boolean }`, pool `selection` value `"value"`, and presets `"unlock"` and `"value"` (Workshop only).

- [ ] **Step 1: Install UI deps if needed**

Run: `test -d web/ui/node_modules || (cd web/ui && npm ci)`

- [ ] **Step 2: Write the failing tests**

Append to `strategyBlocks.test.ts` (add `presetsForLane` to its import list):

```ts
test("unlock and value presets are Workshop-only and build valid blocks", () => {
  expect(makeBlock("unlock", "workshop")).toMatchObject({ type: "unlock", upgrade_ids: ["knockback_chance", "orbs"], max_price: 20000, hold: true });
  expect(makeBlock("value", "workshop")).toMatchObject({ type: "pool", selection: "value",
    weights: { defense_percent: 12, health: 9, attack_speed: 10 } });
  expect(makeBlock("value", "workshop")).not.toHaveProperty("decay_pct");
  expect(presetsForLane("battle").map(item => item.id)).not.toEqual(expect.arrayContaining(["unlock"]));
  expect(presetsForLane("battle").map(item => item.id)).not.toEqual(expect.arrayContaining(["value"]));
  const names = new Map([["orbs", "Orbs"]]);
  expect(blockTitle(makeBlock("unlock", "workshop"), names)).toBe("Unlock missing skills");
  expect(blockTitle(makeBlock("value", "workshop"), names)).toBe("Buy best value per coin");
  expect(GUIDE_BLOCKS.map(item => item.type)).toContain("unlock");
});
```

Append to `StrategyStudio.test.tsx`:

```tsx
test("switching a weighted pool to value drops decay settings", () => {
  const onChange = vi.fn();
  render(<StrategyBlockInspector block={{ id: "p", type: "pool", upgrade_ids: ["thorns"], selection: "weighted",
    decay_pct: 10, weight_floor: 1, weights: { thorns: 2 } }} lane="workshop" catalog={catalog} locked={false}
    onChange={onChange} onRemove={() => {}} onCopy={() => {}} />);
  fireEvent.change(screen.getByLabelText("Selection"), { target: { value: "value" } });
  const next = onChange.mock.calls[0][0];
  expect(next.selection).toBe("value");
  expect(next.decay_pct).toBeUndefined();
  expect(next.weight_floor).toBeUndefined();
});

test("value pool shows weights and coin share; battle has no value option", () => {
  render(<StrategyBlockInspector block={{ id: "p", type: "pool", upgrade_ids: ["thorns", "damage"], selection: "value",
    weights: { thorns: 3, damage: 1 } }} lane="workshop" catalog={catalog} locked={false}
    onChange={() => {}} onRemove={() => {}} onCopy={() => {}} />);
  expect(screen.getByLabelText("Weight for Thorn Damage")).toHaveValue(3);
  expect(screen.getByText(/75% of coins/)).toBeInTheDocument();
  expect(screen.queryByLabelText("Reduce weight after each buy (%)")).not.toBeInTheDocument();
});

test("unlock block edits its skills, price limit and hold", () => {
  const onChange = vi.fn();
  render(<StrategyBlockInspector block={{ id: "u", type: "unlock", upgrade_ids: ["thorns"], max_price: 20000, hold: true }}
    lane="workshop" catalog={catalog} locked={false} onChange={onChange} onRemove={() => {}} onCopy={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Damage" }));
  expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ upgrade_ids: ["thorns", "damage"] }));
  fireEvent.click(screen.getByLabelText("Hold coins until the unlock is affordable"));
  expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ hold: false }));
  expect(screen.getByLabelText("Highest unlock price (coins)")).toHaveValue(20000);
});
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd web/ui && npx vitest run app/fleet/reroll/strategies/strategyBlocks.test.ts app/fleet/reroll/strategies/StrategyStudio.test.tsx`
Expected: FAIL. The `"unlock"` preset is unknown and there is no "value" option.

- [ ] **Step 4: Types**

In `web/ui/lib/strategyStudio.ts`, change the pool member's `selection: "priority" | "weighted" | "cheapest"` to `selection: "priority" | "weighted" | "cheapest" | "value"`. Add a union member after the `buy` member:

```ts
  | (BlockBase & { type: "unlock"; upgrade_ids: string[]; max_price?: number; hold?: boolean })
```

- [ ] **Step 5: Presets, titles, guide**

In `strategyBlocks.ts`:

```ts
export type BlockPreset = "condition" | "cheap" | "cap" | "weighted" | "value" | "unlock" | "buy" | "fallback" | "wait" | "budget" | "save_for" | "while_saving";
```

In `BLOCK_PRESETS`, after the `weighted` entry:

```ts
  { id: "value", label: "Value per coin", detail: "Buy the lowest price ÷ weight", group: "Logic" },
  { id: "unlock", label: "Unlock missing skills", detail: "Buy the unlocks these skills need first", group: "Logic" },
```

Replace `presetsForLane`:

```ts
/** Budget, unlocks and value pools are Workshop-only, so the In-game palette omits them. */
export function presetsForLane(lane: ProgramLane): typeof BLOCK_PRESETS {
  const workshopOnly = new Set<BlockPreset>(["budget", "unlock", "value"]);
  return lane === "battle" ? BLOCK_PRESETS.filter(item => !workshopOnly.has(item.id)) : BLOCK_PRESETS;
}
```

In `makeBlock`, add cases:

```ts
    case "value": return { id, type: "pool", upgrade_ids: ["defense_percent", "health", "attack_speed"], selection: "value",
      weights: { defense_percent: 12, health: 9, attack_speed: 10 }, count_scope: scope };
    case "unlock": return { id, type: "unlock", upgrade_ids: ["knockback_chance", "orbs"], max_price: 20000, hold: true };
```

In `blockTitle`, add `case "unlock": return "Unlock missing skills";`. In the `pool` case, put `block.selection === "value" ? "Buy best value per coin" :` before the `weighted` test:

```ts
    case "pool": return block.hold_until_capped ? "Buy until purchase caps" : block.selection === "value" ? "Buy best value per coin" : block.selection === "weighted" ? "Draw with evolving weights" : block.selection === "cheapest" ? "Buy cheapest available" : block.discount_pct !== undefined ? "Buy one from a cheap pool" : "Capped upgrade pool";
```

In `blockDetail`, add as the first line after the `native` check:

```ts
  if (block.type === "unlock") return ["Buys the next unlock these skills need, cheapest first",
    block.max_price !== undefined ? `Unlocks ≤ ${block.max_price} coins` : null,
    block.hold === false ? "Later blocks may spend while saving" : "Holds coins until the unlock is affordable"].filter(Boolean).join(" · ");
```

In the pool detail's selection part, replace:

```ts
    block.selection === "weighted" ? `${block.decay_pct ?? 0}% weight reduction / buy` : block.selection === "cheapest" ? "Lowest affordable price" : "First eligible item",
```

with:

```ts
    block.selection === "value" ? "Lowest price ÷ weight" : block.selection === "weighted" ? `${block.decay_pct ?? 0}% weight reduction / buy` : block.selection === "cheapest" ? "Lowest affordable price" : "First eligible item",
```

Change `GuideBlockType` to include `"unlock"`, and add this to `GUIDE_BLOCKS` after `pool`:

```ts
  { type: "unlock", title: "Unlock missing skills", summary: "Buys the next Workshop unlock the listed skills need, cheapest first. Holds coins for it unless hold is off; skips unlocks over the price limit or the bot cannot buy." },
```

Update the `pool` guide summary to: `"Filters a list of upgrades by caps, targets and price limits, then buys the first eligible one, draws by weight, or buys the best value per coin (lowest price ÷ weight)."`

In `guide/diagrams.tsx` `OUTCOMES`, add `unlock: ["buy", "save", "pass"],`. In `guide/page.tsx` `EXAMPLES`, add:

```ts
  unlock: "Skills Knockback Chance, Orbs with Lifesteal owned: buys Unlock Knockback (5,000) first, then Unlock Orbs (15,000). With 3,000 coins it holds them for Knockback; nothing later spends.",
```

- [ ] **Step 6: Inspector**

In `StrategyBlockInspector.tsx`, add a component after `WeightPreview`:

```tsx
function ValueShare({ block, names }: { block: Pool; names: Map<string, string> }): React.JSX.Element {
  const total = block.upgrade_ids.reduce((sum, id) => sum + (block.weights?.[id] ?? 1), 0);
  return <div className={styles.weightPreview}>
    <p className={styles.eyebrow}>Weight → share of coins at balance</p>
    {block.upgrade_ids.map(id => { const share = total ? Math.round((block.weights?.[id] ?? 1) / total * 100) : 0;
      return <div key={id} className={styles.weightRow}>
        <div><span>{names.get(id) ?? id}</span><b>{share}% of coins</b></div>
        <div className={styles.weightTrack}><span style={{ width: `${share}%` }} /></div>
      </div>; })}
    <p className={styles.hint}>Buys the eligible upgrade with the lowest price ÷ weight, so rising prices spread coins in these shares.</p>
  </div>;
}
```

Replace the Selection `<label>…</label>` with:

```tsx
          <label>Selection<select value={block.selection} onChange={event => { const selection = event.target.value as Pool["selection"]; onChange({ ...block, selection,
            price_source: selection === "cheapest" ? block.price_source : undefined,
            batch_size: selection === "cheapest" ? block.batch_size : undefined,
            max_price_premium_pct: selection === "cheapest" ? block.max_price_premium_pct : undefined,
            decay_pct: selection === "weighted" ? block.decay_pct : undefined,
            weight_floor: selection === "weighted" ? block.weight_floor : undefined }); }}><option value="priority">First eligible in pool order</option><option value="weighted">Weighted draw</option>{lane === "workshop" && <option value="value">Best value per coin</option>}{lane === "battle" && <option value="cheapest">Cheapest affordable</option>}</select></label>
```

In the ordered-pool row, change the weight input condition from `{block.selection === "weighted" && <input aria-label={`Weight for …`}` to `{(block.selection === "weighted" || block.selection === "value") && <input aria-label={`Weight for …`}`. Leave the input itself unchanged.

After the `{block.type === "budget" && <>…</>}` section, add the unlock section:

```tsx
        {block.type === "unlock" && <>
          <div className={styles.fieldHeading}>Skills to unlock <span>{block.upgrade_ids.length} chosen</span></div>
          <div className={styles.poolChoices}>{catalog.filter(item => !item.unlock).map(item => <button type="button" key={item.id}
            aria-pressed={block.upgrade_ids.includes(item.id)} onClick={() => onChange({ ...block, upgrade_ids: block.upgrade_ids.includes(item.id)
              ? block.upgrade_ids.filter(id => id !== item.id) : [...block.upgrade_ids, item.id] })}>{item.name}</button>)}</div>
          {!block.upgrade_ids.length && <p role="alert">Choose at least one skill.</p>}
          <div className={styles.fieldHeading}>Price limit<button type="button" onClick={() => onChange({ ...block, max_price: block.max_price === undefined ? 20000 : undefined })}>{block.max_price === undefined ? "Add limit" : "Remove"}</button></div>
          {block.max_price !== undefined && <label>Highest unlock price (coins)<input type="number" min={1} step={1} value={block.max_price}
            onChange={event => { const value = numberOrNull(event); if (value !== null) onChange({ ...block, max_price: value }); }} /></label>}
          <label><input type="checkbox" checked={block.hold !== false} onChange={event => onChange({ ...block, hold: event.target.checked })} />Hold coins until the unlock is affordable</label>
          <p className={styles.hint}>Unlocks are bought in each tab’s order. Tiles the bot cannot buy yet are reported in the decision trace.</p>
        </>}
```

After the existing `{block.type === "pool" && block.selection === "weighted" && <WeightPreview … />}` line, add:

```tsx
      {block.type === "pool" && block.selection === "value" && <ValueShare block={block} names={names} />}
```

The checkbox's accessible name comes from its wrapping label text, "Hold coins until the unlock is affordable".

- [ ] **Step 7: Canvas icon**

In `StrategyCanvas.tsx`, add `Unlock` to the `lucide-react` import. In the icon expression, add `block.type === "unlock" ? Unlock :` before `block.type === "pool" ? Sparkles`. Change the kind test to `block.type === "condition" || block.type === "pool" || block.type === "unlock" ? "logic"`.

- [ ] **Step 8: Run tests and typecheck**

Run: `cd web/ui && npx vitest run app/fleet/reroll/strategies/strategyBlocks.test.ts app/fleet/reroll/strategies/StrategyStudio.test.tsx && npx tsc --noEmit -p .`
Expected: PASS, no type errors. A `switch` without a default case may now miss `"unlock"` (for example in `childGroups`'s callers or `blockTitle`). `tsc` will report it; add `case "unlock":` returning the same as `"wait"` there.

- [ ] **Step 9: Commit**

```bash
git add web/ui/lib/strategyStudio.ts web/ui/app/fleet/reroll/strategies
git commit -m "Edit unlock blocks and value pools in Strategy Studio

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `fleet/blender_v2_update.py`

**Files:**
- Create: `fleet/blender_v2_update.py`
- Test: `tests/test_blender_v2_update.py`

**Interfaces:**
- Consumes: `template_program` (Task 5), `fleet.blender_strategy_update._request`, `RouteBaseline.from_dict`, `_ban_closure`.
- Produces: `blender_v2_workshop(baseline: Mapping[str, Any]) -> dict[str, Any]` and `update_blender(base_url: str, *, apply: bool = False) -> dict[str, Any]`. Run as `python -m fleet.blender_v2_update [--apply]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_blender_v2_update.py`:

```python
"""The saved blender strategy adopts the v2 Workshop program through the coordinator API."""
from __future__ import annotations

from copy import deepcopy

import pytest

import fleet.blender_v2_update as updater
from fleet.blender_v2_update import blender_v2_workshop
from fleet.build_route import RouteBaseline, RouteDocument


def v7_baseline() -> dict:
    baseline = RouteDocument.compatibility().to_dict()["baseline"]
    baseline["workshop"].update(mode="blocks", banned_upgrade_ids=[], blocks=[
        {"id": "block.old", "type": "pool", "selection": "weighted", "count_scope": "account",
         "decay_pct": 20, "weight_floor": 1, "upgrade_ids": ["attack_speed", "thorns"],
         "weights": {"attack_speed": 1, "thorns": 1}}])
    return baseline


def test_v2_replaces_only_the_workshop_program_and_is_idempotent() -> None:
    baseline = v7_baseline()
    original = deepcopy(baseline)
    updated = blender_v2_workshop(baseline)
    assert baseline == original
    assert updated["battle"] == baseline["battle"] and updated["labs"] == baseline["labs"]
    assert [block["id"] for block in updated["workshop"]["blocks"]] == ["bv2.unlocks", "bv2.value", "bv2.wait"]
    assert updated["workshop"]["blocks"][1]["selection"] == "value"
    RouteBaseline.from_dict(updated)
    assert blender_v2_workshop(updated) == updated


def test_v2_refuses_a_banned_pool_skill() -> None:
    baseline = v7_baseline()
    baseline["workshop"]["banned_upgrade_ids"] = ["thorns"]
    with pytest.raises(ValueError, match="banned"):
        blender_v2_workshop(baseline)


def fake_coordinator(baseline: dict) -> tuple[dict, dict, object]:
    library = {"revision": 0, "strategies": [
        {"id": "b", "name": "blender", "version": 7, "source_template": "scratch", "baseline": baseline}]}
    route = {"revision": 0, "assignments": {
        worker: {"account_id": worker, "strategy_id": "b", "strategy_version": 7, "baseline": baseline}
        for worker in ("Tiramisu64_82", "Tiramisu64_83")}}

    def request(_: str, path: str, payload: dict | None = None) -> dict:
        if payload is None:
            return deepcopy(library if path.endswith("/strategies") else route)
        if path.endswith("/strategies"):
            assert payload["expected_revision"] == library["revision"]
            library["revision"] += 1
            row = library["strategies"][0]
            row.update(version=row["version"] + 1, baseline=payload["baseline"])
            return deepcopy(library)
        assert path.endswith("/strategies/assign")
        assert payload["expected_revision"] == route["revision"]
        route["revision"] += 1
        for target in payload["workers"]:
            route["assignments"][target["worker"]].update(
                strategy_version=payload["strategy_version"], baseline=library["strategies"][0]["baseline"])
        return deepcopy(route)
    return library, route, request


def test_preview_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    library, route, request = fake_coordinator(v7_baseline())
    monkeypatch.setattr(updater, "_request", request)
    change = updater.update_blender("unused")
    assert (change["current_version"], change["target_version"], change["save"]) == (7, 8, True)
    assert change["assign_workers"] == ["Tiramisu64_82", "Tiramisu64_83"]
    assert (library["revision"], route["revision"]) == (0, 0)


def test_apply_saves_v8_and_assigns_both_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    library, route, request = fake_coordinator(v7_baseline())
    monkeypatch.setattr(updater, "_request", request)
    updater.update_blender("unused", apply=True)
    assert library["strategies"][0]["version"] == 8
    for worker in ("Tiramisu64_82", "Tiramisu64_83"):
        assert route["assignments"][worker]["strategy_version"] == 8
        assert route["assignments"][worker]["baseline"]["workshop"]["blocks"][0]["type"] == "unlock"
    monkeypatch.setattr(updater, "_request", request)
    again = updater.update_blender("unused")
    assert (again["save"], again["assign_workers"]) == (False, [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_blender_v2_update.py -q -p no:allure_pytest`
Expected: FAIL with `ModuleNotFoundError: No module named 'fleet.blender_v2_update'`.

- [ ] **Step 3: Implement**

Create `fleet/blender_v2_update.py`:

```python
"""Install the Blender v2 Workshop program on the saved ``blender`` strategy.

Run ``python -m fleet.blender_v2_update`` to preview. Add ``--apply`` to save a
new version and assign it to emulators 82 and 83. The battle and other lanes
are left untouched.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from typing import Any, Mapping

from fleet.blender_strategy_update import _request
from fleet.build_route import RouteBaseline
from fleet.reroll_planner import _ban_closure
from fleet.strategy_blocks import template_program

STRATEGY = "blender"
PREFIX = "bv2"
WORKERS = ("Tiramisu64_82", "Tiramisu64_83")


def blender_v2_workshop(baseline: Mapping[str, Any]) -> dict[str, Any]:
    """The baseline with its Workshop program replaced by Blender v2."""
    updated = deepcopy(dict(baseline))
    workshop = updated["workshop"]
    program = deepcopy(list(template_program("turtle", "workshop")[0]["then"]))
    for block in program:
        block["id"] = block["id"].replace("turtle.blender.", f"{PREFIX}.", 1)
    wanted = {uid for block in program for uid in block.get("upgrade_ids", ())}
    if _ban_closure(frozenset(workshop["banned_upgrade_ids"])) & wanted:
        raise ValueError("a Blender v2 skill is banned by Never Buy")
    workshop.update(mode="blocks", blocks=program)
    RouteBaseline.from_dict(updated)
    return updated


def update_blender(base_url: str, *, apply: bool = False) -> dict[str, Any]:
    """Preview or save the new version and assign it through the guarded API."""
    library = _request(base_url, "/api/fleet/reroll/strategies")
    route = _request(base_url, "/api/fleet/reroll/route")
    matches = [row for row in library["strategies"] if row["name"] == STRATEGY]
    if len(matches) != 1:
        raise ValueError(f"expected one saved strategy named {STRATEGY!r}")
    saved = matches[0]
    baseline = blender_v2_workshop(saved["baseline"])
    needs_save = baseline != saved["baseline"]
    next_version = saved["version"] + int(needs_save)
    workers = []
    for worker in WORKERS:
        assignment = route["assignments"][worker]
        if assignment["strategy_id"] != saved["id"]:
            raise ValueError(f"{worker} is no longer assigned {STRATEGY}")
        if assignment["strategy_version"] != next_version or assignment["baseline"] != baseline:
            workers.append({"worker": worker, "account_id": assignment["account_id"]})
    change = {"strategy": STRATEGY, "current_version": saved["version"], "target_version": next_version,
              "save": needs_save, "assign_workers": [row["worker"] for row in workers]}
    if not apply:
        return change
    if needs_save:
        library = _request(base_url, "/api/fleet/reroll/strategies", {
            "expected_revision": library["revision"], "strategy_id": saved["id"],
            "name": STRATEGY, "source_template": saved["source_template"], "baseline": baseline,
        })
        saved = next(row for row in library["strategies"] if row["id"] == saved["id"])
        if saved["version"] != next_version:
            raise RuntimeError(f"{STRATEGY}: saved version changed unexpectedly")
    if workers:
        route = _request(base_url, "/api/fleet/reroll/strategies/assign", {
            "expected_revision": route["revision"], "strategy_id": saved["id"],
            "strategy_version": saved["version"], "workers": workers,
        })
        change["route_revision"] = route["revision"]
    return change


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--apply", action="store_true",
                        help="save the strategy version and assign it; default is read-only preview")
    args = parser.parse_args()
    print(json.dumps(update_blender(args.base_url, apply=args.apply), sort_keys=True))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_blender_v2_update.py tests/test_blender_strategy_update.py -q -p no:allure_pytest`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fleet/blender_v2_update.py tests/test_blender_v2_update.py
git commit -m "Add Blender v2 update script for the saved blender strategy

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Ship and roll out (user-gated)

**Files:** none (operations)

- [ ] **Step 1: Open the PR.** Push the branch to its own remote branch (verify with `git branch -vv`) using the personal gh account (memory: `GH_TOKEN=$(gh auth token --user shahar061)`). Open a PR against `main` whose summary covers the unlock block, value selection, new unlock tiles, Blender v2 and the update script. Stop. The user merges and restarts the fleet.
- [ ] **Step 2: Dry run** after the fleet runs the merged code: `uv run python -m fleet.blender_v2_update`. Expected output: `{"assign_workers": ["Tiramisu64_82", "Tiramisu64_83"], "current_version": 7, "save": true, "strategy": "blender", "target_version": 8}`. Show it to the user.
- [ ] **Step 3: Apply only after the user says yes:** `uv run python -m fleet.blender_v2_update --apply`. Then confirm both `~/.local/share/thetowerbot/fleet/workers/Tiramisu64_8{2,3}/build-route-applied.json` carry the new route revision.
- [ ] **Step 4: Live verification (read-only)**, after each worker's next Workshop visit:
  - In `Tiramisu64_83/build-route-choice.json` / `build-route-facts.json`, the first v2 decision buys `unlock_free_upgrades` (800). After that it saves for `unlock_knockback`.
  - On `Tiramisu64_82`, `build-route-facts.json` → `prices` contains `unlock_interest` (the tile resolved by name). Within a few visits `confirmed_purchases` gains `unlock_interest`.
  - No `unknown_identity` skip for an "Unlock Interest" row appears in that worker's recent `events` (`sqlite3 "file:$DB?mode=ro" "select ts, reason from events where reason like '%unknown_identity%' order by seq desc limit 5"`).

  If any check fails, report it to the user rather than editing strategies by hand.
