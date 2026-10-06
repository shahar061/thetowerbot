# Labs always running, and a faster time between games

Date: 2026-10-06
Status: approved in chat, awaiting written-spec review
Builds on: `2026-09-28-lab-strategy-core-design.md` (ranked `lab_list`, fillers, just-in-time saving)

## 1. Problem

### 1.1 Labs sit idle

Evidence: the two active bots (Tiramisu64_82, _83) between 2026-10-04 19:20 and 2026-10-06 19:20.

- No `LabResearchStarted` in the window. The last starts were on 2026-10-03.
- Every lab visit went to Lab 1. Slots 2 and 3 are owned, idle, and never visited.
- Visit outcomes:

| Outcome | 82 | 83 |
|---|---|---|
| `wait_running` | 107 | 108 |
| `wait_coins` | 23 | 32 |
| `picker_stage_timeout` | 13 | 21 |
| `auto_start_off` | 11 | 14 |

Root causes, in order of impact:

1. **The active strategy, `blender`, has no lab list.** Its `labs` is `mode: steps` with only `research_game_speed`, `idle_fill: leave_idle` and `direct_start: false`. The planner reports "No track plans this slot" for slots 2 and 3, so they can never be filled. Three other strategies are the same: "Turtle Discord Guide", "Turtle Eco Wall" and "Turtle Opening".
2. **Game Speed L4 costs 50,000 coins, and the Workshop spends the wallet first.** In one example a visit took the wallet from 11,590 to about 420. `lab_share` is `when_affordable 25%`, so the jar never fills (`amount: 0` on both bots). Since 2026-10-05 08:00 every visit has ended in `wait_coins`.
3. **The picker timed out for 6–8 hours.** This ran from 2026-10-05 00:40 to 08:45, right after slot 1 freed up. By the time the picker worked again, the wallet was spent.
4. **The starter rollout gate.** Every lab other than slot-1 Game Speed must pass dry-run rehearsals (2 per lab, at least 600 s apart), then a canary, before any real start. The rollout file shows 0 rehearsals for every slot.
5. **Busy-slot visit loops.** Bot 82 made 72 `wait_running` visits in 31 minutes. This wastes time and never fills anything.

### 1.2 Time between games

This is the time from `RunEnded` to the next `RunStarted`: a median of 78 s / 62 s and a p90 of 108 s / 98 s (82 / 83).

| Where the time goes | Share | Median |
|---|---|---|
| Workshop shopping | ~40% | 30 s (buys land about 5 s apart) |
| Idle on the home screen | ~18% | 12 s |
| Lab visits, often useless or duplicated | ~15% | 11 s |
| Missions-screen guard holds | 6–12% | |

The cause is the menu loop timing:
- `SCAN_INTERVAL_SECONDS = 2.0`
- `SCREEN_CONFIRMATIONS = 2`
- `NAVIGATION_COOLDOWN_SECONDS = 3.0`
- one walk armed per frame

On top of that, the bot makes visits where there is nothing to do.

Out of scope: bot downtime and pauses, which came to 17–27 h of the 48 h.

## 2. Decisions (user, 2026-10-06)

| # | Decision |
|---|---|
| D1 | Labs are filled before the Workshop spends. The Workshop gets what's left. |
| D2 | Save coins for top-priority (S+) labs, e.g. Game Speed. While saving, a cheap filler keeps the slot busy. Fillers may be paid from saved coins. **A lab slot is never left idle while any lab is affordable.** |
| D3 | Labs start directly (`direct_start: true`). The per-lab rehearsal and canary rollout no longer gates starts. The per-start safety checks stay. |
| D4 | Faster scanning **only between games**. Battle timing is unchanged. |

Standing rules that still apply:
- Slot 1 is Game Speed to max. Slot 2 is Labs Speed.
- Never Black Hole Damage, and never the bot-cooldown labs. Neither is in the template list.

## 3. Part 1: labs always running

### 3.1 Strategy data: every strategy gets a lab list

- **Strategies in `labs.mode: steps`** (`blender`, Turtle Discord Guide, Turtle Eco Wall, Turtle Opening) get `mode: blocks` with `template_lab_list()` (`fleet/resource_blocks.py:392`).
- **Strategies that already have `mode: blocks`** keep their lists.
- **Every strategy gets these rules:**
  - `coins.lab_share = {mode: just_in_time, pct: 25}`
  - `labs.filler.enabled = true`
  - `labs.direct_start = true`
  - `labs.auto_start = true`
- **How it's applied.** Through the coordinator API on `127.0.0.1:8765`, the same save path the dashboard uses, so each change is a new revision in the history. A one-off script under `tools/` does the edit. It runs with `--dry-run` first, which prints a per-strategy diff, then saves.
- **The default changes too.** `template_lab_list_rules()` sets `direct_start: true`, so new strategies built from the template start labs directly.

### 3.2 Guaranteed filler (`fleet/lab_list.py`)

The current `_filler` needs both of these:
- price ≤ `max_price_pct_of_wallet`% of the wallet
- duration ≤ the gap until the target is affordable

If no entry passes, the slot plan has `covered=False` and the slot stays idle.

New step 5, the **last-resort filler**. It runs when the target and the step-4 filler both give nothing for an idle owned slot:
- Pick, from the slot's eligible entries, the cheapest one with price ≤ the wallet, including saved coins. Ties go to the shortest duration, then to rank.
- Its plan line in `why` reads "last-resort filler: keeps the slot busy".
- A slot is idle only if no eligible entry is affordable at all. Its `why` then says so, with the cheapest price it found.

Saving still works. The filler's price comes out of the wallet, and the saving plan recomputes against what's left. A filler may delay the saved-for target a little; D2 accepts that.

`choose_lab_action` (`fleet/resource_blocks.py:1077`) must treat the last-resort filler as `covered=True`, the same as the step-4 filler.

### 3.3 Labs before the Workshop

**On the main menu** (`tower_bot.py` elif chain, around 2969–3018), move the planned lab visit ahead of `_offer_claim` and Workshop `shopping.begin`, but only when a lab start is due (§3.5). Missions verification, the maintenance inspection, the first Workshop visit and the cards intro keep their places.

**At game over**, go HOME instead of RETRY when a lab start is due (§3.5). This replaces `lab_due()` / `_direct_lab_visit_due()` in the HOME decision.

### 3.4 Fill every idle slot in one visit (`lab_visit.py`)

- Today a verified start goes to `return`, which taps Battle.
- New behaviour: after a verified start (the debit is seen and the slot shows the research running), the visit goes back to `home` (the slot strip). It re-reads the strip and asks the `plan_action` hook for the next action. It moves to `return` only when the hook has nothing more to start.
- The whole-visit scan budget grows by one start's worth per extra slot. The per-stage budgets don't change.
- The one-transaction-at-a-time journal rule stays. Each start is prepared, tapped and proven before the next starts.

### 3.5 When is a lab start due? (one predicate)

Add one function, `lab_start_due(now)`. It returns true when any of these holds:
- (a) an owned slot was last read as idle
- (b) a slot's read completion time is ≤ now + 30 s
- (c) a slot was unlocked since the last strip read
- (d) the strip has never been read, or the last full read is older than 6 h (a safety refresh)

It is false while every owned slot is researching with a known completion time in the future.

The HOME decision at game over, the main-menu lab arm and the cadence all use this predicate. That removes the `wait_running` loop.

The 900 s backoff after a visit that didn't verify a start still applies to the **same lab on the same slot**. A different lab, or a different slot, is not held back (§3.6).

### 3.6 Picker timeouts

1. **Find the root cause first.** Read the saved evidence PNGs and worker.log context for the 34 `picker_stage_timeout` visits (2026-10-05 00:40–08:45, bots 82 and 83). Find what the picker saw and why the search didn't match. The fix depends on that cause and is decided during implementation, with a regression test built from a captured frame.
2. **Don't get stuck on one lab.** After 2 picker failures for the same lab, the planner skips that lab for 30 min and picks the next eligible entry or filler. The existing "3 misses make it `unfindable`" rule stays.

### 3.7 Safety checks kept under direct start

Direct start skips the rehearsal and canary only. Every per-start check stays:
- two identical reads before each tap
- title and price on the confirm dialog must match the plan
- the Rush-control proximity refusal
- the journal intent written before the tap
- a proven start: the coin debit is seen and the slot reads as researching
- `lab_start_uncertain` still halts lab starts on that bot and saves evidence

## 4. Part 2: a faster time between games

### 4.1 Between-games timing profile

**When it applies.** The profile is active from the `GAME_OVER` screen until a run is confirmed in progress, and ends on the first in-run frame. All values live in `config.py` and can be changed without code.

| Setting | Battle (unchanged) | Between games |
|---|---|---|
| Scan interval | 2.0 s | `MENU_FAST_SCAN_SECONDS = 0.6` |
| Screen confirmations | 2 | 2, with the second read taken `MENU_CONFIRM_GAP_SECONDS = 0.3` after the first |
| Navigation cooldown | 3.0 s | `MENU_FAST_NAV_COOLDOWN_SECONDS = 1.0` |
| Jitter | ±15% | ±15% |

`MENU_FAST_PROFILE = True` is a kill switch. Setting it to False restores today's timing.

### 4.2 RETRY when nothing is due

At game over, go HOME only when at least one of these is due:
- `lab_start_due`
- the Workshop is worthwhile (§4.3)
- a claim is owed
- the hourly badge check
- stats, cards or missions are scheduled

Otherwise tap RETRY.

### 4.3 Skip pointless Workshop visits

Visit the Workshop only when both of these hold:
- the wallet minus the lab savings reserve covers the cheapest planned purchase
- the visit budget isn't used up

This extends the existing `workshop_worthwhile` gate. A skipped visit logs one line with the reason.

### 4.4 Faster Workshop buying

- Purchases speed up through the faster scan interval alone.
- When the current page shows more than one affordable planned upgrade, buy them one after another without re-reading the whole page in between. Each buy still needs the wallet to go down before the next tap.
- Re-read the page after the run of buys, or as soon as the wallet doesn't change.

### 4.5 No idle frames on the home screen

- When a visit ends, the next due visit is armed on the very next frame instead of waiting for a full cycle.
- If nothing is due, tap Battle right away.
- **Watchdog:** after 20 s on the home screen with nothing armed, tap Battle and log `between_games_idle_watchdog`.

### 4.6 Missions guard

There is no logic change. The guard loop runs at the fast interval, so its hold time shrinks on its own.

## 5. Measurement and rollout

- **Measurement script** (`tools/between_games_report.py`):
  - Reads a worker DB read-only and prints the median, p90 and mean time between games.
  - Prints the share of that time per activity, the lab starts per hour, and the idle-slot hours.
  - Reuses the gap and attribution method from the 2026-10-06 analysis.
- **Baseline:** a median of 62–78 s, a p90 of 98–108 s, and 0 lab starts in 48 h.
- **Targets:**
  - median time between games of 20–30 s
  - every owned lab slot busy at least 95% of the time the bot is running
  - no increase in recovery episodes or `lab_start_uncertain`
- **Rollout:** Tiramisu64_82 runs for a few hours first, then the report is checked, then the rest of the fleet. The strategy data change (§3.1) goes out together with the code.

## 6. Testing

Unit tests, run per file only:
- last-resort filler selection, including saved coins, ties, and "nothing affordable"
- `lab_start_due` for each of (a)–(d), and false when every slot is busy with a known time
- main-menu arm order when a lab is due and when it isn't
- the HOME vs RETRY decision
- the multi-slot visit: two idle slots get two proven starts in one visit; the second start waits for the first to be proven
- the picker skip after 2 failures
- a regression test from a captured picker-failure frame
- timing profile switching at GAME_OVER and at the first in-run frame, plus the kill switch
- the Workshop worthwhile gate with the lab reserve
- the batched Workshop buys stopping when the wallet doesn't change
- the home-screen watchdog
- the strategy migration script's dry-run diff

## 7. Out of scope

- Bot downtime and pauses
- Battle scan timing
- Changes to the lab ranking itself
- The planner UI
