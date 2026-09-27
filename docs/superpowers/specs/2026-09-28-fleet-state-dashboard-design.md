# Fleet State Dashboard — Design

Date: 2026-09-28
Mockup (approved): https://claude.ai/artifact/GK3DA2zT6wtAYK1nVQ7t9R

## Goal

One live page that shows the full state of every emulator account side by side:
Workshop, Cards, Labs, bot activity, the live battle, recent runs, in-run upgrades
and balances. The user can focus on a subset of emulators. The page refreshes on
each scan, works on a phone, and uses a vibrant look based on the existing
Telemetry theme.

## Decisions

| Topic | Decision |
|---|---|
| Scope | Backend data and the page, in one spec. Every section is backed by real data except the gaps listed under Out of scope. |
| Live updates | The page polls `GET /api/fleet/state` every 2 s. A column re-renders only when that account's `scan` counter changes. Fanning worker SSE streams into the parent is out of scope, because SSE works only inside one process today. |
| Route | `/fleet/state`, a new fleet page with a sidebar entry. |
| Stones | A placeholder showing "—" with a "not tracked yet" hint. Reading stones needs a new vision scanner, which is a separate task. |
| Unknown prices | Shown as "price unknown". There is no new price-observation scanning. |
| Next navigation | Shows the bot's **current activity** (last navigation, shopping step or claim), labelled "Now". It is not a planned future target. |

## Architecture

```
worker DBs (read-only) ─┐
worker /api/status  ────┼─> fleet/state_view.py::fleet_state() ─> GET /api/fleet/state ─> web/ui/app/fleet/state (poll 2s)
catalog JSON files  ────┘
```

- `fleet/state_view.py` (new) holds pure builder functions, one per section.
  Each builder takes plain inputs (revision dict, ledger rows, run rows, status
  dict, catalogs) and returns a typed dict, so each one can be unit tested
  without a live worker.
- `fleet_state()` enumerates the members the same way `reroll_snapshot()` does.
  For each worker it opens the SQLite DB **read-only** and fetches
  `/api/status` with the existing 0.2 s timeout. Workers are fetched
  concurrently (a thread pool), so one slow worker doesn't delay the rest.
- `web/app.py` adds the route `GET /api/fleet/state`.

## Payload (`/api/fleet/state`)

```jsonc
{
  "generated_at": "iso",
  "accounts": [{
    "id": "emu-1", "name": "Main", "serial": "emulator-5554",
    "online": true, "stale_seconds": 0, "scan": 18442,
    "bot": { "screen": "IN_RUN", "now": "Shopping · Workshop", "live": true },
    "battle": { "tier": 11, "wave": 4812, "cash": 8.42e9, "elapsed_s": 5780, "best_wave": 5020 },  // null when not live
    "balances": { "coins": 3.84e9, "gems": 1842, "stones": null },
    "decision": { "phase": "...", "upgrade_id": "damage", "category": "attack", "name": "Damage", "cost": 1.2e8 },  // null if none
    "workshop": {
      "totals": { "attack": 3110, "defense": 2804, "utility": 2990 },
      "categories": { "attack": { "unlocked": 9, "total": 10,
        "skills": [{ "id": "damage", "name": "Damage", "level": 480, "invested": 2.1e9, "bot_spent": 1.7e9, "next_cost": 1.2e8, "status": "..." }],
        "next_unlock": { "id": "unlock_multishot", "name": "Multishot", "cost": null } } },
      "recent": [{ "ts": "iso", "id": "damage", "name": "Damage", "category": "attack", "level": 480, "price": 1.1e8 }]
    },
    "cards": { "slots": { "equipped": 6, "capacity": 7, "next_slot_gems": 150 },
               "items": [{ "name": "Damage", "level": 5, "copies": 12 }],
               "gems_invested": 910, "recent": [{ "ts": "iso", "name": "...", "gems": 20 }] },
    "labs": { "slots": 2, "running": [{ "id": "game_speed", "name": "Game Speed", "to_level": 12, "completes_at": "iso" }],
              "levels": [{ "id": "...", "name": "...", "level": 11, "next_cost": 5.2e6 }],   // next_cost null when unknown
              "next": { "id": "...", "name": "...", "cost": null },
              "recent": [{ "ts": "iso", "name": "...", "price": 4.1e6 }] },
    "run_upgrades": { "scope": "current" /* or "last" */, "total": 214,
                      "by_category": { "attack": 90, "defense": 70, "utility": 54 },
                      "items": [{ "id": "damage", "name": "Damage", "category": "attack", "levels": 31 }] },
    "runs": [{ "tier": 11, "wave": 5020, "coins": 3.1e8, "duration_s": 6020, "ended_at": "iso", "abandoned": false }]
  }]
}
```

## Data sources and new backend work

| Field | Source | Change |
|---|---|---|
| Workshop level, next cost, status | `workshop_levels.workshop_state()` on the latest revision | reuse |
| Category totals | sum of `level_min` per `category`, skipping `unseen`/`unmatched` | new, in the builder |
| Invested per skill | `sum(ladder.next_coins[:level_min])` from `catalog/workshop-levels.v1.json` via `workshop_levels.ladders()` | new |
| Bot-spent per skill | ledger `WORKSHOP_BUY` rows (`dry_run=0`, verdict bought/free), summing `-delta` per item | new query |
| Next unlock per category | first unowned `unlock=True` entry of `upgrades.CATALOG` in order, with ownership from `AccountRevision.unlocks`; price from `workshop-prices.v1.json`, else null | new |
| Workshop recent | ledger `WORKSHOP_BUY`, last 30 | widen the existing query |
| Decision (next buy) | `events.AutopilotDecided` | `sinks/state.py::BotState` stores `decision` and includes it in `snapshot()`, so `/api/status` carries it |
| "Now" (current activity) | `Navigated`, `ShoppingStarted`, `ClaimStarted` events | `BotState.activity` string plus timestamp, in `snapshot()` |
| Screen, cash, elapsed, scan | `BotState` via `/api/status` | reuse |
| Wave | `perception` `combat["wave"]` | `ScanCompleted` carries `wave` when in a run, and `BotState.wave` stores it |
| Tier (live) | tier of the last `runs` row, or the route/plan tier if known | reuse the aggregator logic |
| Best wave per tier | `SELECT tier, MAX(wave) FROM runs WHERE abandoned=0 GROUP BY tier` | new query |
| Last 5 runs | `runs` table | new query (widen `observed_metrics`' 3-row read) |
| In-run upgrades | current run: worker `/api/runs/{run_id}/upgrades`; otherwise `run_upgrades` for the last run | new |
| Cards | `cards.facts()` currently persists only slots | **persist per-card `level` and `copies`** as facts; gems invested = sum of ledger `CARD_BUY` spent; next slot = `labs.v1.json` `card_slots[capacity+1]` |
| Labs | `AccountRevision.lab_levels`, `lab_jobs`, `lab_slots_owned`; ledger `LAB`; `labs.v1.json` | builder reuses `fleet/labs_view` helpers; next cost is null where the catalog has no price |
| Coins, gems | `currencies.currency_overview()` | reuse |
| Stones | none | `null` (placeholder) |

Wave and decision are small event and state additions. They must not change bot
behaviour, only what the bot records.

## Frontend

`web/ui/app/fleet/state/page.tsx`, plus components in the same folder:

- `FleetStateBar`: a sticky bar with emulator chips (status dot and live
  hint), an All / Only live segmented control, and the global scan heartbeat,
  live count and fleet coins. Chips wrap on mobile. The selection is kept in
  `localStorage` inside try/catch.
- `AccountColumn`, with sections in a fixed order:
  1. header: id, name, LIVE badge, serial, stale badge
  2. bot: screen chip, Now, scan number and age
  3. battle HUD: wave, cash, elapsed, bar against the tier's best
  4. balances
  5. bot buy queue
  6. Workshop
  7. Cards
  8. Labs
  9. in-run upgrades
  10. last 5 runs
- `CategoryLedger`: a shared table (level, invested, next) with the NEXT
  highlight, next unlock and a scrollable recent-purchases list. Workshop,
  Cards and Labs all use it.
- `RunUpgradesChart`: total, category split bar, and the top 8 horizontal bars.
- `useFleetState`: polls every 2 s, keeps the previous payload, and memoizes
  per-account by `scan`.

Layout: a grid with `minmax(min(100%, 340px), 1fr)`. With 1–2 accounts selected,
the inner sections flow into CSS columns and Cards and Labs open by default.
With 3 or more, those sections start collapsed. Open/closed state is remembered
per section.

Styling adds tokens to `globals.css`: `--cat-attack` (red), `--cat-defense` (blue),
`--cat-utility` (gold), `--cat-cards`, `--cat-labs`, `--live`. It uses Chakra
Petch for display labels and big numbers, and the existing Inter and JetBrains
Mono. Numbers are tabular and use K/M/B/T formatting; `workshopFormat.ts` is
reused or extended. Animations respect `prefers-reduced-motion`. A null value
renders as "—" or "price unknown", never as a made-up number.

## Error handling

- Worker status request fails or times out: build the account from its DB only,
  set `online: false`, and show a "stale · Ns" badge. Battle is null.
- Worker DB is missing or locked: the account appears with an `error` string
  and an offline card. Other accounts are unaffected.
- A builder throws: that section is `null` with the error logged, and the rest
  of the account still renders.
- The whole request fails on the frontend: keep the last payload and show a
  "connection lost" pill in the bar.

## Testing

Only the new tests are run locally.

- `tests/test_fleet_state_view.py`, using fixture revisions, ledgers and runs:
  category totals, invested ladder sums, bot-spent, next unlock (with and
  without a price), best wave per tier, last 5 runs, current vs last run
  upgrades, offline worker, and a failing builder.
- `tests/test_bot_state.py`, with additions: `AutopilotDecided` sets
  `decision`; navigation, shopping and claim events set `activity`;
  `ScanCompleted` wave populates `wave`.
- Cards facts: `cards.facts()` emits per-card level and copies.
- Frontend: `next build` type-checks, plus one manual check at desktop and
  ~400px width.

## Out of scope

- Stones balance scanner.
- Scanning in-game prices for unlocks and labs without catalog prices.
- A true planned "next navigation" target.
- Fleet-wide SSE fan-in.
