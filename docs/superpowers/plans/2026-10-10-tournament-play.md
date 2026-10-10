# Tournament Play Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically use one free tournament entry, buy capped cash and combat upgrades without coins or gems, record the run, and return to farming.

**Architecture:** A bounded tournament controller owns navigation and a durable attempt journal owns recovery. Tournament purchases use an effective runtime policy that takes precedence over farming/fleet progression. A stored tournament flag separates outcomes from farm/milestone statistics without changing SQLite purpose constraints.

**Tech Stack:** Python, existing OCR/OpenCV readers and verified device controls, SQLite, existing event/store/ledger pipeline, Next.js/React strategy and run pages.

**Spec:** `docs/superpowers/specs/2026-09-30-tournament-play-design.md` (approved 2026-10-10).

## Global Constraints

- Never spend gems to enter or retry; one supplied free-ticket run per account per tournament. No ad-supported retries.
- Never buy `coins_per_kill_bonus`, `coins_per_wave` or unlock tiles during tournaments.
- Cash opening requires finite targets, cumulative budget and wave cap. Missing values disable the opening.
- Finish the active farm run before entry; never terminate it automatically.
- Require fresh screen-specific target, wallet, account and lease evidence; at most one verified action per advance.
- Unknown observations stay unknown. Payment prompts or overlays cannot authorize entry.
- Journal entry intent before tapping BATTLE. Pending entry blocks generic BATTLE and RETRY.
- Preserve the user's selected farming profile and concurrent profile edits.
- Tournament outcomes cannot raise farm records or unlock tiers.
- No automatic public-name construction from account IDs. Missing required name means setup required.
- No automatic commits. Show staged changes and request explicit commit approval at completion.
- Only specific changed/new tests and justified neighboring files; Python uses `-p no:allure_pytest` and a 300-second execution timeout.
- Rebuild production UI when Python runtime/API changes; verify the build manifest backend hash.
- Missing real payment/claim captures are evidence gaps. Final prize claiming remains unsupported until captured and tested.

## Review Focus

- A newer unrelated modal overlays a readable ticket page: zero entry/payment taps (Task 2 and Task 4).
- Crash between intent persistence and entry confirmation: reconcile without a second BATTLE (Task 3 and Task 4).
- A user changes the farming profile during a tournament: resume that current profile rather than overwriting it (Task 5).
- Missing tournament ID near a UTC boundary or clock change: do not classify an ambiguous pending attempt as a fresh event (Task 3).
- Missing death rank/coins or final rewards differing from provisional rewards: retain unknown values and never fabricate a claim (Task 5 and Task 6).

## Execution readiness

This plan implements the approved focused entry/purchase/result slice. It does not complete the full tournament autonomy roadmap, which currently has incomplete prerequisites and an explicit dependency gate. Before execution, obtain a user decision to implement this focused feature independently of that gate, or wait for those prerequisites. Do not silently change the roadmap dependencies/status.

- [ ] Inspect attached worktrees and use the using-git-worktrees skill. Create/reuse an isolated feature worktree with a `codex/` branch and no tracking of main/master; leave unrelated working changes untouched.
- [ ] Record the base commit and confirm the existing fixtures, profile/store APIs and source anchors below. Anchors are symbols because active work may shift line numbers.
- [ ] Keep all six tasks in one feature branch, executed sequentially; they share journal, mode and configuration interfaces. Do not represent them as six certified autonomy graph tasks.

## File responsibilities

- Create `tournament_policy.py`: strict configuration and pure runtime rule resolution.
- Create `tournament_screen.py`: immutable fresh-screen readings and pure parsers.
- Create `tournament_store.py`: durable attempt journal and idempotent SQLite transitions.
- Create `tournament_visit.py`: verified UI state machine, entry and return ownership.
- Modify `strategy.py`, `fleet/build_route.py`, `fleet/reroll_progress.py`: profile configuration and override priority.
- Modify `tower_bot.py`, `runs.py`, `combat_context.py`: run identity, startup reconciliation, controller scheduling and completion.
- Modify `db.py`, `events.py`, `sinks/store.py`, `ledger.py`: mode migration, results and reconciled ledger entries.
- Modify farming aggregators in `fleet/reroll_metrics.py`, `fleet/state_records.py`, `web/app.py`: exclude tournament outcomes.
- Modify `web/ui/lib/types.ts`, `web/ui/lib/strategyStudio.ts`, strategy editor pages, run tables/details and ledger pages: expose the supported configuration and results.

## Task 1: Dedicated purchase configuration and resolver

**Files:** Create `tournament_policy.py`, `tests/test_tournament_policy.py`; modify `strategy.py` (`Strategy` serialization/validation), `fleet/build_route.py` (strategy parsing) and their directly affected test functions.

**Interfaces:** Define frozen `OpeningCash(cash_bonus_target: float | None = None, cash_per_wave_target: float | None = None, cash_budget: int | None = None, until_wave: int | None = None)`, `TournamentConfig(enabled: bool = True, public_name: str | None = None, opening_cash: OpeningCash = OpeningCash(), rules: tuple[UpgradeRule, ...] = DEFAULT_COMBAT_RULES, cash_reserve: int = 0, cash_spend_limit_pct: int = 100)`, and `PurchaseCursor(opening_spent: int = 0, growth_index: int = 0)` with strict `from_dict`/`to_dict` validation. Define `DEFAULT_COMBAT_RULES = (UpgradeRule("health"), UpgradeRule("attack_speed"), UpgradeRule("damage"))`; cheap finite survival targets are added only when configured. `resolve_policy(config: TournamentConfig, cursor: PurchaseCursor, observations: Mapping[str, Mapping[str, Any]], combat: Mapping[str, Any]) -> AutopilotPolicy` reuses existing `policy.choose`. `advance_purchase(cursor: PurchaseCursor, upgrade_id: str, verified_cost: int, rules: tuple[UpgradeRule, ...]) -> PurchaseCursor` runs only after purchase proof.

Fleet configuration belongs in `RouteBaseline`, propagated through `StrategyAssignment`, `EffectiveRoute` and `resolve_route`; do not invent a second fleet assignment mechanism. Main configuration must also survive `Strategy.merged` and the live patch API request model in `web/app.py`.

- [ ] Write failing tests, including:

```python
@pytest.mark.parametrize("uid", ["coins_per_wave", "coins_per_kill_bonus", "unlock_cash_bonuses"])
def test_tournament_forbids_coin_rows_and_unlocks(uid: str) -> None:
    with pytest.raises(PolicyError):
        TournamentConfig.from_dict({"rules": [{"upgrade_id": uid}]})

def test_partial_opening_configuration_does_not_buy_cash() -> None:
    cfg = TournamentConfig.from_dict({"opening_cash": {"cash_budget": 100},
                                     "rules": [{"upgrade_id": "health"}]})
    rows = {"cash_bonus": {"value": 1, "price": 10, "status": "available"},
            "health": {"value": 100, "price": 10, "status": "available"}}
    resolved = resolve_policy(cfg, PurchaseCursor(), rows, {"cash": 100, "wave": 1})
    assert choose(resolved, rows, {"cash": 100, "wave": 1}).upgrade_id == "health"
```

- [ ] Run `.venv/bin/python -m pytest -p no:allure_pytest tests/test_tournament_policy.py -q` (300-second timeout); require expected missing-interface failures.
- [ ] Implement strict finite/nonnegative validation, forbid booleans as numbers, constrain percentages, reuse catalog IDs. Allow only cash plus named recovery/skip utility IDs. Require finite targets for non-growth priority rows; rotate uncapped growth rules after confirmed purchases. Filter all coin rows again when resolving runtime policy.

```python
FORBIDDEN = frozenset({"coins_per_wave", "coins_per_kill_bonus"})
# Before feeding rule selection, enforce both opening caps and price evidence.
# A burst must fit the remaining opening budget, not only its first level.
remaining = config.opening_cash.cash_budget - cursor.opening_spent
resolved = dataclasses.replace(base_policy, preset="manual", rules=ordered_rules,
                               max_purchase_price=remaining,
                               burst_price_ceiling=None)
```

`base_policy` is constructed within `resolve_policy` from the config's reserve/percentage; `ordered_rules` contains validated, eligible opening rules followed by finite survival targets and the rotated growth list. Use this price ceiling only during the opening; combat uses its normal price checks. Prefer one-level opening buys until verified burst-total accounting exists.

- [ ] Add tests for exhausted targets/budget/cap, no readable wave, survival pressure when supported, growth rotation, locked/maxed rows, missing cost proof, and main/fleet round trips. A missing config section defaults safely to the dedicated combat policy, never farm economy rules.
- [ ] Re-run this file and only added/changed profile/build-route test functions. No commits during intermediate tasks.

## Task 2: Tournament screen contracts

**Files:** Create `tournament_screen.py`, `tests/test_tournament_screen.py`; use the nine existing `tests/fixtures/tournament/*.png` images and recorded OCR JSON alongside existing fixture conventions.

**Interfaces:** Each `read_*` function takes `(frame: device.Image, boxes: tuple[ocr.TextBox, ...])`. Define frozen readings for menu control, page (`tickets`, `league`, `join_time_left_s`, `tournament_id`, `battle`, `return_to_game`, `conditions`, `entry_is_ticket`, `blocked`), username field/save/close, profile close, buy-ticket cancel, stats (`league`, `wave`, `rank`, `coins`, `ad_coins`, `killed_by`, `ok`). Controls use `account_screens.ControlTarget`; numeric fields are optional. `read_hud_tournament_marker(frame: Image, boxes: tuple[ocr.TextBox, ...]) -> bool` requires the trophy/Tier-plus context, not a plus character elsewhere. `scan(frame: Image, boxes: tuple[ocr.TextBox, ...]) -> TournamentReading` consumes `FrameReads.full()` so it does not repeat OCR. Define frozen `TournamentReading` containing optional menu/page/username/profile/buy-ticket/stats readings, `hud_marker: bool` and `blocked: bool`; precedence prevents entry through any overlay.

- [ ] Record OCR JSON for existing real screenshots with the existing OCR runtime, then assert ticket-1, ticket-0, Copper label, countdown, stats and HUD. Raw private account identifiers stay outside Git or are anonymized without weakening geometry contracts.

```python
def test_missing_ticket_evidence_is_unknown() -> None:
    frame = np.zeros((1920, 1080, 3), dtype=np.uint8)
    boxes = (ocr.TextBox("TOURNAMENT", .99, config.Rect(300, 100, 400, 50)),)
    page = read_page(frame, boxes)
    assert page is None or page.tickets is None
```

- [ ] Run the new reader test file and require its missing-reader failures.
- [ ] Implement exact headings, trusted confidence, uniqueness, geometry and overlay detection consistent with existing screen readers. Never expose a gem/ad action as a safe entry control.
- [ ] Add synthetic negative tests for gem prices, duplicate BATTLE, unrelated overlays, ordinary GAME STATS, normal tier HUD and unreadable count. Label synthetic inputs as contracts, not live captures.
- [ ] Capture the real Buy Ticket frame only through an authorized read-only capture of an already visible prompt; do not spend a ticket/gems to obtain it. Until captured, unknown payment overlays cause zero actions and report recognition required. The final prize reader is deliberately not enabled in this slice.
- [ ] Re-run `tests/test_tournament_screen.py` only.

## Task 3: Durable journal and mode storage

**Files:** Create `tournament_store.py`, `tests/test_tournament_store.py`; modify `db.py` (`connect`, schema probe, run start/finish/list), `events.py` and relevant cases in `sinks/store.py`.

**Interfaces:** Define frozen `Attempt(account_key: str, event_key: str, attempt_id: str, stage: str, league: str | None, tickets_before: int | None, run_id: int | None, growth_index: int = 0, opening_spent: int = 0)`. Journal methods `begin_entry(conn, attempt: Attempt) -> Attempt`, `load_pending(conn, account_key: str) -> Attempt | None`, `transition(conn, attempt_id: str, expected_stage: str, next_stage: str, run_id: int | None = None) -> Attempt`, `save_cursor(conn, attempt_id: str, cursor: PurchaseCursor) -> None` use SQLite transactions. Account/event uniqueness suppresses a second attempt. Runtime persistence completes before device execution; adapt existing persistence ownership without blocking async device I/O.

- [ ] Write migration, uniqueness and recovery failures. Use `sqlite3.connect(":memory:")` or `tmp_path` following current database tests, including an old schema with farm/milestone CHECK.

```python
def test_same_event_cannot_create_a_second_entry() -> None:
    conn = db.connect(":memory:")
    first = Attempt("account", "utc-window", "attempt-a", "entry_pending", "Copper", 1, None)
    assert begin_entry(conn, first).attempt_id == "attempt-a"
    second = dataclasses.replace(first, attempt_id="attempt-b")
    with pytest.raises(DuplicateTournamentAttempt):
        begin_entry(conn, second)
```

Define `DuplicateTournamentAttempt(ValueError)` in `tournament_store.py`; protect stages using conditional SQL updates and explicit conflict errors, not last-write-wins updates.

- [ ] Run new store tests and the specifically added migration tests.
- [ ] Add `runs.tournament INTEGER NOT NULL DEFAULT 0`, journal and tournament result tables using the existing migration path. Preserve existing purpose CHECK and historical rows. Add defaulted tournament context to run events and propagate it through store handlers.
- [ ] Prevent startup `close_abandoned_runs` from abandoning a journal-linked pending tournament before reconciliation. Persist durable cursor/entry status separately from queued analytics events.
- [ ] Cover clock rollback, join timer unreadable, absent event ID, pending intent after a UTC boundary, old read-only worker databases and cursor round trips. An ambiguous pending event blocks re-entry regardless of date.
- [ ] Re-run changed/new store, migration and event-handler tests only.

## Task 4: Entry, reconciliation and return state machine

**Files:** Create `tournament_visit.py`, `tests/test_tournament_visit.py`; use existing verified control methods in `account_collection.py` without broad refactoring.

**Interfaces:** `TournamentVisit(ControlTaps)` exposes `request(now: float | None = None) -> bool`, `active: bool`, `snapshot() -> dict[str, Any]`, `cancel(reason: str, detail: str, now: float | None = None) -> None`, and `advance(*, screen: Image, device: Any, templates: Any, readings: Any, state: str, bus: Any, now: float | None = None, tuning: Any = None) -> CollectionAction | None`. Constructor receives config, journal access and fresh account/lease identity. Journal writes must be acknowledged before the next advance authorizes BATTLE. `owns_navigation: bool` stays true throughout unresolved recovery, including when no action is available.

- [ ] Write sequence tests with fake journal/device and typed page readings. Assert journal order and zero forbidden actions:

```python
def test_entry_commit_precedes_battle(tournament_harness: TournamentHarness) -> None:
    tournament_harness.show_ticket_page(tickets=1, entry_is_ticket=True)
    tournament_harness.advance_until_entry()
    assert tournament_harness.trace.index("entry_intent_committed") < tournament_harness.trace.index("battle_tap")
    assert tournament_harness.trace.count("battle_tap") == 1
```

Create test-only `TournamentHarness` in `tests/test_tournament_visit.py` with `trace: list[str]`, `show_ticket_page`, `advance_until_entry` and fresh-frame identity. Its device records actions; journal records synchronous durable acknowledgments; advance is bounded to 20 frames and fails the test if no entry state is reached. Follow `tests/test_mail_claim.py` for fake control patterns.

- [ ] Run the new state-machine tests, then implement OPEN/SETUP/READ/PREPARE/ENTER/VERIFY/PLAY/RESULT/RETURN and RECOVER states. Each input frame allows at most one verified action, including username typing/Save as separate steps.
- [ ] Test zero/unknown tickets, gem/ad prompts, late-window expiry, unknown overlay, absent public name, profile popup, lost confirmation, and cancellation. Verify no repeated BATTLE after frame-budget expiry.
- [ ] Test crash before tap, after tap, on HUD, death stats and leaderboard; exhausted ticket does not imply another entry. Preserve journal ownership on uncertainty; distinguish safe exit from unresolved account state.
- [ ] Re-run the state-machine test file only.

## Task 5: Runtime wiring, outcomes and farming isolation

**Files:** Modify `tower_bot.py` (`_any_walk_active`, `_start_run`, run transition handling, purchase-policy resolution, generic navigation, startup DB bootstrap), `runs.py`, `combat_context.py`, `fleet/reroll_progress.py`, `db.py`, `ledger.py`, `sinks/store.py`, `fleet/reroll_metrics.py`, `fleet/state_records.py`, `web/app.py`. Create `tests/test_tournament_runtime.py`; extend only relevant functions in `tests/test_db.py`, `tests/test_store_sink.py`, `tests/test_ledger.py`, `tests/test_runs.py`, `tests/test_reroll_progress.py`.

**Interfaces:** `TournamentEntered`, `TournamentResult` events carry attempt/account/event identifiers and observed optional fields. Context on RunStarted/RunEnded has `tournament: bool = False`, league and attempt link. Runtime publishes an entered event only after reconciliation proof; ledger entry and result uniqueness use attempt/event keys. Tournament purchase resolution uses Task 1 and cursor updates require verified `BattlePurchased` evidence.

- [ ] Write integration failures proving OPEN is checked before farm BATTLE, tournament ownership suppresses generic RETRY, and mode is restored before any purchase after restart. Spy on progression policy resolution: it must receive no tournament purchasing authority.
- [ ] Run new runtime tests and only the added existing-file tests.
- [ ] Wire the controller before farming navigation and policy selection. Detect HUD/stats before generic IN_RUN handling. Reconcile startup before abandoned-run cleanup. Preserve run/attempt linkage and end the run once on tournament stats.

```python
if tournament_context.active:
    effective_policy = resolve_policy(tournament_config, purchase_cursor, observations, combat)
else:
    effective_policy = ordinary_battle_policy
# Later: only confirmed purchases advance the tournament cursor and opening spend.
```

`tournament_context`, `purchase_cursor` and config are loaded from the journal/controller in this task; ordinary policy resolution occurs only in the else branch. Never route tournament policy through an existing farm override afterward.

- [ ] Add ticket ledger expenditure only after confirmed entry. Store run payout separately from final prize claims. Unknown wave/rank/coins remain NULL; no `TournamentPrizeClaimed` event is emitted from expected leaderboard rewards.
- [ ] Gate every in-memory and stored farm record/tier transition by `not tournament`. Set tournament normal tier NULL, retain league separately and filter history/pace comparisons. Verify a tournament wave above farm best does not raise farm best or tier unlock state.
- [ ] Test concurrent profile change during tournament followed by restoration of the newly selected profile, provisional/final rank distinction, partial stats and idempotent duplicate result frames.
- [ ] Run focused affected files/functions only. Confirm normal farm start/retry behavior through the directly affected navigation tests.

## Task 6: Configuration UI, run visibility and acceptance

**Files:** Modify `web/ui/lib/types.ts`, `web/ui/lib/strategyStudio.ts`, `web/ui/components/StrategyEditor.tsx`, `web/ui/app/fleet/reroll/strategies/StrategyStudio.tsx`, `web/ui/components/RunTable.tsx`, `web/ui/app/fleet/reroll/runs/RunsTable.tsx`, `web/ui/app/fleet/reroll/runs/RunDetailPanel.tsx`, `web/ui/app/fleet/reroll/runs/runsView.ts`, `web/ui/components/LedgerEntries.tsx`, `web/ui/app/ledger/page.tsx` and `web/ui/app/fleet/reroll/ledger/page.tsx`. Retain existing component boundaries. Extend their existing focused `.test.tsx`/`.test.ts` files.

**Interfaces:** Strategy payload carries Task 1 config; run API carries `tournament`, league and provisional rank. Journal snapshot exposes idle/pending/running/completed/skipped/setup-required state and a concrete reason. Old rows default tournament to false; absent numeric fields remain null.

- [ ] Add failing UI tests: coin IDs cannot be selected; opening controls preserve disabled nulls; saving invalid limits shows field errors; no paid-entry toggle exists. Use the existing page/API mock setup.
- [ ] Add run/ledger tests using explicit mixed rows:

```typescript
const tournamentRow = { ...ordinaryRow, tournament: true, tier: null,
  league: 'Copper', rank: null };
// Render with ordinaryRow and tournamentRow using the existing test helper.
// Select the Tournaments filter: only tournamentRow remains, with a trophy,
// league and an unknown/provisional rank label, never a fabricated zero.
```

`ordinaryRow` is the existing test's complete valid API fixture; preserve all required fields and override only mode fields. Add keyboard/small-screen coverage for new controls and ensure every server default matches client defaults.

- [ ] Implement Tournament controls and statuses, amber tournament rows, trophy/league labels, filters and detail context. Ledger shows only reconciled entries; label final prize automation as unavailable until its real-screen reader is accepted.
- [ ] Run `npm --prefix web/ui run test -- components/StrategyEditor.test.tsx app/fleet/reroll/strategies/StrategyStudio.test.tsx components/RunTable.test.tsx app/fleet/reroll/runs/page.test.tsx app/fleet/reroll/runs/runsView.test.ts components/LedgerEntries.test.tsx` only when those files have been changed; add another exact test path only if its implementation changed. Then run `npm run build --prefix web/ui`; verify `web/static/.build-manifest.json` matches the backend hash.
- [ ] Review the complete branch for entry/payment authorization, restart recovery and policy precedence. Re-run checks only for changes or newly found concerns.
- [ ] On an eligible configured account, use the next supplied free ticket for live acceptance: automatic entry, no gems/coin purchases, result recorded once, resume farming, UI accurate. Do not force paid entry to complete testing. If no free window exists, report live acceptance pending without claiming completion.
- [ ] Stage only relevant source/tests/UI artifacts and sanitized fixtures. Show staged diff summary, focused results and remaining evidence limitations; ask before committing. After approval, verify branch tracking, push explicitly to its feature branch, create and attach the PR. Do not merge without a user request.

## Plan self-review

- Spec coverage: configuration/purchases (1), readings (2), persistence/restart (3), verified entry/return (4), main/fleet policy ownership and farm isolation (5), UI and live proof (6).
- Final-prize claiming, automatic loadout changes and condition optimization remain excluded exactly as specified.
- Interface chain: `TournamentConfig`/`PurchaseCursor` feed policy and journal; readings feed controller; controller context feeds events/runtime/API; UI serializes the same config.
- Review Focus cases are assigned explicitly to their owning tasks.
- This plan does not approve commits, certify a full roadmap task or override its dependency gate.
