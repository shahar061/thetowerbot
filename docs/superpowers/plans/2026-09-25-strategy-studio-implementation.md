# Strategy Studio and Build Route Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give reroll operators a fleet-first, editable Build Route whose preview and live workers agree about Workshop, battle, gem, and lab decisions.

**Architecture:** A coordinator-owned, versioned route is validated and stored atomically. One pure evaluator produces decisions and traces for previews and workers; existing screen readers and purchase executors retain final authority to spend. The UI starts with a side-by-side fleet preview and offers a constrained Flow Builder rather than arbitrary executable graphs.

**Tech Stack:** Python, FastAPI, Pydantic, existing SQLite/account evidence and reroll planner, Next.js 15, React 19, TypeScript, Vitest, pytest.

**Spec:** `docs/superpowers/specs/2026-09-25-strategy-studio-design.md`

## Global Constraints

- Preserve due, affordable Game Speed lab research before Workshop coins; reserve the first 100 gems for lab slot 2 until ownership is confirmed.
- Keep Game Speed in slot 1 until observed maxed; do not rush labs with gems or automate the first Ultimate Weapon choice at Tier 1 Wave 60.
- Preserve account variant level caps, current cheap survival starters, existing value-weighted selection, and generic single-emulator Strategy profiles until an explicit Build Route publish changes a reroll rule.
- A Never Buy Workshop ID must be excluded from starters, cheap fillers, normal ranking, and projections; executor rereads the live screen, wallet, price, and account before every tap.
- Unknown facts are a third condition outcome, not false. Unknown or stale currency, price, identity, or route evidence cannot authorize a purchase.
- Initial route is a compatibility block with present behavior; future gem and lab actions stay `Planned · not automated` until their own screen reader and confirmed executor are available.
- Only focused changed test files/functions run locally. Include `-p no:allure_pytest` in each pytest command; normal CI covers broader suites. Do not commit automatically without the user's approval; never push directly to `main`.

## Review Focus

- Two tabs publish from revision 4: one succeeds and the other receives a 409 with revision 5; Task 4 pins this behavior.
- A registered worker changes accounts while a draft or override is open: the old account's rule has no effect and publish refuses the stale binding; Tasks 1, 4, and 11 pin this behavior.
- A buyer sees a route file halfway through replacement or corrupted on disk: it spends nothing and reports the route error; Task 3 pins this behavior.
- A weighted choice is rescanned repeatedly before purchase confirmation: it selects the same candidate and does not farm rerolls; Task 7 pins this behavior.
- A fleet member is paused, hidden, or missing price evidence: the UI distinguishes pending/unknown from an applied, affordable route; Tasks 5 and 9 pin this behavior.

---

## File map and delivery boundaries

`fleet/build_route.py` owns typed route data, catalog validation, account-bound override resolution, and the compatibility route. `fleet/build_route_store.py` owns atomic persistence, immutable revisions, audit and compare-and-swap publishing. `fleet/build_route_eval.py` owns the pure evidence-to-decision function and trace, including budgets and deterministic weighted choice. `fleet/build_route_runtime.py` owns worker revision loading, acknowledgement, and stable decision sequence. Existing `fleet/reroll_progress.py`, `fleet/reroll_planner.py`, `policy.py`, `tower_bot.py`, `fleet/setup.py`, and `web/app.py` are adapters, not second decision engines.

`web/ui/lib/buildRoute.ts` defines the wire types. `web/ui/app/fleet/reroll/strategies/` holds the fleet preview and Flow Builder components; `web/ui/lib/api.ts` owns requests. The route editor only exposes action types implemented by the worker. Task groups are sequential releases: foundation/preview (1–5), Workshop (6–9), battle (10), account overrides (11), and gem/lab display plus release verification (12).

### Task 1: Typed route, compatibility revision, and validation

**Files:** Create `fleet/build_route.py`, `tests/test_build_route.py`.

**Interfaces:** Produce `RouteDocument.from_dict(raw: Mapping[str, object]) -> RouteDocument`, `RouteDocument.to_dict() -> dict[str, object]`, `RouteDocument.compatibility() -> RouteDocument`, and `resolve_route(route: RouteDocument, worker: str, account_id: str) -> EffectiveRoute`. `RouteDocument` has schema `1`, revision `0` for an unpublished compatibility route, stable rule IDs, a fleet baseline, and `overrides: dict[str, AccountOverride]`. An override carries `account_id` and small patches by rule ID. `EffectiveRoute` has `revision`, `workshop`, `battle`, `gems`, `labs`, and `override_state`. The gem lane holds the 100-gem hard reserve plus an optional spend percentage above that reserve for future supported actions.

- [ ] **Step 1: Write failing model tests.** In `tests/test_build_route.py`, pin round-trip serialization, an unknown catalog ID, duplicate priorities, overlapping inclusive wave intervals, invalid 0/101 percentages, zero weights, dependency cycle, and account replacement. Example: `assert resolve_route(route, "Air_38", "new").override_state == "inactive_account_changed"` after an override bound to `"old"`.

  ```python
  route = RouteDocument.compatibility()
  assert RouteDocument.from_dict(route.to_dict()) == route
  assert resolve_route(route, "Air_38", "new").revision == 0
  ```
- [ ] **Step 2: Run** `pytest tests/test_build_route.py -q -p no:allure_pytest` with a long timeout; expect missing module or failed assertions.
- [ ] **Step 3: Implement the model.** Use frozen dataclasses and explicit parsers. Validate `type(pct) is int and 0 <= pct <= 100`, `type(weight) is int and weight > 0`, all upgrade IDs via `upgrades.by_id`, and `end < next.start` for ordered inclusive phases. Reject unknown keys and cycles during `from_dict`; build `compatibility()` with a `legacy_planner` Workshop block, the current battle policy, the 100-gem reserve, and Game Speed slot 1. Keep stable IDs such as `workshop.default` and `battle.opening` so override patches survive baseline reorder.

  ```python
  if type(spend_pct) is not int or not 0 <= spend_pct <= 100:
      raise ValueError("coin_spend_limit_pct must be an integer from 0 to 100")
  if any(upgrades.by_id(uid) is None for uid in set(priority_ids) | banned_upgrade_ids):
      raise ValueError("unknown Workshop upgrade ID")
  ```
- [ ] **Step 4: Rerun** `pytest tests/test_build_route.py -q -p no:allure_pytest`; expect pass.
- [ ] **Step 5: Stage the two files, show the diff, and obtain the user's existing or fresh commit authorization before** `git commit -m "feat: define versioned reroll build routes"`.

### Task 2: Atomic coordinator store and audit history

**Files:** Create `fleet/build_route_store.py`, `tests/test_build_route_store.py`.

**Interfaces:** Consume Task 1 `RouteDocument`. Produce `BuildRouteStore(root: Path)`, `read() -> RouteDocument`, `publish(draft: RouteDocument, expected_revision: int, actor: str) -> RouteDocument`, `revisions() -> tuple[RouteDocument, ...]`, and `rollback(revision: int, expected_revision: int, actor: str) -> RouteDocument`. Define `RouteConflict(current: RouteDocument)` and `RouteUnavailable(reason: str)`.

- [ ] **Step 1: Write failing tests** for first read returning an unpersisted compatibility route only when no revision was ever published, publish 0→1, immutable history, old revision conflict, rollback creating a new revision, concurrent publisher serialization, and simulated write failure leaving the previous revision readable. A missing current route when history exists is `RouteUnavailable`, never a compatibility reset. Use `tmp_path` and monkeypatch `os.replace` to fail before replacement.

  ```python
  store = BuildRouteStore(tmp_path)
  assert store.read().revision == 0
  saved = store.publish(RouteDocument.compatibility(), 0, "operator")
  assert saved.revision == 1
  with pytest.raises(RouteConflict):
      store.publish(RouteDocument.compatibility(), 0, "other tab")
  ```
- [ ] **Step 2: Run** `pytest tests/test_build_route_store.py -q -p no:allure_pytest`; expect failure.
- [ ] **Step 3: Implement store.** Persist `build-route.json` under the coordinator root and immutable `build-route-history/<revision>.json`; use a process/file lock for read-compare-write, same-directory temporary file, `json.dump(..., sort_keys=True)`, flush + `os.fsync`, `os.replace`, and directory fsync. Validate the entire document again on read. Write each immutable history entry with actor, source/target revisions, timestamp and changed rule IDs before atomically replacing the current route; ignore an orphan entry after failed replacement because the current revision is authoritative. Never recover a corrupt current route by silently writing compatibility rules over it.

  ```python
  with temporary.open("x", encoding="utf-8") as output:
      json.dump(saved.to_dict(), output, sort_keys=True)
      output.flush()
      os.fsync(output.fileno())
  os.replace(temporary, current_path)
  ```
- [ ] **Step 4: Rerun** `pytest tests/test_build_route_store.py -q -p no:allure_pytest`; expect pass.
- [ ] **Step 5: Stage, review diff, and commit only with authorization:** `git commit -m "feat: persist build route revisions atomically"`.

### Task 3: Shared pure evaluator and worker revision safety

**Files:** Create `fleet/build_route_eval.py`, `fleet/build_route_runtime.py`, `tests/test_build_route_eval.py`, `tests/test_build_route_runtime.py`; modify `fleet/reroll_progress.py`.

**Interfaces:** `evaluate(route: EffectiveRoute, facts: RouteFacts, pending: PendingDecision | None) -> RouteEvaluation` returns `account_id`, `decision`, `trace`, `evidence_at`, and `revision`; `RouteEvaluation.unknown(reason: str, facts: RouteFacts) -> RouteEvaluation` is a named blocked result. `RouteFacts` carries verified account ID, worker, screen, best T1 wave, current run/wave, wallet/price observations with timestamps, upgrade rows, purchases, variant, lab-slot-2 ownership, and Game Speed max status. `PendingDecision` carries account ID, revision, visit/run ID, sequence, candidate fingerprint, and chosen ID. `BuildRouteRuntime(root: Path, worker: str, account_id: str)` exposes `current() -> RouteDocument`, `pending() -> PendingDecision | None`, `acknowledge(revision: int, account_id: str) -> None`, `applied_revision() -> int | None`, and `error() -> str | None`; a route read error makes spending unavailable.

- [ ] **Step 1: Write tests** for compatibility evaluation matching `choose_next` on a fixed `RerollFacts`, a missing wallet yielding `Unknown`, an old-account route yielding `Blocked`, stale route data, Wave 60 operator gate, and a corrupt/half-written file causing runtime `RouteUnavailable` and no spend while observation and gameplay continue. Assert the trace names evidence source, age, matched rule, rejected candidates, and variant cap.

  ```python
  evaluation = evaluate(resolve_route(RouteDocument.compatibility(), "Air_38", "a1"), facts, None)
  assert evaluation.decision.upgrade_id == choose_next(legacy_facts).upgrade_id
  assert evaluation.revision == 0
  ```
- [ ] **Step 2: Run** `pytest tests/test_build_route_eval.py tests/test_build_route_runtime.py -q -p no:allure_pytest`; expect failures.
- [ ] **Step 3: Implement evaluator and runtime.** In compatibility mode, call `choose_next` and wrap its decision and projection in a trace; do not duplicate its ranking. Load a complete validated revision at each purchase decision boundary, cache only immutable data, atomically write `workers/<name>/build-route-applied.json` with account ID and revision, and prevent `shopping_policy` from returning an enabled purchase when route loading fails. An offline worker has no acknowledgement.

  ```python
  try:
      route = self.route_runtime.current()
  except RouteUnavailable as exc:
      self.route_error = exc.reason
      return replace(base, enabled=False, workshop=())
  ```
- [ ] **Step 4: Rerun** those two test files and `tests/test_reroll_progress.py` only; expect pass.
- [ ] **Step 5: Stage, review diff, and commit only with authorization:** `git commit -m "feat: share build route evaluation with reroll workers"`.

### Task 4: Route API with compare-and-swap preview and rollback

**Files:** Modify `web/app.py`, `fleet/setup.py`; create `tests/test_build_route_api.py`.

**Interfaces:** `FleetController` exposes `build_route_store() -> BuildRouteStore` and `build_route_preview(draft: RouteDocument) -> dict[str, object]` using the Task 3 evaluator on each worker's latest verified facts. The response contains current/proposed evaluations, changed rule IDs, blockers, fact snapshot time, saved revision and proposed revision for each account. API: `GET /api/fleet/reroll/route`, `POST /api/fleet/reroll/route/preview`, `PUT /api/fleet/reroll/route` with `{expected_revision, actor, route}`, `POST /api/fleet/reroll/route/rollback` with `{expected_revision, actor, revision}`, and `GET /api/fleet/reroll/route/revisions`. Preview must never persist or tap.

- [ ] **Step 1: Add focused FastAPI tests** using the `TestClient` fixture pattern in `tests/test_fleet_setup.py` with `fleet=controller`. Assert GET on no file returns revision 0 compatibility, POST preview returns a current/proposed diff from the same evaluator without creating a file, PUT 0 succeeds, second PUT 0 returns 409 with the new revision, malformed catalog data returns 422, and missing fleet capability returns 503. Use a stale worker/account binding payload and assert 409.

  ```python
  first = client.put("/api/fleet/reroll/route", json={"expected_revision": 0, "actor": "operator", "route": draft})
  assert first.status_code == 200
  assert client.put("/api/fleet/reroll/route", json={"expected_revision": 0, "actor": "operator", "route": draft}).status_code == 409
  ```
- [ ] **Step 2: Run** `pytest tests/test_build_route_api.py -q -p no:allure_pytest`; expect 404/failures.
- [ ] **Step 3: Add narrow request models and handlers.** The core write is `store.publish(RouteDocument.from_dict(body.route), body.expected_revision, body.actor)`; map `RouteConflict` to HTTP 409 including current revision, invalid route to 422, and unavailable store to 503. Resolve registered worker/account bindings through the same `registered_worker` path as `reroll_snapshot`; never trust a client-supplied account ID alone.

  ```python
  @app.put("/api/fleet/reroll/route")
  def publish_build_route(body: BuildRoutePublishRequest) -> dict[str, object]:
      saved = fleet.build_route_store().publish(RouteDocument.from_dict(body.route), body.expected_revision, body.actor)
      return saved.to_dict()
  ```
- [ ] **Step 4: Rerun** `pytest tests/test_build_route_api.py -q -p no:allure_pytest`; expect pass.
- [ ] **Step 5: Stage, review diff, and commit only with authorization:** `git commit -m "feat: expose build route preview and revision API"`.

### Task 5: Truthful fleet preview and decision inspector

**Files:** Modify `fleet/setup.py`, `fleet/reroll_metrics.py`, `web/ui/lib/api.ts`, `web/ui/lib/fleet.ts`, `web/ui/components/Sidebar.tsx`, `web/ui/app/fleet/reroll/strategies/page.tsx`, related page tests; create `web/ui/lib/buildRoute.ts`, `web/ui/app/fleet/reroll/strategies/FleetRoutePreview.tsx`, `web/ui/app/fleet/reroll/strategies/DecisionInspector.tsx`.

**Interfaces:** Extend `RerollMember` with `route_revision_applied`, `route_error`, `workshop_evaluation`, and optional `battle_evaluation`; `RouteEvaluation` wire type mirrors Task 3. `fetchBuildRoute()` and `previewBuildRoute(draft)` use the new API without account-scoped headers.

- [ ] **Step 1: Add UI tests** with three members. Assert stable equal-width columns, one selected decision inspector showing rule/price provenance, a hidden worker omitted, a paused worker marked pending, an account-mismatched plan and missing price marked unavailable, a future Workshop purchase marked `Projected`, and historical battle purchases not labelled live intent.

  ```tsx
  render(<StrategiesPage />);
  expect(screen.getAllByRole("article", { name: /Strategy for/ })).toHaveLength(3);
  expect(screen.getByText("Route revision pending")).toBeInTheDocument();
  expect(screen.queryByText("Live battle intent", { exact: true })).not.toBeInTheDocument();
  ```
- [ ] **Step 2: Run** `npm test -- --run app/fleet/reroll/strategies/page.test.tsx` from `web/ui`; expect failures.
- [ ] **Step 3: Implement fleet preview.** Rename sidebar label to `Strategy Studio` at the same URL. Default to the full fleet and offer a one-emulator filter while keeping the scope selector visible. Put phase, next supported decision, currency gap, lab and gem targets near the top of each card; use `Observed`, `Projected`, `Blocked`, and `Unknown` labels. Show worker-applied revision separately from saved revision; do not mark publish complete while a running worker has not acknowledged. Fetch/compose current revision and member data without synthetic prices or inferred battle intent.

  ```tsx
  const applied = member.account_id === evaluation?.account_id &&
    member.route_revision_applied === savedRoute.revision;
  const label = applied ? "Applied" : member.state === "paused" ? "Pending until next spend" : "Route revision pending";
  ```
- [ ] **Step 4: Run the focused page and Sidebar tests, then `npm run lint -- app/fleet/reroll/strategies components/Sidebar.tsx` and `npm run build`** from `web/ui`; expect pass. The build is a compiler/bundle check, not a broad test suite.
- [ ] **Step 5: Stage, review diff, and commit only with authorization:** `git commit -m "feat: compare live build routes across the fleet"`.

### Task 6: Never Buy, Workshop priority and spend limits

**Files:** Modify `fleet/build_route_eval.py`, `fleet/reroll_planner.py`, `fleet/reroll_progress.py`; create `tests/test_build_route_workshop.py`; modify `tests/test_reroll_planner.py` for targeted regression cases.

**Interfaces:** Route Workshop block has `banned_upgrade_ids: frozenset[str]`, ordered `priority_ids: tuple[str, ...]`, and `coin_spend_limit_pct: int`. `evaluate_workshop(route: EffectiveRoute, facts: RouteFacts, pending: PendingDecision | None) -> RouteEvaluation` returns a single candidate or a reasoned wait. The planner must accept an exclusion set through starter, normal build, cheap filler and projection paths; the live `Shopping` still gets one concrete `ShoppingRule` and a bounded `coin_budget`.

- [ ] **Step 1: Write tests** showing Defense Absolute banned in starter, normal ranking, cheap filler and projection; banning an unlock blocks dependent upgrades with a named blocker; removing from priority does not ban filler; 30% of a 140-coin visit is 42 coins; wallet unknown blocks a percentage-limited buy; live price still overrides catalog estimate.

  ```python
  plan = choose_next(replace(facts, wallet_coins=140), banned_upgrade_ids=frozenset({"defense_absolute"}))
  assert plan.upgrade_id != "defense_absolute"
  assert all(step.upgrade_id != "defense_absolute" for step in project_next(facts, banned_upgrade_ids=frozenset({"defense_absolute"})))
  assert evaluate_workshop(route_with_30_pct, route_facts, None).trace.spend_ceiling == 42
  ```
- [ ] **Step 2: Run** `pytest tests/test_build_route_workshop.py -q -p no:allure_pytest`; expect failures.
- [ ] **Step 3: Implement a single exclusion path.** Pass `banned_upgrade_ids` into `choose_next` and `project_next`, filter before each existing starter/filler/ranking path, and never retry through an unfiltered fallback. For edited routes, rank eligible IDs by explicit priority before unprioritized cheap fillers; compatibility routes keep the existing planner ranking. Snapshot the wallet when a Workshop visit starts; compute `floor(wallet_coins * pct / 100)` after explicit reserves and cap cumulative confirmed debits for that visit, intersecting this with existing starter/filler/utility limits. Keep the live buyer's screen and price recheck as final authority.

  ```python
  allowed = tuple(candidate for candidate in candidates if candidate.upgrade_id not in banned_upgrade_ids)
  spend_ceiling = (wallet_at_visit_start * coin_spend_limit_pct) // 100
  coin_budget = min(spend_ceiling, existing_ceiling) if existing_ceiling is not None else spend_ceiling
  ```
- [ ] **Step 4: Run** `pytest tests/test_build_route_workshop.py tests/test_reroll_planner.py tests/test_reroll_progress.py -q -p no:allure_pytest`; expect pass.
- [ ] **Step 5: Stage, review diff, and commit only with authorization:** `git commit -m "feat: enforce workshop bans priorities and coin limits"`.

### Task 7: Weighted luck with stable pending decisions

**Files:** Modify `fleet/build_route_eval.py`, `fleet/build_route_runtime.py`, `fleet/reroll_progress.py`; create `tests/test_build_route_weighted.py`.

**Interfaces:** A rule has `draw_chance_pct: int` and positive integer `weights: dict[str, int]`. Task 3 `PendingDecision` carries verified account ID, revision, run/visit ID, sequence, candidate fingerprint and chosen ID. `RouteEvaluation.trace` records the gate roll, eligible weights, normalized odds and selected ID. `BuildRouteRuntime.confirm_purchase(event_id: int, upgrade_id: str) -> None` increments sequence only after an existing confirmed purchase event; `abandon_decision(reason: str) -> None` increments it with an audit reason.

- [ ] **Step 1: Write tests** for 0% always using first-ranked eligible candidate, 100% always drawing, 25% gate distinct from 30% spend limit, odds computed only after bans/caps/affordability, identical rescans returning the same choice, confirmation advancing sequence, and account/revision change invalidating the pending decision. Use an injected deterministic integer seed rather than flaky statistical assertions.

  ```python
  first = evaluate_workshop(route_with_luck, facts, pending=None)
  again = evaluate_workshop(route_with_luck, facts, pending=first.pending)
  assert again.decision == first.decision
  assert again.trace.eligible_odds == {"cash_per_wave": 0.6, "coins_per_kill_bonus": 0.3, "cash_bonus": 0.1}
  ```
- [ ] **Step 2: Run** `pytest tests/test_build_route_weighted.py -q -p no:allure_pytest`; expect failures.
- [ ] **Step 3: Implement weighted choice.** Derive a stable hash from account ID, route revision, run/visit ID and persisted decision sequence; make separate deterministic draws for the chance gate and weighted candidate. Persist pending state before handing a decision to a buyer. Do not change the pending choice merely because repeated scans find a different affordable candidate; explicitly abandon it with reason when live evidence makes it impossible. Append draw facts to the confirmed ledger event, not just the UI trace.

  ```python
  key = f"{account_id}:{revision}:{visit_id}:{sequence}".encode()
  gate = int.from_bytes(hashlib.sha256(key + b":gate").digest()[:8], "big") % 100
  draw = int.from_bytes(hashlib.sha256(key + b":candidate").digest()[:8], "big") % sum(eligible_weights.values())
  ```
- [ ] **Step 4: Run** `pytest tests/test_build_route_weighted.py tests/test_reroll_progress.py -q -p no:allure_pytest`; expect pass.
- [ ] **Step 5: Stage, review diff, and commit only with authorization:** `git commit -m "feat: make weighted reroll decisions stable and explainable"`.

### Task 8: Constrained Flow Builder and publish UX

**Files:** Create `web/ui/app/fleet/reroll/strategies/FlowBuilder.tsx`, `RouteInspector.tsx`, `RouteDraft.ts`, `FlowBuilder.test.tsx`; modify `web/ui/app/fleet/reroll/strategies/page.tsx`, `web/ui/lib/api.ts`, `web/ui/lib/buildRoute.ts`.

**Interfaces:** `RouteDraft` holds a local editable `RouteDocument` plus its `expected_revision`; operations are `movePriority(id, direction)`, `setBan(id, banned)`, `setSpendLimit(ruleId, pct)`, `setDrawChance(ruleId, pct)`, `setWeight(ruleId, upgradeId, weight)`, `bindOverride(worker, accountId)`, and `resetOverride(worker)`. Only server preview/publish can produce an effective decision.

- [ ] **Step 1: Write UI tests** for catalog search and Never Buy count, reorder by pointer and Move up/Move down buttons, keyboard focus after reorder, separate 30% spend/25% luck controls, only eligible candidate odds, a broken dependency warning, inactive override on account change, side-by-side current/proposed preview, 409 conflict refresh without losing a local draft, and rollback confirmation showing its new revision.

  ```tsx
  await user.click(screen.getByRole("button", { name: "Move Cash/Wave up" }));
  expect(screen.getAllByTestId("priority-row")[0]).toHaveTextContent("Cash/Wave");
  expect(screen.getByLabelText("Spend limit")).toHaveValue(30);
  expect(screen.getByLabelText("Weighted luck")).toHaveValue(25);
  ```
- [ ] **Step 2: Run** `npm test -- --run app/fleet/reroll/strategies/FlowBuilder.test.tsx` from `web/ui`; expect failures.
- [ ] **Step 3: Build the constrained editor.** Use typed gates, phase rows and Workshop/Gems/Labs lanes; allow dragging only inside a priority list, with equivalent buttons. Keep draft edits local, call preview on demand, and disable Publish when validation errors, stale account binding, unsupported executable action, or expected-revision conflict exist. Publish with `expected_revision`; show per-worker applied/pending status. Make canvas labels/buttons nonselectable where decorative while preserving text selection in form controls and full keyboard interaction. Respect reduced-motion preferences.

  ```tsx
  const canPublish = validation.errors.length === 0 && !staleBinding && !conflict && !unsupportedAction;
  <button disabled={!canPublish} onClick={() => publishBuildRoute(draft, expectedRevision)}>Publish route</button>
  ```
- [ ] **Step 4: Run the focused UI tests, `npm run lint -- app/fleet/reroll/strategies`, and `npm run build`** from `web/ui`; expect pass.
- [ ] **Step 5: Stage, review diff, and commit only with authorization:** `git commit -m "feat: edit and preview fleet build routes"`.

### Task 9: Published-route application and acknowledgement

**Files:** Modify `tower_bot.py`, `fleet/reroll_progress.py`, `fleet/reroll_metrics.py`, `fleet/setup.py`; create `tests/test_build_route_integration.py`.

**Interfaces:** At each Workshop/battle decision boundary, `RerollProgress` gets an effective validated revision from `BuildRouteRuntime`; `shopping_policy` and `battle_policy` take its evaluator result. `observed_metrics` exposes account-bound applied revision and route error. A publish is reported as applied only after each active worker's acknowledgement file matches its current verified account and route revision.

- [ ] **Step 1: Write integration tests** with two worker roots: publishing revision 2 changes a running worker's next Workshop decision without restart; a paused worker stays pending; a swapped account drops its override; a corrupt route fails closed; lab-before-Workshop order remains; a variant cap still rejects a route candidate. Assert an unacknowledged running worker is not shown as updated.

  ```python
  store.publish(reordered_route, expected_revision=1, actor="operator")
  assert worker.route_runtime.current().revision == 2
  assert paused_worker.applied_revision() == 1
  assert worker.shopping_policy(base).workshop[0].item == "Cash / Wave"
  ```
- [ ] **Step 2: Run** `pytest tests/test_build_route_integration.py -q -p no:allure_pytest`; expect failures.
- [ ] **Step 3: Connect decision boundaries.** Construct runtime from the coordinator root discoverable through worker registration (not from a UI-supplied path), reload before `shopping_policy` and battle evaluation, and write acknowledgement only after successful validation and account verification. Keep `tower_bot.py`'s current lab check before Workshop; use the existing purchase executor and its confirmed ledger event to advance weighted state.

  ```python
  route = self.route_runtime.current()
  effective = resolve_route(route, self.worker_name, self.account_id)
  decision = evaluate(effective, self.route_facts(), self.route_runtime.pending())
  self.route_runtime.acknowledge(route.revision, self.account_id)
  ```
- [ ] **Step 4: Run** `pytest tests/test_build_route_integration.py tests/test_reroll_progress.py tests/test_lab_visit.py -q -p no:allure_pytest`; expect pass.
- [ ] **Step 5: Stage, review diff, and commit only with authorization:** `git commit -m "feat: hot reload and acknowledge fleet build routes"`.

### Task 10: Conditional in-game phases and cash budget

**Files:** Modify `fleet/build_route_eval.py`, `fleet/reroll_progress.py`, `policy.py`, `tower_bot.py`; create `tests/test_build_route_battle.py`.

**Interfaces:** Route battle gates compare observed highest Tier 1 wave; each branch has inclusive run-wave phases, ordered in-game IDs, `cash_spend_limit_pct`, emergency survival flag and optional weighted draw. `evaluate_battle(route: EffectiveRoute, facts: RouteFacts, pending: PendingDecision | None) -> RouteEvaluation` returns a chosen `UpgradeRule` or reasoned wait and carries run ID, wave, cash and observed row evidence.

- [ ] **Step 1: Write tests** for best wave 49 versus 50, run waves 1, 7, 10, 11, unknown best wave taking conservative branch, unknown current wave/cash waiting, 30% cash cap from that decision's observed wallet, emergency Defense Absolute interrupting economy, and weighted choice after eligibility. Assert the exact same evaluator output in preview and worker for identical facts.

  ```python
  assert evaluate_battle(route, replace(facts, best_tier_1_wave=49, wave=7), None).trace.branch_id == "opening"
  assert evaluate_battle(route, replace(facts, best_tier_1_wave=50, wave=7), None).trace.branch_id == "economy_50"
  assert evaluate_battle(route, replace(facts, best_tier_1_wave=50, wave=11), None).trace.phase_id != "first_10"
  ```
- [ ] **Step 2: Run** `pytest tests/test_build_route_battle.py -q -p no:allure_pytest`; expect failures.
- [ ] **Step 3: Implement battle evaluation.** Read fresh run identity, wave, cash and upgrade rows from the existing autopilot observation path. Apply the account gate, phase, survival interrupt, reserves and cash percentage before weighted draw; convert the result to an `AutopilotPolicy` with one supported rule while the existing executor still verifies the tap. Publish battle evaluation with its evidence timestamp to the fleet snapshot; when any required field is missing, return `Unknown` and keep recorded purchases in history only.

  ```python
  if facts.run_id is None or facts.wave is None or facts.battle_cash is None:
      return RouteEvaluation.unknown("Live run, wave or cash is unverified", facts)
  cash_ceiling = (facts.battle_cash * phase.cash_spend_limit_pct) // 100
  eligible = tuple(row for row in rows if row.price is not None and row.price <= cash_ceiling)
  ```
- [ ] **Step 4: Run** `pytest tests/test_build_route_battle.py tests/test_autopilot_policy.py tests/test_build_route_integration.py -q -p no:allure_pytest`; expect pass.
- [ ] **Step 5: Stage, review diff, and commit only with authorization:** `git commit -m "feat: apply conditional battle route phases"`.

### Task 11: Account overrides, reset, and safe reapply

**Files:** Modify `fleet/build_route.py`, `fleet/build_route_eval.py`, `web/app.py`, `web/ui/app/fleet/reroll/strategies/FlowBuilder.tsx`; create `tests/test_build_route_overrides.py`; extend `FlowBuilder.test.tsx`.

**Interfaces:** Override patch keys are stable rule IDs; `resolve_route` calls `apply_rule_patches(base: EffectiveRoute, patches: Mapping[str, object]) -> EffectiveRoute` only when the override's `account_id` matches verified registration. `POST /api/fleet/reroll/route/rebind-preview` accepts worker, old account, new verified account, and draft revision; it returns a diff and never writes. Publish of a rebound override requires explicit UI reapply plus fresh expected revision.

- [ ] **Step 1: Write tests** showing a fleet priority edit reaches two workers, a per-worker patch changes only one, reset restores the fleet rule, unknown rule IDs fail validation, an account swap inactivates the override, and reapply requires a verified new account and user action. Test that stale preview cannot be published after account replacement.

  ```python
  assert resolve_route(route, "Air_38", "old").workshop.priority_ids[0] == "cash_per_wave"
  assert resolve_route(route, "Air_38", "new").override_state == "inactive_account_changed"
  assert resolve_route(route, "Air_39", "other").workshop.priority_ids == route.baseline.workshop.priority_ids
  ```
- [ ] **Step 2: Run** `pytest tests/test_build_route_overrides.py -q -p no:allure_pytest` and focused `FlowBuilder.test.tsx`; expect failures.
- [ ] **Step 3: Implement patch resolution and reapply UX.** Patch only explicit fields on the matching stable rule ID, display inherited fields in the inspector, and show `Inactive: account changed` until an operator previews then rebinds. Server recomputes registration at publish and returns 409 if it changed. Reset deletes the patch from the draft; it does not alter the fleet baseline.

  ```python
  override = route.overrides.get(worker)
  if override is not None and override.account_id != verified_account_id:
      return replace(base, override_state="inactive_account_changed")
  return apply_rule_patches(base, override.patches) if override is not None else base
  ```
- [ ] **Step 4: Rerun the two focused test files** plus `tests/test_build_route.py`; expect pass.
- [ ] **Step 5: Stage, review diff, and commit only with authorization:** `git commit -m "feat: bind build route overrides to verified accounts"`.

### Task 12: Gem/lab route path and release verification

**Files:** Modify `fleet/build_route_eval.py`, `fleet/reroll_progress.py`, `fleet/setup.py`, `web/ui/app/fleet/reroll/strategies/FleetRoutePreview.tsx`, `FlowBuilder.tsx`; create `tests/test_build_route_resources.py`; extend `FlowBuilder.test.tsx` and `tests/test_build_route_integration.py`.

**Interfaces:** `RouteEvaluation` includes `gem_step` and `lab_step` with `status: supported | planned | blocked | unknown`. `ResourceStep(action: str, status: str, reason: str)` describes one path node; `ResourceEvaluation(gem_step: ResourceStep, lab_step: ResourceStep)` is returned by `evaluate_resources(route: EffectiveRoute, facts: RouteFacts) -> ResourceEvaluation`, wrapping the existing `LabCadence` and `lab_visit` decisions. Supported executable actions in this release are the existing lab slot 2 unlock and Game Speed slot 1 research only. Later lab slots, card slots, cards and lab slot 2 research can be ordered visually but always have `planned` execution status until separate recorded-screen work validates each executor.

- [ ] **Step 1: Write tests** for 99 gems → reserve/wait, 100 gems and slot 2 unowned → supported unlock, slot 2 owned → reserve released, future gem percentages applying only above the hard reserve, due affordable Game Speed before Workshop, occupied/unaffordable lab allowing Workshop, maxed Game Speed no repeat, and unsupported card/card-slot/later-lab actions never reaching a worker tap. UI test the `Planned · not automated` badge and disabled executable selector.

  ```python
  assert evaluate_resources(route, replace(facts, wallet_gems=99, lab_slot2_owned=False)).gem_step.status == "blocked"
  assert evaluate_resources(route, replace(facts, wallet_gems=100, lab_slot2_owned=False)).gem_step.action == "unlock_lab_slot_2"
  assert evaluate_resources(route, replace(facts, game_speed_maxed=True)).lab_step.action != "research_game_speed"
  ```
- [ ] **Step 2: Run** `pytest tests/test_build_route_resources.py -q -p no:allure_pytest` and focused `FlowBuilder.test.tsx`; expect failures.
- [ ] **Step 3: Adapt existing `LabCadence` and `lab_visit` decisions into route traces.** Do not duplicate their purchase logic. Show gem/lab path position, next supported action, blocker and account evidence age in each fleet column. Reject a route that claims an unsupported step is executable. Add an explicit release note in the PR describing which visual path steps are automated.

  ```python
  if step.action in {"cards", "card_slot", "later_lab_slot", "slot2_research"}:
      return ResourceStep(action=step.action, status="planned", reason="Planned · not automated")
  if cadence.slot2_due(now, facts.wallet_gems):
      return ResourceStep(action="unlock_lab_slot_2", status="supported", reason="100 gems reserved")
  ```
- [ ] **Step 4: Run** `pytest tests/test_build_route_resources.py tests/test_build_route_integration.py tests/test_lab_plan.py tests/test_lab_visit.py -q -p no:allure_pytest`, focused route UI tests, `npm run lint -- app/fleet/reroll/strategies`, and `npm run build`; expect pass. Review the full branch diff and spot-check the fleet page against the approved mockup. Do not run the broad Python suite.
- [ ] **Step 5: Stage the final changes, show the exact diff and test results, obtain commit authorization, then commit to a `codex/` feature branch. Open a PR only when the user asks, or when earlier authorization in this task explicitly covers it; never push directly to `main`.
