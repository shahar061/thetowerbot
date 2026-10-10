# Tournament play

Updated 2026-10-10. Revised design awaiting review; implementation and live
acceptance are not complete.

## Goal

When a tournament is open and the account holds a free ticket, the bot enters it
on its own, plays the run buying capped opening cash upgrades followed by combat
and survival upgrades from an
editable priority list, records the result, and goes back to farming. Tournament
runs are flagged so the UI can filter them and draw them with a yellowish theme,
and they produce account-ledger entries. Applies to the main bot and to every
fleet worker.

## Decisions

- **Trigger:** automatic. Checked between runs on the main menu; no operator step.
- **Entry cost:** free tickets only. Never spend gems or paid entries. If the
  ticket count is 0 or unreadable, do not enter.
- **Purchases:** a dedicated tournament policy with editable combat priorities,
  finite opening Cash Bonus/Cash per Wave targets and budget, and explicitly
  allowed survival utility upgrades. Coins/Kill Bonus and Coins/Wave are forbidden
  at validation and runtime, including through fleet progression overrides.
- **Initial entry limit:** one supplied free ticket per account per tournament.
  No gem purchases or ad-supported retries.
- **League:** the user is in Copper/Silver/Gold; observe the exact league in game.
- **Active farming run:** finish it before checking entry. Give tournaments
  priority over starting another farm run; record a missed join window.
- **Scope:** main bot (`Strategy` profile) and fleet workers (fleet strategy,
  assignable per account).
- **Public name:** when setup requires a name, use an explicitly configured name.
  If missing, exit with setup required. Never derive a public name from an
  account identifier.
- **Ledger:** ticket spent on entry, the run payout tagged as tournament, and
  prizes claimed. In-run cash purchases stay in the run's purchase list only.
- **Approach:** a between-runs visit, modelled on the small `ControlTaps` walks
  (`mail_claim.py`, `milestones_claim.py`), not on the Workshop shopping session.

## Observed game flow

Tournaments start Wednesday/Saturday at 00:00 UTC (Jerusalem: 03:00 during
daylight saving, 02:00 during standard time). Use the UTC schedule as a hint;
fresh OPEN, join timer and event identity evidence authorize entry. Do not infer
availability or expiry solely from the local weekday.

Sources: [Tournament rules](https://the-tower-idle-tower-defense.fandom.com/wiki/Tournaments),
[developer changes](https://www.techtreegames.com/post/v29-patch-notes-august-25-2026).
Older guides may omit recent changes. Record observed league and conditions,
preserving unknown values. Initial lower-league priorities require tuning from
account results; no fixed purchase cutoff is claimed optimal.

Captured on a fleet account (best tier-1 wave 100) on 2026-09-30; frames are in
`tests/fixtures/tournament/`.

| Fixture | Screen | Facts the bot relies on |
|---|---|---|
| `menu_tournament_open.png` | Main menu | Trophy with an `OPEN` label in the left column. |
| `menu_tournament_open_after_entry.png` | Main menu, ticket spent | Still `OPEN`: the menu does not tell whether a ticket is left. |
| `tournament_username_prompt.png` | `USER NAME` modal over the tournament page | Shown on the first visit. Text field, `Save`, `X`. Closing with `X` returns to the main menu. "You cannot change your name during an active tournament." |
| `tournament_player_profile.png` | `PLAYER PROFILE` popup | Shown right after Save; closed with `X`. |
| `tournament_join_ticket_1.png` | Tournament page before entry | Header `TOURNAMENT`, ticket counter top right (`1`), league (`Copper League`), `BATTLE` with a ticket icon, `Time left to join: 5h 59m`, `Tap To Return To Game`. |
| `tournament_run_start.png`, `tournament_run_midway.png` | Tournament run | `screens.classify` returns `IN_RUN`. The HUD wave panel shows a trophy icon and `Tier 1+` instead of `Tier 1`. |
| `tournament_stats.png` | `TOURNAMENT STATS` modal at death | League, `Wave 8`, `Killed By Basic`, `currently at rank: 30`, coins earned, ad coins earned, `OK`. `screens.classify` still returns `IN_RUN` here. |
| `tournament_leaderboard_ticket_0.png` | Tournament page after the run | Ticket counter `0`, leaderboard, current and next prize, `Tournament ID: …`, `BATTLE` still drawn, `Tap To Return To Game`. |
| _(not captured yet)_ | `Buy Ticket` modal over the tournament page | Seen when the page is opened after the free ticket was spent: ticket counter `0` behind it. Title `Buy Ticket`, "Get another ticket to try again and improve your rank", `Cancel` on the left, and an ad button (video icon, no text) on the right. `Cancel` closes it and leaves the page. |

Not yet observed: the prize-claim screen shown after the tournament closes. The
`Buy Ticket` modal has only been seen in a BlueStacks window screenshot; its
device frame still has to be captured for the reader's fixture.

## Components

### `tournament_screen.py` — pure readers

Each reader takes a frame and its OCR boxes and returns a frozen reading or
`None`. No taps, no I/O.

- `read_menu_entry` → trophy centre and `OPEN` label.
- `read_page` → `tickets: int | None`, `league`, `battle` centre,
  `join_time_left_s`, `tournament_id | None`, `return_to_game` centre.
- `read_buy_ticket` → `Cancel` centre, keyed on the `Buy Ticket` title and the
  `Cancel` text. It is checked before `read_page`, because the dimmed page
  (header, ticket counter) can still be read through the modal.
- `read_username_prompt` → field centre, `Save` centre, `X` centre, field text.
- `read_profile_popup` → `X` centre.
- `read_stats_modal` → `league`, `wave`, `rank`, `coins`, `ad_coins`, `killed_by`,
  `OK` centre (each field `None` when unreadable).
- `read_hud_tournament_marker` → `True` when the wave panel shows the trophy and
  `Tier <n>+`.

Readers follow the style of `free_ticket.py` / `unlocked_screen.py`: trusted OCR
boxes only, exact-pattern matching, and centres taken from the current frame.

### `tournament_visit.py` — `TournamentVisit(ControlTaps)`

A bounded state machine, run from the main menu when no other walk is active.
It has the same public surface as the other walks: `request()`, `advance(...)`,
`active`, `snapshot()`, `cancel()`.

1. **OPEN** — tap the trophy, then wait for the tournament page or the name prompt.
2. **NAME** (only when the prompt shows):
   1. Tap the field and type the name via `input text`.
   2. Require the field text to equal the name exactly, then tap `Save`.
   3. Close the profile popup.
3. **READ** — read the page.
   - `Buy Ticket` modal showing: the free ticket is already spent. Tap `Cancel`
     once, require the modal gone on a later frame, then go to RETURN and
     finish with `no_free_entry`.
   - `tickets == 0` or `None`: go to RETURN and finish with `no_free_entry` or
     `tickets_unknown`.
4. **ENTER** — tap `BATTLE` once. This requires `tickets >= 1` and no
   `Buy Ticket` modal, both read on the same frame the tap targets.
   - Require an open join window, a ticket-based action and no gem price or
     overlay. Unknown/payment controls never authorize entry.
   - Before tapping, durably journal account/lease identity, event key, attempt
     ID, league, ticket observation, prior farming context and pending entry.
   - Proof is `IN_RUN` plus the HUD tournament marker within the frame budget.
   - On proof: emit `TournamentEntered` and arm tournament mode on the bot.
   - With no proof: never tap again. Persist `entry_unconfirmed` and reconcile
     before releasing control; generic BATTLE/RETRY must remain suppressed.
5. **RETURN** — tap `Tap To Return To Game`, falling back to Android back, and
   confirm the main menu.

Scheduling:
- A visit is due when tournaments are enabled for the account and the menu entry
  is visible. The menu keeps showing `OPEN` after the ticket is spent, so `OPEN`
  alone does not make a visit due:
  - After a confirmed entry or a `no_free_entry` result, no visit is due until
    the `Time left to join` read on that visit has run out.
  - After a `tickets_unknown` result, or when no join time was read, no visit
    is due for 30 minutes.
- A visit is not due while tournament mode is armed.

The journal survives restarts. A new event needs a visible ID or validated UTC
window plus fresh page evidence. Suppress additional entries for an already
attempted event. Unknown observations use a persisted bounded backoff, shortened
when a known join deadline is closer; elapsed time never clears pending entry.
Existing startup closes unfinished run records, so journal recovery must run
before that cleanup and before generic run-start handling. Use idempotent
attempt/run/result/ledger keys. Recover run HUD, stats and leaderboard states
without consuming another ticket. Pause, account or lease changes stop actions.

### `tower_bot.py` wiring

- **Build and schedule**:
  - Build `self.tournament_visit` next to the other walks and add it to
    `_any_walk_active`.
  - On `MAIN_MENU`, request the visit before the normal `BATTLE` navigation when
    it is due.
- **Arming**: tournament mode is armed by a confirmed visit entry, or by the HUD
  marker on any in-run frame. The HUD case covers a restart mid-tournament.
- **Run start**: `RunStarted` / `RunEnded` carry `tournament` from the armed
  state.
- **Stats modal**: `read_stats_modal` is checked before the existing IN_RUN
  handling. When it matches:
  1. End the run, emit `TournamentResult`, and tap `OK`.
  2. Read the leaderboard page for `tournament_id`, then return to the game.
  3. Disarm tournament mode.
- **Autopilot**: while armed, the autopilot uses the tournament policy (below).
  The legacy `strategy.actions` template clicks are skipped.
  Generic RETRY is also suppressed. Tournament policy is an effective runtime
  override; do not replace the user's active profile pointer. On return to the
  menu, use the current farming settings, preserving edits made during the run.

## Purchase policy and configuration

- The tournament policy is
  `AutopilotPolicy(enabled=True, preset="manual", rules=<tournament rules>, cash_reserve, cash_spend_limit_pct, purpose=<current>)`.
  - `policy.choose` already buys the highest-priority affordable rule. A
    tournament rule resolver must apply opening limits and rotate growth rules
    before selection; the static ordered policy alone is insufficient.
  - Reuse the executor's verified wallet, price and target checks. Utility is
    permitted for the explicitly allowed cash/survival rules.
- Validate at save time and runtime: no coin upgrade IDs or unlock tiles.
- Opening cash rules require finite targets, a finite cumulative cash budget,
  and a configured wave cap. If not configured, skip cash investment rather
  than guessing targets. End the opening at the earliest limit, or sooner when
  a supported fresh survival-pressure signal applies.
- Rotate uncapped Health, Attack Speed and Damage rules after successful
  purchases so the first affordable uncapped rule cannot monopolize cash.
  Persist the cycle per run; skip locked/maxed/unaffordable rows.

Main bot: a new `tournament` section in `Strategy` (`strategy.py`):

```json
"tournament": {
  "enabled": true,
  "public_name": null,
  "opening_cash": {
    "cash_bonus_target": null,
    "cash_per_wave_target": null,
    "cash_budget": null,
    "until_wave": null
  },
  "rules": [{"upgrade_id": "damage"}, {"upgrade_id": "attack_speed"}],
  "cash_reserve": 0,
  "cash_spend_limit_pct": 100
}
```

Null opening fields disable cash investment until configured; they never mean
unlimited spending. The example combat rules are illustrative, not optimal
account defaults. Finite survival targets use the existing rule target field.

Fleet: the same `tournament` section is an optional top-level part of a fleet
strategy (`fleet/build_route.py`), next to `battle` and `workshop`.
- It is resolved per account through the existing assignment.
- While armed, `reroll_progress.battle_policy` returns the tournament policy and
  bypasses phases and blocks.
- A strategy without the section uses the defaults.

Initial lower-league priorities, editable and tuned from observed levels:
1. Cheap finite `defense_percent` and `thorns` targets.
2. Rotating `health`, `attack_speed` and `damage` growth purchases.
3. Finite `lifesteal`, knockback and orb targets, if unlocked.
4. Explicitly configured Recovery Package/Enemy Level Skip utility, if unlocked.

Defense Absolute and Health Regen are optional account-specific choices rather
than universal defaults. Before combat, buy only configured capped `cash_bonus`
and `cash_per_wave` rules; unknown prices/targets never authorize spending.

Locked rows never appear in battle and are skipped.

UI: a "Tournament" panel with an enable toggle and an ordered upgrade list, whose
picker excludes coins/unlock tiles and includes explicitly allowed utility.
Expose opening cash targets/budget/cap and setup-required/pending/skipped state.
Free-only entry is fixed and has no paid-entry toggle.
- Fleet: in Strategy Studio (`web/ui/app/fleet/reroll/strategies/`).
- Main bot: on the strategy page (`web/ui/app/strategy/page.tsx`).

## Recording

### Storage (`db.py`)

Columns are added with the existing ad-hoc `ALTER TABLE` migration in
`db.connect`. `_schema_probe` defaults the new fields for read-only worker
databases that have not migrated.

- `runs.tournament INTEGER NOT NULL DEFAULT 0`. It sits alongside `purpose`.
  This is the canonical mode flag; existing farm/milestone purpose constraints
  stay intact. Event context is captured per run, never inferred afterward from
  a mutable selected profile.
- `tournament_entries`, one row per entry:
  - `id`, `run_id`, `entered_at`, `league`, `tournament_id`
  - `wave`, `rank`, `coins`, `ad_coins`
  - `prize_gems`, `prize_stones`, `claimed_at`
- A separate durable attempt journal preserves pending entry before a run exists,
  including the event key, attempt ID, controller stage and reconciliation status.
  Uniqueness/idempotency keys apply per account and event. Existing-database
  migration tests must establish journal recovery as well as default mode flags.
- A tournament run's `runs.tier` stays NULL. Its league is stored in
  `tournament_entries`.

### Events (`events.py`, persisted by `sinks/store.py`)

- `RunStarted` and `RunEnded` gain `tournament: bool = False`.
- `TournamentEntered(run_id, league, tickets_before)`
- `TournamentResult(run_id, league, wave, rank, coins, ad_coins, tournament_id)`
- `TournamentPrizeClaimed(tournament_id, gems, stones)`

The reroll journal records the new event names with no schema change.

### Ledger (`ledger.py`)

- `TOURNAMENT_ENTRY`: currency `ticket`, delta `-1`, with `run_id` and
  `detail = {league}`.
- `RUN_PAYOUT` for a tournament run: unchanged amounts, with
  `detail.tournament = true`, `league` and `rank`.
- `TOURNAMENT_PRIZE`: one line per currency (`gems`, `stones`), with
  `detail = {tournament_id, rank}`.

The prize-claim reader and its tap flow are built only after the claim screen is
captured. Until then the kind and the event exist, but nothing emits them.

### Keeping farming numbers clean

Tournament runs are excluded from:
- The per-tier wave and coin records (`db._records`).
- Best tier-1 wave in `fleet/reroll_metrics.py`, `fleet/state_records.py` and
  `web/app.py`. Queries filtering on `tier = 1` already skip them because their
  tier is NULL; every other query adds `tournament = 0`.
- The fleet variant comparison and the recent-run pace statistics.

Also gate in-memory best-wave, ladder-tier and tier-best-wave updates in
RunEnded. Query filters alone do not protect tier progression. Rank recorded at
death is provisional; leaderboard expected prizes are not confirmed claims.

### API and UI

- `db.list_runs` and `/api/runs` return `tournament`, `league` and `rank`.
- The `RunRow` type in `web/ui/lib/types.ts` gains `tournament`, `league` and
  `rank`.
- Fleet runs page (`RunsTable.tsx`):
  - Tournament rows get a yellowish background tint using the amber of the
    existing record badge (`oklch(0.82 0.14 85)` at low alpha).
  - Each shows a `Trophy` icon and `<league> · rank <n>` in place of the tier.
  - A "Tournaments" chip next to "Records only" filters to tournament runs.
- Main-bot runs page (`web/ui/components/RunTable.tsx`): the same tint and chip.
- `RunDetailPanel.tsx`: shows "Tournament run", plus league, rank and tournament
  id.
- Ledger pages (`LedgerEntries.tsx`, single and fleet): tournament lines, meaning
  the new kinds and `RUN_PAYOUT` with `detail.tournament`, get the same tint and
  a "Tournaments" chip.

## Error handling

- **Entry safety**:
  - `BATTLE` on the tournament page is tapped only from ENTER, with a same-frame
    reading of `tickets >= 1`, and at most once per visit.
  - An unreadable ticket count never leads to entry.
  - The `Buy Ticket` ad button is never tapped. If `Cancel` does not close the
    modal, the visit finishes `uncertain` and leaves through Android back.
- **Unconfirmed entry**: no retry tap or unverified ledger line. Durable recovery
  owns navigation until fresh evidence reconciles run/page state; never release
  a pending entry straight to generic farming BATTLE.
- **Name step**:
  1. If the field text does not match after typing, clear it and type once more.
  2. If it still fails, close with `X`, record `name_failed`, and disable
    tournament visits for the account until an operator resets the flag.
- **Stats modal partly unreadable**: the run still ends, unread fields are
  stored as NULL, and the bot taps `OK`.
- **Tournament id unreadable**: store NULL and return to the game.
- **Lost frames**: every step has a frame budget. When it is exhausted the visit
  finishes `uncertain`, and the bot leaves any modal through its `X` / `Tap To
  Return To Game` / Android back.
- **Pause, lease change or identity change mid-visit**: the walk cancels as the
  other `ControlTaps` walks do.

## Testing

Only the new and changed test files are run.

- `tests/test_tournament_screen.py`:
  - Every reader against the fixtures, with OCR JSON recorded once per fixture
    under `tests/fixtures/ocr/`.
  - Negatives:
    - A normal `GAME STATS` modal is not a tournament stats modal.
    - The normal in-run HUD has no marker.
    - The ticket-0 page reports `tickets == 0`.
    - The ticket-0 page without the modal is not a `Buy Ticket` modal.
- `tests/test_tournament_visit.py`, with a fake device:
  - Happy path.
  - Tickets 0.
  - `Buy Ticket` modal on arrival: `Cancel` tapped once, the ad button never,
    `BATTLE` never, result `no_free_entry`.
  - No visit due again until the join time read on the last visit runs out.
  - Tickets unreadable.
  - Name prompt, success and failure.
  - Unconfirmed entry.
  - Cancel mid-visit.
  - `BATTLE` tapped at most once.
- Policy tests:
  - Coin and unlock IDs are rejected; permitted cash/survival utility validates.
  - Cash targets/budget/cap, rotating growth rules and runtime coin rejection
    work despite fleet progression/profile overrides.
  - The default list validates.
  - The fleet `battle_policy` returns the tournament policy while armed.
- Recording tests:
  - The `db` migration on an old database.
  - `list_runs` fields.
  - Records and best wave ignore tournament runs.
  - In-memory tier progression ignores tournaments.
  - Crash before/after entry, stats/leaderboard recovery, and startup cleanup do
    not duplicate entry, runs or ledger events.
  - `ledger.classify` for the three kinds.
- vitest for `RunsTable` (tint, chip) and `LedgerEntries` (tint, chip).
- Live acceptance on one paused fleet worker at the next tournament with a
  ticket:
  1. It enters automatically.
  2. It buys only permitted cash/combat/survival upgrades, no coins or gems.
  3. The stats row and ledger lines are written.
  4. It returns to farming.
  5. The UI shows the yellow row and the filter works.

## Out of scope

- Automatic battle-condition optimization and automatic loadout switching.
- Spending gems or paid tickets, and retrying a tournament after the free entry.
- Prize-claim automation, until the claim screen is captured.

## Implementation readiness

This revision is a focused free-entry implementation design, not certification
of the broader autonomy roadmap. Its tournament task currently has incomplete
prerequisites and an explicit dependency gate. Resolve that gate in planning
before claiming the full roadmap task can execute or be completed. Do not mark
dependencies complete from this design or from existing fixtures.

Verify only new/changed files and directly affected neighboring tests, using
`-p no:allure_pytest` and an explicit long timeout; never run a directory suite.
The real Buy Ticket and final claim captures remain evidence requirements.
