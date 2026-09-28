# Fleet State Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One live `/fleet/state` page that shows every emulator account side by side (bot activity, live battle, balances, buy queue, Workshop, Cards, Labs, in-run upgrades and the last five runs), backed by a new read-only `GET /api/fleet/state` endpoint.

**Architecture:** The bot records three more things it already knows: the autopilot's latest decision, its current activity ("Now") and the HUD wave. They go into `BotState.snapshot()`, so each worker's `/api/status` carries them. A new `fleet/state_records.py` reads one worker DB read-only with bounded SQL. A new `fleet/state_view.py` holds one pure builder per section, plus `fleet_state()`, which fetches every worker concurrently and isolates errors per worker and per section. `FleetSetupService.state_snapshot()` feeds the route. The Next.js page polls every 2 s and re-renders a column only when that account's scan changes.

**Tech Stack:** Python 3 / SQLite (`db.py`, read-only `mode=ro` URIs), FastAPI (`web/app.py`), `concurrent.futures.ThreadPoolExecutor`, Next.js 15 App Router static export + React 19 + Tailwind v4 (`web/ui`), `next/font/google` (Chakra Petch), pytest, vitest + Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-28-fleet-state-dashboard-design.md`
**Approved mockup:** https://claude.ai/artifact/GK3DA2zT6wtAYK1nVQ7t9R (the markup and CSS in Tasks 7 and 8 are ported from it onto the Telemetry tokens)

## Deviations from spec

Each one is the least invasive change that stays faithful to what the spec wants.

1. **`decision.cost` is always `null`.** `events.AutopilotDecided` carries only `phase`, `reason` and `upgrade_id`. `name` and `category` come from `upgrades.CATALOG`. Adding a price to the event would mean changing autopilot code, and the spec forbids changing bot behaviour. The page shows "price unknown".
2. **"Now" labels.** `ShoppingStarted` does not say which page it shops (Workshop or Cards), so the label is `Shopping` (or `Shopping · dry run`), not `Shopping · Workshop`. `Navigated` gives `Navigating · RETRY` and `ClaimStarted` gives `Claiming · mail`.
3. **Wave source.** `combat["wave"]` is read only inside the autopilot branch of `TowerBot._run_once` (through `frame_combat(...)`), not on every scan. `ScanCompleted.wave` carries the last wave the autopilot read in this run, and only on IN_RUN scans. With the autopilot off, `wave` stays `null`.
4. **Current-run upgrades are read from the DB, not over HTTP.** The worker route `/api/runs/{run_id}/upgrades` reads `run_upgrades`, which `finish_run` writes only when a run ends, so it is always empty for a live run. The plan reads the live run's `BattlePurchased` events with `db.run_purchases()` and the last finished run with `db.run_upgrade_levels()`, over the same read-only connection.
5. **Cards will mostly render empty.** `cards.facts()` has no production caller, and `cards.parse_frame()` never observes a card row today. Task 2 makes `facts()` emit `cards.<id>.level` and `cards.<id>.copies` for observed cards, and the builder reads them. Until something persists them, the Cards table says "Nothing read yet". Gems invested and recent card buys come from the ledger and are real.
6. **`--live` already exists** in `globals.css`. It is reused, not redefined.
7. **BotState tests go in the existing `tests/test_state.py`.** There is no `tests/test_bot_state.py`.
8. **Account `name` is the worker name** (for example `Air_38`). The pool has no display names. `serial` is the pool member's `endpoint`.
9. **Balances fall back to the ledger.** `currencies.currency_overview()` returns values only for the current live scope, observed within the last 120 s. When it returns `null`, the builder uses the ledger's last observed `balance_after` (`db.last_balances`). That is a real reading, never a guess.
10. **Workshop `recent[].level` is `null`.** The ledger does not record which level a purchase bought.
11. **`/fleet/state` gets the fleet rail and shell.** `isRerollPath()` does not match it. A new `isFleetWorkspacePath()` is used by the Sidebar, `AccountShell` and the SSE provider, so the page shows the fleet navigation, no game-account picker and no single-bot event stream. `isRerollPath()` and `fleetRedirect` are unchanged.
12. **Offline `stale_seconds`** is `now - MAX(events.ts)` from the worker DB, or `null` when the DB has no events.
13. **A lab job past its `completes_at` is not listed as running.** The frontend `Countdown` also shows "done" for a job that finishes between two polls.
14. **The stylesheet is imported by `app/fleet/state/layout.tsx`, not by `page.tsx`.** vitest does not run the PostCSS pipeline, so importing CSS from the page breaks the page test.

## Global Constraints

Copied from the spec; every task's requirements include these.

- Stones: "A placeholder showing "—" with a "not tracked yet" hint. Reading stones needs a new vision scanner, which is a separate task."
- Unknown prices: "Shown as "price unknown". There is no new price-observation scanning."
- "A null value renders as "—" or "price unknown", never as a made-up number."
- "The page polls `GET /api/fleet/state` every 2 s. A column re-renders only when that account's `scan` counter changes."
- "For each worker it opens the SQLite DB **read-only** and fetches `/api/status` with the existing 0.2 s timeout. Workers are fetched concurrently (a thread pool), so one slow worker doesn't delay the rest."
- "Wave and decision are small event and state additions. They must not change bot behaviour, only what the bot records."
- "Next navigation: Shows the bot's **current activity** (last navigation, shopping step or claim), labelled "Now". It is not a planned future target."
- Route: "`/fleet/state`, a new fleet page with a sidebar entry."
- "Layout: a grid with `minmax(min(100%, 340px), 1fr)`. With 1–2 accounts selected, the inner sections flow into CSS columns and Cards and Labs open by default. With 3 or more, those sections start collapsed. Open/closed state is remembered per section."
- "The selection is kept in `localStorage` inside try/catch."
- Tokens: "`--cat-attack` (red), `--cat-defense` (blue), `--cat-utility` (gold), `--cat-cards`, `--cat-labs`, `--live`."
- "It uses Chakra Petch for display labels and big numbers, and the existing Inter and JetBrains Mono. Numbers are tabular and use K/M/B/T formatting; `workshopFormat.ts` is reused or extended."
- "Animations respect `prefers-reduced-motion`."
- "Worker status request fails or times out: build the account from its DB only, set `online: false`, and show a "stale · Ns" badge. Battle is null."
- "Worker DB is missing or locked: the account appears with an `error` string and an offline card. Other accounts are unaffected."
- "A builder throws: that section is `null` with the error logged, and the rest of the account still renders."
- "The whole request fails on the frontend: keep the last payload and show a "connection lost" pill in the bar."
- "Only the new tests are run locally."
- Work only in the worktree `/Users/shahar/BifrostProjects/thetowerbot-fleet-state` (branch `feat/fleet-state`). Never check out, restore or edit files in the shared checkout `/Users/shahar/BifrostProjects/thetowerbot`; other editors use it.
- Python tests run from the worktree root with `../thetowerbot/.venv/bin/pytest <file> -q -p no:allure_pytest`. Run **only** the files named in the step, never `tests/` as a whole. Two tests in `tests/test_bot_reporting.py` (`test_log_sink_is_closed_even_when_the_scan_raises`, `test_the_sink_is_closed_when_startup_fails_after_it_started`) already fail at the merge base. They are not in this plan's run lists.
- Frontend: run `npm ci` once in `web/ui` before the first vitest step. Then use `npx vitest run <paths>` from `web/ui`.
- Commit after each task. Never push. Every commit message ends with these two lines:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01QQCpP6UkpvfBfCfHxKaoqD
  ```

## Review Focus

These are the five inputs most likely to hurt a real user of the page. The spec does not mention them, but each one now has a test in the task that owns the code.

1. **A worker DB locked in the middle of a write.** The read must fail within about 0.1 s, not hang for SQLite's 5 s default. That account shows `Worker database unavailable (database is locked)`, and every other account renders normally. Pinned in Task 5 (`test_a_database_locked_mid_write_fails_fast`, `test_a_locked_database_errors_one_account_and_leaves_the_others`).
2. **A worker whose `/api/status` answers but whose DB file is missing.** The account stays `online`, with its screen, scan and "Now". `error` is `Worker database is missing`, and the DB sections are `null`. Pinned in Task 5 (`test_a_reachable_worker_whose_database_is_missing_keeps_its_live_status`).
3. **A brand-new account.** It has no revision, or a revision without `workshop_stats`, and zero runs. Every skill must read `unseen`, with totals 0 and invested/next `null`. `tier` and `best_wave` are `null`, `runs` is `[]`, and the page says "No runs yet" rather than inventing a wave. Pinned in Task 3 (`test_a_new_account_or_a_revision_without_workshop_stats_reads_as_unseen`), Task 4 (`test_a_new_account_with_zero_runs_has_no_tier_no_best_and_no_upgrades`, `test_labs_on_a_new_account_point_at_game_speed_level_one`), Task 5 (`test_a_new_account_has_no_runs_no_revision_and_zero_gems_spent`) and Task 7 (`a new account with zero runs says so instead of inventing a wave`).
4. **A lab job already past `completes_at`.** The revision still holds a job that the game has already finished. It must not be listed as running with a negative timer, and a job that finishes between two polls reads "done". Pinned in Task 4 (`test_a_lab_job_past_its_completion_time_is_not_running`) and Task 7 (`a lab job that finishes between polls reads done, not a negative timer`).
5. **A huge number of ledger rows.** Sums and recent lists must be computed and bounded in SQL (30 recent rows), never loaded whole into Python. Dry-run and unconfirmed rows must not count. Pinned in Task 5 (`test_a_huge_ledger_is_summed_and_bounded_by_sql`, 50,000 rows).

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `events.py` | modify | `ScanCompleted.wave: int \| None = None` |
| `sinks/state.py` | modify | `BotState.wave`, `.decision`, `.activity`, set in `apply()`, cleared by `reset()` and run boundaries, returned by `snapshot()` |
| `tower_bot.py` | modify | Remember the autopilot's last wave read (`_scan_wave`) and pass it on IN_RUN `ScanCompleted` via `_reported_wave()` |
| `cards.py` | modify | `card_level_key()`, `card_copies_key()`; `facts()` also emits per-card level and copies |
| `fleet/state_view.py` | create | Pure section builders, `read_status()`, `build_account()`, `fleet_state()` |
| `fleet/state_records.py` | create | `WorkerRecords` and `read_records()`: one bounded, read-only read of a worker DB |
| `fleet/setup.py` | modify | `FleetSetupService.state_snapshot()` |
| `web/app.py` | modify | `GET /api/fleet/state` |
| `tests/test_state.py` | modify | BotState decision, activity and wave |
| `tests/test_autopilot_loop.py` | modify | `ScanCompleted.wave` from the scan loop |
| `tests/test_cards.py` | modify | Per-card facts |
| `tests/test_fleet_state_view.py` | create | Builders, aggregator and route |
| `tests/test_fleet_state_records.py` | create | Read-only DB reads |
| `web/ui/lib/fleetState.ts` (+ `.test.ts`) | create | Payload types, `accountKey()`, `reuseUnchanged()` |
| `web/ui/lib/api.ts` | modify | `fetchFleetState()` |
| `web/ui/app/globals.css` | modify | `--cat-*` tokens |
| `web/ui/app/layout.tsx` | modify | Chakra Petch as `--font-chakra-petch` |
| `web/ui/app/fleet/state/stateFormat.ts` (+ test) | create | `amount`, `priceText`, `whole`, `span`, `secondsUntil`, `agoText`, colors, accents |
| `web/ui/app/fleet/state/selection.ts` (+ test) | create | Chip selection, section open state, storage in try/catch |
| `web/ui/app/fleet/state/useFleetState.ts` (+ test) | create | 2 s poll, keep last payload, reuse unchanged accounts |
| `web/ui/app/fleet/state/fixtures.ts` | create | `makeAccount()`, `makePayload()` for tests |
| `web/ui/app/fleet/state/Tickers.tsx` | create | `Ago`, `Countdown` (tick themselves, not the column) |
| `web/ui/app/fleet/state/CategoryLedger.tsx` | create | Shared level / invested / next table, NEXT row, next unlock, recent list |
| `web/ui/app/fleet/state/RunUpgradesChart.tsx` | create | Total, category split bar, top 8 bars |
| `web/ui/app/fleet/state/AccountColumn.tsx` (+ test) | create | One emulator's column, memoized |
| `web/ui/app/fleet/state/fleet-state.css` | create | Page styles ported from the mockup |
| `web/ui/app/fleet/state/layout.tsx` | create | Imports the stylesheet |
| `web/ui/app/fleet/state/FleetStateBar.tsx` | create | Sticky bar: chips, All / Only live, heartbeat, live count, fleet coins, connection pill |
| `web/ui/app/fleet/state/page.tsx` (+ test) | create | Page shell, filtering, section state |
| `web/ui/lib/workspace.ts` (+ test) | modify | `isFleetWorkspacePath()` |
| `web/ui/components/Sidebar.tsx` (+ test) | modify | "Fleet State" entry; fleet rail on `/fleet/state` |
| `web/ui/components/AccountShell.tsx` (+ test) | modify | Fleet shell on `/fleet/state` |
| `web/ui/lib/useEventStream.tsx` | modify | No single-bot SSE on `/fleet/state` |

---

### Task 1: BotState records the decision, the current activity and the wave

**Files:**
- Modify: `events.py` (`ScanCompleted`, ~:43-47)
- Modify: `sinks/state.py` (`__init__`, `reset`, `apply`, `snapshot`)
- Modify: `tower_bot.py` (`__init__` near `_last_wave_progress` ~:243; the autopilot branch that computes `wave_number` ~:2404; the gem-path `ScanCompleted` ~:2368; the final `ScanCompleted` ~:2639)
- Test: `tests/test_state.py`, `tests/test_autopilot_loop.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `events.ScanCompleted(..., wave: int | None = None)`
  - `BotState.snapshot()` gains `"wave": int | None`, `"decision": {"phase": str, "reason": str, "upgrade_id": str | None, "at": float} | None` and `"activity": {"label": str, "at": float} | None`. Each worker's `/api/status` returns `state.snapshot()`, so it carries all three. Task 4's `build_bot`, `build_decision` and `build_battle` read these keys.
  - `TowerBot._scan_wave: int | None` and `TowerBot._reported_wave(state: screens.ScreenState) -> int | None`

- [ ] **Step 1: Write the failing tests**

In `tests/test_state.py`, change the imports at the top to:

```python
from __future__ import annotations

import pytest

import events
from sinks.state import BotState
```

and append:

```python
def test_an_autopilot_decision_is_kept_until_the_run_ends() -> None:
    state = BotState()
    state.apply(stamped(events.RunStarted(run_id=3)))
    state.apply(stamped(events.AutopilotDecided(phase="buying", reason="cheapest attack",
                                                upgrade_id="damage")))

    decision = state.snapshot()["decision"]
    assert (decision["phase"], decision["reason"], decision["upgrade_id"]) == (
        "buying", "cheapest attack", "damage")
    assert decision["at"] > 0

    state.apply(stamped(events.RunEnded(run_id=3, duration=10.0)))
    assert state.snapshot()["decision"] is None


@pytest.mark.parametrize(("event", "label"), [
    (events.Navigated(target="RETRY"), "Navigating · RETRY"),
    (events.ShoppingStarted(visit=2, dry_run=False), "Shopping"),
    (events.ShoppingStarted(visit=3, dry_run=True), "Shopping · dry run"),
    (events.ClaimStarted(target="mail"), "Claiming · mail"),
])
def test_navigation_shopping_and_claims_set_the_current_activity(
        event: events.Event, label: str) -> None:
    state = BotState()
    published = stamped(event)

    state.apply(published)

    assert state.snapshot()["activity"] == {"label": label, "at": published.ts}
    # These events always wrote a feed line; recording them must not stop that.
    assert len(state.snapshot()["tail"]) == 1


def test_the_wave_follows_in_run_scans_and_clears_off_the_run() -> None:
    state = BotState()
    assert state.snapshot()["wave"] is None

    state.apply(stamped(events.ScanCompleted(screen="IN_RUN", duration_ms=9.0, wave=4812)))
    assert state.snapshot()["wave"] == 4812
    # An IN_RUN scan that read no wave keeps the last one.
    state.apply(stamped(events.ScanCompleted(screen="IN_RUN", duration_ms=9.0)))
    assert state.snapshot()["wave"] == 4812

    state.apply(stamped(events.ScanCompleted(screen="GAME_OVER", duration_ms=9.0)))
    assert state.snapshot()["wave"] is None


def test_reset_forgets_the_decision_the_activity_and_the_wave() -> None:
    state = BotState()
    state.apply(stamped(events.ScanCompleted(screen="IN_RUN", duration_ms=9.0, wave=12)))
    state.apply(stamped(events.AutopilotDecided(phase="saving", reason="x")))
    state.apply(stamped(events.ClaimStarted(target="missions")))

    state.reset()

    snapshot = state.snapshot()
    assert (snapshot["wave"], snapshot["decision"], snapshot["activity"]) == (None, None, None)
```

In `tests/test_autopilot_loop.py`, insert these two tests just above `def test_the_overlay_shows_what_the_autopilot_read(` (the file already imports `pytest`, `Callable`, `Shopping` and `TowerBot`):

```python
def test_an_in_run_scan_reports_the_wave_the_autopilot_read(
    bot_in_run_on: Callable[[str], TowerBot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import events
    import tower_bot

    bot = bot_in_run_on("in_run_lit")
    bot.controls.apply({"autopilot": {"enabled": True}})
    monkeypatch.setattr(bot.autopilot, "step", lambda *args, **kwargs: False)
    monkeypatch.setattr(tower_bot, "frame_combat", lambda context, observation: {"wave": 4812})

    bot.run_once()

    scans = [e for e in bot.bus.published if isinstance(e, events.ScanCompleted)]
    assert scans[-1].wave == 4812


def test_a_scan_off_the_run_reports_no_wave(
    bot_on_main_menu: Callable[..., TowerBot],
) -> None:
    import events

    bot = bot_on_main_menu(Shopping())
    bot._scan_wave = 4812  # left over from the run that just ended

    bot.run_once()

    scans = [e for e in bot.bus.published if isinstance(e, events.ScanCompleted)]
    assert scans[-1].wave is None
    assert bot._scan_wave is None
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `../thetowerbot/.venv/bin/pytest tests/test_state.py tests/test_autopilot_loop.py -q -p no:allure_pytest -k "decision or activity or wave"`
Expected: FAIL. You should see `KeyError: 'decision'` / `KeyError: 'wave'` / `KeyError: 'activity'`, `TypeError: ScanCompleted.__init__() got an unexpected keyword argument 'wave'` and `AttributeError: 'ScanCompleted' object has no attribute 'wave'`.

- [ ] **Step 3: Add the event field**

In `events.py`, replace the `ScanCompleted` body with:

```python
@dataclass(frozen=True, kw_only=True)
class ScanCompleted(Event):
    screen: str
    duration_ms: float
    wallet: int | None = None
    # The HUD wave the autopilot last read in this run. None off a run, or
    # when no wave has been read yet: an unread wave is not wave 0.
    wave: int | None = None
```

`sinks/store.py` does not store `ScanCompleted`, and the SSE and log sinks render it generically, so no other file needs changes.

- [ ] **Step 4: Record the three values in `BotState`**

In `sinks/state.py`:

1. In `__init__`, after `self.recovery: dict[str, Any] | None = None`, add:

```python
        self.wave: int | None = None
        self.decision: dict[str, Any] | None = None
        self.activity: dict[str, Any] | None = None
```

2. In `reset()`, after `self.recovery = None`, add:

```python
            self.wave = None
            self.decision = None
            self.activity = None
```

3. In `apply()`, at the end of the `case events.ScanCompleted():` branch (after `self.last_error = None`), add:

```python
                    # Kept across IN_RUN scans that did not read the HUD, so
                    # the wave does not flicker to "unknown" between reads.
                    if event.screen != "IN_RUN":
                        self.wave = None
                    elif event.wave is not None:
                        self.wave = event.wave
```

4. In the `case events.RunStarted():` branch, after `self.run_taps = Counter()`, and in the `case events.RunEnded():` branch, after `self.run_started = None`, add:

```python
                    self.wave = None
                    self.decision = None
```

5. Directly above the final `case _:` add four cases. Each one still appends the feed line that the `case _:` branch used to write, so the tail is unchanged:

```python
                case events.AutopilotDecided():
                    self.decision = {"phase": event.phase, "reason": event.reason,
                                     "upgrade_id": event.upgrade_id, "at": event.ts}
                    self.tail.append(render(event))
                case events.Navigated():
                    self.activity = {"label": f"Navigating · {event.target}", "at": event.ts}
                    self.tail.append(render(event))
                case events.ShoppingStarted():
                    label = "Shopping · dry run" if event.dry_run else "Shopping"
                    self.activity = {"label": label, "at": event.ts}
                    self.tail.append(render(event))
                case events.ClaimStarted():
                    self.activity = {"label": f"Claiming · {event.target}", "at": event.ts}
                    self.tail.append(render(event))
```

6. In `snapshot()`, between the `"recovery": ...` line and `"tail": list(self.tail),`, add:

```python
                "wave": self.wave,
                "decision": dict(self.decision) if self.decision is not None else None,
                "activity": dict(self.activity) if self.activity is not None else None,
```

- [ ] **Step 5: Report the wave from the scan loop**

In `tower_bot.py`:

1. In `__init__`, directly after `self._last_wave_progress: tuple[int | None, int] | None = None`, add:

```python
        # The last HUD wave the autopilot read. Reported on IN_RUN scans only.
        self._scan_wave: int | None = None
```

2. In the autopilot branch, directly after the two lines that compute `wave_number = (int(wave_value) if isinstance(wave_value, (int, str)) and str(wave_value).isdigit() else None)`, add:

```python
                    if wave_number is not None:
                        self._scan_wave = wave_number
```

3. The gem-path publish (the `ScanCompleted` just before `# The strategy's rows, in the strategy's order`) becomes:

```python
                self.bus.publish(events.ScanCompleted(
                    screen=state.value, duration_ms=(time.monotonic() - started) * 1000,
                    wallet=self.wallet, wave=self._reported_wave(state),
                ))
                return True
```

4. The final publish at the end of `_run_once` becomes the following, with the new method added right after `_run_once` returns:

```python
        self.bus.publish(
            events.ScanCompleted(
                screen=state.value,
                duration_ms=(time.monotonic() - started) * 1000,
                wallet=self.wallet,
                wave=self._reported_wave(state),
            )
        )
        return clicked

    def _reported_wave(self, state: screens.ScreenState) -> int | None:
        """The wave a ScanCompleted may carry: the last one read, IN_RUN only.

        Leaving the run forgets it, so the next run never starts on the
        previous run's wave.
        """
        if state is not screens.ScreenState.IN_RUN:
            self._scan_wave = None
        return self._scan_wave
```

The other `ScanCompleted` publishers (lab visit, claim walks, shopping inspection) keep `wave=None`. `BotState` holds the last wave across those IN_RUN scans and clears it on any other screen. No decision the bot makes reads `_scan_wave`.

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `../thetowerbot/.venv/bin/pytest tests/test_state.py tests/test_autopilot_loop.py tests/test_tui_sink.py -q -p no:allure_pytest`
Expected: PASS (all tests in the three files).

- [ ] **Step 7: Commit**

```bash
git add events.py sinks/state.py tower_bot.py tests/test_state.py tests/test_autopilot_loop.py
git commit -F- <<'EOF'
Record the autopilot decision, current activity and wave in BotState

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QQCpP6UkpvfBfCfHxKaoqD
EOF
```

---

### Task 2: `cards.facts()` persists per-card level and copies

**Files:**
- Modify: `cards.py` (imports; after `SLOT_CAPACITY_KEY`; `facts()`)
- Test: `tests/test_cards.py`

**Interfaces:**
- Consumes: `cards.CardObservation(concept_id, domain, effect_kind, status, copies, level, reason)`, `account_state.Fact`, `account_state.Evidence`.
- Produces:
  - `cards.card_level_key(concept_id: str) -> str` returns `"cards.damage.level"`
  - `cards.card_copies_key(concept_id: str) -> str` returns `"cards.damage.copies"`
  - `cards.facts(reading)` returns the two slot facts followed by one `Fact` per observed, non-None level or copies value. Task 4's `build_cards` reads these keys from `revision["cards"]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cards.py`)

```python
def test_observed_cards_persist_their_level_and_copies_and_nothing_else() -> None:
    import dataclasses
    import cards
    page = reading()
    observed = dataclasses.replace(page, cards=(
        cards.CardObservation('cards.damage', 'cards', 'unknown', 'observed', 12, 5, 'read'),
        cards.CardObservation('cards.health', 'cards', 'unknown', 'observed', None, 2, 'copies unread'),
        cards.CardObservation('cards.range', 'cards', 'unknown', 'locked', None, None, 'padlock'),
    ))

    values = {f.concept_id: f.value for f in cards.facts(observed)}

    assert values == {cards.SLOT_EQUIPPED_KEY: 0, cards.SLOT_CAPACITY_KEY: 1,
                      'cards.damage.level': 5, 'cards.damage.copies': 12,
                      'cards.health.level': 2}
    level = next(f for f in cards.facts(observed) if f.concept_id == 'cards.damage.level')
    assert (level.status, level.evidence.raw_name, level.evidence.raw_value) == (
        'observed', 'cards.damage', '5')


def test_card_fact_keys_can_never_be_mistaken_for_a_catalog_identity() -> None:
    import cards
    from concepts import REGISTRY
    ids = {c.concept_id for c in REGISTRY.concepts}
    for key in (cards.card_level_key('cards.damage'), cards.card_copies_key('cards.damage')):
        assert key not in ids
        assert key.count('.') == 2
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `../thetowerbot/.venv/bin/pytest tests/test_cards.py -q -p no:allure_pytest -k "persist_their_level or card_fact_keys"`
Expected: FAIL. The first test's `values` has only the two slot keys, and the second fails with `AttributeError: module 'cards' has no attribute 'card_level_key'`.

- [ ] **Step 3: Implement**

In `cards.py`, change `from dataclasses import dataclass` to `from dataclasses import dataclass, replace`. Directly after `SLOT_CAPACITY_KEY = 'cards.slots.capacity'` add:

```python
def card_level_key(concept_id: str) -> str:
    """The fact key for one card's level: `cards.damage.level`.

    Three segments, like the slot keys, so it can never collide with a
    catalog identity (every one of those has exactly two).
    """
    return f'{concept_id}.level'


def card_copies_key(concept_id: str) -> str:
    """The fact key for how many copies of one card the account holds."""
    return f'{concept_id}.copies'

# Catalog kinds that would separate a passive bonus from an active ability.
# Registry v1 has neither - all 31 cards share the single kind `card` - so
# effect_kind() answers 'unknown' for every identity today, and will answer
# for real the moment the catalog carries the split. Classifying cards here
# instead would attach a rule to a name that no source ever verified.
_CATALOG_EFFECT_KINDS = {'passive-card': 'passive', 'active-card': 'active'}

_MIN_CONFIDENCE = .90
# The measured band of the "Unlock New Slot" tile's own label, at
# (589, 644, 183, 35) on the recorded page.
_NEXT_SLOT_Y = (624, 664)
_NEXT_SLOT_LABEL = 'unlocknew'
```

Replace the whole `facts()` function with:

```python
def facts(reading: CardsReading | None) -> tuple[Fact, ...]:
    """The permanent account facts this page supports.

    The two slot counts, plus a level and a copies fact for every card this
    frame actually observed. An identity with no observation behind it must
    reach the account revision as an absence, and the way to record an
    absence is to write nothing for it - so a card that is unknown, locked,
    unavailable, maxed or unreadable, or whose number is None, writes nothing.
    """
    if reading is None or reading.slots.status != 'observed' or reading.slots.rect is None:
        return ()
    evidence = Evidence(reading.observed_at, reading.slots.confidence, 'ACTIVE',
                        reading.slots.raw_value, reading.slots.rect, reading.frame_width,
                        reading.frame_height, reading.frame_digest)
    card_facts: list[Fact] = []
    for card in reading.cards:
        if card.status != 'observed':
            continue
        for key, value in ((card_level_key(card.concept_id), card.level),
                           (card_copies_key(card.concept_id), card.copies)):
            if value is not None:
                card_facts.append(Fact(key, value, 'observed',
                                       replace(evidence, raw_name=card.concept_id,
                                               raw_value=str(value))))
    return (Fact(SLOT_EQUIPPED_KEY, reading.slots.equipped, 'observed', evidence),
            Fact(SLOT_CAPACITY_KEY, reading.slots.capacity, 'observed', evidence),
            *card_facts)
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `../thetowerbot/.venv/bin/pytest tests/test_cards.py -q -p no:allure_pytest`
Expected: PASS. `test_card_state_survives_a_fresh_repository_over_the_same_file` still passes, because the recorded page observes no card.

- [ ] **Step 5: Commit**

```bash
git add cards.py tests/test_cards.py
git commit -F- <<'EOF'
Persist each observed card's level and copies as account facts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QQCpP6UkpvfBfCfHxKaoqD
EOF
```

---

### Task 3: Workshop builders in `fleet/state_view.py`

**Files:**
- Create: `fleet/state_view.py`
- Create: `tests/test_fleet_state_view.py`

**Interfaces:**
- Consumes: `workshop_levels.workshop_state(workshop_stats: list[dict] | None) -> list[dict]` (rows with `id, name, category` in upper case, `status`, `level_min`, `level_max`, `max_level` and `next_coins`), `workshop_levels.ladders() -> dict[str, Ladder]`, `upgrades.CATALOG`, `upgrades.by_id()`, `upgrades.resolve(name, category)`, `Upgrade.concept_id` (`"unlocks.multishot"` for `unlock_multishot`), `fleet.workshop_prices.catalog_price(upgrade_id, level) -> int | None`.
- Produces (all in `fleet.state_view`):
  - `CATEGORIES = ("attack", "defense", "utility")`, `RECENT_LIMIT = 30`
  - `iso(ts: float | None) -> str | None`
  - `category_totals(rows: Iterable[Mapping]) -> dict[str, int]`
  - `invested_coins(upgrade_id: str, level: int | None) -> int | None`
  - `bot_spent(rows: Iterable[tuple[item, category, coins]]) -> dict[str, int]`
  - `owned_unlocks(revision: Mapping | None, rows_by_id: Mapping[str, Mapping], bought: set[str]) -> set[str]`
  - `next_unlock(category: str, owned: set[str]) -> {"id", "name", "cost": int | None} | None`
  - `workshop_recent(rows: Iterable[Mapping], limit: int = RECENT_LIMIT) -> list[dict]`. Input rows have `ts, item, category, price`.
  - `build_workshop(revision: Mapping | None, spent_rows: Iterable[tuple], recent_rows: Iterable[Mapping]) -> {"totals": {cat: int}, "categories": {cat: {"unlocked", "total", "skills": [{"id", "name", "level", "invested", "bot_spent", "next_cost", "status", "locked"}], "next_unlock"}}, "recent": [{"ts", "id", "name", "category", "level", "price"}]}`
  - Private helpers used by Task 4: `_category(value) -> str | None`, `_resolve(item, category) -> upgrades.Upgrade | None`

- [ ] **Step 1: Write the failing tests** (create `tests/test_fleet_state_view.py`)

```python
"""The Fleet State page's builders: plain inputs in, JSON-safe sections out."""

from __future__ import annotations

from typing import Any

import workshop_levels
from fleet import state_view


def _fact(upgrade_id: str, raw: str, value: float) -> dict[str, Any]:
    return {"concept_id": f"stats.{upgrade_id}", "value": value, "status": "verified",
            "evidence": {"observed_at": 100., "raw_value": raw}}


def _skill(workshop: dict[str, Any], category: str, upgrade_id: str) -> dict[str, Any]:
    return next(s for s in workshop["categories"][category]["skills"] if s["id"] == upgrade_id)


# Health "154" is level 14, Attack Speed "1.55" level 11, Range "30.50m" level 1,
# Critical Factor "x1.20" level 0 - pinned in tests/test_workshop_levels.py.
STATS = [_fact("health", "154", 154.), _fact("attack_speed", "1.55", 1.55),
         _fact("range", "30.50m", 30.5), _fact("critical_factor", "x1.20", 1.2)]


def test_category_totals_sum_known_levels_and_skip_unseen_and_unmatched() -> None:
    rows = workshop_levels.workshop_state(STATS)
    assert state_view.category_totals(rows) == {"attack": 12, "defense": 14, "utility": 0}
    unmatched = workshop_levels.workshop_state([_fact("attack_speed", "1.57", 1.57),
                                                _fact("health", "154", 154.)])
    assert state_view.category_totals(unmatched) == {"attack": 0, "defense": 14, "utility": 0}


def test_invested_is_the_sum_of_the_ladder_rungs_below_the_level() -> None:
    ladder = workshop_levels.ladders()["health"]
    assert state_view.invested_coins("health", 14) == sum(ladder.next_coins[:14])
    assert state_view.invested_coins("health", 0) == 0
    assert state_view.invested_coins("health", None) is None
    assert state_view.invested_coins("unlock_multishot", 3) is None  # no ladder
    # A maxed read never reaches the ladder's final None rung.
    assert state_view.invested_coins("health", ladder.max_level + 5) == sum(
        ladder.next_coins[:ladder.max_level])


def test_bot_spent_resolves_display_names_and_ids_and_sums_per_upgrade() -> None:
    spent = state_view.bot_spent([("Damage", "ATTACK", 300), ("damage", None, 50),
                                  ("Health", "DEFENSE", None), ("Not a row", "ATTACK", 9)])
    assert spent == {"damage": 350}


def test_the_workshop_section_carries_levels_invested_bot_spent_and_next_cost() -> None:
    workshop = state_view.build_workshop(
        {"workshop_stats": STATS}, [("Health", "DEFENSE", 1200)],
        [{"ts": 1700000000., "item": "Health", "category": "DEFENSE", "price": 1100}])

    assert workshop["totals"] == {"attack": 12, "defense": 14, "utility": 0}
    health = _skill(workshop, "defense", "health")
    assert (health["level"], health["bot_spent"], health["next_cost"], health["status"]) == (
        14, 1200, 1233, "exact")
    assert health["invested"] == sum(workshop_levels.ladders()["health"].next_coins[:14])
    assert workshop["recent"] == [{"ts": "2023-11-14T22:13:20+00:00", "id": "health",
                                   "name": "Health", "category": "defense", "level": None,
                                   "price": 1100}]


def test_next_unlock_is_the_first_unowned_tile_with_its_catalog_price_or_none() -> None:
    workshop = state_view.build_workshop({"workshop_stats": STATS}, [], [])
    # Range was read, so Unlock Range Upgrades is owned; Multishot has no catalog price.
    assert workshop["categories"]["attack"]["next_unlock"] == {
        "id": "unlock_multishot", "name": "Unlock Multishot", "cost": None}
    assert workshop["categories"]["defense"]["next_unlock"] == {
        "id": "unlock_defense_upgrades", "name": "Unlock Defense Upgrades", "cost": 75}
    owned = state_view.build_workshop(
        {"workshop_stats": STATS,
         "unlocks": [{"concept_id": "unlocks.defense_upgrades", "value": True}]}, [], [])
    assert owned["categories"]["defense"]["next_unlock"]["id"] == "unlock_thorns"
    assert owned["categories"]["defense"]["next_unlock"]["cost"] == 500


def test_a_bought_unlock_counts_as_owned_and_unlocks_its_rows() -> None:
    workshop = state_view.build_workshop(None, [("Unlock Multishot", "ATTACK", 900)], [])
    attack = workshop["categories"]["attack"]
    assert _skill(workshop, "attack", "multishot_chance")["locked"] is False
    assert _skill(workshop, "attack", "range")["locked"] is True
    assert attack["unlocked"] == attack["total"] - sum(s["locked"] for s in attack["skills"])


def test_a_new_account_or_a_revision_without_workshop_stats_reads_as_unseen() -> None:
    """Review Focus: a brand-new account has no revision, or one with no Workshop reads."""
    for revision in (None, {}, {"lab_levels": []}):
        workshop = state_view.build_workshop(revision, [], [])
        assert workshop["totals"] == {"attack": 0, "defense": 0, "utility": 0}
        damage = _skill(workshop, "attack", "damage")
        assert (damage["level"], damage["invested"], damage["next_cost"], damage["status"]) == (
            None, None, None, "unseen")
        assert workshop["categories"]["attack"]["next_unlock"]["id"] == "unlock_range_upgrades"
        assert workshop["recent"] == []


def test_recent_workshop_buys_are_capped() -> None:
    rows = [{"ts": float(i), "item": "Damage", "category": "ATTACK", "price": i}
            for i in range(50)]
    assert len(state_view.workshop_recent(rows)) == state_view.RECENT_LIMIT == 30
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `../thetowerbot/.venv/bin/pytest tests/test_fleet_state_view.py -q -p no:allure_pytest`
Expected: FAIL at collection with `ImportError: cannot import name 'state_view' from 'fleet'`.

- [ ] **Step 3: Implement** (create `fleet/state_view.py`)

```python
"""The Fleet State page: one live column per emulator account.

Every `build_*` function takes plain values - a revision's JSON, ledger rows
SQL has already bounded or summed, a worker's /api/status dict - and returns
a JSON-safe dict, so each one is tested on literals without a live worker. A
value nobody observed is None, never 0 and never a guess.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import upgrades
import workshop_levels
from fleet import workshop_prices

CATEGORIES = ("attack", "defense", "utility")
RECENT_LIMIT = 30


def iso(ts: float | None) -> str | None:
    """Epoch seconds as an ISO-8601 UTC string, or None."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _category(value: object) -> str | None:
    lowered = value.lower() if isinstance(value, str) else ""
    return lowered if lowered in CATEGORIES else None


def _resolve(item: object, category: object) -> upgrades.Upgrade | None:
    """A ledger row's upgrade: its item is an id or the name the game showed."""
    if not isinstance(item, str) or not item:
        return None
    return upgrades.by_id(item) or upgrades.resolve(item, category if isinstance(category, str) else None)


def category_totals(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Sum of known levels per Workshop tab; unseen and unmatched rows add nothing."""
    totals = dict.fromkeys(CATEGORIES, 0)
    for row in rows:
        category = _category(row.get("category"))
        level = row.get("level_min")
        if category is None or row.get("status") in ("unseen", "unmatched") or level is None:
            continue
        totals[category] += int(level)
    return totals


def invested_coins(upgrade_id: str, level: int | None) -> int | None:
    """Coins the ladder charges to reach `level`: the sum of its first `level` rungs."""
    ladder = workshop_levels.ladders().get(upgrade_id)
    if ladder is None or level is None or level < 0:
        return None
    rungs = ladder.next_coins[:min(level, ladder.max_level)]
    if any(rung is None for rung in rungs):
        return None
    return sum(rungs)  # type: ignore[arg-type]


def bot_spent(rows: Iterable[tuple[Any, Any, Any]]) -> dict[str, int]:
    """Coins the bot provably spent per upgrade, from (item, category, coins) sums."""
    spent: dict[str, int] = {}
    for item, category, coins in rows:
        upgrade = _resolve(item, category)
        if upgrade is None or coins is None:
            continue
        spent[upgrade.id] = spent.get(upgrade.id, 0) + int(coins)
    return spent


def owned_unlocks(revision: Mapping[str, Any] | None, rows_by_id: Mapping[str, Mapping[str, Any]],
                  bought: set[str]) -> set[str]:
    """Unlock tiles this account owns.

    Owned when the revision records it, when the bot bought it, or when any
    row it grants has been read - a granted row is only drawn once unlocked.
    """
    facts = {fact.get("concept_id"): fact.get("value")
             for fact in (revision or {}).get("unlocks") or ()}
    owned: set[str] = set()
    for upgrade in upgrades.CATALOG:
        if not upgrade.unlock:
            continue
        if (facts.get(upgrade.concept_id) or upgrade.id in bought
                or any(rows_by_id.get(granted, {}).get("status", "unseen") != "unseen"
                       for granted in upgrade.unlocks)):
            owned.add(upgrade.id)
    return owned


def next_unlock(category: str, owned: set[str]) -> dict[str, Any] | None:
    """The first unowned unlock tile of a tab, in the game's order; None when all are owned."""
    for upgrade in upgrades.CATALOG:
        if upgrade.unlock and upgrade.category.lower() == category and upgrade.id not in owned:
            return {"id": upgrade.id, "name": upgrade.name,
                    "cost": workshop_prices.catalog_price(upgrade.id, 0)}
    return None


def workshop_recent(rows: Iterable[Mapping[str, Any]], limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
    """Newest-first Workshop buys. The ledger does not record the level bought."""
    recent: list[dict[str, Any]] = []
    for row in rows:
        if len(recent) >= limit:
            break
        upgrade = _resolve(row.get("item"), row.get("category"))
        recent.append({
            "ts": iso(row.get("ts")),
            "id": upgrade.id if upgrade else None,
            "name": upgrade.name if upgrade else str(row.get("item") or "?"),
            "category": _category(upgrade.category if upgrade else row.get("category")),
            "level": None,
            "price": row.get("price"),
        })
    return recent


def build_workshop(revision: Mapping[str, Any] | None,
                   spent_rows: Iterable[tuple[Any, Any, Any]],
                   recent_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The Workshop section: per-tab totals, skills, next unlock and recent buys."""
    spent_rows = list(spent_rows)
    rows = workshop_levels.workshop_state((revision or {}).get("workshop_stats"))
    rows_by_id = {row["id"]: row for row in rows}
    spent = bot_spent(spent_rows)
    bought = {upgrade.id for item, category, _ in spent_rows
              if (upgrade := _resolve(item, category)) is not None}
    owned = owned_unlocks(revision, rows_by_id, bought)
    gate = {granted: upgrade.id for upgrade in upgrades.CATALOG if upgrade.unlock
            for granted in upgrade.unlocks}
    categories: dict[str, Any] = {}
    for category in CATEGORIES:
        skills = []
        for row in rows:
            if row["category"].lower() != category:
                continue
            gated_by = gate.get(row["id"])
            skills.append({
                "id": row["id"], "name": row["name"], "level": row["level_min"],
                "invested": invested_coins(row["id"], row["level_min"]),
                "bot_spent": spent.get(row["id"], 0),
                "next_cost": row["next_coins"], "status": row["status"],
                "locked": gated_by is not None and gated_by not in owned,
            })
        categories[category] = {"unlocked": sum(not skill["locked"] for skill in skills),
                                "total": len(skills), "skills": skills,
                                "next_unlock": next_unlock(category, owned)}
    return {"totals": category_totals(rows), "categories": categories,
            "recent": workshop_recent(recent_rows)}
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `../thetowerbot/.venv/bin/pytest tests/test_fleet_state_view.py -q -p no:allure_pytest`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add fleet/state_view.py tests/test_fleet_state_view.py
git commit -F- <<'EOF'
Build the Fleet State Workshop section from revisions and the ledger

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QQCpP6UkpvfBfCfHxKaoqD
EOF
```

---

### Task 4: Builders for bot, decision, battle, balances, runs, run upgrades, cards and labs

**Files:**
- Modify: `fleet/state_view.py` (imports, then append)
- Test: `tests/test_fleet_state_view.py` (append)

**Interfaces:**
- Consumes: Task 1's status keys `screen`, `scans`, `wallet`, `wave`, `run: {"id", "elapsed"}`, `activity: {"label", "at"}` and `decision: {"phase", "reason", "upgrade_id", "at"}`. Task 2's `cards.SLOT_EQUIPPED_KEY`, `cards.SLOT_CAPACITY_KEY` and the per-card `cards.<id>.level` / `cards.<id>.copies` fact keys. Task 3's `_category`, `iso`, `CATEGORIES` and `RECENT_LIMIT`. `lab_catalog.level(lab_id, n) -> LabLevel | None` (`.coins`), `lab_catalog.lab(id)`, `lab_catalog.card_slot_gems(slot) -> int | None`, `lab_catalog.CATALOG.labs`, `account_state.completed_lab_level(status, value) -> int | None`, `concepts.REGISTRY.by_id(id)` (`.name`).
- Produces (all in `fleet.state_view`):
  - `build_bot(status: Mapping | None) -> {"screen": str | None, "now": str | None, "live": bool}`
  - `build_decision(raw: Mapping | None) -> {"phase", "reason", "upgrade_id", "category", "name", "cost": None} | None`
  - `build_battle(status: Mapping | None, tier: int | None, best_waves: Mapping[int, int]) -> {"tier", "wave", "cash", "elapsed_s", "best_wave"} | None`
  - `build_balances(overview: Mapping | None, ledger: Mapping | None) -> {"coins", "gems", "stones": None}`
  - `build_runs(rows: Iterable[Mapping], limit: int = 5) -> list[{"tier", "wave", "coins", "duration_s", "ended_at", "abandoned"}]`. Input rows have `tier, wave, coins, started_at, ended_at, abandoned`.
  - `build_run_upgrades(rows: Iterable[{"upgrade_id", "levels"}], scope: str | None) -> {"scope", "total", "by_category", "items": [{"id", "name", "category", "levels"}]} | None`
  - `build_cards(revision: Mapping | None, gems_invested: int | None, recent_rows: Iterable[{"ts", "item", "price"}]) -> {"slots": {"equipped", "capacity", "next_slot_gems"}, "items": [{"name", "level", "copies"}], "gems_invested", "recent": [{"ts", "name", "gems"}]}`
  - `build_labs(revision: Mapping | None, recent_rows: Iterable[{"ts", "item", "price"}], now: float) -> {"slots", "running": [{"id", "name", "to_level", "completes_at"}], "levels": [{"id", "name", "level", "next_cost"}], "next": {"id", "name", "cost"} | None, "recent": [{"ts", "name", "price"}]}`
  - `_whole(value) -> int | None`, `_number(value) -> float | int | None` (used by Task 5)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_fleet_state_view.py`)

```python
def test_bot_and_decision_come_from_the_worker_status() -> None:
    status = {"screen": "IN_RUN", "activity": {"label": "Navigating · RETRY", "at": 5.},
              "decision": {"phase": "buying", "reason": "cheapest", "upgrade_id": "damage", "at": 5.}}
    assert state_view.build_bot(status) == {"screen": "IN_RUN", "now": "Navigating · RETRY",
                                            "live": True}
    assert state_view.build_bot(None) == {"screen": None, "now": None, "live": False}
    assert state_view.build_decision(status["decision"]) == {
        "phase": "buying", "reason": "cheapest", "upgrade_id": "damage", "category": "attack",
        "name": "Damage", "cost": None}
    assert state_view.build_decision({"phase": "saving", "reason": "wait"})["name"] is None
    assert state_view.build_decision(None) is None


def test_the_battle_is_live_only_in_a_run_and_measured_against_its_tier_best() -> None:
    status = {"screen": "IN_RUN", "wave": 4812, "wallet": 8420000000, "run": {"elapsed": 5780.4}}
    assert state_view.build_battle(status, 11, {11: 5020, 7: 1420}) == {
        "tier": 11, "wave": 4812, "cash": 8420000000, "elapsed_s": 5780.4, "best_wave": 5020}
    assert state_view.build_battle({**status, "screen": "GAME_OVER"}, 11, {11: 5020}) is None
    assert state_view.build_battle(None, 11, {11: 5020}) is None


def test_a_new_account_with_zero_runs_has_no_tier_no_best_and_no_upgrades() -> None:
    """Review Focus: a brand-new account has not finished a single run."""
    assert state_view.build_runs([]) == []
    battle = state_view.build_battle({"screen": "IN_RUN", "wave": None, "wallet": None}, None, {})
    assert battle == {"tier": None, "wave": None, "cash": None, "elapsed_s": None,
                      "best_wave": None}
    assert state_view.build_run_upgrades([], None) is None


def test_runs_keep_the_newest_five_with_their_duration() -> None:
    rows = [{"tier": 11, "wave": 5020 - i, "coins": 310000000, "started_at": 1000. * i,
             "ended_at": 1000. * i + 6020, "abandoned": i == 1} for i in range(7)]
    runs = state_view.build_runs(rows)
    assert len(runs) == 5
    assert runs[0] == {"tier": 11, "wave": 5020, "coins": 310000000, "duration_s": 6020.0,
                       "ended_at": "1970-01-01T01:40:20+00:00", "abandoned": False}
    assert runs[1]["abandoned"] is True


def test_run_upgrades_total_split_by_category_and_sorted() -> None:
    upgrades_view = state_view.build_run_upgrades(
        [{"upgrade_id": "health", "levels": 4}, {"upgrade_id": "damage", "levels": 31},
         {"upgrade_id": "mystery", "levels": 2}, {"upgrade_id": "", "levels": 9}], "current")
    assert upgrades_view == {
        "scope": "current", "total": 37,
        "by_category": {"attack": 31, "defense": 4, "utility": 0},
        "items": [{"id": "damage", "name": "Damage", "category": "attack", "levels": 31},
                  {"id": "health", "name": "Health", "category": "defense", "levels": 4},
                  {"id": "mystery", "name": "mystery", "category": None, "levels": 2}]}
    assert state_view.build_run_upgrades([], "last") == {
        "scope": "last", "total": 0, "by_category": {"attack": 0, "defense": 0, "utility": 0},
        "items": []}


def test_balances_prefer_the_live_scope_and_never_invent_stones() -> None:
    live = {"coins_lower": 3840000000, "gems": 1842}
    assert state_view.build_balances(live, {"coins": 1, "gems": 2}) == {
        "coins": 3840000000, "gems": 1842, "stones": None}
    assert state_view.build_balances({"coins_lower": None, "gems": None},
                                     {"coins": 500, "gems": None}) == {
        "coins": 500, "gems": None, "stones": None}
    assert state_view.build_balances(None, None) == {"coins": None, "gems": None, "stones": None}


def test_cards_read_slots_per_card_facts_and_the_next_slot_price() -> None:
    revision = {"cards": [{"concept_id": "cards.slots.equipped", "value": 1},
                          {"concept_id": "cards.slots.capacity", "value": 1},
                          {"concept_id": "cards.damage.level", "value": 5},
                          {"concept_id": "cards.damage.copies", "value": 12}]}
    view = state_view.build_cards(revision, 70, [{"ts": 10., "item": "Damage", "price": 20}])
    assert view["slots"] == {"equipped": 1, "capacity": 1, "next_slot_gems": 50}
    assert view["items"] == [{"name": "Damage", "level": 5, "copies": 12}]
    assert view["gems_invested"] == 70
    assert view["recent"] == [{"ts": "1970-01-01T00:00:10+00:00", "name": "Damage", "gems": 20}]
    empty = state_view.build_cards(None, 0, [])
    assert empty["slots"] == {"equipped": None, "capacity": None, "next_slot_gems": None}
    assert empty["items"] == []


def test_labs_list_levels_running_jobs_and_the_cheapest_known_next() -> None:
    revision = {"lab_slots_owned": 2,
                "lab_levels": [{"concept_id": "labs.game-speed", "value": 3, "status": "owned"},
                               {"concept_id": "labs.damage", "value": 11, "status": "owned"}],
                "lab_jobs": [{"concept_id": "labs.damage", "value": 5000., "status": "researching"}]}
    labs = state_view.build_labs(revision, [{"ts": 10., "item": "labs.damage", "price": 4100000},
                                            {"ts": 9., "item": "Game Speed", "price": 12000}],
                                 now=1000.)
    assert labs["slots"] == 2
    assert labs["running"] == [{"id": "labs.damage", "name": "Damage", "to_level": 12,
                                "completes_at": "1970-01-01T01:23:20+00:00"}]
    assert labs["levels"] == [
        {"id": "labs.game-speed", "name": "Game Speed", "level": 3, "next_cost": 50000},
        {"id": "labs.damage", "name": "Damage", "level": 11, "next_cost": None}]
    assert labs["next"] == {"id": "labs.game-speed", "name": "Game Speed", "cost": 50000}
    assert [row["name"] for row in labs["recent"]] == ["Damage", "Game Speed"]


def test_a_lab_job_past_its_completion_time_is_not_running() -> None:
    """Review Focus: the revision still holds a job the game has already finished."""
    revision = {"lab_levels": [{"concept_id": "labs.damage", "value": 11, "status": "owned"}],
                "lab_jobs": [{"concept_id": "labs.damage", "value": 999., "status": "researching"}]}
    labs = state_view.build_labs(revision, [], now=1000.)
    assert labs["running"] == []
    assert labs["next"] == {"id": "labs.game-speed", "name": "Game Speed", "cost": 300}


def test_labs_on_a_new_account_point_at_game_speed_level_one() -> None:
    labs = state_view.build_labs(None, [], now=1000.)
    assert (labs["slots"], labs["running"], labs["levels"]) == (None, [], [])
    assert labs["next"] == {"id": "labs.game-speed", "name": "Game Speed", "cost": 300}
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `../thetowerbot/.venv/bin/pytest tests/test_fleet_state_view.py -q -p no:allure_pytest`
Expected: the 8 Task 3 tests pass, and the new ones FAIL with `AttributeError: module 'fleet.state_view' has no attribute 'build_bot'` (and the same for `build_battle`, `build_runs` and the rest).

- [ ] **Step 3: Implement**

In `fleet/state_view.py`, replace the three module imports with:

```python
import cards
import lab_catalog
import upgrades
import workshop_levels
from account_state import completed_lab_level
from concepts import REGISTRY
from fleet import workshop_prices
```

Then append at the end of the file:

```python
def _whole(value: object) -> int | None:
    return value if type(value) is int else None


def _number(value: object) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def build_bot(status: Mapping[str, Any] | None) -> dict[str, Any]:
    """Screen, current activity ("Now") and whether a battle is live."""
    if status is None:
        return {"screen": None, "now": None, "live": False}
    screen = status.get("screen") if isinstance(status.get("screen"), str) else None
    activity = status.get("activity")
    now = activity.get("label") if isinstance(activity, Mapping) else None
    return {"screen": screen, "now": now if isinstance(now, str) else None,
            "live": screen == "IN_RUN"}


def build_decision(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The autopilot's latest decision. It carries no price, so `cost` stays None."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("phase"), str):
        return None
    upgrade_id = raw.get("upgrade_id") if isinstance(raw.get("upgrade_id"), str) else None
    upgrade = upgrades.by_id(upgrade_id) if upgrade_id else None
    return {"phase": raw["phase"], "reason": str(raw.get("reason") or ""),
            "upgrade_id": upgrade_id,
            "category": _category(upgrade.category) if upgrade else None,
            "name": upgrade.name if upgrade else None, "cost": None}


def build_battle(status: Mapping[str, Any] | None, tier: int | None,
                 best_waves: Mapping[int, int]) -> dict[str, Any] | None:
    """The live battle HUD, or None when the worker is not in a run."""
    if status is None or status.get("screen") != "IN_RUN":
        return None
    run = status.get("run") if isinstance(status.get("run"), Mapping) else {}
    return {"tier": tier, "wave": _whole(status.get("wave")),
            "cash": _number(status.get("wallet")), "elapsed_s": _number(run.get("elapsed")),
            "best_wave": best_waves.get(tier) if tier is not None else None}


def build_balances(overview: Mapping[str, Any] | None,
                   ledger: Mapping[str, Any] | None) -> dict[str, Any]:
    """Coins and gems from the live scope when fresh, else the ledger's last reading.

    Stones have no reader yet, so they are always None.
    """
    overview, ledger = overview or {}, ledger or {}
    coins = overview.get("coins_lower")
    gems = overview.get("gems")
    return {"coins": coins if coins is not None else ledger.get("coins"),
            "gems": gems if gems is not None else ledger.get("gems"),
            "stones": None}


def build_runs(rows: Iterable[Mapping[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """The newest finished runs, newest first."""
    runs = []
    for row in list(rows)[:limit]:
        started, ended = _number(row.get("started_at")), _number(row.get("ended_at"))
        runs.append({"tier": row.get("tier"), "wave": row.get("wave"), "coins": row.get("coins"),
                     "duration_s": round(ended - started, 1)
                     if started is not None and ended is not None else None,
                     "ended_at": iso(ended), "abandoned": bool(row.get("abandoned"))})
    return runs


def build_run_upgrades(rows: Iterable[Mapping[str, Any]], scope: str | None) -> dict[str, Any] | None:
    """In-run upgrade levels bought in the current run, or the last one.

    None only when there is no run to describe. A run that bought nothing is
    a real answer: total 0 and no items.
    """
    if scope is None:
        return None
    by_category = dict.fromkeys(CATEGORIES, 0)
    items = []
    for row in rows:
        upgrade_id, levels = row.get("upgrade_id"), row.get("levels")
        if not isinstance(upgrade_id, str) or not upgrade_id or type(levels) is not int or levels <= 0:
            continue
        upgrade = upgrades.by_id(upgrade_id)
        category = _category(upgrade.category) if upgrade else None
        if category is not None:
            by_category[category] += levels
        items.append({"id": upgrade_id, "name": upgrade.name if upgrade else upgrade_id,
                      "category": category, "levels": levels})
    items.sort(key=lambda item: (-item["levels"], item["name"]))
    return {"scope": scope, "total": sum(item["levels"] for item in items),
            "by_category": by_category, "items": items}


def build_cards(revision: Mapping[str, Any] | None, gems_invested: int | None,
                recent_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Card slots, per-card level and copies, gems spent and recent card buys."""
    facts = {fact.get("concept_id"): fact.get("value")
             for fact in (revision or {}).get("cards") or ()}
    capacity = _whole(facts.get(cards.SLOT_CAPACITY_KEY))
    items: dict[str, dict[str, Any]] = {}
    for key, value in facts.items():
        if not isinstance(key, str) or not key.startswith("cards.") or key.count(".") != 2:
            continue
        concept_id, _, field = key.rpartition(".")
        if field not in ("level", "copies"):
            continue
        concept = REGISTRY.by_id(concept_id)
        entry = items.setdefault(concept_id, {"name": concept.name if concept else concept_id,
                                              "level": None, "copies": None})
        entry[field] = _whole(value)
    return {"slots": {"equipped": _whole(facts.get(cards.SLOT_EQUIPPED_KEY)), "capacity": capacity,
                      "next_slot_gems": lab_catalog.card_slot_gems(capacity + 1)
                      if capacity is not None else None},
            "items": sorted(items.values(), key=lambda item: item["name"]),
            "gems_invested": gems_invested,
            "recent": [{"ts": iso(row.get("ts")), "name": str(row.get("item") or "Card"),
                        "gems": row.get("price")} for row in list(recent_rows)[:RECENT_LIMIT]]}


def _lab_name(concept_id: str) -> str:
    concept = REGISTRY.by_id(concept_id)
    if concept is not None:
        return concept.name
    entry = lab_catalog.lab(concept_id)
    return entry.name if entry is not None else concept_id


def build_labs(revision: Mapping[str, Any] | None, recent_rows: Iterable[Mapping[str, Any]],
               now: float) -> dict[str, Any]:
    """Lab slots, running jobs, known levels with the next price, and the next lab."""
    revision = revision or {}
    known: dict[str, int | None] = {}
    levels = []
    for fact in revision.get("lab_levels") or ():
        concept_id = fact.get("concept_id")
        if not isinstance(concept_id, str):
            continue
        level = completed_lab_level(fact.get("status"), fact.get("value"))
        known[concept_id] = level
        step = lab_catalog.level(concept_id, level + 1) if level is not None else None
        levels.append({"id": concept_id, "name": _lab_name(concept_id), "level": level,
                       "next_cost": step.coins if step is not None else None})
    running = []
    for fact in revision.get("lab_jobs") or ():
        concept_id, completes = fact.get("concept_id"), _number(fact.get("value"))
        # A job past its completion time has finished; the revision just has
        # not been re-read since. Listing it would show a timer below zero.
        if not isinstance(concept_id, str) or completes is None or completes <= now:
            continue
        level = known.get(concept_id)
        running.append({"id": concept_id, "name": _lab_name(concept_id),
                        "to_level": level + 1 if level is not None else None,
                        "completes_at": iso(completes)})
    busy = {job["id"] for job in running}
    priced = [row for row in levels if row["id"] not in busy and row["next_cost"] is not None]
    upcoming: dict[str, Any] | None = None
    if priced:
        cheapest = min(priced, key=lambda row: row["next_cost"])
        upcoming = {"id": cheapest["id"], "name": cheapest["name"], "cost": cheapest["next_cost"]}
    else:
        entry = next((lab for lab in lab_catalog.CATALOG.labs if lab.id not in busy), None)
        if entry is not None:
            step = lab_catalog.level(entry.id, (known.get(entry.id) or 0) + 1)
            upcoming = {"id": entry.id, "name": entry.name,
                        "cost": step.coins if step is not None else None}
    return {"slots": _whole(revision.get("lab_slots_owned")), "running": running,
            "levels": levels, "next": upcoming,
            "recent": [{"ts": iso(row.get("ts")),
                        "name": _lab_name(item) if isinstance(item := row.get("item"), str)
                        and item.startswith("labs.") else str(item or "Lab"),
                        "price": row.get("price")} for row in list(recent_rows)[:RECENT_LIMIT]]}
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `../thetowerbot/.venv/bin/pytest tests/test_fleet_state_view.py -q -p no:allure_pytest`
Expected: PASS (18 tests).

- [ ] **Step 5: Commit**

```bash
git add fleet/state_view.py tests/test_fleet_state_view.py
git commit -F- <<'EOF'
Build the Fleet State bot, battle, balances, runs, cards and labs sections

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QQCpP6UkpvfBfCfHxKaoqD
EOF
```

---

### Task 5: Read-only worker records, the `fleet_state()` aggregator and `GET /api/fleet/state`

**Files:**
- Create: `fleet/state_records.py`
- Modify: `fleet/state_view.py` (imports, constants, append)
- Modify: `fleet/setup.py` (add `state_snapshot()` directly above `build_route_validate_bindings`)
- Modify: `web/app.py` (add the route directly after `fleet_labs_snapshot`, above `def _build_route_capability`)
- Create: `tests/test_fleet_state_records.py`
- Test: `tests/test_fleet_state_view.py` (imports, append)

**Interfaces:**
- Consumes: every Task 3 and Task 4 builder. `db.connection_account(conn)`, `db.run_purchases(conn, run_id) -> list[{"upgrade_id", ...}]` (needs `sqlite3.Row`), `db.run_upgrade_levels(conn, run_id) -> list[{"upgrade_id", "levels", ...}] | None`, `db.last_balances(conn) -> {"coins", "gems"}`, `web.account_catalog.registered_worker(worker_root) -> AccountChoice | None` (`.account_id`, `.db_path`, `.web_port`), `runtime_records.RuntimeRecords(path).read()`, `currencies.currency_overview(path, *, account_id, lease_id, generation, now)`. Pool members are dicts with `name`, and `endpoint` and `lease_id` when the pool knows them.
- Produces:
  - `fleet.state_records.WorkerRecords` (frozen dataclass: `revision`, `workshop_spent`, `workshop_recent`, `card_gems`, `card_recent`, `lab_recent`, `runs`, `best_waves`, `run_upgrades`, `run_upgrades_scope`, `last_seen`, `balances`)
  - `fleet.state_records.read_records(db_path: Path, account_id: str, live_run_id: int | None) -> WorkerRecords`. It raises `FileNotFoundError`, `ForeignDatabase(ValueError)` or `sqlite3.Error`.
  - `fleet.state_view.STATUS_TIMEOUT = .2`, `MAX_WORKERS = 8`
  - `fleet.state_view.read_status(web_port: int | None, fetch) -> dict | None`
  - `fleet.state_view.build_account(root: Path, member: Mapping, fetch, now: float) -> dict`
  - `fleet.state_view.fleet_state(root: Path, members: Sequence[Mapping], *, fetch=None, now: float | None = None) -> {"generated_at": str, "accounts": [account]}`. `fetch` defaults to the module-level `urlopen`, resolved at call time so tests can monkeypatch it.
  - The account dict (the frontend contract in Task 6): `id, name, serial, online, stale_seconds, scan, error, bot, battle, balances, decision, workshop, cards, labs, run_upgrades, runs`
  - `FleetSetupService.state_snapshot() -> dict`
  - `GET /api/fleet/state` returns 200 with that payload, or 503 `fleet_state_unavailable` without a fleet.

- [ ] **Step 1: Write the failing records tests** (create `tests/test_fleet_state_records.py`)

```python
"""Read-only worker DB reads for the Fleet State page."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import db
from fleet.state_records import RECENT_LIMIT, ForeignDatabase, read_records

BOUGHT = json.dumps({"verdict": "bought"})


def _database(tmp_path: Path, account: str = "account-a") -> Path:
    path = tmp_path / "tower_bot.db"
    db.bind_account(path, account)
    return path


def _ledger(conn: sqlite3.Connection, ts: float, kind: str, item: str, category: str,
            delta: int | None, price: int | None, detail: str = BOUGHT, dry_run: int = 0) -> None:
    conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,price,dry_run,detail) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (ts, kind, item, category, "gems" if kind == "CARD_BUY" else "coins",
                  delta, price, dry_run, detail))


def _run(conn: sqlite3.Connection, run_id: int, tier: int, wave: int, abandoned: int = 0,
         ended: bool = True) -> None:
    conn.execute("INSERT INTO runs(id, started_at, ended_at, wave, coins, tier, abandoned) "
                 "VALUES(?,?,?,?,?,?,?)",
                 (run_id, 100. * run_id, 100. * run_id + 50 if ended else None, wave, 1000,
                  tier, abandoned))


def test_a_missing_or_foreign_database_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_records(tmp_path / "absent.db", "account-a", None)
    path = _database(tmp_path, "account-b")
    with pytest.raises(ForeignDatabase):
        read_records(path, "account-a", None)


def test_best_wave_is_per_tier_over_finished_runs_that_were_not_abandoned(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        _run(conn, 1, 11, 5020)
        _run(conn, 2, 11, 9999, abandoned=1)
        _run(conn, 3, 7, 1420)
        _run(conn, 4, 11, 4000)
        _run(conn, 5, 11, 8000, ended=False)
    records = read_records(path, "account-a", None)
    assert records.best_waves == {11: 5020, 7: 1420}


def test_the_last_five_finished_runs_are_newest_first(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        for run_id in range(1, 8):
            _run(conn, run_id, 1, 10 * run_id)
        _run(conn, 8, 1, 999, ended=False)
    records = read_records(path, "account-a", None)
    assert [run["id"] for run in records.runs] == [7, 6, 5, 4, 3]


def test_current_run_upgrades_come_from_its_purchase_events(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        _run(conn, 1, 1, 10)
        conn.execute("INSERT INTO run_upgrades VALUES (1, 'health', 3, 90, 0)")
        _run(conn, 2, 1, 0, ended=False)
        for seq, upgrade_id in enumerate(["damage", "damage", "health", None], start=1):
            conn.execute("INSERT INTO events(seq, run_id, ts, type, detail) VALUES (?,?,?,?,?)",
                         (seq, 2, 200. + seq, "BattlePurchased",
                          json.dumps({"item": "x", "upgrade_id": upgrade_id, "value": None})))
    current = read_records(path, "account-a", 2)
    assert current.run_upgrades_scope == "current"
    assert sorted((row["upgrade_id"], row["levels"]) for row in current.run_upgrades) == [
        ("damage", 2), ("health", 1)]
    last = read_records(path, "account-a", None)
    assert last.run_upgrades_scope == "last"
    assert [(row["upgrade_id"], row["levels"]) for row in last.run_upgrades] == [("health", 3)]


def test_a_new_account_has_no_runs_no_revision_and_zero_gems_spent(tmp_path: Path) -> None:
    records = read_records(_database(tmp_path), "account-a", None)
    assert (records.revision, records.runs, records.best_waves) == (None, [], {})
    assert (records.run_upgrades, records.run_upgrades_scope) == ([], None)
    assert (records.card_gems, records.workshop_recent, records.last_seen) == (0, [], None)
    assert records.balances == {"coins": None, "gems": None}


def test_a_revision_stamped_with_another_account_is_ignored(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES (?)",
                     (json.dumps({"account_id": "account-old", "workshop_stats": []}),))
    assert read_records(path, "account-a", None).revision is None


def test_a_huge_ledger_is_summed_and_bounded_by_sql(tmp_path: Path) -> None:
    """Review Focus: years of ledger rows must not be loaded into Python."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.executemany(
            "INSERT INTO ledger(ts,kind,item,category,currency,delta,price,dry_run,detail) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(float(i), "WORKSHOP_BUY", "Damage", "ATTACK", "coins", -10, 10, 0, BOUGHT)
             for i in range(50_000)])
        _ledger(conn, 60_000., "WORKSHOP_BUY", "Damage", "ATTACK", -99, 99, dry_run=1)
        _ledger(conn, 60_001., "WORKSHOP_BUY", "Damage", "ATTACK", -99, 99,
                detail=json.dumps({"verdict": "unconfirmed"}))
        _ledger(conn, 60_002., "CARD_BUY", "Card", "CARDS", -20, 20)
        _ledger(conn, 60_003., "CARD_BUY", "Card", "CARDS", None, 20)
    started = time.monotonic()
    records = read_records(path, "account-a", None)
    assert time.monotonic() - started < 2
    assert records.workshop_spent == [("Damage", "ATTACK", 500_000)]
    assert len(records.workshop_recent) == RECENT_LIMIT
    assert records.workshop_recent[0] == {"ts": 49_999., "item": "Damage", "category": "ATTACK",
                                          "price": 10}
    assert records.card_gems == 20
    assert [row["price"] for row in records.card_recent] == [20, 20]


def test_a_database_locked_mid_write_fails_fast(tmp_path: Path) -> None:
    """Review Focus: a worker holding a write lock costs this read 0.1 s, not a hang."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")  # WAL would let the reader through
    locker = sqlite3.connect(path)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            read_records(path, "account-a", None)
        assert time.monotonic() - started < 1
    finally:
        locker.rollback()
        locker.close()
```

- [ ] **Step 2: Write the failing aggregator and route tests**

In `tests/test_fleet_state_view.py`, replace the imports with:

```python
from __future__ import annotations

import io
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

import db
import workshop_levels
from events import EventBus
from fleet import state_view
from fleet.setup import FleetSetupService
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app
```

and append:

```python
def _registered(root: Path, worker: str, account: str, port: int) -> Path:
    worker_root = root / "workers" / worker
    checkpoint = worker_root / "checkpoints" / ("a" * 32 + ".json")
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"worker_id": worker, "account_id": account,
                                      "endpoint": "127.0.0.1:5555", "lease_id": "lease"}))
    (worker_root / "fleet-registration.json").write_text(json.dumps({
        "state": "registered", "instance": worker, "account_id": account,
        "web_port": port, "binding": str(checkpoint),
        "endpoint": "127.0.0.1:5555", "lease_id": "lease"}))
    db.bind_account(worker_root / "tower_bot.db", account)
    return worker_root


def _fetch(statuses: dict[int, Any]) -> Callable[..., Any]:
    """A urlopen stand-in: a dict is the status JSON, an exception is raised."""
    def fetch(url: str, timeout: float) -> io.BytesIO:
        assert timeout == state_view.STATUS_TIMEOUT
        value = statuses.get(int(url.split(":")[2].split("/")[0]), OSError("refused"))
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        return io.BytesIO(json.dumps(value).encode())
    return fetch


LIVE = {"screen": "IN_RUN", "scans": 18442, "wallet": 8420000000, "wave": 4812,
        "run": {"id": 2, "elapsed": 5780.0},
        "activity": {"label": "Shopping", "at": 1.0},
        "decision": {"phase": "buying", "reason": "cheapest", "upgrade_id": "damage", "at": 1.0}}


def test_an_online_worker_becomes_a_full_account_column(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO runs(id, started_at, ended_at, wave, coins, tier) "
                     "VALUES (1, 0, 6020, 5020, 310000000, 11)")
    member = {"name": "Air_1", "endpoint": "127.0.0.1:5555", "lease_id": "lease"}

    state = state_view.fleet_state(tmp_path, [member], fetch=_fetch({8001: LIVE}), now=7000.)

    (account,) = state["accounts"]
    assert state["generated_at"] == "1970-01-01T01:56:40+00:00"
    assert {k: account[k] for k in ("id", "serial", "online", "stale_seconds", "scan", "error")} == {
        "id": "Air_1", "serial": "127.0.0.1:5555", "online": True, "stale_seconds": 0,
        "scan": 18442, "error": None}
    assert account["bot"] == {"screen": "IN_RUN", "now": "Shopping", "live": True}
    assert account["battle"] == {"tier": 11, "wave": 4812, "cash": 8420000000,
                                 "elapsed_s": 5780.0, "best_wave": 5020}
    assert account["decision"]["name"] == "Damage"
    assert account["balances"] == {"coins": None, "gems": None, "stones": None}
    assert account["run_upgrades"]["scope"] == "current"
    assert [run["wave"] for run in account["runs"]] == [5020]
    assert account["workshop"]["totals"] == {"attack": 0, "defense": 0, "utility": 0}


def test_an_offline_worker_is_built_from_its_database_with_a_stale_age(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO events(seq, ts, type) VALUES (1, 6958.0, 'ScanCompleted')")
    (account,) = state_view.fleet_state(tmp_path, [{"name": "Air_1"}], fetch=_fetch({}),
                                        now=7000.)["accounts"]
    assert (account["online"], account["stale_seconds"], account["scan"]) == (False, 42, None)
    assert account["battle"] is None and account["bot"]["screen"] is None
    assert account["workshop"] is not None and account["error"] is None


def test_a_reachable_worker_whose_database_is_missing_keeps_its_live_status(
        tmp_path: Path) -> None:
    """Review Focus: the bot answers /api/status but its database file is gone."""
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    (root / "tower_bot.db").unlink()
    (account,) = state_view.fleet_state(tmp_path, [{"name": "Air_1"}],
                                        fetch=_fetch({8001: LIVE}), now=7000.)["accounts"]
    assert account["online"] is True and account["scan"] == 18442
    assert account["bot"]["screen"] == "IN_RUN"
    assert account["error"] == "Worker database is missing"
    assert all(account[key] is None for key in ("workshop", "cards", "labs", "runs", "battle"))


def test_a_locked_database_errors_one_account_and_leaves_the_others(tmp_path: Path) -> None:
    """Review Focus: one worker holds a write lock while the page polls."""
    locked = _registered(tmp_path, "Air_1", "account-a", 8001)
    _registered(tmp_path, "Air_2", "account-b", 8002)
    with db.connect(locked / "tower_bot.db") as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    locker = sqlite3.connect(locked / "tower_bot.db")
    locker.execute("BEGIN EXCLUSIVE")
    try:
        accounts = state_view.fleet_state(tmp_path, [{"name": "Air_1"}, {"name": "Air_2"}],
                                          fetch=_fetch({}), now=7000.)["accounts"]
    finally:
        locker.rollback()
        locker.close()
    assert accounts[0]["error"].startswith("Worker database unavailable")
    assert accounts[0]["workshop"] is None
    assert accounts[1]["error"] is None and accounts[1]["workshop"] is not None


def test_workers_are_fetched_concurrently(tmp_path: Path) -> None:
    _registered(tmp_path, "Air_1", "account-a", 8001)
    _registered(tmp_path, "Air_2", "account-b", 8002)
    both = threading.Barrier(2, timeout=2)

    def waiting() -> dict[str, Any]:
        both.wait()  # raises BrokenBarrierError if the two fetches were sequential
        return LIVE

    accounts = state_view.fleet_state(tmp_path, [{"name": "Air_1"}, {"name": "Air_2"}],
                                      fetch=_fetch({8001: waiting, 8002: waiting}),
                                      now=7000.)["accounts"]
    assert [account["online"] for account in accounts] == [True, True]


def test_unregistered_and_foreign_workers_are_error_accounts(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_2", "account-b", 8002)
    (root / "tower_bot.db").unlink()
    db.bind_account(root / "tower_bot.db", "account-other")
    accounts = state_view.fleet_state(tmp_path, [{"name": "Air_1"}, {"name": "Air_2"}],
                                      fetch=_fetch({}), now=7000.)["accounts"]
    assert [account["error"] for account in accounts] == [
        "Worker is not registered to an account", "Worker database belongs to another account"]


def test_a_failing_builder_blanks_only_its_own_section(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registered(tmp_path, "Air_1", "account-a", 8001)

    def broken(*args: Any) -> dict[str, Any]:
        raise KeyError("boom")

    monkeypatch.setattr(state_view, "build_cards", broken)
    (account,) = state_view.fleet_state(tmp_path, [{"name": "Air_1"}], fetch=_fetch({}),
                                        now=7000.)["accounts"]
    assert account["cards"] is None
    assert account["labs"] is not None and account["workshop"] is not None
    assert account["error"] is None


def _client(root: Path, names: tuple[str, ...]) -> TestClient:
    fleet = FleetSetupService(root, qualification_root=root / "qualifications")
    fleet._reroll_pool = SimpleNamespace(members=lambda: [{"name": name} for name in names])
    fleet._reroll_runs = SimpleNamespace(hidden_names=lambda: {"Air_9"})
    return TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                 db_path=None, fleet=fleet))


def test_the_state_endpoint_lists_visible_members_in_name_order(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state_view, "urlopen", _fetch({}))
    _registered(tmp_path, "Air_2", "account-b", 8002)
    response = _client(tmp_path, ("Air_2", "Air_9", "Air_1")).get("/api/fleet/state")
    assert response.status_code == 200
    assert [account["id"] for account in response.json()["accounts"]] == ["Air_1", "Air_2"]


def test_the_state_endpoint_is_unavailable_without_the_fleet() -> None:
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None))
    assert client.get("/api/fleet/state").status_code == 503
```

- [ ] **Step 3: Run the tests and confirm they fail**

Run: `../thetowerbot/.venv/bin/pytest tests/test_fleet_state_records.py tests/test_fleet_state_view.py -q -p no:allure_pytest`
Expected: FAIL. `test_fleet_state_records.py` fails at collection with `ModuleNotFoundError: No module named 'fleet.state_records'`, and the new view tests fail with `AttributeError: module 'fleet.state_view' has no attribute 'fleet_state'` / `'STATUS_TIMEOUT'`.

- [ ] **Step 4: Implement the records reader** (create `fleet/state_records.py`)

```python
"""Read-only reads of one worker database for the Fleet State page.

Opened mode=ro with a 0.1 s busy timeout, so a worker in the middle of a
write costs this page a tenth of a second and never costs the bot a lock.
Every list is bounded in SQL and every total is summed by SQL, so a ledger of
any length is read in a fixed number of rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import db

RECENT_LIMIT = 30
RUNS_LIMIT = 5
_BOUGHT = "dry_run=0 AND json_extract(detail, '$.verdict') IN ('bought','free')"


class ForeignDatabase(ValueError):
    """The worker database is bound to another account, or to none."""


@dataclass(frozen=True)
class WorkerRecords:
    revision: dict[str, Any] | None
    workshop_spent: list[tuple[Any, Any, Any]]
    workshop_recent: list[dict[str, Any]]
    card_gems: int
    card_recent: list[dict[str, Any]]
    lab_recent: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    best_waves: dict[int, int]
    run_upgrades: list[dict[str, Any]]
    run_upgrades_scope: str | None
    last_seen: float | None
    balances: dict[str, int | None]


def _recent(conn: sqlite3.Connection, kind: str, where: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT ts, item, category, COALESCE(-delta, price) AS price FROM ledger "
        f"WHERE kind=? AND {where} ORDER BY ts DESC, id DESC LIMIT ?",
        (kind, RECENT_LIMIT)).fetchall()
    return [dict(row) for row in rows]


def read_records(db_path: Path, account_id: str, live_run_id: int | None) -> WorkerRecords:
    """Everything the page needs from one worker DB, in one short read-only connection.

    Raises FileNotFoundError when the DB is missing, ForeignDatabase when it
    belongs to another account, and sqlite3.Error when it cannot be read
    (locked mid-write, corrupt). The caller turns each into an account error.
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    uri = f"{path.absolute().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=.1)) as conn:
        conn.row_factory = sqlite3.Row
        if db.connection_account(conn) != account_id:
            raise ForeignDatabase(str(path))
        row = conn.execute("SELECT detail FROM account_revisions ORDER BY id DESC LIMIT 1").fetchone()
        revision = json.loads(row["detail"]) if row is not None else None
        # A revision stamped with another account predates a replacement.
        if revision is not None and revision.get("account_id") not in (None, account_id):
            revision = None
        spent = [tuple(row) for row in conn.execute(
            f"SELECT item, category, SUM(-delta) FROM ledger WHERE kind='WORKSHOP_BUY' AND {_BOUGHT} "
            "GROUP BY item, category")]
        card_gems = conn.execute(
            "SELECT COALESCE(SUM(-delta), 0) FROM ledger "
            "WHERE kind='CARD_BUY' AND dry_run=0 AND delta IS NOT NULL").fetchone()[0]
        runs = [dict(row) for row in conn.execute(
            "SELECT id, tier, wave, coins, started_at, ended_at, abandoned FROM runs "
            "WHERE ended_at IS NOT NULL ORDER BY id DESC LIMIT ?", (RUNS_LIMIT,))]
        best = {tier: wave for tier, wave in conn.execute(
            "SELECT tier, MAX(wave) FROM runs WHERE abandoned=0 AND ended_at IS NOT NULL "
            "AND tier IS NOT NULL AND wave IS NOT NULL GROUP BY tier")}
        scope: str | None = None
        bought: list[dict[str, Any]] = []
        if live_run_id is not None:
            counts = Counter(p["upgrade_id"] for p in db.run_purchases(conn, live_run_id)
                             if p["upgrade_id"])
            bought = [{"upgrade_id": key, "levels": levels} for key, levels in counts.items()]
            scope = "current"
        elif runs:
            bought = db.run_upgrade_levels(conn, runs[0]["id"]) or []
            scope = "last"
        last_seen = conn.execute("SELECT MAX(ts) FROM events").fetchone()[0]
        return WorkerRecords(
            revision=revision, workshop_spent=spent,
            workshop_recent=_recent(conn, "WORKSHOP_BUY", _BOUGHT),
            card_gems=int(card_gems), card_recent=_recent(conn, "CARD_BUY", "dry_run=0"),
            lab_recent=_recent(conn, "LAB", "dry_run=0"), runs=runs, best_waves=best,
            run_upgrades=bought, run_upgrades_scope=scope, last_seen=last_seen,
            balances=db.last_balances(conn))
```

- [ ] **Step 5: Implement the aggregator**

In `fleet/state_view.py`, replace the standard-library imports (`from datetime import ...` and `from typing import ...`) with:

```python
import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.request import urlopen
```

Replace `from fleet import workshop_prices` with:

```python
from currencies import currency_overview
from fleet import workshop_prices
from fleet.state_records import ForeignDatabase, read_records
from runtime_records import RuntimeRecords, RuntimeRecordsError

logger = logging.getLogger(__name__)
```

After `RECENT_LIMIT = 30` add:

```python
STATUS_TIMEOUT = .2
MAX_WORKERS = 8
```

Then append at the end of the file:

```python
def read_status(web_port: int | None, fetch: Callable[..., Any]) -> dict[str, Any] | None:
    """The worker's /api/status, or None when it is not answering."""
    if not web_port:
        return None
    try:
        with fetch(f"http://127.0.0.1:{web_port}/api/status", timeout=STATUS_TIMEOUT) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _section(name: str, builder: Callable[..., Any], *args: Any) -> Any:
    """One section of one account. A builder that raises blanks only itself."""
    try:
        return builder(*args)
    except Exception:  # noqa: BLE001 - one broken section must not hide the rest
        logger.exception("Fleet state section %s failed", name)
        return None


def _live_run_id(status: Mapping[str, Any] | None) -> int | None:
    run = status.get("run") if status is not None and status.get("screen") == "IN_RUN" else None
    run_id = run.get("id") if isinstance(run, Mapping) else None
    return run_id if type(run_id) is int and run_id > 0 else None


def _currency(worker_root: Path, db_path: Path, account_id: str, lease_id: object,
              now: float) -> dict[str, Any] | None:
    """The live-scope balance overview, when this worker's scope can be named."""
    try:
        records = RuntimeRecords(worker_root / "runtime-records.json").read()
    except RuntimeRecordsError:
        return None
    start = records.get("start") if isinstance(records, dict) else None
    if (not isinstance(start, dict) or not isinstance(start.get("generation"), str)
            or not isinstance(lease_id, str) or not lease_id):
        return None
    return currency_overview(db_path, account_id=account_id, lease_id=lease_id,
                             generation=start["generation"], now=now)


def _blank(member: Mapping[str, Any]) -> dict[str, Any]:
    name = str(member["name"])
    return {"id": name, "name": name, "serial": member.get("endpoint") or None,
            "online": False, "stale_seconds": None, "scan": None, "error": None,
            "bot": build_bot(None), "battle": None, "balances": None, "decision": None,
            "workshop": None, "cards": None, "labs": None, "run_upgrades": None, "runs": None}


def build_account(root: Path, member: Mapping[str, Any], fetch: Callable[..., Any],
                  now: float) -> dict[str, Any]:
    """One account column: status first, then the worker's own database."""
    from web.account_catalog import registered_worker

    account = _blank(member)
    worker_root = Path(root) / "workers" / account["id"]
    registration = registered_worker(worker_root)
    if registration is None or registration.account_id is None:
        return {**account, "error": "Worker is not registered to an account"}
    status = read_status(registration.web_port, fetch)
    if status is not None:
        scans = status.get("scans")
        account.update(online=True, stale_seconds=0,
                       scan=scans if type(scans) is int else None,
                       bot=_section("bot", build_bot, status) or build_bot(None),
                       decision=_section("decision", build_decision, status.get("decision")))
    try:
        records = read_records(registration.db_path, registration.account_id, _live_run_id(status))
    except ForeignDatabase:
        return {**account, "error": "Worker database belongs to another account"}
    except FileNotFoundError:
        return {**account, "error": "Worker database is missing"}
    except (OSError, sqlite3.Error) as exc:
        return {**account, "error": f"Worker database unavailable ({exc})"}
    if status is None and records.last_seen is not None:
        account["stale_seconds"] = max(0, round(now - records.last_seen))
    tier = records.runs[0]["tier"] if records.runs else None
    overview = _section("currency", _currency, worker_root, registration.db_path,
                        registration.account_id, member.get("lease_id"), now)
    account.update(
        battle=_section("battle", build_battle, status, tier, records.best_waves),
        balances=_section("balances", build_balances, overview, records.balances),
        workshop=_section("workshop", build_workshop, records.revision,
                          records.workshop_spent, records.workshop_recent),
        cards=_section("cards", build_cards, records.revision, records.card_gems,
                       records.card_recent),
        labs=_section("labs", build_labs, records.revision, records.lab_recent, now),
        run_upgrades=_section("run_upgrades", build_run_upgrades, records.run_upgrades,
                              records.run_upgrades_scope),
        runs=_section("runs", build_runs, records.runs))
    return account


def _guarded(root: Path, member: Mapping[str, Any], fetch: Callable[..., Any],
             now: float) -> dict[str, Any]:
    try:
        return build_account(root, member, fetch, now)
    except Exception:  # noqa: BLE001 - one worker's failure must not hide its peers
        logger.exception("Fleet state unavailable for %s", member.get("name"))
        return {**_blank(member), "error": "Account state unavailable"}


def fleet_state(root: Path, members: Sequence[Mapping[str, Any]], *,
                fetch: Callable[..., Any] | None = None, now: float | None = None) -> dict[str, Any]:
    """Every member's column, fetched concurrently so one slow worker delays no other."""
    moment = time.time() if now is None else now
    reader = fetch if fetch is not None else urlopen
    members = list(members)
    accounts: list[dict[str, Any]] = []
    if members:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(members))) as pool:
            accounts = list(pool.map(lambda member: _guarded(Path(root), member, reader, moment),
                                     members))
    return {"generated_at": iso(moment), "accounts": accounts}
```

- [ ] **Step 6: Add the service method and the route**

In `fleet/setup.py`, directly above `def build_route_validate_bindings(self, route: Any) -> None:` add:

```python
    def state_snapshot(self) -> dict[str, Any]:
        """Read-only Fleet State columns for the visible pool members."""
        from fleet.state_view import fleet_state
        hidden = self._runs().hidden_names()
        members = sorted((member for member in self._manual_pool().members()
                          if member["name"] not in hidden), key=lambda member: member["name"])
        return fleet_state(self.root, members)
```

In `web/app.py`, directly after the `fleet_labs_snapshot` route (and so still above the unmatched-API catch-all, which the comment above `/api/fleet/setup` requires), add:

```python
    @app.get("/api/fleet/state")
    def fleet_state_snapshot() -> dict[str, Any]:
        if fleet is None or not callable(getattr(fleet, "state_snapshot", None)):
            raise HTTPException(status_code=503, detail="fleet_state_unavailable")
        return fleet.state_snapshot()
```

- [ ] **Step 7: Run the tests and confirm they pass**

Run: `../thetowerbot/.venv/bin/pytest tests/test_fleet_state_records.py tests/test_fleet_state_view.py tests/test_labs_view.py tests/test_lifecycle_api.py -q -p no:allure_pytest`
Expected: PASS (8 records tests, 27 view tests, and the existing labs and lifecycle route tests unchanged).

- [ ] **Step 8: Commit**

```bash
git add fleet/state_records.py fleet/state_view.py fleet/setup.py web/app.py tests/test_fleet_state_records.py tests/test_fleet_state_view.py
git commit -F- <<'EOF'
Serve every worker's Fleet State from GET /api/fleet/state

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QQCpP6UkpvfBfCfHxKaoqD
EOF
```

---

### Task 6: Frontend data layer: types, formatters, selection, the `useFleetState` hook, tokens and fonts

**Files:**
- Create: `web/ui/lib/fleetState.ts`, `web/ui/lib/fleetState.test.ts`
- Modify: `web/ui/lib/api.ts`
- Create: `web/ui/app/fleet/state/fixtures.ts`, `stateFormat.ts`, `stateFormat.test.ts`, `selection.ts`, `selection.test.ts`, `useFleetState.ts`, `useFleetState.test.tsx`
- Modify: `web/ui/app/globals.css`, `web/ui/app/layout.tsx`

**Interfaces:**
- Consumes: the Task 5 JSON payload. `gameNumber(value: number): string` and `ago(seconds: number): string` from `@/app/fleet/reroll/workshop/workshopFormat`.
- Produces:
  - `@/lib/fleetState`: `StateCategory`, `STATE_CATEGORIES`, `FleetStateAccount`, `FleetStatePayload`, `FleetStateBot`, `FleetStateBattle`, `FleetStateBalances`, `FleetStateDecision`, `FleetStateWorkshop`, `WorkshopSkill`, `WorkshopCategory`, `WorkshopRecent`, `UnlockTarget`, `FleetStateCards`, `FleetStateLabs`, `LabJob`, `LabLevelRow`, `FleetStateRunUpgrades`, `FleetStateRun`, `accountKey(account): string`, `reuseUnchanged(previous, next): FleetStateAccount[]`
  - `@/lib/api`: `fetchFleetState(): Promise<FleetStatePayload>`
  - `./stateFormat`: `DASH`, `PRICE_UNKNOWN`, `amount`, `priceText`, `whole`, `span`, `secondsUntil(iso, nowMs)`, `agoText(iso, nowMs)`, `CAT_COLOR`, `CAT_LABEL`, `ACCENTS`, `accentAt(index)`
  - `./selection`: `Selection`, `SelectionMode`, `SELECTION_KEY`, `OPEN_KEY`, `DEFAULT_SELECTION`, `loadSelection`, `saveSelection`, `loadOpen`, `saveOpen`, `isLive`, `visibleAccounts`, `toggleAccount`, `sectionOpen(open, accountId, section, shownCount)`
  - `./useFleetState`: `POLL_MS = 2000`, `FleetStateView { payload, accounts, changedAt, connectionLost }`, `useFleetState(intervalMs?)`
  - `./fixtures`: `makeAccount(overrides)`, `makePayload(accounts)`
  - CSS: `--cat-attack`, `--cat-defense`, `--cat-utility`, `--cat-cards`, `--cat-labs`; the font variable `--font-chakra-petch`

- [ ] **Step 1: Install dependencies once**

Run: `cd web/ui && npm ci`
Expected: completes, and `node_modules/.bin/vitest` exists.

- [ ] **Step 2: Write the failing tests**

`web/ui/app/fleet/state/fixtures.ts` (test helper, created now because every test below uses it):

```ts
import type { FleetStateAccount, FleetStatePayload } from "@/lib/fleetState";

/** A complete account for tests: offline, nothing observed. Override what a test is about. */
export function makeAccount(overrides: Partial<FleetStateAccount> = {}): FleetStateAccount {
  const id = overrides.id ?? "Air_1";
  return {
    id, name: id, serial: "127.0.0.1:5555", online: false,
    stale_seconds: null, scan: null, error: null,
    bot: { screen: null, now: null, live: false }, battle: null, balances: null,
    decision: null, workshop: null, cards: null, labs: null, run_upgrades: null, runs: null,
    ...overrides,
  };
}

export function makePayload(accounts: FleetStateAccount[]): FleetStatePayload {
  return { generated_at: "2026-09-28T10:00:00+00:00", accounts };
}
```

`web/ui/lib/fleetState.test.ts`:

```ts
import { expect, test } from "vitest";
import { makeAccount } from "@/app/fleet/state/fixtures";
import { accountKey, reuseUnchanged } from "./fleetState";

test("an account keeps its previous object until its scan or reachability changes", () => {
  const before = [makeAccount({ id: "Air_1", scan: 5, online: true }), makeAccount({ id: "Air_2", scan: 9, online: true })];
  const after = reuseUnchanged(before, [makeAccount({ id: "Air_1", scan: 5, online: true }), makeAccount({ id: "Air_2", scan: 10, online: true })]);
  expect(after[0]).toBe(before[0]);
  expect(after[1]).not.toBe(before[1]);
  expect(after[1].scan).toBe(10);
  expect(reuseUnchanged(null, before)).toBe(before);
  expect(accountKey(makeAccount({ online: false, stale_seconds: 42 }))).not.toBe(accountKey(makeAccount({ online: false, stale_seconds: 44 })));
});
```

`web/ui/app/fleet/state/stateFormat.test.ts`:

```ts
import { expect, test } from "vitest";
import { agoText, amount, priceText, secondsUntil, span, whole } from "./stateFormat";

test("null renders as a dash or 'price unknown', never as a number", () => {
  expect(amount(null)).toBe("—");
  expect(amount(undefined)).toBe("—");
  expect(amount(0)).toBe("0");
  expect(amount(8.42e9)).toBe("8.42B");
  expect(priceText(null)).toBe("price unknown");
  expect(priceText(1.2e8)).toBe("120.00M");
  expect(whole(null)).toBe("—");
  expect(whole(18442)).toBe("18,442");
});

test("durations and times", () => {
  expect(span(5780)).toBe("1h 36m");
  expect(span(245)).toBe("4m 05s");
  expect(span(42)).toBe("42s");
  expect(span(null)).toBe("—");
  const now = Date.parse("2026-09-28T10:00:00Z");
  expect(secondsUntil("2026-09-28T10:01:00Z", now)).toBe(60);
  expect(secondsUntil("2026-09-28T09:59:00Z", now)).toBe(0);
  expect(agoText("2026-09-28T09:55:00Z", now)).toBe("5m ago");
  expect(agoText(null, now)).toBe("—");
});
```

`web/ui/app/fleet/state/selection.test.ts`:

```ts
import { afterEach, expect, test, vi } from "vitest";
import { makeAccount } from "./fixtures";
import { DEFAULT_SELECTION, loadOpen, loadSelection, saveSelection, sectionOpen, toggleAccount, visibleAccounts } from "./selection";

const live = makeAccount({ id: "Air_1", online: true, bot: { screen: "IN_RUN", now: null, live: true } });
const menu = makeAccount({ id: "Air_2", online: true, bot: { screen: "MAIN_MENU", now: null, live: false } });
const gone = makeAccount({ id: "Air_3" });
const fleet = [live, menu, gone];

afterEach(() => { vi.restoreAllMocks(); window.localStorage.clear(); });

test("all, only live, and a custom pick", () => {
  expect(visibleAccounts(fleet, DEFAULT_SELECTION)).toEqual(fleet);
  expect(visibleAccounts(fleet, { mode: "live", ids: [] })).toEqual([live]);
  const picked = toggleAccount(DEFAULT_SELECTION, "Air_2", fleet);
  expect(picked).toEqual({ mode: "custom", ids: ["Air_1", "Air_3"] });
  expect(toggleAccount(picked, "Air_2", fleet)).toEqual(DEFAULT_SELECTION);
});

test("the selection survives a reload and blocked storage falls back to all", () => {
  saveSelection({ mode: "custom", ids: ["Air_2"] });
  expect(loadSelection()).toEqual({ mode: "custom", ids: ["Air_2"] });
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
  expect(loadSelection()).toEqual(DEFAULT_SELECTION);
  expect(loadOpen()).toEqual({});
  expect(() => saveSelection(DEFAULT_SELECTION)).not.toThrow();
});

test("cards and labs open by default only while one or two columns show", () => {
  expect(sectionOpen({}, "Air_1", "cards", 2)).toBe(true);
  expect(sectionOpen({}, "Air_1", "cards", 3)).toBe(false);
  expect(sectionOpen({}, "Air_1", "workshop", 4)).toBe(true);
  expect(sectionOpen({ "Air_1:cards": true }, "Air_1", "cards", 4)).toBe(true);
});
```

`web/ui/app/fleet/state/useFleetState.test.tsx`:

```tsx
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { makeAccount, makePayload } from "./fixtures";
import { useFleetState, type FleetStateView } from "./useFleetState";

const fetchState = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ fetchFleetState: fetchState }));

const seen: FleetStateView[] = [];
function Probe(): React.JSX.Element {
  const view = useFleetState();
  seen.push(view);
  return <div data-testid="probe">{view.accounts.map(a => `${a.id}#${a.scan}`).join(",")}|{view.connectionLost ? "lost" : "ok"}</div>;
}

beforeEach(() => { vi.useFakeTimers(); fetchState.mockReset(); seen.length = 0; });
afterEach(() => { vi.useRealTimers(); });

test("polls every 2 s, keeps the last payload when a poll fails, and reuses unchanged accounts", async () => {
  const account = () => makeAccount({ id: "Air_1", scan: 5, online: true });
  fetchState.mockResolvedValueOnce(makePayload([account()]))
    .mockRejectedValueOnce(new Error("down"))
    .mockResolvedValueOnce(makePayload([account()]))
    .mockResolvedValueOnce(makePayload([makeAccount({ id: "Air_1", scan: 6, online: true })]));
  const view = render(<Probe />);
  await act(async () => {});
  expect(screen.getByTestId("probe")).toHaveTextContent("Air_1#5|ok");
  const first = seen.at(-1)!.accounts[0];
  const firstChanged = seen.at(-1)!.changedAt.Air_1;

  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(fetchState).toHaveBeenCalledTimes(2);
  expect(screen.getByTestId("probe")).toHaveTextContent("Air_1#5|lost");

  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByTestId("probe")).toHaveTextContent("Air_1#5|ok");
  expect(seen.at(-1)!.accounts[0]).toBe(first);
  expect(seen.at(-1)!.changedAt.Air_1).toBe(firstChanged);

  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByTestId("probe")).toHaveTextContent("Air_1#6|ok");
  expect(seen.at(-1)!.accounts[0]).not.toBe(first);

  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(fetchState).toHaveBeenCalledTimes(4);
});
```

- [ ] **Step 3: Run the tests and confirm they fail**

Run (in `web/ui`): `npx vitest run lib/fleetState.test.ts app/fleet/state`
Expected: FAIL. You should see "Failed to resolve import" errors for `./fleetState`, `./stateFormat`, `./selection` and `./useFleetState`.

- [ ] **Step 4: Implement the types** (create `web/ui/lib/fleetState.ts`)

```ts
/** GET /api/fleet/state: one column per emulator account. Mirrors fleet/state_view.py.
 *
 * Every number may be null. Null means nobody observed it, and the page shows
 * it as a dash or "price unknown" - never as 0. */

export type StateCategory = "attack" | "defense" | "utility";
export const STATE_CATEGORIES: readonly StateCategory[] = ["attack", "defense", "utility"];

export interface FleetStateBot { screen: string | null; now: string | null; live: boolean }
export interface FleetStateBattle {
  tier: number | null; wave: number | null; cash: number | null;
  elapsed_s: number | null; best_wave: number | null;
}
export interface FleetStateBalances { coins: number | null; gems: number | null; stones: null }
export interface FleetStateDecision {
  phase: string; reason: string; upgrade_id: string | null;
  category: StateCategory | null; name: string | null; cost: number | null;
}
export interface WorkshopSkill {
  id: string; name: string; level: number | null; invested: number | null; bot_spent: number;
  next_cost: number | null; status: string; locked: boolean;
}
export interface UnlockTarget { id: string; name: string; cost: number | null }
export interface WorkshopCategory {
  unlocked: number; total: number; skills: WorkshopSkill[]; next_unlock: UnlockTarget | null;
}
export interface WorkshopRecent {
  ts: string | null; id: string | null; name: string; category: StateCategory | null;
  level: number | null; price: number | null;
}
export interface FleetStateWorkshop {
  totals: Record<StateCategory, number>;
  categories: Record<StateCategory, WorkshopCategory>;
  recent: WorkshopRecent[];
}
export interface FleetStateCards {
  slots: { equipped: number | null; capacity: number | null; next_slot_gems: number | null };
  items: { name: string; level: number | null; copies: number | null }[];
  gems_invested: number | null;
  recent: { ts: string | null; name: string; gems: number | null }[];
}
export interface LabJob { id: string; name: string; to_level: number | null; completes_at: string }
export interface LabLevelRow { id: string; name: string; level: number | null; next_cost: number | null }
export interface FleetStateLabs {
  slots: number | null; running: LabJob[]; levels: LabLevelRow[]; next: UnlockTarget | null;
  recent: { ts: string | null; name: string; price: number | null }[];
}
export interface FleetStateRunUpgrades {
  scope: "current" | "last"; total: number; by_category: Record<StateCategory, number>;
  items: { id: string; name: string; category: StateCategory | null; levels: number }[];
}
export interface FleetStateRun {
  tier: number | null; wave: number | null; coins: number | null; duration_s: number | null;
  ended_at: string | null; abandoned: boolean;
}
export interface FleetStateAccount {
  id: string; name: string; serial: string | null; online: boolean;
  stale_seconds: number | null; scan: number | null; error: string | null;
  bot: FleetStateBot; battle: FleetStateBattle | null; balances: FleetStateBalances | null;
  decision: FleetStateDecision | null; workshop: FleetStateWorkshop | null;
  cards: FleetStateCards | null; labs: FleetStateLabs | null;
  run_upgrades: FleetStateRunUpgrades | null; runs: FleetStateRun[] | null;
}
export interface FleetStatePayload { generated_at: string; accounts: FleetStateAccount[] }

/** What decides whether a column has to re-render: a new scan, or a change
 * in whether the worker answers. */
export function accountKey(account: FleetStateAccount): string {
  return JSON.stringify([account.scan, account.online, account.stale_seconds, account.error]);
}

/** The next accounts, with every unchanged account swapped for the previous
 * object - so a memoized column sees the same prop and skips its render. */
export function reuseUnchanged(previous: readonly FleetStateAccount[] | null,
  next: FleetStateAccount[]): FleetStateAccount[] {
  if (!previous) return next;
  const byId = new Map(previous.map(account => [account.id, account]));
  return next.map(account => {
    const old = byId.get(account.id);
    return old && accountKey(old) === accountKey(account) ? old : account;
  });
}
```

- [ ] **Step 5: Add the fetcher**

In `web/ui/lib/api.ts`, add `import type { FleetStatePayload } from "./fleetState";` directly after `import type { LabsSnapshot } from "./labs";`, and add this directly after the `fetchFleetLabs` line:

```ts
export const fetchFleetState = () => getJson<FleetStatePayload>("/api/fleet/state", { cache: "no-store" }, false);
```

(`scoped = false`, like `fetchFleetLabs`, because this is a fleet-wide read, not one account's.)

- [ ] **Step 6: Implement the formatters** (create `web/ui/app/fleet/state/stateFormat.ts`)

```ts
import { ago, gameNumber } from "@/app/fleet/reroll/workshop/workshopFormat";
import type { StateCategory } from "@/lib/fleetState";

export const DASH = "—";
export const PRICE_UNKNOWN = "price unknown";

const known = (value: number | null | undefined): value is number =>
  value !== null && value !== undefined && Number.isFinite(value);

/** A balance or count in K/M/B/T. Null is a dash, never 0. */
export function amount(value: number | null | undefined): string {
  return known(value) ? gameNumber(value) : DASH;
}

/** A price. Null is a price nobody has observed. */
export function priceText(value: number | null | undefined): string {
  return known(value) ? gameNumber(value) : PRICE_UNKNOWN;
}

/** A whole number with separators: waves, levels, scan counters. */
export function whole(value: number | null | undefined): string {
  return known(value) ? Math.round(value).toLocaleString("en-US") : DASH;
}

/** 1h 36m, 4m 05s, 42s. */
export function span(seconds: number | null | undefined): string {
  if (!known(seconds)) return DASH;
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), rest = s % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(rest).padStart(2, "0")}s`;
  return `${rest}s`;
}

/** Seconds from now until an ISO time, never below zero. */
export function secondsUntil(iso: string, nowMs: number): number {
  const at = Date.parse(iso);
  return Number.isNaN(at) ? 0 : Math.max(0, (at - nowMs) / 1000);
}

/** "5m ago" for an ISO time; a dash when there is none. */
export function agoText(iso: string | null, nowMs: number): string {
  if (!iso) return DASH;
  const at = Date.parse(iso);
  return Number.isNaN(at) ? DASH : ago(Math.max(0, (nowMs - at) / 1000));
}

export const CAT_COLOR: Record<StateCategory, string> = {
  attack: "var(--cat-attack)", defense: "var(--cat-defense)", utility: "var(--cat-utility)",
};
export const CAT_LABEL: Record<StateCategory, string> = {
  attack: "Attack", defense: "Defense", utility: "Utility",
};

/** One accent per emulator, by its position in the fleet. */
export const ACCENTS = [
  "oklch(0.72 0.19 300)", "oklch(0.78 0.13 195)", "oklch(0.74 0.18 350)",
  "oklch(0.72 0.16 265)", "oklch(0.80 0.15 75)", "oklch(0.76 0.16 150)",
] as const;
export const accentAt = (index: number): string => ACCENTS[index % ACCENTS.length];
```

- [ ] **Step 7: Implement the selection** (create `web/ui/app/fleet/state/selection.ts`)

```ts
import type { FleetStateAccount } from "@/lib/fleetState";

export type SelectionMode = "all" | "live" | "custom";
export interface Selection { mode: SelectionMode; ids: string[] }

export const SELECTION_KEY = "fleet-state:selection";
export const OPEN_KEY = "fleet-state:open";
export const DEFAULT_SELECTION: Selection = { mode: "all", ids: [] };

/** The viewer's saved filter. Storage can be missing or throw (private
 * window, blocked site data); then every emulator is shown. */
export function loadSelection(): Selection {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(SELECTION_KEY) ?? "null");
    if (raw && typeof raw === "object") {
      const { mode, ids } = raw as { mode?: unknown; ids?: unknown };
      if ((mode === "all" || mode === "live" || mode === "custom") && Array.isArray(ids)
        && ids.every(id => typeof id === "string")) return { mode, ids };
    }
  } catch { /* unreadable storage: fall back to showing everything */ }
  return DEFAULT_SELECTION;
}

export function saveSelection(selection: Selection): void {
  try { window.localStorage.setItem(SELECTION_KEY, JSON.stringify(selection)); } catch { /* per-viewer convenience only */ }
}

export function loadOpen(): Record<string, boolean> {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(OPEN_KEY) ?? "{}");
    if (raw && typeof raw === "object" && !Array.isArray(raw)) {
      return Object.fromEntries(Object.entries(raw).filter(([, value]) => typeof value === "boolean"));
    }
  } catch { /* unreadable storage: every section takes its default */ }
  return {};
}

export function saveOpen(open: Record<string, boolean>): void {
  try { window.localStorage.setItem(OPEN_KEY, JSON.stringify(open)); } catch { /* per-viewer convenience only */ }
}

export const isLive = (account: FleetStateAccount): boolean => account.online && account.bot.live;

export function visibleAccounts(accounts: FleetStateAccount[], selection: Selection): FleetStateAccount[] {
  if (selection.mode === "all") return accounts;
  if (selection.mode === "live") return accounts.filter(isLive);
  const chosen = new Set(selection.ids);
  return accounts.filter(account => chosen.has(account.id));
}

/** Flip one chip. Choosing every emulator again is "All". */
export function toggleAccount(selection: Selection, id: string, accounts: FleetStateAccount[]): Selection {
  const shown = new Set(visibleAccounts(accounts, selection).map(account => account.id));
  if (shown.has(id)) shown.delete(id); else shown.add(id);
  const ids = accounts.map(account => account.id).filter(key => shown.has(key));
  return ids.length === accounts.length ? DEFAULT_SELECTION : { mode: "custom", ids };
}

/** A remembered choice wins. Otherwise Workshop is open, and Cards and Labs
 * are open only while one or two columns share the screen. */
export function sectionOpen(open: Record<string, boolean>, accountId: string, section: string,
  shownCount: number): boolean {
  const key = `${accountId}:${section}`;
  if (key in open) return open[key];
  return section === "workshop" || shownCount <= 2;
}
```

- [ ] **Step 8: Implement the hook** (create `web/ui/app/fleet/state/useFleetState.ts`)

```ts
"use client";

import { useEffect, useRef, useState } from "react";
import { fetchFleetState } from "@/lib/api";
import { reuseUnchanged, type FleetStateAccount, type FleetStatePayload } from "@/lib/fleetState";

export const POLL_MS = 2000;

export interface FleetStateView {
  payload: FleetStatePayload | null;
  /** Unchanged accounts keep their previous object, so memoized columns skip. */
  accounts: FleetStateAccount[];
  /** Date.now() when each account last changed; drives the "scan age". */
  changedAt: Record<string, number>;
  /** The last poll failed. The previous payload stays on screen. */
  connectionLost: boolean;
}

const EMPTY: FleetStateView = { payload: null, accounts: [], changedAt: {}, connectionLost: false };

/** Polls /api/fleet/state every 2 s, one request at a time. */
export function useFleetState(intervalMs: number = POLL_MS): FleetStateView {
  const [view, setView] = useState<FleetStateView>(EMPTY);
  const previous = useRef<FleetStateAccount[] | null>(null);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    const tick = async (): Promise<void> => {
      try {
        const payload = await fetchFleetState();
        if (!active) return;
        const before = previous.current;
        const accounts = reuseUnchanged(before, payload.accounts);
        previous.current = accounts;
        const now = Date.now();
        setView(old => ({
          payload, accounts, connectionLost: false,
          changedAt: Object.fromEntries(accounts.map(account => [account.id,
            before?.includes(account) ? (old.changedAt[account.id] ?? now) : now])),
        }));
      } catch {
        if (active) setView(old => ({ ...old, connectionLost: true }));
      } finally {
        if (active) timer = window.setTimeout(() => { void tick(); }, intervalMs);
      }
    };
    void tick();
    return () => { active = false; window.clearTimeout(timer); };
  }, [intervalMs]);

  return view;
}
```

- [ ] **Step 9: Add the tokens and the display font**

In `web/ui/app/globals.css`, directly after the `--ws-utility: ...;` line in `:root`, add:

```css

  /* Fleet State hues. Louder than --ws-*: that page is a command view read
     at a glance across several columns. Utility is gold like the in-game tab.
     --live above is reused for the LIVE badge rather than redefined. */
  --cat-attack:  light-dark(oklch(0.55 0.21 25),  oklch(0.66 0.23 25));
  --cat-defense: light-dark(oklch(0.52 0.16 245), oklch(0.68 0.17 245));
  --cat-utility: light-dark(oklch(0.60 0.14 85),  oklch(0.84 0.16 88));
  --cat-cards:   light-dark(oklch(0.55 0.20 325), oklch(0.74 0.19 325));
  --cat-labs:    light-dark(oklch(0.52 0.12 210), oklch(0.78 0.14 210));
```

In `web/ui/app/layout.tsx`, change the font import to `import { Chakra_Petch, Inter, JetBrains_Mono } from "next/font/google";`. Add this directly above `export const metadata`:

```tsx
// Display labels and big numbers on the Fleet State page. Not a variable
// font, so its weights are listed; self-hosted at build time like the others.
const chakraPetch = Chakra_Petch({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-chakra-petch",
  display: "swap",
});
```

and change the `<html>` className to ``className={`${inter.variable} ${jetbrainsMono.variable} ${chakraPetch.variable}`}``.

- [ ] **Step 10: Run the tests and type-check**

Run (in `web/ui`): `npx vitest run lib/fleetState.test.ts app/fleet/state`
Expected: PASS (4 files, 7 tests).
Run: `npx tsc --noEmit -p .`
Expected: no output.

- [ ] **Step 11: Commit**

```bash
git add web/ui/lib/fleetState.ts web/ui/lib/fleetState.test.ts web/ui/lib/api.ts web/ui/app/globals.css web/ui/app/layout.tsx web/ui/app/fleet/state/fixtures.ts web/ui/app/fleet/state/stateFormat.ts web/ui/app/fleet/state/stateFormat.test.ts web/ui/app/fleet/state/selection.ts web/ui/app/fleet/state/selection.test.ts web/ui/app/fleet/state/useFleetState.ts web/ui/app/fleet/state/useFleetState.test.tsx
git commit -F- <<'EOF'
Add the Fleet State data layer, formatters, tokens and display font

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QQCpP6UkpvfBfCfHxKaoqD
EOF
```

---

### Task 7: The account column: CategoryLedger, RunUpgradesChart, battle HUD, buy queue and sections

**Files:**
- Create: `web/ui/app/fleet/state/Tickers.tsx`, `CategoryLedger.tsx`, `RunUpgradesChart.tsx`, `AccountColumn.tsx`, `AccountColumn.test.tsx`, `fleet-state.css`, `layout.tsx`

**Interfaces:**
- Consumes: everything Task 6 produces. `cn` from `@/lib/utils`.
- Produces:
  - `Ago({ since: number })` and `Countdown({ until: string })` from `./Tickers`
  - `CategoryLedger(props)` with `LedgerRow { id, name, level, invested?, spent?, next, maxed?, locked? }`, `LedgerRecent { key, ts, name, detail?, price, color? }`, `LedgerUnlock { name, cost }`. Props: `label`, `head`, `rows`, `nextId`, `color`, `recent`, `nowMs`, `unlock?` (undefined hides it; null means "All unlocked"), `investedHead?: string | null` (null hides the column), `nextHead?`, `nextIsPrice?`
  - `RunUpgradesChart({ data: FleetStateRunUpgrades | null })`
  - `AccountColumn` (memoized) with `AccountColumnProps { account, accent, changedAt, shownCount, open, onToggleSection(key, open) }`. It renders `<article aria-label={id or "id name"}>`. Section keys are `${account.id}:workshop|cards|labs`.
  - CSS classes prefixed `fs-` (root class `fs-root` comes from Task 8's page)

- [ ] **Step 1: Write the failing test** (create `web/ui/app/fleet/state/AccountColumn.test.tsx`)

```tsx
import { render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { FleetStateAccount, FleetStateWorkshop, WorkshopSkill } from "@/lib/fleetState";
import { AccountColumn } from "./AccountColumn";
import { makeAccount } from "./fixtures";

const NOW = Date.parse("2026-09-28T10:00:00Z");

function skill(id: string, name: string, overrides: Partial<WorkshopSkill> = {}): WorkshopSkill {
  return { id, name, level: 480, invested: 2.1e9, bot_spent: 1.7e9, next_cost: 1.2e8, status: "exact", locked: false, ...overrides };
}
const category = (skills: WorkshopSkill[]) => ({ unlocked: skills.length, total: skills.length, skills, next_unlock: null });
const workshop: FleetStateWorkshop = {
  totals: { attack: 480, defense: 0, utility: 0 },
  categories: {
    attack: { ...category([skill("damage", "Damage"), skill("attack_speed", "Attack Speed", { next_cost: null })]),
      next_unlock: { id: "unlock_multishot", name: "Unlock Multishot", cost: null } },
    defense: category([]), utility: category([]),
  },
  recent: [],
};

function show(account: FleetStateAccount, shownCount = 1) {
  return render(<AccountColumn account={account} accent="red" changedAt={NOW} shownCount={shownCount}
    open={{}} onToggleSection={vi.fn()} />);
}

test("the bot's next buy is highlighted and unknown prices never become numbers", () => {
  show(makeAccount({
    online: true, scan: 18442, bot: { screen: "IN_RUN", now: "Shopping", live: true },
    decision: { phase: "buying", reason: "cheapest", upgrade_id: "damage", category: "attack", name: "Damage", cost: null },
    balances: { coins: 3.84e9, gems: null, stones: null }, workshop,
  }));
  const table = screen.getByRole("region", { name: "Attack workshop" });
  const next = within(table).getByText("NEXT").closest("tr")!;
  expect(next).toHaveTextContent("Damage");
  expect(within(table).getByText("Attack Speed").closest("tr")).toHaveTextContent("price unknown");
  expect(screen.getByText("Unlock Multishot").parentElement).toHaveTextContent("price unknown");
  expect(screen.getByText("LIVE")).toBeInTheDocument();
  expect(screen.getByText(/#18,442/)).toBeInTheDocument();
  expect(screen.getByText("3.84B")).toBeInTheDocument();
  expect(screen.getByText("not tracked yet")).toBeInTheDocument();
});

test("an offline worker shows its stale age, its error, and a dash for every unknown", () => {
  show(makeAccount({ stale_seconds: 42, error: "Worker database is missing" }));
  expect(screen.getByText("stale · 42s")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Worker database is missing");
  expect(screen.getByText("OFFLINE")).toBeInTheDocument();
  expect(screen.getAllByText("Unavailable")).toHaveLength(4);  // workshop, cards, labs, runs
  expect(screen.getByText("No run recorded yet")).toBeInTheDocument();
});

test("a new account with zero runs says so instead of inventing a wave", () => {
  show(makeAccount({ online: true, bot: { screen: "MAIN_MENU", now: null, live: false }, runs: [] }));
  expect(screen.getByText("No battle")).toBeInTheDocument();
  expect(screen.getAllByText("No runs yet")).toHaveLength(2);  // HUD last run, and the runs table
});

test("a lab job that finishes between polls reads done, not a negative timer", () => {
  show(makeAccount({ labs: { slots: 2, levels: [], next: null, recent: [],
    running: [{ id: "labs.damage", name: "Damage", to_level: 12, completes_at: "2020-01-01T00:00:00Z" }] } }));
  expect(screen.getByText("done")).toBeInTheDocument();
  expect(screen.getByText("1 slot free")).toBeInTheDocument();
});

test("cards and labs start open with one column and closed with three", () => {
  const account = makeAccount({ id: "Air_1" });
  const first = show(account, 1);
  const titles = () => ["Cards", "Labs"].map(title => screen.getByText(title).closest("details")!);
  expect(titles().map(d => d.open)).toEqual([true, true]);
  first.unmount();
  show(account, 3);
  expect(titles().map(d => d.open)).toEqual([false, false]);
  expect(screen.getByText("Workshop").closest("details")!.open).toBe(true);
});
```

- [ ] **Step 2: Run the test and confirm it fails**

Run (in `web/ui`): `npx vitest run app/fleet/state/AccountColumn.test.tsx`
Expected: FAIL with `Failed to resolve import "./AccountColumn"`.

- [ ] **Step 3: Implement the tickers** (create `web/ui/app/fleet/state/Tickers.tsx`)

```tsx
"use client";

import { useEffect, useState } from "react";
import { secondsUntil, span } from "./stateFormat";

/** Re-renders only itself once a second, so a ticking clock never
 * re-renders the memoized column around it. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/** "12s ago" since a Date.now() timestamp. */
export function Ago({ since }: { since: number }): React.JSX.Element {
  const now = useNow();
  return <span>{span((now - since) / 1000)} ago</span>;
}

/** Time left until an ISO completion time; "done" once it has passed. */
export function Countdown({ until }: { until: string }): React.JSX.Element {
  const now = useNow();
  const left = secondsUntil(until, now);
  return <span className="fs-mono">{left > 0 ? span(left) : "done"}</span>;
}
```

- [ ] **Step 4: Implement the shared ledger** (create `web/ui/app/fleet/state/CategoryLedger.tsx`)

```tsx
import { cn } from "@/lib/utils";
import { agoText, amount, DASH, priceText, whole } from "./stateFormat";

export interface LedgerRow {
  id: string; name: string; level: number | null;
  invested?: number | null; spent?: number | null;
  next: number | null; maxed?: boolean; locked?: boolean;
}
export interface LedgerRecent {
  key: string; ts: string | null; name: string; detail?: string | null;
  price: number | null; color?: string;
}
export interface LedgerUnlock { name: string; cost: number | null }

/** The shared Workshop, Cards and Labs table: level, invested, next, with the
 * bot's NEXT row highlighted, the next unlock and a scrolling purchase list. */
export function CategoryLedger({
  label, head, rows, nextId, color, recent, nowMs,
  unlock, investedHead = "Invested", nextHead = "Next", nextIsPrice = true,
}: {
  label: string; head: string; rows: LedgerRow[]; nextId: string | null; color: string;
  recent: LedgerRecent[]; nowMs: number;
  /** Undefined hides the line; null means every unlock is owned. */
  unlock?: LedgerUnlock | null;
  /** Null hides the column. */
  investedHead?: string | null; nextHead?: string; nextIsPrice?: boolean;
}): React.JSX.Element {
  const columns = investedHead === null ? 3 : 4;
  return (
    <div className="fs-ledger" style={{ "--c": color } as React.CSSProperties}>
      <div className="fs-tw" role="region" aria-label={label} tabIndex={0}>
        <table>
          <thead>
            <tr>
              <th scope="col">{head}</th>
              <th scope="col" className="n">Lv</th>
              {investedHead !== null && <th scope="col" className="n">{investedHead}</th>}
              <th scope="col" className="n">{nextHead}</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={columns} className="fs-dim">Nothing read yet</td></tr>}
            {rows.map(row => {
              const next = row.id === nextId;
              return (
                <tr key={row.id} className={cn(next && "next", row.locked && "locked")}>
                  <td>{row.name}{next && <span className="fs-tn">NEXT</span>}</td>
                  <td className="n">{row.level === null ? DASH : whole(row.level)}</td>
                  {investedHead !== null && (
                    <td className="n fs-dim" title={row.spent ? `Bot spent ${amount(row.spent)}` : undefined}>
                      {amount(row.invested)}
                    </td>)}
                  <td className="n">
                    {row.locked ? <span className="fs-dim">locked</span>
                      : row.maxed ? <span className="fs-dim">MAX</span>
                        : nextIsPrice ? priceText(row.next) : amount(row.next)}
                  </td>
                </tr>);
            })}
          </tbody>
        </table>
      </div>
      {unlock !== undefined && (
        <div className="fs-unlock">
          {unlock === null ? "All unlocked" : <>Next unlock <b>{unlock.name}</b><span className="fs-unlock-cost">{priceText(unlock.cost)}</span></>}
        </div>)}
      <div className="fs-rh">Recent purchases</div>
      {recent.length === 0 ? <p className="fs-none">None recorded</p> : (
        <ol className="fs-recent" tabIndex={0} aria-label={`${label} recent purchases`}>
          {recent.map(item => (
            <li key={item.key}>
              <span className="ago">{agoText(item.ts, nowMs)}</span>
              <i className="cd" style={{ background: item.color ?? color }} aria-hidden="true" />
              <span className="rn">{item.name}{item.detail ? <small> {item.detail}</small> : null}</span>
              <span className="n">{priceText(item.price)}</span>
            </li>))}
        </ol>)}
    </div>);
}
```

- [ ] **Step 5: Implement the run upgrades chart** (create `web/ui/app/fleet/state/RunUpgradesChart.tsx`)

```tsx
import { STATE_CATEGORIES, type FleetStateRunUpgrades } from "@/lib/fleetState";
import { CAT_COLOR, CAT_LABEL, whole } from "./stateFormat";

/** Total, the attack/defense/utility split and the top 8 upgrades as bars. */
export function RunUpgradesChart({ data }: { data: FleetStateRunUpgrades | null }): React.JSX.Element {
  if (data === null) return <p className="fs-none">No run recorded yet</p>;
  const top = data.items.slice(0, 8);
  const max = Math.max(1, ...top.map(item => item.levels));
  return (
    <div className="fs-upg">
      <div className="fs-upt"><b>{whole(data.total)}</b><small>levels bought {data.scope === "current" ? "this run" : "last run"}</small></div>
      {data.total > 0 && (
        <div className="fs-split" role="img"
          aria-label={STATE_CATEGORIES.map(c => `${CAT_LABEL[c]} ${data.by_category[c]}`).join(", ")}>
          {STATE_CATEGORIES.map(c => <i key={c} style={{ flex: data.by_category[c], background: CAT_COLOR[c] }} />)}
        </div>)}
      <div className="fs-leg">
        {STATE_CATEGORIES.map(c => (
          <span key={c}><i style={{ background: CAT_COLOR[c] }} aria-hidden="true" />{CAT_LABEL[c]} <b>{data.by_category[c]}</b></span>))}
      </div>
      {top.map(item => (
        <div className="fs-ub" key={item.id}>
          <span>{item.name}</span>
          <div className="b"><i style={{ width: `${(item.levels / max) * 100}%`, background: item.category ? CAT_COLOR[item.category] : "var(--muted-foreground)" }} /></div>
          <em>{item.levels}</em>
        </div>))}
    </div>);
}
```

- [ ] **Step 6: Implement the column** (create `web/ui/app/fleet/state/AccountColumn.tsx`)

```tsx
"use client";

import { memo, useState } from "react";
import { cn } from "@/lib/utils";
import {
  STATE_CATEGORIES, type FleetStateAccount, type FleetStateCards, type FleetStateDecision,
  type FleetStateLabs, type FleetStateWorkshop, type StateCategory,
} from "@/lib/fleetState";
import { CategoryLedger } from "./CategoryLedger";
import { RunUpgradesChart } from "./RunUpgradesChart";
import { sectionOpen } from "./selection";
import { agoText, amount, CAT_COLOR, CAT_LABEL, DASH, priceText, span, whole } from "./stateFormat";
import { Ago, Countdown } from "./Tickers";

export interface AccountColumnProps {
  account: FleetStateAccount;
  accent: string;
  /** Date.now() when this account last changed; also "now" for relative times. */
  changedAt: number;
  shownCount: number;
  open: Record<string, boolean>;
  onToggleSection: (key: string, open: boolean) => void;
}

function Section({ title, value, color, open, onToggle, children }: {
  title: string; value?: string; color: string; open: boolean;
  onToggle: (open: boolean) => void; children: React.ReactNode;
}): React.JSX.Element {
  return (
    <details className="fs-sec" open={open} style={{ "--sc": color } as React.CSSProperties}
      onToggle={event => { const now = event.currentTarget.open; if (now !== open) onToggle(now); }}>
      <summary className="fs-sh">{title}{value !== undefined && <span className="fs-sh-v">{value}</span>}</summary>
      <div className="fs-sb">{children}</div>
    </details>);
}

const Unavailable = (): React.JSX.Element => <p className="fs-none">Unavailable</p>;

function BattleHud({ account, nowMs }: { account: FleetStateAccount; nowMs: number }): React.JSX.Element {
  const battle = account.battle;
  if (battle !== null) {
    const pct = battle.wave !== null && battle.best_wave ? Math.min(100, (battle.wave / battle.best_wave) * 100) : null;
    return (
      <div className="fs-hud on">
        <div className="fs-wave"><small>Wave</small><b>{whole(battle.wave)}</b>{battle.tier !== null && <span className="fs-tier">T{battle.tier}</span>}</div>
        <div className="fs-kvs">
          <div className="fs-kv"><small>Cash</small><b className="fs-cash">{battle.cash === null ? DASH : `$${amount(battle.cash)}`}</b></div>
          <div className="fs-kv"><small>Elapsed</small><b>{span(battle.elapsed_s)}</b></div>
        </div>
        <div className="fs-best">
          {pct === null ? <small>No best wave on this tier yet</small> : <>
            <div className="fs-pb"><i style={{ width: `${pct}%` }} /></div>
            <small>vs best T{battle.tier} wave {whole(battle.best_wave)} · {pct.toFixed(0)}%</small></>}
        </div>
      </div>);
  }
  const last = account.runs?.[0];
  return (
    <div className="fs-hud">
      <div className="fs-wave"><small>{account.bot.screen === "GAME_OVER" ? "Run ended" : "No battle"}</small><b className="fs-dim">{DASH}</b></div>
      <div className="fs-kv"><small>Last run</small>
        <b className="fs-small">{last ? `T${last.tier ?? "?"} · W${whole(last.wave)} · ${amount(last.coins)} · ${agoText(last.ended_at, nowMs)}` : "No runs yet"}</b></div>
    </div>);
}

function QueueRow({ kind, color, name, detail, cost }: {
  kind: string; color: string; name: string; detail?: string; cost?: number | null;
}): React.JSX.Element {
  return (
    <div className="fs-q" style={{ "--c": color } as React.CSSProperties}>
      <span className="qk">{kind}</span>
      <span className="qn">{name}{detail ? <small> · {detail}</small> : null}</span>
      <span className="n">{cost === undefined ? "" : priceText(cost)}</span>
    </div>);
}

function BuyQueue({ decision, labs, cards }: {
  decision: FleetStateDecision | null; labs: FleetStateLabs | null; cards: FleetStateCards | null;
}): React.JSX.Element {
  return (
    <div className="fs-queue">
      <div className="fs-qh">Bot buy queue</div>
      <QueueRow kind="Autopilot" color={decision?.category ? CAT_COLOR[decision.category] : "var(--primary)"}
        name={decision ? (decision.name ?? decision.phase) : "No decision yet"}
        detail={decision?.reason || undefined} cost={decision ? decision.cost : undefined} />
      {labs?.next && <QueueRow kind="Labs" color="var(--cat-labs)" name={labs.next.name} cost={labs.next.cost} />}
      {cards && cards.slots.capacity !== null && (
        <QueueRow kind="Cards" color="var(--cat-cards)" name={`Slot ${cards.slots.capacity + 1}`} cost={cards.slots.next_slot_gems} />)}
    </div>);
}

function WorkshopBody({ workshop, decision, nowMs }: {
  workshop: FleetStateWorkshop; decision: FleetStateDecision | null; nowMs: number;
}): React.JSX.Element {
  const [tab, setTab] = useState<StateCategory>(decision?.category ?? "attack");
  const category = workshop.categories[tab];
  return (<>
    <div className="fs-split" aria-hidden="true">
      {STATE_CATEGORIES.map(c => <i key={c} style={{ flex: workshop.totals[c], background: CAT_COLOR[c] }} />)}
    </div>
    <div className="fs-tabs" role="tablist" aria-label="Workshop category">
      {STATE_CATEGORIES.map(c => (
        <button key={c} type="button" role="tab" aria-selected={c === tab}
          className={cn("fs-tab", decision?.category === c && "hn")}
          style={{ "--c": CAT_COLOR[c] } as React.CSSProperties} onClick={() => setTab(c)}>
          <span>{CAT_LABEL[c]}</span><b>{whole(workshop.totals[c])}</b>
          <small>{workshop.categories[c].unlocked}/{workshop.categories[c].total} unlocked</small>
        </button>))}
    </div>
    <CategoryLedger label={`${CAT_LABEL[tab]} workshop`} head="Skill" color={CAT_COLOR[tab]} nowMs={nowMs}
      nextId={decision?.category === tab ? decision.upgrade_id : null} unlock={category.next_unlock}
      rows={category.skills.map(skill => ({
        id: skill.id, name: skill.name, level: skill.level, invested: skill.invested,
        spent: skill.bot_spent, next: skill.next_cost, maxed: skill.status === "maxed", locked: skill.locked,
      }))}
      recent={workshop.recent.map((item, index) => ({
        key: `${item.ts}-${index}`, ts: item.ts, name: item.name,
        detail: item.level === null ? null : `→ ${item.level}`, price: item.price,
        color: item.category ? CAT_COLOR[item.category] : undefined,
      }))} />
  </>);
}

function LabsBody({ labs, nowMs }: { labs: FleetStateLabs; nowMs: number }): React.JSX.Element {
  const free = labs.slots === null ? null : Math.max(0, labs.slots - labs.running.length);
  return (<>
    <ul className="fs-timers">
      {labs.running.map(job => (
        <li key={job.id}><div className="tr"><span>{job.name} <small>→ Lv {job.to_level ?? "?"}</small></span><Countdown until={job.completes_at} /></div></li>))}
      {free !== null && free > 0 && <li className="free">{free} slot{free === 1 ? "" : "s"} free{labs.next ? ` · next ${labs.next.name}` : ""}</li>}
      {labs.slots === null && <li className="fs-dim">Slots not counted yet</li>}
    </ul>
    <CategoryLedger label="Labs" head="Lab" color="var(--cat-labs)" nowMs={nowMs} investedHead={null}
      nextId={labs.next?.id ?? null}
      rows={labs.levels.map(row => ({ id: row.id, name: row.name, level: row.level, next: row.next_cost }))}
      recent={labs.recent.map((item, index) => ({ key: `${item.ts}-${index}`, ts: item.ts, name: item.name, price: item.price }))} />
  </>);
}

function CardsBody({ cards, nowMs }: { cards: FleetStateCards; nowMs: number }): React.JSX.Element {
  return (<>
    <p className="fs-note">Gems invested <b>{amount(cards.gems_invested)}</b> · next slot {priceText(cards.slots.next_slot_gems)}</p>
    <CategoryLedger label="Cards" head="Card" color="var(--cat-cards)" nowMs={nowMs} investedHead={null}
      nextHead="Copies" nextIsPrice={false} nextId={null}
      rows={cards.items.map(item => ({ id: item.name, name: item.name, level: item.level, next: item.copies }))}
      recent={cards.recent.map((item, index) => ({ key: `${item.ts}-${index}`, ts: item.ts, name: item.name, price: item.gems }))} />
  </>);
}

function AccountColumnView({ account, accent, changedAt, shownCount, open, onToggleSection }: AccountColumnProps): React.JSX.Element {
  const live = account.online && account.bot.live;
  const screen = account.bot.screen ?? (account.online ? "UNKNOWN" : "OFFLINE");
  const toggle = (section: string) => (value: boolean) => onToggleSection(`${account.id}:${section}`, value);
  const isOpen = (section: string) => sectionOpen(open, account.id, section, shownCount);
  const workshop = account.workshop, cards = account.cards, labs = account.labs;
  const total = workshop ? workshop.totals.attack + workshop.totals.defense + workshop.totals.utility : null;
  const maxWave = Math.max(1, ...(account.runs ?? []).map(run => run.wave ?? 0));
  return (
    <article className="fs-acct" style={{ "--acc": accent } as React.CSSProperties}
      aria-label={account.name === account.id ? account.id : `${account.id} ${account.name}`}>
      <header className="fs-ah">
        <div className="fs-ah-row">
          <span className="fs-emu">{account.id.toUpperCase()}</span>
          {account.name !== account.id && <h2>{account.name}</h2>}
          {live && <span className="fs-live"><i aria-hidden="true" />LIVE</span>}
          {!account.online && <span className="fs-stale">{account.stale_seconds === null ? "offline" : `stale · ${span(account.stale_seconds)}`}</span>}
          <span className="fs-ser">{account.serial ?? DASH}</span>
        </div>
        {account.error && <p className="fs-error" role="status">{account.error}</p>}
        <dl className="fs-bot">
          <div><dt>Screen</dt><dd><span className="fs-scr" data-screen={screen}>{screen}</span></dd></div>
          <div><dt>Now</dt><dd>{account.bot.now ?? DASH}</dd></div>
          <div><dt>Scan</dt><dd className="fs-mono fs-dim">{account.scan === null ? DASH : `#${whole(account.scan)}`} · <Ago since={changedAt} /></dd></div>
        </dl>
      </header>
      <div className="fs-top3">
        <BattleHud account={account} nowMs={changedAt} />
        <div className="fs-bal">
          <div className="fs-kv"><small>Coins</small><b className="fs-coin">{amount(account.balances?.coins)}</b></div>
          <div className="fs-kv"><small>Gems</small><b className="fs-gem">{amount(account.balances?.gems)}</b></div>
          <div className="fs-kv"><small>Stones</small><b>{DASH}</b><small className="fs-hint">not tracked yet</small></div>
        </div>
        <BuyQueue decision={account.decision} labs={labs} cards={cards} />
      </div>
      <div className={cn("fs-flow", shownCount <= 2 && "wide")}>
        <Section title="Workshop" value={total === null ? undefined : `${whole(total)} lv`} color="var(--cat-utility)"
          open={isOpen("workshop")} onToggle={toggle("workshop")}>
          {workshop ? <WorkshopBody workshop={workshop} decision={account.decision} nowMs={changedAt} /> : <Unavailable />}
        </Section>
        <Section title="Cards" value={cards ? `${cards.slots.equipped ?? "?"}/${cards.slots.capacity ?? "?"} slots` : undefined}
          color="var(--cat-cards)" open={isOpen("cards")} onToggle={toggle("cards")}>
          {cards ? <CardsBody cards={cards} nowMs={changedAt} /> : <Unavailable />}
        </Section>
        <Section title="Labs" value={labs ? `${labs.running.length}/${labs.slots ?? "?"} running` : undefined}
          color="var(--cat-labs)" open={isOpen("labs")} onToggle={toggle("labs")}>
          {labs ? <LabsBody labs={labs} nowMs={changedAt} /> : <Unavailable />}
        </Section>
        <section className="fs-sec" style={{ "--sc": "var(--live)" } as React.CSSProperties}>
          <div className="fs-sh">In-run upgrades</div>
          <div className="fs-sb"><RunUpgradesChart data={account.run_upgrades} /></div>
        </section>
        <section className="fs-sec" style={{ "--sc": "var(--primary)" } as React.CSSProperties}>
          <div className="fs-sh">Last 5 runs</div>
          <div className="fs-sb">
            {account.runs === null ? <Unavailable /> : account.runs.length === 0 ? <p className="fs-none">No runs yet</p> : (
              <div className="fs-tw" role="region" aria-label={`${account.id} last runs`} tabIndex={0}>
                <table className="fs-runs">
                  <thead><tr><th scope="col">Tier</th><th scope="col" className="n">Wave</th><th scope="col" className="n">Coins</th><th scope="col" className="n">Time</th><th scope="col" className="n">Ended</th></tr></thead>
                  <tbody>
                    {account.runs.map((run, index) => (
                      <tr key={`${run.ended_at}-${index}`}>
                        <td className="tier">T{run.tier ?? "?"}</td>
                        <td className="n"><span className="wb" style={{ "--w": `${((run.wave ?? 0) / maxWave) * 100}%` } as React.CSSProperties}>{whole(run.wave)}</span></td>
                        <td className="n">{amount(run.coins)}</td>
                        <td className="n">{span(run.duration_s)}</td>
                        <td className="n fs-dim">{run.abandoned ? "abandoned" : agoText(run.ended_at, changedAt)}</td>
                      </tr>))}
                  </tbody>
                </table>
              </div>)}
          </div>
        </section>
      </div>
    </article>);
}

/** One emulator's column. Memoized: it re-renders when its account object
 * changes, which useFleetState only allows on a new scan. */
export const AccountColumn = memo(AccountColumnView);
```

- [ ] **Step 7: Add the column stylesheet and the layout that loads it**

Create `web/ui/app/fleet/state/fleet-state.css` (Task 8 appends the bar and grid rules):

```css
/* Fleet State: one column per emulator. Ported from the approved mockup onto
   the Telemetry tokens, so both themes work. Every class is prefixed fs-. */

.fs-root {
  --fs-display: var(--font-chakra-petch), "Rajdhani", ui-sans-serif, system-ui, sans-serif;
  --fs-line-soft: color-mix(in oklch, var(--border) 65%, transparent);
  --fs-cash: light-dark(oklch(0.52 0.15 145), oklch(0.83 0.17 145));
  --fs-coin: light-dark(oklch(0.58 0.13 85), oklch(0.86 0.14 92));
  --fs-gem: var(--cat-cards);
  min-height: 100%;
  font-size: 14px;
  line-height: 1.45;
  overflow-x: clip;
}
.fs-mono, .fs-root .n { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.fs-dim { color: var(--faint-foreground); }
.fs-root small { font-size: 11px; color: var(--muted-foreground); }
.fs-none { margin: 0; padding: 4px 0; font-size: 12px; color: var(--muted-foreground); }

/* ---------- account column ---------- */
.fs-acct {
  --cut: 16px;
  position: relative; min-width: 0;
  background: linear-gradient(180deg, color-mix(in oklch, var(--acc) 12%, var(--card)) 0, var(--card) 220px);
  border: 1px solid var(--border);
  clip-path: polygon(0 0, calc(100% - var(--cut)) 0, 100% var(--cut), 100% 100%, var(--cut) 100%, 0 calc(100% - var(--cut)));
}
.fs-acct::before { content: ""; position: absolute; left: 0; top: 0; height: 3px; width: calc(100% - var(--cut)); background: var(--acc); }
.fs-ah { padding: 14px 14px 10px; }
.fs-ah-row { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 10px; }
.fs-emu { font-family: var(--fs-display); font-weight: 700; font-size: 18px; letter-spacing: .06em; color: var(--acc); }
.fs-ah h2 { font-family: var(--fs-display); font-weight: 700; font-size: 20px; letter-spacing: .04em; margin: 0; text-transform: uppercase; }
.fs-ser { margin-left: auto; font-family: var(--font-mono); font-size: 11px; color: var(--faint-foreground); }
.fs-live {
  display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px;
  font-family: var(--fs-display); font-weight: 700; font-size: 11px; letter-spacing: .16em;
  color: var(--live); background: var(--live-surface); border: 1px solid color-mix(in oklch, var(--live) 50%, transparent);
}
.fs-live i { width: 8px; height: 8px; border-radius: 50%; background: var(--live); animation: fs-pulse 1.4s ease-out infinite; }
@keyframes fs-pulse { 0% { box-shadow: 0 0 0 0 color-mix(in oklch, var(--live) 80%, transparent); } 100% { box-shadow: 0 0 0 9px transparent; } }
.fs-stale { padding: 1px 8px; font-family: var(--font-mono); font-size: 11px; color: var(--warn); border: 1px dashed var(--warn); }
.fs-error { margin: 8px 0 0; padding: 6px 8px; font-size: 12px; color: var(--danger); background: var(--danger-surface); }
.fs-bot { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; margin: 10px 0 0; font-size: 12px; }
.fs-bot div { display: contents; }
.fs-bot dt { color: var(--faint-foreground); font-family: var(--fs-display); text-transform: uppercase; letter-spacing: .1em; font-size: 11px; padding-top: 2px; white-space: nowrap; }
.fs-bot dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.fs-scr { --c: var(--muted-foreground); font-family: var(--font-mono); font-weight: 600; font-size: 12px; color: var(--c); padding: 1px 6px; background: color-mix(in oklch, var(--c) 14%, transparent); border-left: 2px solid var(--c); }
.fs-scr[data-screen="IN_RUN"] { --c: var(--live); }
.fs-scr[data-screen="GAME_OVER"] { --c: var(--warn); }
.fs-scr[data-screen="OFFLINE"] { --c: var(--danger); }

.fs-top3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 1fr)); border-top: 1px solid var(--border); }
.fs-top3 > * { min-width: 0; padding: 12px 14px; border-bottom: 1px solid var(--fs-line-soft); }
.fs-flow { column-gap: 0; }
.fs-flow.wide { columns: 340px; column-rule: 1px solid var(--fs-line-soft); }
.fs-sec { min-width: 0; break-inside: avoid; padding: 0 14px; border-bottom: 1px solid var(--fs-line-soft); }
.fs-sb { padding-bottom: 14px; }
.fs-sh {
  display: flex; align-items: center; gap: 8px; padding: 12px 0; list-style: none; cursor: pointer;
  font-family: var(--fs-display); font-weight: 600; text-transform: uppercase; letter-spacing: .14em; font-size: 12px; color: var(--muted-foreground);
}
.fs-sh::-webkit-details-marker { display: none; }
.fs-sh::before { content: ""; width: 8px; height: 8px; background: var(--sc, var(--primary)); transform: rotate(45deg); }
.fs-sh-v { margin-left: auto; color: var(--foreground); letter-spacing: 0; font-family: var(--font-mono); font-size: 12px; text-transform: none; }
details > .fs-sh::after { content: ""; width: 6px; height: 6px; border-right: 1.5px solid var(--faint-foreground); border-bottom: 1.5px solid var(--faint-foreground); transform: rotate(-45deg); transition: transform .15s; margin-left: 4px; }
details[open] > .fs-sh::after { transform: rotate(45deg); }
section.fs-sec > .fs-sh { cursor: default; }

/* battle HUD, balances, buy queue */
.fs-hud { display: flex; gap: 14px; align-items: flex-end; flex-wrap: wrap; }
.fs-hud.on { background: linear-gradient(90deg, color-mix(in oklch, var(--live) 10%, transparent), transparent 70%); }
.fs-wave small, .fs-kv small { display: block; font-family: var(--fs-display); text-transform: uppercase; letter-spacing: .14em; font-size: 10px; }
.fs-wave b { font-family: var(--fs-display); font-weight: 700; font-size: 40px; line-height: 1; letter-spacing: .02em; font-variant-numeric: tabular-nums; }
.fs-hud.on .fs-wave b { text-shadow: 0 0 18px color-mix(in oklch, var(--live) 45%, transparent); }
.fs-tier { font-family: var(--fs-display); font-weight: 700; font-size: 12px; padding: 1px 6px; margin-left: 6px; color: var(--live-foreground); background: var(--live); vertical-align: 8px; }
.fs-kvs { display: flex; gap: 16px; flex-wrap: wrap; }
.fs-kv b { font-family: var(--font-mono); font-weight: 600; font-size: 16px; }
.fs-kv b.fs-small { font-size: 13px; }
.fs-cash { color: var(--fs-cash); }
.fs-coin { color: var(--fs-coin); }
.fs-gem { color: var(--fs-gem); }
.fs-hint { display: block; }
.fs-best { flex: 1 1 100%; }
.fs-pb { height: 5px; background: var(--fs-line-soft); overflow: hidden; }
.fs-pb i { display: block; height: 100%; background: var(--c, var(--live)); transition: width .6s; }
.fs-bal { display: flex; gap: 6px 18px; flex-wrap: wrap; align-items: baseline; }
.fs-bal .fs-kv b { font-size: 18px; }
.fs-queue { display: grid; gap: 8px; }
.fs-qh { font-family: var(--fs-display); font-weight: 600; text-transform: uppercase; letter-spacing: .14em; font-size: 12px; color: var(--muted-foreground); }
.fs-q { --c: var(--primary); display: grid; grid-template-columns: 70px minmax(0, 1fr) auto; gap: 2px 10px; align-items: center; font-size: 12px; }
.fs-q .qk { font-family: var(--fs-display); text-transform: uppercase; letter-spacing: .1em; font-size: 10px; color: var(--c); }
.fs-q .qn { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* workshop tabs */
.fs-split { display: flex; gap: 2px; height: 6px; margin-bottom: 10px; }
.fs-split i { display: block; min-width: 2px; }
.fs-tabs { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; margin-bottom: 10px; }
.fs-tab { --c: var(--primary); position: relative; text-align: left; background: transparent; border: 1px solid var(--border); border-top: 3px solid color-mix(in oklch, var(--c) 45%, transparent); padding: 6px 8px; min-width: 0; color: inherit; }
.fs-tab span { display: block; font-family: var(--fs-display); font-weight: 600; font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: var(--c); }
.fs-tab b { display: block; font-family: var(--font-mono); font-size: 16px; font-weight: 600; }
.fs-tab small { display: block; font-size: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fs-tab[aria-selected="true"] { background: color-mix(in oklch, var(--c) 14%, transparent); border-color: color-mix(in oklch, var(--c) 60%, transparent); border-top-color: var(--c); }
.fs-tab.hn::after { content: ""; position: absolute; top: 6px; right: 6px; width: 7px; height: 7px; background: var(--c); transform: rotate(45deg); box-shadow: 0 0 8px var(--c); }

/* shared ledger */
.fs-tw { overflow-x: auto; max-width: 100%; }
.fs-root table { width: 100%; border-collapse: collapse; font-size: 12px; }
.fs-root th { font-family: var(--fs-display); font-weight: 600; font-size: 10px; letter-spacing: .12em; text-transform: uppercase; color: var(--faint-foreground); text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--border); white-space: nowrap; }
.fs-root td { padding: 5px; border-bottom: 1px solid var(--fs-line-soft); white-space: nowrap; }
.fs-root th.n, .fs-root td.n { text-align: right; }
.fs-ledger tr.next td { background: color-mix(in oklch, var(--c) 17%, transparent); }
.fs-ledger tr.next td:first-child { box-shadow: inset 3px 0 0 var(--c); }
.fs-ledger tr.next { animation: fs-glow 2.4s ease-in-out infinite; }
.fs-ledger tr.locked td { opacity: .55; }
@keyframes fs-glow { 50% { filter: brightness(1.25); } }
.fs-tn { margin-left: 6px; padding: 0 5px; font: 700 9px/15px var(--fs-display); letter-spacing: .14em; color: var(--background); background: var(--c); vertical-align: 1px; }
.fs-unlock { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 8px 0 2px; padding: 6px 8px; font-size: 12px; border: 1px dashed var(--border); color: var(--muted-foreground); }
.fs-unlock b { color: var(--foreground); font-weight: 600; }
.fs-unlock-cost { margin-left: auto; font-family: var(--font-mono); }
.fs-rh { margin: 12px 0 4px; font-family: var(--fs-display); font-size: 10px; letter-spacing: .14em; text-transform: uppercase; color: var(--faint-foreground); }
.fs-recent { list-style: none; margin: 0; padding: 0; max-height: 150px; overflow-y: auto; border: 1px solid var(--fs-line-soft); font-size: 12px; scrollbar-width: thin; }
.fs-recent li { display: grid; grid-template-columns: 52px 8px 1fr auto; gap: 8px; align-items: center; padding: 4px 8px; border-bottom: 1px solid var(--fs-line-soft); }
.fs-recent .ago { color: var(--faint-foreground); font-size: 11px; font-family: var(--font-mono); }
.fs-recent .cd { width: 8px; height: 8px; transform: rotate(45deg); }
.fs-recent .rn { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fs-note { margin: 0 0 8px; font-size: 12px; color: var(--muted-foreground); }
.fs-note b { color: var(--foreground); font-family: var(--font-mono); }

/* labs timers */
.fs-timers { list-style: none; margin: 0 0 10px; padding: 0; display: grid; gap: 8px; font-size: 12px; }
.fs-timers .tr { display: flex; justify-content: space-between; gap: 8px; }
.fs-timers .free { padding: 6px 8px; border: 1px dashed var(--cat-labs); color: var(--cat-labs); }

/* in-run upgrades and runs */
.fs-upt { display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px; }
.fs-upt b { font-family: var(--fs-display); font-size: 28px; font-weight: 700; line-height: 1; }
.fs-leg { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 11px; color: var(--muted-foreground); margin: 6px 0 10px; }
.fs-leg i { display: inline-block; width: 8px; height: 8px; margin-right: 5px; transform: rotate(45deg); }
.fs-ub { display: grid; grid-template-columns: minmax(0, 120px) 1fr 30px; gap: 8px; align-items: center; font-size: 12px; margin: 3px 0; }
.fs-ub span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted-foreground); }
.fs-ub .b { height: 10px; background: var(--fs-line-soft); }
.fs-ub .b i { display: block; height: 100%; transition: width .5s; }
.fs-ub em { font-style: normal; text-align: right; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.fs-runs td .wb { display: inline-block; padding: 0 4px; background: linear-gradient(90deg, color-mix(in oklch, var(--primary) 35%, transparent) var(--w), transparent var(--w)); }
.fs-runs .tier { font-family: var(--fs-display); font-weight: 700; color: var(--primary); }

@media (max-width: 520px) {
  .fs-wave b { font-size: 34px; }
  .fs-ser { margin-left: 0; flex-basis: 100%; }
}
@media (prefers-reduced-motion: reduce) {
  .fs-root *, .fs-root *::before, .fs-root *::after { animation: none !important; transition: none !important; }
}
```

Create `web/ui/app/fleet/state/layout.tsx`:

```tsx
import "./fleet-state.css";

// The page's stylesheet is imported here rather than by page.tsx, so the page
// stays importable by vitest, which does not run the PostCSS pipeline.
export default function FleetStateLayout({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <>{children}</>;
}
```

- [ ] **Step 8: Run the test and type-check**

Run (in `web/ui`): `npx vitest run app/fleet/state`
Expected: PASS (AccountColumn: 5 tests; the Task 6 files still pass).
Run: `npx tsc --noEmit -p . && npx eslint app/fleet/state`
Expected: no output.

- [ ] **Step 9: Commit**

```bash
git add web/ui/app/fleet/state/Tickers.tsx web/ui/app/fleet/state/CategoryLedger.tsx web/ui/app/fleet/state/RunUpgradesChart.tsx web/ui/app/fleet/state/AccountColumn.tsx web/ui/app/fleet/state/AccountColumn.test.tsx web/ui/app/fleet/state/fleet-state.css web/ui/app/fleet/state/layout.tsx
git commit -F- <<'EOF'
Add the Fleet State account column with its ledgers, HUD and charts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QQCpP6UkpvfBfCfHxKaoqD
EOF
```

---

### Task 8: FleetStateBar and the page shell with filtering

**Files:**
- Create: `web/ui/app/fleet/state/FleetStateBar.tsx`, `page.tsx`, `page.test.tsx`
- Modify: `web/ui/app/fleet/state/fleet-state.css` (append)

**Interfaces:**
- Consumes: `useFleetState`, the selection helpers, `accentAt`, `amount`, `whole`, `DASH` and `AccountColumn`.
- Produces:
  - `FleetStateBar({ accounts, selection, onSelect(next: Selection), connectionLost })`. Chips are `<button aria-pressed>` whose accessible name starts with the account id; the segmented control has buttons "All" and "Only live"; the pill reads `connection lost` (`role="status"`).
  - The default export `FleetStatePage` at route `/fleet/state/`

- [ ] **Step 1: Write the failing test** (create `web/ui/app/fleet/state/page.test.tsx`)

```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { makeAccount, makePayload } from "./fixtures";
import FleetStatePage from "./page";
import { SELECTION_KEY } from "./selection";

const fetchState = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ fetchFleetState: fetchState }));

const live = makeAccount({ id: "Air_1", online: true, scan: 7, bot: { screen: "IN_RUN", now: null, live: true },
  battle: { tier: 3, wave: 120, cash: 10, elapsed_s: 60, best_wave: 200 }, balances: { coins: 1000, gems: 5, stones: null } });
const menu = makeAccount({ id: "Air_2", online: true, scan: 9, bot: { screen: "MAIN_MENU", now: null, live: false },
  balances: { coins: 500, gems: 1, stones: null } });
const columns = () => screen.queryAllByRole("article").map(article => article.getAttribute("aria-label"));

beforeEach(() => { fetchState.mockReset(); window.localStorage.clear(); });
afterEach(() => { vi.restoreAllMocks(); });

test("chips and Only live filter the columns, and the pick is remembered", async () => {
  fetchState.mockResolvedValue(makePayload([live, menu]));
  const view = render(<FleetStatePage />);
  await waitFor(() => expect(columns()).toEqual(["Air_1", "Air_2"]));
  expect(screen.getByText("1.50K")).toBeInTheDocument();  // fleet coins
  expect(screen.getByText("#9")).toBeInTheDocument();      // heartbeat: newest scan

  fireEvent.click(screen.getByRole("button", { name: /Air_1/ }));
  expect(columns()).toEqual(["Air_2"]);
  expect(JSON.parse(window.localStorage.getItem(SELECTION_KEY)!)).toEqual({ mode: "custom", ids: ["Air_2"] });

  fireEvent.click(screen.getByRole("button", { name: "Only live" }));
  expect(columns()).toEqual(["Air_1"]);
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  expect(columns()).toEqual(["Air_1", "Air_2"]);

  fireEvent.click(screen.getByRole("button", { name: /Air_1/ }));
  view.unmount();
  render(<FleetStatePage />);
  await waitFor(() => expect(columns()).toEqual(["Air_2"]));
});

test("deselecting everything explains how to get the columns back", async () => {
  fetchState.mockResolvedValue(makePayload([live]));
  render(<FleetStatePage />);
  await waitFor(() => expect(columns()).toEqual(["Air_1"]));
  fireEvent.click(screen.getByRole("button", { name: /Air_1/ }));
  expect(screen.getByText(/No emulators selected/)).toBeInTheDocument();
});

test("blocked storage still renders every emulator", async () => {
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
  fetchState.mockResolvedValue(makePayload([live, menu]));
  render(<FleetStatePage />);
  await waitFor(() => expect(columns()).toEqual(["Air_1", "Air_2"]));
  fireEvent.click(screen.getByRole("button", { name: /Air_2/ }));
  expect(columns()).toEqual(["Air_1"]);
});

test("a failed first request says the state is unavailable and shows the pill", async () => {
  fetchState.mockRejectedValue(new Error("down"));
  render(<FleetStatePage />);
  await waitFor(() => expect(screen.getByText("connection lost")).toBeInTheDocument());
  expect(screen.getByText(/Fleet state unavailable/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run the test and confirm it fails**

Run (in `web/ui`): `npx vitest run app/fleet/state/page.test.tsx`
Expected: FAIL with `Failed to resolve import "./page"`.

- [ ] **Step 3: Implement the bar** (create `web/ui/app/fleet/state/FleetStateBar.tsx`)

```tsx
"use client";

import type { FleetStateAccount } from "@/lib/fleetState";
import { isLive, toggleAccount, visibleAccounts, type Selection } from "./selection";
import { accentAt, amount, DASH, whole } from "./stateFormat";

/** Sticky header: emulator chips, All / Only live, and the fleet heartbeat. */
export function FleetStateBar({ accounts, selection, onSelect, connectionLost }: {
  accounts: FleetStateAccount[]; selection: Selection;
  onSelect: (next: Selection) => void; connectionLost: boolean;
}): React.JSX.Element {
  const shown = new Set(visibleAccounts(accounts, selection).map(account => account.id));
  const scans = accounts.flatMap(account => account.scan === null ? [] : [account.scan]);
  const scan = scans.length ? Math.max(...scans) : null;
  const coins = accounts.reduce<number | null>(
    (sum, account) => account.balances?.coins == null ? sum : (sum ?? 0) + account.balances.coins, null);
  return (
    <header className="fs-top">
      <div className="fs-top-in">
        <div className="fs-brand-row">
          <h1 className="fs-brand">Fleet<span>{"//"}</span>State</h1>
          {connectionLost && <span className="fs-lost" role="status">connection lost</span>}
          <div className="fs-gstats">
            <span><i key={scan ?? 0} className="fs-beat" aria-hidden="true" />Scan <b>{scan === null ? DASH : `#${whole(scan)}`}</b></span>
            <span><b>{accounts.filter(isLive).length}</b> live</span>
            <span>Fleet coins <b>{amount(coins)}</b></span>
          </div>
        </div>
        <div className="fs-bar" role="toolbar" aria-label="Emulator filter">
          <div className="fs-chips">
            {accounts.map((account, index) => (
              <button key={account.id} type="button" className="fs-chip" aria-pressed={shown.has(account.id)}
                data-state={isLive(account) ? "live" : account.online ? "online" : "offline"}
                style={{ "--acc": accentAt(index) } as React.CSSProperties}
                onClick={() => onSelect(toggleAccount(selection, account.id, accounts))}>
                <i className="sd" aria-hidden="true" />
                <span className="c-id">{account.id}</span>
                <span className="c-s">{isLive(account) && account.battle?.wave != null ? `W${whole(account.battle.wave)}`
                  : account.online ? (account.bot.screen ?? "") : "offline"}</span>
              </button>))}
          </div>
          <div className="fs-seg">
            <button type="button" aria-pressed={selection.mode === "all"} onClick={() => onSelect({ mode: "all", ids: [] })}>All</button>
            <button type="button" aria-pressed={selection.mode === "live"} onClick={() => onSelect({ mode: "live", ids: [] })}>Only live</button>
          </div>
        </div>
      </div>
    </header>);
}
```

- [ ] **Step 4: Implement the page** (create `web/ui/app/fleet/state/page.tsx`)

```tsx
"use client";

import { useCallback, useEffect, useState } from "react";
import { AccountColumn } from "./AccountColumn";
import { FleetStateBar } from "./FleetStateBar";
import { DEFAULT_SELECTION, loadOpen, loadSelection, saveOpen, saveSelection, visibleAccounts, type Selection } from "./selection";
import { accentAt } from "./stateFormat";
import { useFleetState } from "./useFleetState";

export default function FleetStatePage(): React.JSX.Element {
  const { payload, accounts, changedAt, connectionLost } = useFleetState();
  const [selection, setSelection] = useState<Selection>(DEFAULT_SELECTION);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  // Read after mount: storage is per viewer and the export has no server render.
  useEffect(() => { setSelection(loadSelection()); setOpen(loadOpen()); }, []);
  const choose = useCallback((next: Selection) => { setSelection(next); saveSelection(next); }, []);
  const toggleSection = useCallback((key: string, value: boolean) => setOpen(old => {
    const next = { ...old, [key]: value };
    saveOpen(next);
    return next;
  }), []);
  const shown = visibleAccounts(accounts, selection);
  const order = new Map(accounts.map((account, index) => [account.id, index]));

  let body: React.ReactNode;
  if (payload === null) body = <div className="fs-empty">{connectionLost ? "Fleet state unavailable. Retrying…" : "Loading fleet state…"}</div>;
  else if (accounts.length === 0) body = <div className="fs-empty">No emulators in the fleet yet.</div>;
  else if (shown.length === 0) body = <div className="fs-empty">No emulators selected. Pick one above, or press <b>All</b>.</div>;
  else body = shown.map(account => (
    <AccountColumn key={account.id} account={account} accent={accentAt(order.get(account.id) ?? 0)}
      changedAt={changedAt[account.id] ?? 0} shownCount={shown.length} open={open} onToggleSection={toggleSection} />));

  return (
    <div className="fs-root">
      <FleetStateBar accounts={accounts} selection={selection} onSelect={choose} connectionLost={connectionLost} />
      <main className="fs-grid" aria-live="off">{body}</main>
    </div>);
}
```

- [ ] **Step 5: Append the bar and grid styles** (to the end of `web/ui/app/fleet/state/fleet-state.css`)

```css
/* ---------- sticky bar and grid ---------- */
.fs-top { position: sticky; top: 0; z-index: 20; padding: 10px 16px; border-bottom: 1px solid var(--border);
  background: color-mix(in oklch, var(--background) 86%, transparent); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
.fs-top-in { max-width: 1800px; margin: 0 auto; display: flex; flex-direction: column; gap: 10px; }
.fs-brand-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px; }
.fs-brand { margin: 0; font-family: var(--fs-display); font-weight: 700; font-size: 20px; letter-spacing: .08em; text-transform: uppercase; }
.fs-brand span { color: var(--primary); }
.fs-lost { padding: 2px 8px; font-size: 11px; font-weight: 600; color: var(--danger-foreground); background: var(--danger); border-radius: 999px; }
.fs-gstats { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-left: auto; font-size: 12px; color: var(--muted-foreground); }
.fs-gstats b { color: var(--foreground); font-weight: 600; font-family: var(--font-mono); }
.fs-beat { display: inline-block; width: 7px; height: 7px; margin-right: 6px; border-radius: 50%; background: var(--live); vertical-align: 1px; animation: fs-ping .6s ease-out; }
@keyframes fs-ping { 0% { box-shadow: 0 0 0 0 var(--live); } 100% { box-shadow: 0 0 0 9px transparent; } }
.fs-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.fs-chips { display: flex; flex-wrap: wrap; gap: 6px; flex: 1 1 320px; }
.fs-chip { --sc: var(--faint-foreground); display: inline-flex; align-items: center; gap: 7px; padding: 6px 10px 6px 8px; font-size: 12px; color: inherit;
  background: var(--card); border: 1px solid var(--border); clip-path: polygon(0 0, calc(100% - 8px) 0, 100% 8px, 100% 100%, 0 100%);
  transition: background .15s, border-color .15s, opacity .15s; }
.fs-chip[data-state="live"] { --sc: var(--live); }
.fs-chip[data-state="offline"] { --sc: var(--danger); }
.fs-chip[aria-pressed="false"] { opacity: .5; background: transparent; }
.fs-chip[aria-pressed="true"] { border-color: var(--acc); background: color-mix(in oklch, var(--acc) 16%, var(--card)); }
.fs-chip .sd { width: 8px; height: 8px; border-radius: 50%; background: var(--sc); }
.fs-chip .c-id { font-family: var(--fs-display); font-weight: 600; letter-spacing: .06em; text-transform: uppercase; }
.fs-chip .c-s { color: var(--sc); font-size: 11px; font-family: var(--font-mono); }
.fs-seg { display: inline-flex; border: 1px solid var(--border); }
.fs-seg button { background: transparent; border: 0; padding: 6px 11px; color: var(--muted-foreground);
  font-family: var(--fs-display); font-weight: 600; font-size: 12px; letter-spacing: .08em; text-transform: uppercase; }
.fs-seg button + button { border-left: 1px solid var(--border); }
.fs-seg button[aria-pressed="true"] { background: var(--primary); color: var(--primary-foreground); }
.fs-grid { max-width: 1800px; margin: 0 auto; padding: 16px; display: grid; gap: 14px; align-items: start;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr)); }
.fs-empty { grid-column: 1 / -1; text-align: center; padding: 60px 16px; color: var(--muted-foreground); border: 1px dashed var(--border); }
```

- [ ] **Step 6: Run the tests, type-check and lint**

Run (in `web/ui`): `npx vitest run app/fleet/state lib/fleetState.test.ts`
Expected: PASS (6 files, 16 tests).
Run: `npx tsc --noEmit -p . && npx eslint app/fleet/state`
Expected: no output. (Write the brand's slashes as `{"//"}`. A bare `//` inside JSX fails `react/jsx-no-comment-textnodes`.)

- [ ] **Step 7: Commit**

```bash
git add web/ui/app/fleet/state/FleetStateBar.tsx web/ui/app/fleet/state/page.tsx web/ui/app/fleet/state/page.test.tsx web/ui/app/fleet/state/fleet-state.css
git commit -F- <<'EOF'
Add the Fleet State page with emulator filtering and the live bar

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QQCpP6UkpvfBfCfHxKaoqD
EOF
```

---

### Task 9: Sidebar entry, fleet shell on `/fleet/state`, and final verification

**Files:**
- Modify: `web/ui/lib/workspace.ts`, `web/ui/lib/workspace.test.ts`
- Modify: `web/ui/components/Sidebar.tsx`, `web/ui/components/Sidebar.test.tsx`
- Modify: `web/ui/components/AccountShell.tsx`, `web/ui/components/AccountShell.test.tsx`
- Modify: `web/ui/lib/useEventStream.tsx`

**Interfaces:**
- Consumes: the page route `/fleet/state/` from Task 8.
- Produces: `isFleetWorkspacePath(pathname: string): boolean` in `@/lib/workspace`, and the sidebar link `{ href: "/fleet/state/", label: "Fleet State", icon: LayoutGrid }` in `FLEET_GROUPS`.

- [ ] **Step 1: Write the failing tests**

In `web/ui/lib/workspace.test.ts`, change the import to `import { isFleetWorkspacePath, isRerollPath } from "./workspace";` and append:

```ts
test.each(["/fleet/state", "/fleet/state/", "/fleet/reroll/labs/"])("the fleet rail covers %s", pathname => {
  expect(isFleetWorkspacePath(pathname)).toBe(true);
});
test.each(["/", "/fleet/history/", "/fleet/stateful/"])("single mode keeps %s", pathname => {
  expect(isFleetWorkspacePath(pathname)).toBe(false);
});
```

In `web/ui/components/Sidebar.test.tsx`, add `"/fleet/state/"` to the end of the pathname list of the first `test.each` (the "fleet navigation at %s excludes account signals and polling" test). Directly after its `Fleet Live` assertion, add:

```tsx
  expect(screen.getByRole("link", { name: "Fleet State" })).toHaveAttribute("href", "/fleet/state/");
```

Append to `web/ui/components/AccountShell.test.tsx`:

```tsx
test("the fleet state page gets the fleet shell, not the game account picker", () => {
  state.pathname = "/fleet/state/";
  render(<AccountShell><p>fleet columns</p></AccountShell>);
  expect(screen.getByText("fleet columns")).toBeInTheDocument();
  expect(screen.queryByLabelText("Game account")).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run (in `web/ui`): `npx vitest run lib/workspace.test.ts components/Sidebar.test.tsx components/AccountShell.test.tsx`
Expected: FAIL. `isFleetWorkspacePath` is not exported, the sidebar at `/fleet/state/` shows the single-emulator rail (no "Fleet Live" link), and the shell renders the "Game account" picker.

- [ ] **Step 3: Implement**

Append to `web/ui/lib/workspace.ts`:

```ts

/** Pages that use the fleet rail and shell: the reroll workspace, plus the
 * fleet-wide Fleet State page that lives outside it. */
export function isFleetWorkspacePath(pathname: string): boolean {
  return isRerollPath(pathname) || pathname === "/fleet/state" || pathname.startsWith("/fleet/state/");
}
```

In each of `web/ui/components/Sidebar.tsx`, `web/ui/components/AccountShell.tsx` and `web/ui/lib/useEventStream.tsx`, change the `isRerollPath` import to `isFleetWorkspacePath` (from `@/lib/workspace` in the two components, from `./workspace` in the hook), and change the one `isRerollPath(...)` call to `isFleetWorkspacePath(...)`. The local variable keeps its name `reroll`. Leave `lib/fleetRedirect.ts` on `isRerollPath`.

In `web/ui/components/Sidebar.tsx`, add `LayoutGrid` to the `lucide-react` import (alphabetically, after `FlaskConical`), and in `FLEET_GROUPS` add this directly after the `Fleet Live` item:

```tsx
    { href: "/fleet/state/", label: "Fleet State", icon: LayoutGrid },
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run (in `web/ui`): `npx vitest run lib/workspace.test.ts components/Sidebar.test.tsx components/AccountShell.test.tsx lib/useEventStream.test.tsx app/fleet/state lib/fleetState.test.ts`
Expected: PASS.

- [ ] **Step 5: Type-check, lint and build**

Run (in `web/ui`): `npx tsc --noEmit -p .`
Expected: no output.
Run: `npx eslint app/fleet/state lib/fleetState.ts lib/workspace.ts lib/api.ts lib/useEventStream.tsx components/Sidebar.tsx components/AccountShell.tsx app/layout.tsx`
Expected: no output.
Run: `npx next build`
Expected: succeeds, and the route table lists `○ /fleet/state`. (Use `npx next build`, not `npm run build`: the npm script also publishes the export.)

- [ ] **Step 6: Manual check**

Start the fleet dashboard the way you normally do, from this worktree's build (the parent that serves `/api/fleet/*`, with at least one registered worker). Then open `/fleet/state/`:

1. **Desktop (≥1280px):** one column per visible pool member. The heartbeat dot pings and "Scan #…" advances about every 2 s while a worker is scanning. A live worker shows LIVE, its wave, cash, elapsed time and the bar against its tier's best. Stones read "—" with "not tracked yet". Unpriced unlocks and labs read "price unknown".
2. **Chips:** toggle a chip off and reload. The selection survives. "Only live" leaves only IN_RUN workers.
3. **Sections:** with 3 or more columns, Cards and Labs start closed. Open one, reload, and it stays open.
4. **~400px wide** (DevTools device toolbar): one column, chips wrap, no horizontal page scroll, and the ledgers scroll inside their own box.
5. **Offline:** stop one worker's bot. Within a few seconds its column shows "stale · Ns" and OFFLINE, and the other columns keep updating.
6. **Reduced motion:** turn on "Emulate CSS prefers-reduced-motion: reduce". The LIVE pulse, NEXT glow and heartbeat ping stop.

- [ ] **Step 7: Commit**

```bash
git add web/ui/lib/workspace.ts web/ui/lib/workspace.test.ts web/ui/components/Sidebar.tsx web/ui/components/Sidebar.test.tsx web/ui/components/AccountShell.tsx web/ui/components/AccountShell.test.tsx web/ui/lib/useEventStream.tsx
git commit -F- <<'EOF'
Link Fleet State from the fleet rail and give it the fleet shell

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QQCpP6UkpvfBfCfHxKaoqD
EOF
```

---

## Self-review

**Spec coverage.**

| Spec item | Task |
|---|---|
| Route `/fleet/state` with a sidebar entry | 8, 9 |
| Poll every 2 s; a column re-renders only on a new scan | 6 (`useFleetState`, `reuseUnchanged`), 7 (`memo`) |
| Stones "—" with a "not tracked yet" hint | 4 (`stones: None`), 7 |
| Unknown prices read "price unknown" | 6 (`priceText`), 7 |
| "Now" = current activity | 1, 4 (`build_bot`), 7 |
| Architecture: pure builders, read-only DB, 0.2 s status fetch, thread pool | 3, 4, 5 |
| Payload shape | 3, 4, 5 (`test_an_online_worker_becomes_a_full_account_column`), 6 (types) |
| Workshop level, next cost, status; category totals; invested; bot-spent; next unlock; recent 30 | 3 |
| Decision (from `AutopilotDecided`) | 1, 4 |
| Wave in `ScanCompleted` and `BotState` | 1 |
| Tier from the last run; best wave per tier; last 5 runs | 4, 5 |
| In-run upgrades, current vs last | 4, 5 (Deviation 4) |
| Cards: per-card facts, gems invested, next slot price | 2, 4, 5 |
| Labs: levels, jobs, slots, ledger, catalog, next cost null when unknown | 4, 5 |
| Coins and gems from `currency_overview` | 4, 5 (Deviation 9) |
| Bot behaviour unchanged | 1 (only records; `_scan_wave` is read by nothing that decides) |
| FleetStateBar: chips, All / Only live, heartbeat, live count, fleet coins, wrap on mobile, selection in `localStorage` in try/catch | 6, 8 |
| AccountColumn section order 1–10 | 7 |
| CategoryLedger shared by Workshop, Cards and Labs | 7 |
| RunUpgradesChart: total, split bar, top 8 | 7 |
| Grid `minmax(min(100%, 340px), 1fr)`; CSS columns with 1–2 shown; default open/closed; remembered per section | 6 (`sectionOpen`), 7, 8 |
| Tokens, Chakra Petch, tabular K/M/B/T, reduced motion | 6, 7, 8 |
| Error handling: status down, DB missing or locked, builder throws, request fails | 5, 7, 8 |
| Testing list in the spec | 1, 2, 3, 4, 5 (Python); 6–9 (vitest, tsc, `next build`, manual check) |

**Placeholder scan.** No "TBD", "similar to" or unwritten code remains. Every code step carries the full code.

**Type consistency.** Python keys match the TypeScript types field for field (`FleetStateAccount` against `_blank()` and `build_account()`, and each section type against its builder). `useFleetState` returns `changedAt`, which the page passes to `AccountColumn` as `changedAt`. `sectionOpen` keys (`${id}:${section}`) match `AccountColumn`'s `onToggleSection` keys. `fetchFleetState` is the only name the hook and its test mock.

**Review Focus.** All five lines have named tests in their owning tasks, listed in the Review Focus section above.
