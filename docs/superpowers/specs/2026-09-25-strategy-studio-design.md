# Strategy Studio and Build Route

## Goal and product shape

Give the reroll operator one place to understand and adjust what every active
emulator will buy next. The page is **Strategy Studio**; a saved, versioned set
of rules is a **Build Route**. The default view is a fleet preview, not an
editor. It shows each emulator's current phase, next supported decision,
currency gap, lab job, gem target, and the rule and evidence behind each
answer. A second view, Flow Builder, edits the route.

The operator can ban Workshop upgrades completely, reorder Workshop priorities,
set bounded spending percentages, and define conditions such as “when highest
Tier 1 wave is at least 50, prefer economy during run waves 1–10.” They can
assign positive weights to eligible upgrades and choose what percentage of
decisions use a weighted draw. The same page shows gem and lab paths, including
which steps the current executor supports and which await implementation.

Success means an operator can compare three active accounts at a glance,
explain the next choice on each, edit a fleet rule or a single-emulator
override, preview its effect without spending, and publish a revision without
restarting bots. “Never buy” must mean every Workshop path, including survival
starters and cheap fillers. Unknown evidence must never become a confident
purchase recommendation.

## Existing behavior to preserve

`web/ui/app/fleet/reroll/strategies/page.tsx` currently compares account-scoped
Workshop decisions and projected purchases; battle purchases are historical
evidence, not live battle intent. `fleet/reroll_planner.py` chooses one Workshop
move and produces a projection whose future prices are unknown.
`fleet/reroll_progress.py` adapts that choice to the actual shopping and battle
policies. `policy.py` contains the Turtle in-game decision, including emergency
Defense Absolute and an economy window. `lab_plan.py` and `lab_visit.py` reserve
lab slot 1 for Game Speed, and the reroll path reserves the first 100 gems for
lab slot 2. Due, affordable Game Speed research wins over Workshop spending;
an occupied or unaffordable lab allows Workshop to proceed. The first Ultimate
Weapon choice after the Tier 1 Wave 60 finish line remains an operator action.
Existing reroll variants set account-specific level caps, and the Workshop
planner already has a value-weighted draw. Neither may be lost during route
migration.
“Prioritize Workshop” in this design means ordering Workshop upgrades; it does
not reverse the previously chosen lab-before-Workshop coin order.

The new route replaces hard-coded reroll preferences only after an explicit
publish. Migration creates an equivalent initial route from the current
behavior: a compatibility block delegates to the existing planner, variant
caps and weighted draw until the operator replaces that block with editable
rules. Existing per-worker Strategy profiles continue to handle generic
controls and must not be silently overwritten. A missing route keeps current
behavior until the operator publishes one.

## Interface

The existing fleet **Strategies** navigation entry becomes **Strategy Studio**
at `/fleet/reroll/strategies/`; no extra top-level menu item is added. The two
views share a scope selector: Fleet, then each verified emulator. Fleet is the
default. An emulator view previews the fleet baseline plus its override. An
override is bound to the worker identity *and* current verified account ID; it
becomes inactive after an account changes, and the UI offers an explicit
reapply action. This avoids carrying an account-specific ban or budget into a
new reroll by accident.

**Fleet preview.** Equal-width emulator columns keep the live next action near
the top, then show Workshop coins and target, battle cash and current rule,
gems and next unlock, and lab slot status. Labels distinguish `Observed`,
`Projected`, `Blocked`, and `Unknown`. A projected chain is a route through
milestones, not a claim that ten purchases are currently affordable. The
operator can filter to one emulator without losing access to fleet comparison.
Selecting a decision opens an inspector with its matched rule, account facts,
alternatives rejected, price source and age, and route revision applied by that
worker. A worker on an old revision or with unavailable evidence stays visible
with a clear status.

**Flow Builder.** The visual grammar is a constrained directed route, not an
arbitrary n8n-style graph. There are only typed account gates, run-wave phases,
priority lists, weighted draws, resource reserves, and action blocks. The
canvas shows the account gate and its true/false branches above three resource
lanes: Workshop, Gems, and Labs. In-game purchases belong to the current
branch's run phase. Dragging changes order within a priority list; it never
creates an unvalidated dependency. A click opens a precise form in the right
inspector. Every drag action has Move up / Move down buttons for keyboard and
touch users. Reduced-motion mode uses the same layout without animated paths.

The Workshop lane has a prominent **Never Buy** pool with catalog search,
category labels, and a count. Removing a row from priority is different from
banning it: an unprioritized row may still be a cheap filler, while a banned
row is excluded from all Workshop decisions. If a ban blocks an unlock needed
by a later block, the canvas highlights the broken path and the preview shows
that account waiting; it does not silently bypass the ban.

The editor exposes two different percentage controls:

- **Spend limit**: the maximum share of currently observed, spendable currency
  usable in one decision or visit, after explicit reserves. A Workshop 30%
  limit applies to the coin wallet at the start of that visit; a battle 30%
  limit applies to the cash observed at that decision. The UI displays both
  the percentage and resulting currency amount when known. Unknown balances
  do not produce a calculated amount or authorize a buy. A gem percentage for
  later card or slot purchases applies only to gems above the mandatory lab-2
  reserve. The dedicated Game Speed lab rule keeps its existing priority and
  is not silently constrained by a new Workshop percentage.
- **Weighted luck**: the share of otherwise valid decisions routed to a
  weighted draw, from 0% (always take the highest-ranked candidate) to 100%
  (always draw). Within the draw, each candidate has a positive integer
  weight. The preview converts the weights of *currently eligible* candidates
  into visible odds. A 25% luck setting is not a promise that every fourth
  purchase will be random; each new decision has its own roll.

The inspector always puts a plain-language example under the controls, such
as “At Air 38's current balance: at most 42 cash; if this phase uses luck,
Cash/Wave 60%, Coins/Kill 30%, Cash Bonus 10%.” No numeric odds appear when
the eligible set or currency balance is unknown.

**Preview and publish.** Editing creates a local draft. Preview runs the same
pure policy evaluator used by the workers over each account's latest verified
facts. A side-by-side diff shows “current next decision → proposed next
decision,” changed spending limits, new bans, matched conditions, and blockers.
The preview is tagged with the fact snapshot time and route revision. Publish
requires the draft to pass validation and still match the saved revision;
concurrent edits return a conflict with the newer route. Publishing creates a
new immutable revision with an audit entry and offers one-click rollback to a
prior revision. It never taps the game directly.

The visual mockup shown during design is illustrative, not live account data.
Its Fleet Preview and Flow Builder views establish layout and interaction
hierarchy; the implementation uses the application's existing theme tokens
and accessible component primitives.

## Route model and resolution

Persist one coordinator-owned, schema-versioned route document with:

- revision, authoring timestamp, and a fleet baseline;
- typed Workshop priority blocks, a set of banned `upgrade_id` values, and
  per-visit coin limits;
- ordered account gates on observed facts such as highest Tier 1 wave, each
  containing disjoint run-wave phases and their in-game priorities, cash
  limits, emergency survival behavior, and optional weighted draw;
- gem milestones and reserves, and lab slot research paths;
- zero or one account-bound override for each registered emulator.

Use catalog IDs, never OCR display names, as rule identities. The coordinator
rejects unknown IDs, duplicate priorities, negative or out-of-range budgets,
zero/negative/non-integer weights, overlapping run-wave phases within one
branch, and cyclic dependencies. A phase must have a deterministic fallback
when its weighted pool is empty. Condition evaluation uses only account facts
with a provenance and timestamp; unknown is a third outcome that follows the
explicit conservative branch rather than being treated as false.

Resolution order is fixed:

1. Verify worker and account identity, route revision, and current screen.
2. Resolve fleet baseline plus the current account-bound worker override.
3. Filter all Workshop candidates through the hard ban pool; filter all
   candidates through unlock, cap, currency, price, and fresh-screen checks.
4. Honor hard commitments: keep the initial 100 gems for lab slot 2 until it
   is confirmed owned; reserve slot 1 for Game Speed until observed maxed;
   keep the Wave 60 Ultimate Weapon choice with the operator. A due,
   affordable Game Speed research is attempted before Workshop spending.
5. Apply the matched account gate and run-wave phase. Emergency survival can
   interrupt an economy phase, and the UI explains when it does.
6. Apply percentage budgets and ordered priorities. Only then run optional
   weighted luck over eligible candidates. If none are eligible, wait with a
   reason; never fall through to a banned item.
7. The existing executor rereads the screen, wallet, price and target before
   each tap and records success only from confirmed post-action evidence.

For repeatable previews and retry safety, each weighted decision uses a stable
seed derived from verified account ID, route revision, run or visit ID, and a
persisted decision sequence. Repeated scans of the same pending decision
return the same draw. The sequence advances only after a confirmed purchase
or an explicit decision abandonment, so retries cannot reroll until they get
an affordable result. The ledger records the candidate weights, computed
odds, draw gate and selected result alongside the route revision and matched
rule. This permits a user to explain a surprising buy later.

Fleet defaults are evaluated independently for each account. Worker overrides
are small patches against stable rule IDs, not copies of the whole route; a
baseline edit remains visible in every worker unless explicitly overridden.
The UI displays each override and offers Reset to fleet route. An inactive
override on an account change has no effect and cannot be published against a
new account without rebinding it.

## Runtime and data flow

The reroll coordinator owns the route store and a narrow API to read the
current document, preview a draft, publish with an expected revision, and
inspect each worker's applied revision. Store revisions and audit history
durably with atomic file replacement under the coordinator's state root.
Only one coordinator host and its registered local workers are in scope.

Workers receive the route-store path through their registered runtime and
reload a validated immutable revision at a decision boundary. A running bot
does not need pause/resume. If a route read or validation fails, spending
fails closed while observation and gameplay continue; the fleet view surfaces
the error. A worker reports the applied revision after loading it. Publish is
shown as complete only when active workers acknowledge it; an offline worker
is marked pending and loads the new revision before its next spend. The
coordinator never reports an unacknowledged worker as updated.

A pure route evaluator accepts a route revision and an account-bound evidence
snapshot and returns a decision plus a trace of matched and rejected rules.
The preview API and worker purchase paths call this same evaluator. A new
account snapshot for live in-game intent must include current run ID, wave,
cash, observed upgrade rows, and the evaluation trace; if a field is missing,
the interface says `Unknown` and shows historical battle purchases only as
history. The fleet page must not invent live battle intent from prior runs.
Existing next-Workshop projections retain their warning that future prices
and balances are unknown.

The initial gem path reserves 100 gems and unlocks lab slot 2, which existing
code supports. Later lab slots, card slots, and cards are displayed as route
steps, but a step cannot be published as executable until that specific
screen reader, transaction confirmation, and account evidence are implemented
and tested. The same rule applies to lab slot 2 research selection beyond the
existing Game Speed slot-1 path. Unsupported steps remain clearly labelled
`Planned · not automated` in preview and cannot be selected as a live action.
The account's assigned reroll variant remains a constraint underneath the
fleet route; its observed level caps appear in the rule trace. Changing a
fleet priority does not silently remove a variant cap.

## Delivery slices

1. **Truthful fleet preview and route foundation.** Add the versioned route
   model, shared evaluator and trace, API, account-bound revision reporting,
   migration route, and the fleet-first page. Preserve current behavior.
2. **Workshop control.** Add the hard ban pool, priority ordering, coin spend
   percentage and optional weighted draw. Wire every Workshop path,
   including starters and fillers, through the evaluator and preview.
3. **Conditional battle control.** Add account gates, run-wave phases, battle
   cash budgets and weighted draws. Publish fresh live battle intent rather
   than presenting past purchases as current decisions.
4. **Gem and lab paths.** Surface the supported lab slot 2 and Game Speed
   steps, then add later gem and lab actions one at a time with recorded game
   screens and confirmed transaction evidence. Each action is unavailable in
   the editor until its executor is validated.

Each slice can be reviewed and tested independently; the page labels future
capabilities rather than pretending the complete route is already executable.

## Verification and acceptance

Pure evaluator tests cover account-gate thresholds (including exactly wave
50), unknown facts, phase boundaries (waves 1 and 10), emergency survival,
priority order, cap and affordability filtering, all-banned pools, and
fleet-versus-override resolution. Weighted tests verify 0% and 100% draw
gates, odds after eligibility filtering, stable retries, advancement after
confirmation, and a recorded trace. Budget tests distinguish a wallet share
from odds and enforce reserves. Account-change tests prove an old override
and stale preview cannot apply to the new account.

API tests cover revision conflicts, invalid rules, an unavailable worker,
atomic persistence and reload, fail-closed behavior, and a preview whose
answer matches the live evaluator for the same facts. UI tests cover
drag-and-drop plus keyboard reorder, never-buy search and conflict warnings,
side-by-side fleet comparison, unknown versus projected labels, and the
publish/rollback flow. Executor tests use recorded screens for every new
spending action and verify post-action acknowledgement before the ledger
records a purchase. Run only focused tests locally; normal PR CI handles the
repository's broader checks.

The first fully usable release meets these observable cases:

- Banning Defense Absolute removes it from the next Workshop decision,
  every starter and filler fallback, and every projection on all affected
  workers; the reason reads “blocked by Never Buy.”
- With best Tier 1 wave 49, the opening branch remains active; with 50 and
  run wave 7, the economy phase is active; at run wave 11 it has ended.
- A 30% spend limit never authorizes a purchase above that amount. A 25%
  weighted-draw setting displays its actual eligible odds and a stable
  selected result without confusing the two percentages.
- Air 38 can show a proposed change while Air 39 remains on the fleet route;
  a new account on Air 38 does not inherit its old override.
- A worker with unknown currency, stale route revision, or unverified
  account shows its blocker and does not make a purchase from the draft or
  from guessed state.

## Boundaries

The canvas is not a general scripting environment, does not accept arbitrary
cycles or custom code, and cannot promise an exact multi-purchase sequence
when future prices are unknown. The route does not bypass executor
confirmation, automate Ultimate Weapon selection, spend gems to rush labs,
or change the single-emulator Strategy page. Cross-host synchronization and
automatic strategy optimization from benchmark data are outside this design.
