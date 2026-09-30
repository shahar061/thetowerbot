# Bot reliability audit

## Coverage and limits

The historical inventory covers 243 pull requests created or updated during
the preceding 30 days: 241 merged and two closed. Titles and metadata were
inventoried across that set; 16 relevant descriptions received detailed
inspection. Repository queries found no open pull requests, no open or recent
issues, no hosted review records, no status checks, and no Actions runs. Some
descriptions report offline reviews. There is no `.github` workflow directory.

The code review follows the perception, account-state, transaction, planning,
execution, recovery, worker-lifecycle, and dashboard boundaries. This is a
targeted reliability audit, not a claim that every path has been exercised or
that the whole bot is defect-free. No full test suite or live purchasing flow
was run. Live fleet evidence was read without changing account data or
restarting workers.

## Confirmed causes and changes

### Live viewing

Emulator 80's dashboard was assigned port 10080. HTTP probes can reach that
port while browsers refuse it as an unsafe port; the browser reproduced
`ERR_UNSAFE_PORT`. A healthy worker and account check therefore did not imply
a usable browser live view. Fleet web-port allocation must exclude the
browser's blocked-port list, and existing affected registrations require a
verified stopped-worker migration before restarting on the safe port.

### Upgrade availability

The executable upgrade catalog describes only the unlock controls the bot can
operate. It omitted later unlock groups from presentation and planning gates,
which incorrectly made future upgrades appear available. The shared Workshop
availability catalog now covers every leveled skill, with separately identified
starter skills and ordered unlock groups. Display metadata does not grant
permission to execute an unsupported unlock control.

Positive, valid account observations and confirmed action receipts establish
ownership. Unknown, unseen, negative, malformed, and foreign-account facts do
not. Fleet State returns the immediate next group explicitly, so the UI can
hide later groups without guessing from row order or the strategy's target.

On the running emulator 80's read-only state, Defense resolves to four owned
skills out of 18 and the next group is Thorns. Health, Health Regen, Defense %,
and Defense Absolute remain visible; later groups stay locked.

### Action-derived Workshop state

Fleet State and the planner share receipt replay and quote invalidation rules.
A confirmed receipt advances a quote only when its timestamp is strictly
after that quote's observation. A delayed receipt already reflected in the
observation does not advance it again. Each stat observation has its own
anchor for projecting levels; lifetime purchase totals are never starting
levels, and a price-derived level estimate is never promoted to an observed
level.

Unproven purchases and unconfirmed attempts invalidate the affected item.
Unexplained debits large enough to represent a Workshop purchase invalidate
older quotes. Same-frame wallet reconciliation, rounding-sized gaps, modifier
signatures, and account binding retain their existing safeguards. An invalid
quote cannot be replaced with an older stat-derived price. Sources accompany
displayed levels and prices, preserving the distinction between observations,
confirmed subsequent actions, and catalog estimates.

An unchanged stat can still clear purchase uncertainty after it is read again.
Two agreeing Workshop frames refresh the persisted raw evidence when that
fact's previous observation is at least one minute old. Readings remain
deduplicated between refreshes; neither a single new frame nor a price estimate
refreshes the stat anchor.

The dashboard database connection remains read-only with a 0.1-second busy
timeout. Receipt replay is aggregated in SQL and returns at most one row per
catalog upgrade, rather than loading lifetime event history into Python.

## Recurring failure patterns

- OCR identity and wallet errors can turn a real purchase into an unresolved
  transaction, affecting both Workshop and Lab spending.
- Incomplete unlock evidence can cause repeated searches for rows that the
  account cannot expose, preventing otherwise eligible battle upgrades.
- Lost prices and observation scheduling can form a loop in which neither
  planning nor navigation requests the observation needed to proceed.
- Stale Lab snapshots and immediately rearmed failed visits can crowd out
  battles despite a responsive device.
- Account verification, worker health, stream transport, browser restrictions,
  and snapshot fallback are independent reasons for a missing live screen.
- Merged changes do not update already running worker processes. Deployment
  must distinguish source state from each worker's applied version.

## Remaining verification work

There is no automated regression gate. Historical change descriptions carry
inherited failures in strategy-budget expectations, clone qualification,
account adapters, frame-loop handling, and fixture coverage. Those reports
should become a bounded baseline-repair effort and a focused pull-request CI
gate; passing newly added regressions alone is insufficient evidence that
those older failures are resolved. The strategy-budget fixture has been
corrected to include its intended unlocked starter state while retaining its
expected budget. An account-adapter test stub now supplies the slot-record
interface required by the current adapter; its original assertions remain.
The other historical reports are not claimed resolved here.

The dashboard also lacks an app-wide Host allowlist. WebSocket Origin
validation alone does not address DNS rebinding. This is independent of the
live-screen port defect and should be handled as a separate change.

Lab recovery receipts still report their pre-action wallet, whereas Workshop
recovery uses the reconciliation wallet. That deserves consistency review, but
Lab receipts require a proven purchase and this audit did not reproduce the
same balance-rewind failure there. It remains a risk to investigate rather
than a confirmed defect.

## Focused validation

The 12 changed Python test files passed 344 tests, and five changed UI test
files passed 70 tests. The production UI build, TypeScript check, focused lint
checks, and whitespace validation passed. Additional focused checks cover
Workshop level parsing, exact-price execution, and preview compatibility.
Coverage
includes the four-of-18 Defense case, immediate unlock membership, invalid
facts, delayed receipts, action-derived prices and levels, per-item
uncertainty, unexplained debits, modifier changes, foreign account data, and a
50,000-row ledger. The new Fleet State payload was also generated from all
four running workers with no account errors and without restarting them.

Browser checks used that captured state to verify the four available Defense
skills, the next Thorns unlock, both chart and table views, and remembered view
selection. An isolated process simulation verified adoption of the legacy
worker, identity-checked stop, migration to port 9999, and subsequent adoption.
The running fleet still needs deployment of these changes.
