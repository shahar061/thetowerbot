# Tournament play

## Goal

When a tournament is open and the account holds a free ticket, the bot enters it
on its own, plays the run buying only attack and defense upgrades from an
editable priority list, records the result, and goes back to farming. Tournament
runs are flagged so the UI can filter them and draw them with a yellowish theme,
and they produce account-ledger entries. Applies to the main bot and to every
fleet worker.

## Decisions

- **Trigger:** automatic. Checked between runs on the main menu; no operator step.
- **Entry cost:** free tickets only. Never spend gems or paid entries. If the
  ticket count is 0 or unreadable, do not enter.
- **Purchases:** a dedicated tournament policy with an ordered, editable list of
  upgrade ids restricted to the ATTACK and DEFENSE categories.
- **Scope:** main bot (`Strategy` profile) and fleet workers (fleet strategy,
  assignable per account).
- **Public name:** when the game asks for a tournament user name, the bot types
  `<name_prefix>` + the first 6 characters of the account id (e.g.
  `Tower8B9CEF`).
- **Ledger:** ticket spent on entry, the run payout tagged as tournament, and
  prizes claimed. In-run cash purchases stay in the run's purchase list only.
- **Approach:** a between-runs visit, modelled on the small `ControlTaps` walks
  (`mail_claim.py`, `milestones_claim.py`), not on the Workshop shopping session.

## Observed game flow

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
   - Proof is `IN_RUN` plus the HUD tournament marker within the frame budget.
   - On proof: emit `TournamentEntered` and arm tournament mode on the bot.
   - With no proof: never tap again, and finish with `entry_unconfirmed`.
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

## Purchase policy and configuration

- The tournament policy is
  `AutopilotPolicy(enabled=True, preset="manual", rules=<tournament rules>, cash_reserve, cash_spend_limit_pct, purpose=<current>)`.
  - `policy.choose` already buys the highest-priority affordable rule.
  - The executor (`autopilot.py`) is unchanged, and it never opens the utility
    tab because no rule lives there.
- Validation, at save time: every rule's `upgrades.by_id(id).category` is
  `ATTACK` or `DEFENSE`, and none is an unlock tile.

Main bot: a new `tournament` section in `Strategy` (`strategy.py`):

```json
"tournament": {
  "enabled": true,
  "name_prefix": "Tower",
  "rules": [{"upgrade_id": "damage"}, {"upgrade_id": "attack_speed"}],
  "cash_reserve": 0,
  "cash_spend_limit_pct": 100
}
```

Fleet: the same `tournament` section is an optional top-level part of a fleet
strategy (`fleet/build_route.py`), next to `battle` and `workshop`.
- It is resolved per account through the existing assignment.
- While armed, `reroll_progress.battle_policy` returns the tournament policy and
  bypasses phases and blocks.
- A strategy without the section uses the defaults.

Default rules, in order:
1. `damage`
2. `attack_speed`
3. `health`
4. `defense_absolute`
5. `critical_chance`
6. `critical_factor`
7. `defense_percent`
8. `health_regen`
9. `thorns`
10. `lifesteal`
11. `range`
12. `multishot_chance`

Locked rows never appear in battle and are skipped.

UI: a "Tournament" panel with an enable toggle and an ordered upgrade list, whose
picker is limited to attack and defense.
- Fleet: in Strategy Studio (`web/ui/app/fleet/reroll/strategies/`).
- Main bot: on the strategy page (`web/ui/app/strategy/page.tsx`).

## Recording

### Storage (`db.py`)

Columns are added with the existing ad-hoc `ALTER TABLE` migration in
`db.connect`. `_schema_probe` defaults the new fields for read-only worker
databases that have not migrated.

- `runs.tournament INTEGER NOT NULL DEFAULT 0`. It sits alongside `purpose`.
- `tournament_entries`, one row per entry:
  - `id`, `run_id`, `entered_at`, `league`, `tournament_id`
  - `wave`, `rank`, `coins`, `ad_coins`
  - `prize_gems`, `prize_stones`, `claimed_at`
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
- **Unconfirmed entry**: no retry tap, nothing armed and no ledger line. Normal
  navigation takes over.
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
  - Utility and unlock ids are rejected in tournament rules.
  - The default list validates.
  - The fleet `battle_policy` returns the tournament policy while armed.
- Recording tests:
  - The `db` migration on an old database.
  - `list_runs` fields.
  - Records and best wave ignore tournament runs.
  - `ledger.classify` for the three kinds.
- vitest for `RunsTable` (tint, chip) and `LedgerEntries` (tint, chip).
- Live acceptance on one paused fleet worker at the next tournament with a
  ticket:
  1. It enters automatically.
  2. It buys only attack and defense.
  3. The stats row and ledger lines are written.
  4. It returns to farming.
  5. The UI shows the yellow row and the filter works.

## Out of scope

- League and battle-condition strategy, and schedule reading beyond the join
  timer.
- Spending gems or paid tickets, and retrying a tournament after the free entry.
- Prize-claim automation, until the claim screen is captured.
