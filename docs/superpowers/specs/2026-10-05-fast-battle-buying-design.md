# Fast battle buying

## Problem

In-run (battle) upgrades are bought far slower than cash comes in, so cash piles
up and the tower dies early. Measured on the two live workers (82, 83) over one
day, 94 completed tier-1 runs:

| | 82, waves 1-19 | 82, wave 20+ | 83, waves 1-19 | 83, wave 20+ |
|---|---|---|---|---|
| mean gap between purchases | 5.4 s | 9.6 s | 5.4 s | 8.0 s |
| levels bought per wave | 2.5 | 1.4 | 2.7 | 1.8 |
| cash at tap / price (median) | 14x | 434x | 14x | 186x |
| share of post-wave-20 cash spent | - | 4% | - | 25% |

After wave 20, scrolling and scanning for rows takes 24-36% of the time, tab
switches 13-17%, and tap plus confirmation 37-44%. Almost half of all purchases
start with a tab switch.

Causes in code:

- **One level per decision.** Each level costs a tap screenshot and a
  confirmation screenshot. The supervisor allows one input per screenshot, and
  the scan interval is 2.0 s.
- **Cheapest of 13 combat upgrades** is picked across two tabs, so the target
  keeps moving between tabs.
- **The blind row search** in `Autopilot._seek` scrolls all the way up, then
  down, until it finds the row.
- **Price quotes expire every wave.** Waves last about 15 s, so the bot re-reads
  a row before buying every few purchases, and a wave change discards a pending
  confirmation.
- **The DB-write fence and the 1 s cooldown.** Buying pauses until the async
  event sink commits each purchase, and the 1 s post-tap cooldown silently skips
  scans.

## Goal and success criteria

After wave 20, on both workers, measured with the same analysis on at least 10
completed runs per worker:

- at least 4 levels bought per wave (today 1.4-1.8);
- at least 70% of post-wave-20 cash spent (today 4-25%).

Failure must stay safe:

- never buy outside the strategy's pool;
- never spend into `cash_reserve`;
- never count a level that was not bought, except the bounded free-upgrade case
  in section 3.

## Scope

Battle (in-run) purchases driven by modeled-price pool blocks, which is the
Blender strategy today. Rows bought without a modeled price, manual buy
commands and Workshop purchases keep today's one-tap behaviour. This changes
how purchases are made and how targets are picked inside the configured pool.
It does not change which upgrades a strategy allows.

Kill switch: `config.BATTLE_BURST_ENABLED = True`. With it off, every change
below falls back to today's behaviour. Both workers turn on together.

## Design

### 1. Burst taps confirmed by the price curve

**Input** (`device.py`, `supervisor.py`)

- `device.tap_burst(device, x, y, n, gap_s)` sends `n` taps in one
  `adb shell` command: `input tap x y` repeated, separated by `sleep gap_s`
  when `gap_s > 0`.
- `DeviceSupervisor.tap_burst(x, y, n, gap_s)` sends it through one
  `_action`. The supervisor still treats it as a single input that waits for a
  fresh screenshot, so the one-input-per-screenshot invariant is unchanged.
- `config.BATTLE_BURST_TAP_GAP_SECONDS` defaults to 0. It is set from the live
  probe in the Rollout section.

**Burst size** (`autopilot.py`)

Bursts happen only when the tap is backed by a verified model quote, whose
`index` points into the upgrade's price curve. Starting at `index`, `k` is the
largest count up to `config.BATTLE_BURST_MAX = 10` such that:

- the sum of `curve[index : index + k]` is at most `cash - policy.cash_reserve`;
- every price in that slice is at most the policy's burst price ceiling
  (section 2);
- `index + k` stays inside the curve.

If `k` is 0, the bot does not buy (today's "saving" path). With the kill switch
off, `k` is 1.

**Pending state**

The pending purchase records:

- the row;
- the time the burst was sent;
- the starting curve index;
- `k`;
- the quote's sequence and rule id.

**Confirmation** (next screenshots while the burst is pending)

1. **Row shows MAX:** bought = `min(k, len(curve) - index)`.
2. **Price matches exactly one curve entry `j`:** bought = `j - index`,
   clamped to `[0, k]`. A `j` behind `index` means the model is wrong: count
   0, invalidate the row's quote and reconcile.
3. **Price unreadable or ambiguous:** keep waiting.
4. **8 s with no case-1/2 confirmation:**
   - if the row's value changed, count 1 level and invalidate the row's quote
     so its next read re-indexes it;
   - otherwise, block the upgrade for 60 s, as today.

For `bought >= 1`:

- emit one `BattlePurchased` per level, each at its own curve price, so
  purchase counts, `run_upgrades` and dashboards keep their meaning;
- call `record_receipt` with the last sequence.

A confirmed burst falls through on the same screenshot to choose the next
purchase, as the current batch path does.

### 2. Tab-sticky choice inside the pool

**Where:** `fleet/strategy_blocks.py`, in the `selection == 'cheapest'` model
branch. A new fact, `RouteFacts.battle_tab`, carries the current panel's
category (ATTACK, DEFENSE or UTILITY). It is filled from the observation in
`reroll_progress.battle_policy`.

**Candidates** are the block's own affordable, cap-respecting pool candidates,
exactly as today. The tab rule only narrows them; it never adds an upgrade
from outside `block['upgrade_ids']`. For example, `health_regen` is not in the
Blender combat pool, so it is never bought.

**Rich mode** applies when
`battle_cash >= config.BATTLE_RICH_MULTIPLE (10) x cheapest candidate price`
(the cheapest across all tabs). In rich mode the bot picks the first match in
this order:

1. the cheapest candidate that is **visible on screen now**
   (`facts.visible_upgrade_ids`) and on the current tab;
2. the cheapest candidate elsewhere on the current tab;
3. the cheapest candidate overall, which means switching tabs.

In rich mode the burst price ceiling is limited by cash only. Burst size still
caps at 10 and still respects the reserve.

**Outside rich mode**, the choice and its 25% premium-to-stay batch rule stay as
they are. The burst ceiling is the chosen price x (100 +
`max_price_premium_pct`)%.

The policy passes the ceiling to the autopilot as a new
`AutopilotPolicy.burst_price_ceiling`.

### 3. Quotes valid across waves

`fleet/battle_prices.py`: a quote is `verified` once its row was matched in
this run, whatever the current wave. It stops being verified when one of these
happens:

- a fresh read disagrees with it (existing re-index or drop logic);
- the run changes;
- section 1 invalidates it.

Removed:

- the `quote.get('wave') != wave` refusal in `Autopilot.step`;
- the "Reconcile the cheapest candidate after a wave change" observation trip
  in `strategy_blocks`;
- discarding a pending modeled purchase on a wave change.

Free upgrades (a wave-end perk granting a level) are still caught in two ways:

- the check just before tapping compares the on-screen row with the quote and
  reconciles on a mismatch (unchanged);
- the confirmation re-indexes from the price it reads.

Known cost: a free level that lands inside a burst's confirmation window is
counted as bought. That is at most one per wave per row and never more than
`k`. The code says so where it happens.

### 4. Shorter loop

**Scan interval** (`config.py`, `tower_bot.py`)

`BATTLE_SCAN_INTERVAL_SECONDS = 0.6` applies when the last battle step tapped
or confirmed a purchase, or when its decision has a buyable target. It stays
at 2.0 s while saving, observing only, idle or blocked, and outside battle.
Jitter and maintenance deadlines are unchanged.

**Cooldown** (`autopilot.py`): the `max(.75, cooldown)` gate is skipped when
the previous input was confirmed on this screenshot. It still applies after a
tab tap, a scroll or an unconfirmed tap.

**Receipt fence** (`fleet/reroll_progress.py`)

`await_battle_receipt` no longer disables the policy. The progress object
keeps an in-memory per-run tally of the levels it confirmed for each upgrade.
`battle_policy` uses `max(db_count, tally)` for each upgrade. The tally resets
when the run changes or the process restarts; after a restart the database is
complete again.

### Scrolling (`autopilot.py`, `Autopilot._seek`)

Every scroll costs one screenshot. Fewer scrolls come from:

1. **Visible first.** Rich mode prefers pool rows already on screen (section
   2), so after a tab is open the bot works through what it can see.
2. **Directional seek.** `upgrades.CATALOG` lists rows in their on-screen order
   for each tab. If the target's catalog position is after the last visible
   row, the bot scrolls down; if it is before the first, it scrolls up. Today's
   up-then-down sweep is used only as a fallback when the visible rows are not
   in the catalog or the directed search reaches the end without the target.
3. **Fewer reasons to scroll.** Quotes no longer expire per wave (section 3),
   so rows are not revisited just to re-read prices. Bursts spread each scroll
   over up to 10 levels.

## Error handling summary

| Situation | Behaviour |
|---|---|
| Burst taps partly dropped by the game | Counted from the price jump, so undercounting is safe |
| Price unreadable after a burst | Wait up to 8 s, then value-change fallback (1 level) or block |
| Price behind the model | Count 0, invalidate the quote, reconcile |
| Cash unreadable | No purchase (unchanged) |
| Wave change mid-burst | Confirmation proceeds; a free level may be counted (bounded) |
| Kill switch off | Today's behaviour everywhere |

## Testing

Write the tests first. Run only the touched test files, with
`-p no:allure_pytest`.

- **Burst size:** cash, reserve and ceiling limits; end of the curve; the
  kill switch.
- **Confirmation:**
  - exact `j - index` count;
  - MAX;
  - ambiguous price, then the 8 s fallback;
  - price behind the model;
  - one `BattlePurchased` per level at the curve prices.
- **Tab-sticky choice:**
  - rich-mode order (visible, then current tab, then global);
  - an upgrade outside the pool is never chosen, even when visible and
    cheapest;
  - outside rich mode, behaviour is unchanged.
- **Quotes across waves:** still verified after a wave change; invalidated by
  a disagreeing fresh read.
- **Tally:** the policy is not disabled after a receipt; counts use
  `max(db, tally)`; the tally resets on a run change.
- **Directional seek:** the scroll direction follows catalog order; the sweep
  is used only as a fallback.
- **Supervisor:** `tap_burst` is one pending action.

## Rollout

1. **Live probe** on one worker in a real battle, before relying on bursts:
   - time one 10-tap `tap_burst`;
   - read how many levels the game registered.

   If any taps are dropped, raise `BATTLE_BURST_TAP_GAP_SECONDS` until none
   are, and record the value in config with the measurement.
2. Enable on both workers. After at least 10 completed runs each, re-run the
   purchase-rate analysis against the success criteria above.
