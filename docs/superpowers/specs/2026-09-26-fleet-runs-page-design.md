# Fleet Runs page, Killed By tracking, and per-run upgrade levels

Approved mock: https://claude.ai/artifact/XyaEwd9BRa3MDYiqjFhVwZ

## Goal

See every battle the reroll fleet played in one place. For each run: coins,
duration, wave, tier, what killed the tower, and which in-run upgrades it
bought. Chronological high-score badges on the wave and coins columns. A side
panel for one run shows its end-of-run in-run upgrade levels as Workshop-style
cube cards.

## Decisions

| Topic | Decision |
|---|---|
| Killed By source | The game-over GAME STATS popup, read by OCR when the run ends. `game_over.parse_frame` already parses it; today nothing persists the result. The Settings → Stats screen is not used. |
| Coins | `runs.coins` stays "coins earned" (glyph reader, unchanged). New `ad_coins` from the same popup. The UI shows `coins + ad_coins` as total. |
| Record scope | Per worker DB, which is one emulator and one account. Wave records are also per tier. Coin records are not per tier. |
| Record semantics | Chronological. A run is a record when it strictly beats every earlier finished, non-abandoned run in that scope. The first finished run is a record. A later record does not remove an earlier badge. Only the latest record is **standing** (filled badge); earlier ones are **broken** (outlined badge). |
| Upgrade levels | Counted from the bot's own `BattlePurchased` events, one level per buy (in-run buys are x1). In-run tiles show value and price, not level, so there is no screen to read a level from. Free-upgrade procs are not counted. The panel says "levels bought". |
| Durability | `events` is pruned after the retention window; `runs` is never pruned. Each run's upgrade summary is written to a new never-pruned table when the run ends. |
| Max levels | `max_level` from `catalog/workshop-levels.v1.json` (Health 6000, Defense Absolute 5000, …). Category from the existing `_category_of`. |

## Data

`runs` gains two nullable columns, added by the migration pattern `purpose` uses:

- `killed_by TEXT`: the enemy name as parsed (`Tank`, `Boss`, …). NULL together with `abandoned = 1` means Abandoned. NULL on a finished run means the read failed (shown as "Unreadable"). No extra status column.
- `ad_coins INTEGER`.

New table, never pruned:

```sql
CREATE TABLE IF NOT EXISTS run_upgrades (
  run_id     INTEGER NOT NULL REFERENCES runs(id),
  upgrade_id TEXT    NOT NULL,
  levels     INTEGER NOT NULL,   -- buys this run
  spent      INTEGER NOT NULL,   -- sum of prices that were read
  unpriced   INTEGER NOT NULL,   -- buys whose price was unreadable
  PRIMARY KEY (run_id, upgrade_id)
);
```

- Written by `db.finish_run` in the same transaction: it aggregates that run's `BattlePurchased` rows. Upsert, so a repeated finish is idempotent.
- The migration backfills every finished run that has no `run_upgrades` rows but still has purchase events. A run whose events were already pruned stays without rows, and the panel shows "No purchase record for this run", as `RunPurchases` does today.

## Capture (bot)

At a confirmed GAME_OVER, `tower_bot.py` already calls `_read_modal_stats` for wave, coins and tier. It will also:

- Run OCR once on that same frame and pass the boxes to `game_over.parse_frame`.
- Take `killed_by` and `ad_coins_earned` from the result.
- Put both on `RunEnded`; `sinks/store.py` writes them.

An absent or unreadable field is stored as NULL, never guessed. The glyph-read wave, coins and tier stay authoritative; the OCR read only adds these two fields. An OCR failure or exception must not break run end.

## API

The UI reuses the per-worker pattern (`x-account-scope` header → that worker's DB). There is no new fleet aggregator endpoint.

- `GET /api/runs` gains fields on each row:
  - `killed_by`, `ad_coins`, `total_coins`
  - `buys`: sum of `run_upgrades.levels`, or NULL when there is no record
  - `wave_record`, `coin_record`: `null` | `"standing"` | `"broken"`
  - for tooltips: `record_prev` (run id and value it beat), `broken_by` (run id)

  Records are computed in `db.list_runs` over the worker's **full** run history, not just the page returned by `limit`. That is one ordered scan of `runs`, which is small.
- `GET /api/runs/{run_id}/upgrades`: `{categories: [{name, items: [{upgrade_id, name, levels, max_level, spent, unpriced}]}], totals: {levels, spent, unpriced}}`, or `null` when there are no rows. Items cover every catalog upgrade in the category, so upgrades not bought show as dimmed zero rows.

## UI

- New tab **Runs** in `Sidebar.tsx` under the fleet section, at `web/ui/app/fleet/reroll/runs/page.tsx`. The existing `/api/fleet/reroll/runs` is about reroll campaigns and is not touched.
- The page fetches `/api/runs?limit=200` for each current reroll member through `workerRead`, then merges and sorts by `ended_at` descending.
- Header filters: emulator chips and a "Records only" toggle.
- **Killed-by strip**: a stacked bar with counts and shares over the loaded rows. It excludes abandoned and unreadable runs and states how many were excluded.
- **Table** columns: Run, Emulator, Ended, Tier, Wave (+HS badge), Coins (+HS badge), Duration, Killed by, Buys. The badge tooltip names the run it beat and the run that broke it.
- **Side panel** (row click; close button): wave with "best so far", coins split earned + ad, duration with seconds per wave, and killed by. Then Attack, Defense and Utility sections in the Workshop cube card style: each card shows `levels/max_level`, a bar (min 2% when non-zero), and `+N this run · $spent`.
- A worker that fails to load shows an inline error for that emulator. The other workers still render.
- Scrollbars are hidden on the table and the panel (`scrollbar-width: none` plus the WebKit rule). Scrolling still works.

## Testing

Run only the files touched:

- `tests/test_db.py`: migration adds the columns and the table; the backfill; `finish_run` writes `run_upgrades` idempotently; `list_runs` record flags (first run, tie is not a record, broken vs standing, per tier, abandoned excluded, records computed beyond `limit`).
- `tests/test_store_sink.py` and `tests/test_runs.py`: `killed_by` and `ad_coins` flow from `RunEnded` to the row.
- The game-over capture: a test using the existing `tests/fixtures/ocr/game_over*.json` captures, including an unreadable one → NULL.
- `tests/test_web_api.py`: the new `/api/runs` fields and `/api/runs/{id}/upgrades`, including the no-record case.
- Vitest for the page, the badge rendering (standing vs broken), the panel, and the per-worker error.

## Out of scope

- Reading Settings → Stats → advanced stats.
- Fleet-wide records.
- Counting free-upgrade levels.
- Killed-by trends over time.
