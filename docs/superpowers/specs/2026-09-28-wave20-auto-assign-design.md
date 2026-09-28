# Wave-20 strategy auto-assignment

## Goal

Every account in the reroll pool runs one shared opening strategy until its best
tier-1 wave reaches 20, then is handed one of the three turtle variants in
round-robin so the variants are compared from the same starting point. No
operator action is needed for new or rerolled accounts.

## Today

- Assignments are only ever written by the operator through
  `FleetSetupService.assign_strategy` (`fleet/setup.py`), reached from
  `POST /api/fleet/reroll/strategies/assign`.
- A rerolled account no longer matches its old assignment's `account_id`, so
  `resolve_route` falls back to the fleet baseline ("assignment inactive") until
  someone assigns it again.
- Running workers re-read the route every menu scan; a publish takes effect
  without a restart.
- Each worker publishes `workers/<w>/build-route-facts.json` with `account_id`
  and `best_tier_1_wave` (from `MAX(wave) FROM runs WHERE tier=1`).

## Settings

New file `fleet/auto-assign.json` (kept apart from `settings.json`, whose key set
is validated exactly):

```json
{
  "enabled": true,
  "opening": "opening",
  "threshold_wave": 20,
  "rotation": ["turtle",
               "strategy-3e323059b77b41e683fce3366c821578",
               "strategy-a168a84394d5445696810e057d45b3a1"]
}
```

`opening` and `rotation` are strategy ids (built-in template ids or saved
library ids). A missing file means disabled. Invalid content (unknown id,
empty rotation, opening inside the rotation, threshold < 1) disables the rule
and logs one error per change of file content; it never raises into the
monitor.

Each assignment pins the strategy's **latest version at assignment time**
(templates are always version 1). Later library saves do not move existing
assignments; that stays an operator decision.

## Rule

A pure function in new module `fleet/auto_assign.py`:

```
plan(settings, route, workers, facts, latest_versions) -> list[Assignment]
```

For each visible active pool member `w` with a registered account `acct`:

1. **Unassigned** — no assignment for `w`, or its `account_id != acct`
   → assign `opening`.
2. **Graduated** — the assignment is `opening`, and `facts[w]` exists with
   `facts[w].account_id == acct` and `best_tier_1_wave >= threshold_wave`
   → assign the rotation strategy with the fewest current active assignments
   among visible workers (ties broken by rotation order). Counts include the
   picks already made earlier in the same `plan` call.
3. **Anything else** (a turtle, a manual choice, missing or mismatched facts)
   → no change.

Manual assignment therefore always wins: once the operator assigns anything
other than the opening, the rule leaves the worker alone.

## Runner

`AutoAssigner(root, service, interval=60.0)` owns a daemon thread started and
stopped with the existing fleet monitor in `FleetSetupService.start_monitor` /
`stop_monitor`. Each cycle:

1. Read settings; stop if disabled or invalid.
2. Read the route, library, pool members and each worker's facts file.
   Unreadable facts for a worker → that worker is skipped this cycle.
3. `plan(...)`; group results by (strategy_id, version) and apply each group
   through `assign_strategy(..., actor="auto")` with the route revision just
   read. On `RouteConflict` or `route_account_binding_changed`, abandon the
   rest of the cycle and retry next interval.

`assign_strategy` gains an `actor: str = "operator"` keyword passed through to
`BuildRouteStore.publish`, so the route history and strategy ledger show
`auto` for these switches. Its existing log line
("Strategy assigned: w → name vN (was …)") gives the coordinator-log trail.

## Rollout

- Write `fleet/auto-assign.json` as above, enabled.
- Workers 56/57/58 are already past wave 20 on a turtle, so rule 3 applies
  and nothing changes for them. New or rerolled accounts start on the opening.

## Out of scope

- UI for editing the settings (the file is enough for now).
- The legacy reroll `variant` (baseline / attack_heavy / income_first); it
  only affects legacy builds and is unchanged.
- Moving workers back to the opening, or re-balancing existing turtles.

## Tests

`tests/test_auto_assign.py`, pure-function cases for `plan`:

- new account (no assignment) → opening
- rerolled account (assignment for another account) → opening
- opening at wave 19 → unchanged; at wave 20 → least-used turtle
- two workers graduating in one cycle → different turtles
- tie → first in rotation order
- manual/turtle assignment → unchanged
- facts for a different account, or missing facts → unchanged

Plus one test that `assign_strategy(actor="auto")` records `auto` as the
publish actor, and one that an invalid settings file disables the rule.
Only these test files are run locally.
