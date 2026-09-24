# Reroll Game Speed Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep lab slot 1 on each reroll emulator researching Game Speed until its observed maximum level.

**Architecture:** A Labs screen reader produces evidence, a pure planner chooses whether to start research, and a guarded visit performs at most one verified purchase. The existing reroll menu coordinator owns the visit and `LabsState` persists confirmed levels and jobs.

**Tech Stack:** Python, OpenCV templates, existing OCR and ADB device abstractions, pytest.

**Spec:** `docs/superpowers/specs/2026-09-24-reroll-game-speed-lab-design.md`

## Global Constraints

- Reroll workers only; do not change single-emulator behavior.
- Slot 1 remains reserved for Game Speed until the maximum level is observed.
- Affordable Game Speed research precedes Workshop purchases; claims may precede both.
- Never cancel an existing job or tap a gem-funded slot, Rush, or Speed Up control.
- Unknown, conflicting, or low-confidence screen evidence authorizes no spend.
- Do not infer purchase success or research completion from a tap or timer alone.
- Stage code and show the diff before requesting explicit commit approval.
- Run only the focused test files changed by this task, with `-p no:allure_pytest`.

## Review Focus

- Labs are locked on a young account: no navigation loop or spend.
- Slot 1 contains a different job: leave it running, including after a worker restart.
- A stale Game Speed price differs from the reopened row: do not tap.
- OCR identifies two possible Game Speed rows or cannot read the coin balance: do not tap.
- A tap opens a popup or has no visible effect: time out and return to the menu without recording a purchase.

---

### Task 1: Record the live path and read Labs screens

**Files:**
- Create: `lab_screen.py` — page, slot-1, and Game Speed row reader.
- Create: `tests/test_lab_screen.py` — recorded-frame assertions.
- Create: `tests/fixtures/menu_labs_slot1_idle.png`, `tests/fixtures/menu_labs_game_speed_picker.png`, and matching OCR JSON files.
- Modify: `config.py` — measured Labs tab and Battle return templates/anchors.
- Modify: `screen_discovery.py`, `labs.py`, `tests/test_labs.py` — mark only verified Labs reading capability supported.

**Interfaces:**
- `read_home(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LabHomeReading`, with `page: bool`, `slot_status: Literal['idle','researching','locked','unknown']`, `job: labs.LabJob | None`, and `slot_point: tuple[int,int] | None`.
- `read_picker(screen: Image, boxes: tuple[ocr.TextBox, ...]) -> LabPickerReading`, with `page: bool`, `game_speed: labs.LabEntry | None`, `coin_balance: int | None`, and `buy_point: tuple[int,int] | None`.

- [ ] **Step 1: Capture evidence.** Pause one reroll worker through the fleet control, wait for its process to stop, then navigate only to Labs and slot 1 with the emulator UI. Save full-resolution frames and OCR boxes for idle slot and Game Speed picker. Measure anchors and tap rectangles from these captures, including the current 1080×2400 fleet geometry. Resume the worker after capture. If slot 1 is not unlocked on any fleet account, collect the same screens from an unlocked account before enabling this task's purchase path.
- [ ] **Step 2: Write failing frame tests.** Assert the recorded active fixture reads `researching` in slot 1; the new idle fixture reads `idle`; the picker resolves exactly `labs.game-speed`, its visible level, coin price, balance, and row control; a Workshop frame reads neither Labs page. Assert duplicate/unclear OCR returns no buy point.
- [ ] **Step 3: Run the new tests red.** `timeout 120s .venv/bin/python -m pytest tests/test_lab_screen.py -p no:allure_pytest -q`
- [ ] **Step 4: Implement the reader.** Use the measured page anchor, slot-1 card bounds, and OCR boxes in those bounds. Resolve names through the Labs catalog; parse only a price explicitly paired with Game Speed and the coin header. Return `unknown` or `None` for any missing or conflicting field. Add only the supported reader to capability reporting; keep acceleration unsupported.
- [ ] **Step 5: Run the reader and Labs model files green.** `timeout 120s .venv/bin/python -m pytest tests/test_lab_screen.py tests/test_labs.py -p no:allure_pytest -q`

### Task 2: Decide when slot 1 may start Game Speed

**Files:**
- Create: `lab_plan.py` — pure decision and visit cadence.
- Create: `tests/test_lab_plan.py` — decision and persistence tests.
- Modify: `fleet/reroll_progress.py` — expose account-bound `lab_due()` and `note_lab_observation()`.

**Interfaces:**
- `decide(slot: LabHomeReading, row: LabPickerReading | None) -> LabDecision`; `LabDecision.kind` is one of `wait_running`, `inspect`, `start`, `wait_coins`, `wait_unlock`, `done`, `unknown`.
- `RerollProgress.lab_due(now: float) -> bool` and `note_lab_observation(decision: LabDecision, now: float) -> None` persist a next-check time under the worker's account-bound runtime root.

- [ ] **Step 1: Write failing planner tests.** For `idle + Game Speed level below max + price <= coins`, expect `start`; for busy slot 1 expect `wait_running`; for `price > coins`, `wait_coins`; for locked, maxed, duplicate row, or unreadable balance, expect no purchase. Reload the cadence from disk and assert the same account retains its next-check time while a different account does not.
- [ ] **Step 2: Run red.** `timeout 120s .venv/bin/python -m pytest tests/test_lab_plan.py -p no:allure_pytest -q`
- [ ] **Step 3: Implement the decision and cadence.** Require exact `labs.game-speed`, a known nonnegative price and balance, a readable level, a visibly enabled card, and an idle slot. If the UI exposes a maximum level, require `level < max_level`; otherwise continue while the row is available and stop only on a verified maxed state. An active job delays checks until near its observed completion time; lack of funds delays checks for a short fixed interval or until the stored balance changes; `done` disables further visits. Store timing atomically with the account id, as `RerollProgress` does for other account-bound artifacts.
- [ ] **Step 4: Run green.** `timeout 120s .venv/bin/python -m pytest tests/test_lab_plan.py tests/test_reroll_progress.py -p no:allure_pytest -q`

### Task 3: Perform one guarded lab visit

**Files:**
- Create: `lab_visit.py` — finite-state menu walk and post-tap confirmation.
- Create: `tests/test_lab_visit.py` — fake-device steps and timeout/recovery tests.
- Create: `tests/fixtures/menu_labs_game_speed_affordable.png`, `menu_labs_game_speed_confirmation.png`, and matching OCR — the live affordable and final-confirmation controls.
- Modify: `config.py` — include only the measured Labs and return controls used by this walk.

**Interfaces:**
- `LabVisit.request() -> bool`, `LabVisit.active: bool`, `LabVisit.cancel(reason: str) -> None`, and `LabVisit.advance(screen: Image, boxes: tuple[ocr.TextBox, ...], device: AdbDevice, now: float) -> LabVisitResult | None`.
- `LabVisitResult` records `status`, `reason`, `confirmed_job`, and `observed_coin_spend`; no positive result exists without a slot-1 Game Speed timer on a fresh frame.

- [ ] **Step 1: Write failing walk tests.** Feed recorded home, idle, picker, and Research confirmation frames through a fake device. Assert one tap per frame; both the picker row and the final Research control require two compatible readings and rechecked affordability; Rush and Speed Up rectangles never receive taps. Feed a changed price, unreadable frame, popup, or timeout and assert a safe exit with zero purchase record. Feed two post-tap Game Speed job frames and assert one confirmed result.
- [ ] **Step 2: Run red.** `timeout 120s .venv/bin/python -m pytest tests/test_lab_visit.py -p no:allure_pytest -q`
- [ ] **Step 3: Implement the finite-state walk.** States: open Labs, inspect slot 1, open idle slot, inspect picker, inspect the Research confirmation dialog, confirm start, return to Battle. Every tap requires a named current screen and a measured target from that frame. Cancel when paused. Re-read price and balance immediately before the final Research tap. Bound transitions and total frames; return through the measured Battle tab on an unexpected screen instead of blindly tapping.
- [ ] **Step 4: Run green.** `timeout 120s .venv/bin/python -m pytest tests/test_lab_visit.py tests/test_lab_screen.py -p no:allure_pytest -q`

### Task 4: Integrate with reroll maintenance and verify end to end

**Files:**
- Modify: `tower_bot.py` — construct `LabVisit` only for reroll workers, add it to exclusive menu ownership, pause cancellation, Game Over home detour, and main-menu scheduling after claims.
- Modify: `fleet/reroll_progress.py`, `labs.py` — persist only confirmed reading/job and journal the verified coin spend.
- Modify: `events.py`, `ledger.py`, `web/ui/lib/ledger.ts`, `web/ui/components/LedgerEntries.tsx` — publish a verified lab purchase and expose LAB in both ledger views.
- Modify: `tests/test_reroll_progress.py`, `tests/test_shopping_loop.py` — scheduling and ownership tests.
- Create: `tests/fixtures/menu_labs_game_speed_running.png` and OCR JSON from the verified start or an already-running account.

**Interfaces:**
- The existing reroll loop calls `lab_due()` before routing Game Over to Home. A started lab walk suppresses shopping, claims, Stats, and normal navigation until it ends. `LabsState.observe()` receives two consistent readings; the ledger receives a coin expense only after post-tap confirmation and a corroborating balance change.

- [ ] **Step 1: Write failing integration tests.** A due lab visit routes Game Over to Home and arms at the main menu only when no other walk owns it. Claimable rewards take priority; an affordable Game Speed start runs before Workshop shopping, while a busy or unaffordable lab does not block Workshop. Pause cancels an active lab visit. A single-emulator bot never constructs the lab walk. A confirmed Game Speed start updates the persisted job and ledger once; a failed confirmation updates neither.
- [ ] **Step 2: Run red.** `timeout 120s .venv/bin/python -m pytest tests/test_reroll_progress.py tests/test_shopping_loop.py -p no:allure_pytest -q`
- [ ] **Step 3: Implement integration.** Reuse the existing menu ownership checks and event bus. Do not change the generic single-emulator navigation path. Capture the confirmed running frame and assert it reads as a slot-1 Game Speed job.
- [ ] **Step 4: Run focused green checks.** `timeout 180s .venv/bin/python -m pytest tests/test_lab_screen.py tests/test_lab_plan.py tests/test_lab_visit.py tests/test_labs.py tests/test_reroll_progress.py tests/test_shopping_loop.py -p no:allure_pytest -q`
- [ ] **Step 5: Verify live safely.** Pause one fleet worker, run one controlled Labs visit while watching its capture and journal, then resume that worker. Confirm the other workers remained untouched and the account records only the observed spend/job. Stage the code and fixtures, run `git diff --cached --check`, show the staged diff and test results, and request commit approval.
