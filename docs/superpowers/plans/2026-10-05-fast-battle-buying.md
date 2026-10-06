# Fast Battle Buying Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After wave 20, buy at least 4 battle levels per wave and spend at least 70% of the cash earned, by sending up to 10 taps per decision, confirming them from the price curve, keeping quotes valid across waves, staying on the open tab when cash is plentiful, seeking rows in catalog order and scanning faster while buying.

**Architecture:** `device.tap_burst` sends several taps in one `adb shell` command and the supervisor checkpoints it as one input. `BattleAutopilot.step` sizes each modeled purchase from the price curve, cash, reserve and a burst price ceiling that `strategy_blocks` computes (tab-sticky in rich mode), then counts the levels the next frames prove from the price jump. `RerollProgress` replaces the database receipt fence with an in-memory per-run tally, and every change sits behind `config.BATTLE_BURST_ENABLED`.

**Tech Stack:** Python 3.12, `uv`, pytest, adbutils, OpenCV/RapidOCR perception, SQLite worker DBs.

**Spec:** docs/superpowers/specs/2026-10-05-fast-battle-buying-design.md

## Global Constraints

- Kill switch: `config.BATTLE_BURST_ENABLED = True`; with it `False`, every change below falls back to today's one-level behaviour.
- `config.BATTLE_BURST_MAX = 10` levels per burst at most.
- `config.BATTLE_RICH_MULTIPLE = 10`: rich mode when `battle_cash >= 10 x cheapest candidate price`.
- `config.BATTLE_SCAN_INTERVAL_SECONDS = 0.6` only while the last battle step tapped, confirmed, or headed for a buyable target; 2.0 s otherwise. Jitter and maintenance deadlines unchanged.
- `config.BATTLE_BURST_TAP_GAP_SECONDS` defaults to `0`; it changes only from the live probe in Task 9.
- Confirmation timeout stays 8 s; an unconfirmed purchase with no value change blocks the upgrade for 60 s, as today.
- Pool-only rule: the tab rule narrows `block['upgrade_ids']` candidates; it never adds an upgrade from outside the pool.
- Never spend into `cash_reserve`: the whole burst's sum must fit `cash - cash_reserve`.
- One `BattlePurchased` event per level, each at its own curve price; `record_receipt` gets the last level's sequence.
- Never count a level that was not bought, except a free wave-end level inside a burst's confirmation window (at most one per wave per row, never more than `k`).
- Bursts only for taps backed by a verified model quote with a curve `index`. Unmodeled rows, manual buys and the Workshop keep one tap.
- `AutopilotPolicy.burst_price_ceiling = None` means "no burst" (`k <= 1`). Production gets a ceiling only from Task 6, so Tasks 1-5 cannot send a multi-tap burst live.
- Tests: write the test first. Run only the touched test file or the named tests with `uv run pytest <file>[::name] -p no:allure_pytest -q`, with a long Bash timeout (pytest can hang after its summary line; the summary line is the result). Never run `pytest tests/` or any directory sweep.
- Existing tests that encode today's behaviour get `monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', False)` instead of being rewritten, so the kill-switch path stays covered.
- Line numbers cite the base commit (`e0d0aad`); earlier tasks shift them, so locate each edit by the quoted code.
- Commit only the files named in each task, never `git add -A`. `docs/` is gitignored, so plan and spec files need `git add -f`.
- Do not run the bot or touch an emulator/ADB device, except in Task 9 and only after the user's explicit go-ahead.

## Review Focus

1. **Over-counting a burst from a bad price read** (abbreviated `1.02K` ranges, raw text that disagrees with the parsed price, a price that matches no level). The fix is to wait instead of guessing. Test: Task 5 `test_an_undecidable_price_waits_then_falls_back_to_the_value`, which has three parametrized reads.
2. **Tally double-count or restart undercount.** `max(db, tally)` must not become a sum. The tally must start from the DB counts when a run is first seen, or a mid-run restart undercounts. It resets per run. Test: Task 3 `test_the_in_memory_tally_replaces_the_receipt_fence`.
3. **Policy churn resetting the row search every frame.** `step()` sets `self.search = None` whenever the policy changes, so a ceiling tied to live cash would make the bot scroll forever. Test: Task 6 `test_rich_burst_ceiling_does_not_follow_live_cash`.
4. **Pool escape through the tab/visible narrowing.** A visible, cheaper, non-pool row (`defense_absolute`) must never be chosen. Test: Task 6 `test_rich_mode_prefers_visible_then_tab_then_global[DEFENSE-...-thorns]`.
5. **Reserve or spend-limit breach across a burst.** The reserve applies to the sum of the burst, not to the first price. Test: Task 4 `test_a_modeled_purchase_bursts_within_cash_reserve_and_ceiling`, with its reserve and spend-limit cases.

---

### Task 1: Burst input (config kill switch, `device.tap_burst`, `DeviceSupervisor.tap_burst`)

**Files:**
- Modify: `config.py` (insert after line 28, `BATTLE_FOLLOWUP_SECONDS`)
- Modify: `device.py` (imports lines 8-17; append after `tap`, lines 101-103)
- Modify: `supervisor.py` (`DeviceSupervisor.tap` at 437-439: add a method after it; `GuardedDevice.click` at 511-512: add a method after it)
- Test: `tests/test_device.py`, `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `DeviceSupervisor._action(perform, description, guard=None)` (supervisor.py:376).
- Produces:
  - `config.BATTLE_BURST_ENABLED: bool = True`
  - `config.BATTLE_BURST_TAP_GAP_SECONDS: float = 0.0`
  - `device.burst_command(x: int, y: int, n: int, gap_s: float = 0.0) -> str`
  - `device.tap_burst(device: Any, x: int, y: int, n: int, gap_s: float = 0.0) -> None`, which calls `device.tap_burst(x, y, n, gap_s)` when the object has one, else `device.shell(burst_command(...))`
  - `DeviceSupervisor.tap_burst(x: int, y: int, n: int, gap_s: float) -> None`
  - `GuardedDevice.tap_burst(x: int, y: int, n: int, gap_s: float) -> None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_device.py`:

```python
def test_burst_command_is_one_shell_line() -> None:
    assert device.burst_command(10, 20, 3, 0) == "input tap 10 20; input tap 10 20; input tap 10 20"
    assert device.burst_command(10, 20, 2, 0.05) == "input tap 10 20; sleep 0.05; input tap 10 20"
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            device.burst_command(10, 20, bad, 0)
    with pytest.raises(ValueError):
        device.burst_command(10, 20, 2, -0.1)


def test_tap_burst_sends_one_shell_command_to_a_raw_device() -> None:
    fake = MagicMock(spec=["shell"])
    device.tap_burst(fake, 10, 20, 3, 0)
    fake.shell.assert_called_once_with("input tap 10 20; input tap 10 20; input tap 10 20")


def test_tap_burst_defers_to_a_supervised_device() -> None:
    fake = MagicMock(spec=["shell", "tap_burst"])
    device.tap_burst(fake, 10, 20, 3, 0.05)
    fake.tap_burst.assert_called_once_with(10, 20, 3, 0.05)
    fake.shell.assert_not_called()
```

Append to `tests/test_supervisor.py`:

```python
class ShellDevice(Device):
    def __init__(self) -> None:
        super().__init__()
        self.commands: list[str] = []

    def shell(self, command: str) -> str:
        self.commands.append(command)
        return ""


def test_a_tap_burst_is_one_checkpointed_input(tmp_path: Path) -> None:
    from device import burst_command
    clock = Clock()
    device = ShellDevice()
    sut = supervisor(tmp_path / "supervisor.json", clock, [device])
    sut.recover()
    with pytest.raises(RuntimeError):
        GuardedDevice(sut).tap_burst(7, 8, 10, 0.0)  # no fresh frame yet
    assert device.commands == []
    assert observed(sut, clock, "before") is RecoveryState.READY
    GuardedDevice(sut).tap_burst(7, 8, 10, 0.0)
    assert device.commands == [burst_command(7, 8, 10, 0.0)]
    assert sut.status().reason == "action_unconfirmed"
    # Ten taps are still one input: nothing else goes out before a new frame.
    with pytest.raises(RuntimeError):
        sut.tap_burst(7, 8, 10, 0.0)
    with pytest.raises(RuntimeError):
        sut.tap(7, 8)
    assert device.commands == [burst_command(7, 8, 10, 0.0)]
    assert device.taps == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_device.py tests/test_supervisor.py -k "burst" -p no:allure_pytest -q`
Expected: FAIL with `AttributeError: module 'device' has no attribute 'burst_command'` and `AttributeError: 'GuardedDevice' object has no attribute 'tap_burst'`.

- [ ] **Step 3: Write minimal implementation**

`config.py`, after line 28 (`BATTLE_FOLLOWUP_SECONDS: float = 0.25`):

```python

# --- Fast battle buying -----------------------------------------------------
# Kill switch for burst taps, quotes valid across waves, the in-memory battle
# purchase tally, tab-sticky choice, directional row seek and the faster
# buying scan. False restores the one-level-per-decision path everywhere.
BATTLE_BURST_ENABLED: bool = True
# Pause between the taps of one burst, inside its single `adb shell` command.
# 0 sends them back to back; set it from tools/probe_tap_burst.py if the game
# drops taps, and record the measurement beside the value.
BATTLE_BURST_TAP_GAP_SECONDS: float = 0.0
```

`device.py`: add `from typing import Any` to the imports (after `import logging`), then append after `tap`:

```python


def burst_command(x: int, y: int, n: int, gap_s: float = 0.0) -> str:
    """One `adb shell` line tapping (x, y) `n` times, `gap_s` seconds apart."""
    if type(n) is not int or n < 1:
        raise ValueError("a tap burst needs at least one tap")
    if gap_s < 0:
        raise ValueError("a tap gap may not be negative")
    line = f"input tap {int(x)} {int(y)}"
    separator = f"; sleep {gap_s:g}; " if gap_s > 0 else "; "
    return separator.join([line] * n)


def tap_burst(device: Any, x: int, y: int, n: int, gap_s: float = 0.0) -> None:
    """Send `n` taps as one input.

    A supervised device brings its own `tap_burst`, which checkpoints the
    whole burst as a single action; a raw adbutils device gets one shell
    command.
    """
    burst = getattr(device, "tap_burst", None)
    if callable(burst):
        burst(x, y, n, gap_s)
    else:
        device.shell(burst_command(x, y, n, gap_s))
```

`supervisor.py`, after `DeviceSupervisor.tap` (line 439):

```python

    def tap_burst(self, x: int, y: int, n: int, gap_s: float) -> None:
        """Several taps in one ADB command, checkpointed as one input.

        The next action still waits for a fresh frame, so the
        one-input-per-screenshot invariant holds for the whole burst.
        """
        from device import burst_command
        command = burst_command(x, y, n, gap_s)
        self._action(lambda: self._device.shell(command), f"tap burst {n}x ({x}, {y})")
```

`supervisor.py`, after `GuardedDevice.click` (line 512):

```python

    def tap_burst(self, x: int, y: int, n: int, gap_s: float) -> None:
        self.supervisor.tap_burst(x, y, n, gap_s)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_device.py tests/test_supervisor.py -p no:allure_pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add config.py device.py supervisor.py tests/test_device.py tests/test_supervisor.py
git commit -m "Add supervised tap bursts behind the fast battle buying switch

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Quotes valid across waves (`battle_prices`, autopilot wave refusal, strategy reconcile trip)

**Files:**
- Modify: `fleet/battle_prices.py` (imports 4-10; class docstring 34-39; `verified=` at 111-112; write at 123-125)
- Modify: `autopilot.py` (quote checks at 539-546, the `quote.get('wave')` line 543)
- Modify: `fleet/strategy_blocks.py` (imports 4-11; reconcile trip 1068-1070)
- Test: `tests/test_battle_prices.py`, `tests/test_autopilot.py`, `tests/test_strategy_blocks.py`

**Interfaces:**
- Consumes: `config.BATTLE_BURST_ENABLED` (Task 1).
- Produces:
  - `BattlePrices.invalidate(upgrade_id: str) -> None` drops one row's state and persists the change.
  - `BattlePrices._save() -> None`
  - With the switch on, quotes always come out `verified=True` while the row state exists.

- [ ] **Step 1: Write the failing test**

`tests/test_battle_prices.py`: add `import config` and `import pytest` under the existing imports. Pin the legacy test to the old behaviour by changing its signature and first line (line 31):

```python
def test_pending_frame_cannot_double_advance_and_uncertain_wave_resyncs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "BATTLE_BURST_ENABLED", False)  # pins the per-wave expiry
```

Keep the rest of that test's body. Then append:

```python
def test_quotes_stay_verified_across_waves_until_a_fresh_read_disagrees(tmp_path: Path) -> None:
    model = BattlePrices(tmp_path, "a")
    model.update(run_id=7, wave=4, counts={}, rows={"attack_speed": row(5)}, now=100)
    quote = model.update(run_id=7, wave=9, counts={"attack_speed": 1}, rows={}, now=103)["attack_speed"]
    assert quote["verified"] and quote["price"] == 7
    # A fresh read that matches no level of the curve drops the quote.
    assert "attack_speed" not in model.update(run_id=7, wave=9, counts={"attack_speed": 1},
        rows={"attack_speed": row(987654321, at=104)}, now=104)


def test_invalidate_forgets_one_row_until_its_next_fresh_read(tmp_path: Path) -> None:
    model = BattlePrices(tmp_path, "a")
    model.update(run_id=7, wave=4, counts={}, rows={"attack_speed": row(5), "health": row(10)}, now=100)
    model.invalidate("attack_speed")
    quotes = model.update(run_id=7, wave=4, counts={}, rows={}, now=101)
    assert "attack_speed" not in quotes and quotes["health"]["price"] == 10
    assert "attack_speed" not in BattlePrices(tmp_path, "a").rows  # persisted
    quotes = model.update(run_id=7, wave=4, counts={}, rows={"attack_speed": row(7, at=102)}, now=102)
    assert quotes["attack_speed"]["index"] == 1
```

`tests/test_autopilot.py`, append:

```python
@pytest.mark.parametrize('enabled', [True, False])
def test_a_model_quote_from_an_earlier_wave_still_authorizes_the_tap(
        monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', enabled)
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    quote = dict(account_id='a', run_id=7, upgrade_id='attack_speed', source='model',
                 status='available', verified=True, price=5, value=row.value,
                 wave=int(obs.combat['wave']) - 1)
    policy = replace(policy, rules=(UpgradeRule('attack_speed'),), single_purchase=True,
                     decision_token='a:1:7:0', modeled_pool=True, battle_price_quote=quote)
    bot.step(frame, device, policy, cash=100, observation=obs, run_id=7)
    assert len(device.actions) == (1 if enabled else 0)
```

`tests/test_strategy_blocks.py`: add `import config` after `import pytest` (line 6). Pin both legacy tests by adding a `monkeypatch: pytest.MonkeyPatch` parameter and this first line to each:

- `test_model_pool_reconciles_cheapest_stale_wave_before_spending` (line 1210)
- `test_visible_batch_cannot_skip_a_stale_cheaper_quote` (line 1314)

```python
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', False)  # pins the wave reconcile trip
```

Append:

```python
def test_burst_mode_buys_the_cheapest_quote_without_a_wave_reconcile_trip(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    block = pool(upgrade_ids=['health', 'attack_speed'], selection='cheapest', price_source='model')
    quote = dict(account_id='account', run_id=7, status='available', source='model', value=1)
    f = replace(battle_facts(), battle_price_quotes={
        'health': dict(quote, price=10, verified=True),
        'attack_speed': dict(quote, price=5, verified=False)})
    result = blocks.evaluate_program(route([block], lane='battle'), f, None, 'battle')
    assert result.trace.observation_ids == ()
    assert result.status == 'observed' and result.decision.upgrade_id == 'attack_speed'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_battle_prices.py tests/test_autopilot.py::test_a_model_quote_from_an_earlier_wave_still_authorizes_the_tap tests/test_strategy_blocks.py::test_burst_mode_buys_the_cheapest_quote_without_a_wave_reconcile_trip -p no:allure_pytest -q`
Expected: FAIL. The cross-wave quote reports `verified=False`, `BattlePrices` has no `invalidate`, `[True]` taps nothing ("Battle price quote requires current run evidence"), and the strategy returns the reconcile observation.

- [ ] **Step 3: Write minimal implementation**

`fleet/battle_prices.py`: add `import config` after `from typing import Any, Mapping`, and replace the class docstring:

```python
class BattlePrices:
    """A price index advances only with a confirmed purchase count or calibration.

    DB runs may start mid-battle: even a new run must read prices initially.
    With config.BATTLE_BURST_ENABLED a quote stays verified across waves
    until a fresh read disagrees, the run changes, or burst confirmation
    invalidates it; the pre-tap row check still catches free wave-end levels.
    With the switch off, a quote verifies only within the wave it was read.
    """
```

Replace lines 111-112:

```python
            quote = dict(state, account_id=self.account_id, run_id=run_id, source="model",
                         verified=(config.BATTLE_BURST_ENABLED or state.get("wave") == wave
                                   or state["status"] in {"locked", "maxed"}))
```

Replace lines 123-125:

```python
        if not self.read_only and before != json.dumps((self.run_id, self.wave, self.rows), sort_keys=True):
            self._save()
        return quotes

    def invalidate(self, upgrade_id: str) -> None:
        """Forget one row's index so its next fresh read re-indexes it.

        Burst confirmation calls this when the screen contradicts the model:
        a price behind the quote, or a level proven only by a value change.
        """
        if self.rows.pop(upgrade_id, None) is not None and not self.read_only:
            self._save()

    def _save(self) -> None:
        _write_json_atomic(self.path, dict(account_id=self.account_id, run_id=self.run_id,
            catalog_revision=catalog()["source_revision"], wave=self.wave, rows=self.rows))
```

`autopilot.py`, replace line 543:

```python
                    or (not config.BATTLE_BURST_ENABLED
                        and quote.get('wave') != observation.combat.get('wave'))
```

`fleet/strategy_blocks.py`: add `import config` after `import builds` (line 10). Replace lines 1068-1070:

```python
                    if (not config.BATTLE_BURST_ENABLED and source == 'model'
                            and (quote := model_quote(chosen)) and not quote.get('verified')):
                        return _Choice(identity, chosen, 'Reconcile the cheapest candidate after a wave change',
                                       observation_ids=(chosen,), price_source='model')
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_battle_prices.py tests/test_autopilot.py tests/test_strategy_blocks.py tests/test_blender_battle_update.py -p no:allure_pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add fleet/battle_prices.py autopilot.py fleet/strategy_blocks.py tests/test_battle_prices.py tests/test_autopilot.py tests/test_strategy_blocks.py
git commit -m "Keep battle price quotes verified across waves

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: In-memory purchase tally replaces the receipt fence

**Files:**
- Modify: `fleet/reroll_progress.py` (imports 3-29; `__init__` 69-101; `await_battle_receipt` 673-678; fence in `battle_policy` 714-723)
- Modify: `autopilot.py` (`step` signature line 373; legacy receipt call at 469-470)
- Modify: `tower_bot.py` (`record_battle_receipt` at 2874-2876)
- Test: `tests/test_strategy_blocks.py`, `tests/test_autopilot.py`, `tests/test_autopilot_loop.py`

**Interfaces:**
- Consumes: `config.BATTLE_BURST_ENABLED`.
- Produces:
  - `RerollProgress.note_battle_levels(run_id: int | None, upgrade_id: str, levels: int) -> None`
  - `RerollProgress._tallied_counts(run_id: int | None, counts: dict[str, int] | None) -> dict[str, int] | None`
  - `BattleAutopilot.step(..., record_receipt: Callable[[int | None, str, int], None] | None = None)`, called as `record_receipt(sequence, upgrade_id, levels)`
  - Test helpers in `tests/test_strategy_blocks.py`: `_assigned_model_progress(tmp_path) -> (progress, root, rows, base)` and `_purchases(root, run_id, *seqs)`
  - Test helper in `tests/test_autopilot_loop.py`: `_BattleProgress` (a fake of the reroll surface the in-run scan touches)

- [ ] **Step 1: Write the failing test**

`tests/test_strategy_blocks.py`: pin the legacy fence test. Change the signature at line 1239 and add the first line:

```python
def test_assigned_model_pool_advances_only_after_persisted_receipt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', False)  # pins the receipt fence
```

Keep the rest of its body. Append:

```python
def _assigned_model_progress(tmp_path: Path) -> tuple[Any, Path, dict[str, dict[str, Any]], Any]:
    """A registered worker whose assigned battle route is one modeled cheapest pool."""
    import time
    from account_state import AccountState
    from fleet.build_route import RouteDocument
    from fleet.build_route_store import BuildRouteStore
    from fleet.build_route_runtime import BuildRouteRuntime
    from fleet.reroll_progress import RerollProgress
    from policy import AutopilotPolicy
    from tests.test_build_route_integration import _registered
    root = _registered(tmp_path, 'Air_38', 'account')
    raw = RouteDocument.compatibility().to_dict()
    raw['baseline']['battle'].update(mode='blocks', blocks=[pool(
        upgrade_ids=['health', 'attack_speed'], selection='cheapest', price_source='model',
        batch_size=5, max_price_premium_pct=25)])
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, 'operator')
    progress = RerollProgress(root, 'account', AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, 'Air_38', 'account')
    rows = {uid: dict(status='available', value=1, price=price, observed_at=time.time())
            for uid, price in [('health', 10), ('attack_speed', 5)]}
    return progress, root, rows, AutopilotPolicy(enabled=True)


def _purchases(root: Path, run_id: int, *seqs: int) -> None:
    """What the async event sink commits: one BattlePurchased row per level."""
    import json
    import db
    with db.connect(root / 'tower_bot.db') as conn:
        for seq in seqs:
            conn.execute("INSERT INTO events(seq,run_id,ts,type,detail) VALUES(?,?,1,'BattlePurchased',?)",
                         (seq, run_id, json.dumps({'upgrade_id': 'attack_speed'})))


def test_the_in_memory_tally_replaces_the_receipt_fence(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    progress, root, rows, base = _assigned_model_progress(tmp_path)
    _purchases(root, 2, 1)  # bought before this process started

    def policy(run_id: int = 2, **extra: Any) -> Any:
        return progress.battle_policy(base, rows, run_id=run_id, wave=2, cash=100, **extra)

    assert policy().decision_token.endswith(':1')  # the tally starts from the database
    rows['attack_speed'].update(price=None, status='unreadable')
    progress.note_battle_levels(2, 'attack_speed', 3)
    confirmed = policy(after_receipt_sequence=3)
    assert confirmed.enabled  # no fence: the receipt never disables buying
    assert confirmed.decision_token.endswith(':4')
    _purchases(root, 2, 2, 3)  # the sink catches up partway
    assert policy().decision_token.endswith(':4')  # max(db, tally), never the sum
    _purchases(root, 2, 4, 5)
    assert policy().decision_token.endswith(':5')  # the database may lead too
    assert policy(run_id=3).decision_token.endswith(':0')  # a new run starts over
    progress.note_battle_levels(2, 'attack_speed', 9)  # a receipt for the old run
    assert policy(run_id=3).decision_token.endswith(':0')
```

`tests/test_autopilot.py`: the batch test's receipt callback takes the new arguments. At line 553, replace `record_receipt=receipts.append)` with:

```python
                 record_receipt=lambda sequence, upgrade_id, levels: receipts.append(sequence))
```

Append:

```python
def test_a_modeled_receipt_names_the_upgrade_and_its_levels() -> None:
    from policy import UpgradeRule
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    policy = replace(policy, rules=(UpgradeRule('attack_speed'),), single_purchase=True,
        decision_token='a:1:7:0', modeled_pool=True, battle_price_quote=dict(
            account_id='a', run_id=7, upgrade_id='attack_speed', source='model',
            status='available', verified=True, price=5, value=row.value,
            wave=int(obs.combat['wave']), sequence=6))
    bot.step(frame, device, policy, cash=100, observation=obs, run_id=7)
    receipts: list[tuple] = []
    changed = replace(obs, observed_at=102, rows=(replace(row, value=row.value + .05, observed_at=102),))
    bot.step(frame, device, policy, cash=95, observation=changed, run_id=7,
             record_receipt=lambda *args: receipts.append(args))
    assert receipts == [(6, 'attack_speed', 1)]
```

`tests/test_autopilot_loop.py`, append:

```python
class _BattleProgress:
    """Just the reroll_progress surface the in-run scan touches."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.battle_prices = SimpleNamespace(
            invalidate=lambda upgrade_id: self.calls.append(('invalidate', upgrade_id)))

    def speed_target(self) -> None:
        return None

    def battle_policy(self, base, rows, **kwargs):  # noqa: ANN001, ANN201 - test fake
        self.calls.append(('policy', kwargs.get('battle_tab')))
        return base

    def note_battle_levels(self, run_id, upgrade_id, levels) -> None:  # noqa: ANN001
        self.calls.append(('levels', upgrade_id, levels))

    def await_battle_receipt(self, run_id, sequence) -> None:  # noqa: ANN001
        self.calls.append(('fence', sequence))


@pytest.mark.parametrize('enabled', [True, False])
def test_a_battle_receipt_feeds_the_tally_instead_of_the_fence(
    bot_in_run_on: Callable[[str], TowerBot], monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    import config
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', enabled)
    bot = bot_in_run_on('in_run_lit')
    bot.controls.apply({'autopilot': {'enabled': True}})
    progress = _BattleProgress()
    bot.reroll_progress = progress
    monkeypatch.setattr(bot.autopilot, 'step',
                        lambda *args, **kwargs: bool(kwargs['record_receipt'](4, 'attack_speed', 3)))
    bot.run_once()
    receipts = [call for call in progress.calls if call[0] in ('levels', 'fence')]
    assert receipts == ([('levels', 'attack_speed', 3)] if enabled else [('fence', 4)])
```

If `run_once` raises `AttributeError` on `_BattleProgress`, the in-run path reached another `reroll_progress` method. Add that method to the fake, returning `None`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_strategy_blocks.py::test_the_in_memory_tally_replaces_the_receipt_fence tests/test_autopilot.py::test_a_modeled_receipt_names_the_upgrade_and_its_levels tests/test_autopilot_loop.py::test_a_battle_receipt_feeds_the_tally_instead_of_the_fence -p no:allure_pytest -q`
Expected: FAIL. `RerollProgress` has no `note_battle_levels`, the receipt is `(6,)`, and the closure takes one positional argument (`TypeError`).

- [ ] **Step 3: Write minimal implementation**

`fleet/reroll_progress.py`: add `import config` after `import db` (line 14). In `__init__`, after `self._route_jar = 0` (line 101):

```python
        # Levels this process confirmed in the current run, per upgrade,
        # ahead of the asynchronous event sink. Starts from the database
        # counts the first time a run is seen, so a restart mid-run loses
        # nothing; battle_policy reads max(database, tally) per upgrade.
        self._battle_tally: tuple[int | None, dict[str, int]] = (None, {})
```

After `await_battle_receipt` (line 678):

```python

    def note_battle_levels(self, run_id: int | None, upgrade_id: str, levels: int) -> None:
        """Count confirmed battle levels before the event sink commits them."""
        if type(run_id) is not int or type(levels) is not int or levels < 1:
            return
        tally_run, tally = self._battle_tally
        if tally_run != run_id:
            return  # never started from this run's database counts; the sink catches up
        tally[upgrade_id] = tally.get(upgrade_id, 0) + levels

    def _tallied_counts(self, run_id: int | None,
                        counts: dict[str, int] | None) -> dict[str, int] | None:
        """Per-upgrade max(database, tally); the tally restarts with each run."""
        if counts is None or type(run_id) is not int:
            return counts
        if self._battle_tally[0] != run_id:
            self._battle_tally = (run_id, dict(counts))
        tally = self._battle_tally[1]
        for upgrade_id, count in counts.items():
            tally[upgrade_id] = max(tally.get(upgrade_id, 0), count)
        return dict(tally)
```

In `battle_policy`, replace lines 715-723 (from `if after_receipt_sequence is not None:` through `self._battle_receipt_fence = None`):

```python
            if config.BATTLE_BURST_ENABLED:
                # The tally replaces the receipt fence: a confirmed level
                # counts at once instead of disabling buying until the sink
                # commits it.
                counts = self._tallied_counts(run_id, counts)
            else:
                if after_receipt_sequence is not None:
                    self.await_battle_receipt(run_id, after_receipt_sequence)
                fence = getattr(self, '_battle_receipt_fence', None)
                if fence is not None:
                    if fence[0] == run_id and (counts is None or sum(counts.values()) < fence[1]):
                        # The event sink commits asynchronously. Do not calibrate
                        # a post-purchase frame against the old receipt count.
                        return replace(base, enabled=False, rules=())
                    self._battle_receipt_fence = None
```

`autopilot.py`, line 373:

```python
             record_receipt: Callable[[int | None, str, int], None] | None = None) -> bool:
```

and lines 469-470:

```python
                if self._pending_modeled and record_receipt is not None:
                    record_receipt(self._pending_sequence, after.upgrade_id, 1)
```

`tower_bot.py`, replace lines 2874-2876:

```python
                    def record_battle_receipt(sequence: int | None, upgrade_id: str, levels: int) -> None:
                        if self.reroll_progress is None:
                            return
                        if config.BATTLE_BURST_ENABLED:
                            self.reroll_progress.note_battle_levels(
                                self.runs.current_id, upgrade_id, levels)
                        else:
                            self.reroll_progress.await_battle_receipt(self.runs.current_id, sequence)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_strategy_blocks.py tests/test_autopilot.py tests/test_autopilot_loop.py tests/test_reroll_progress.py -p no:allure_pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add fleet/reroll_progress.py autopilot.py tower_bot.py tests/test_strategy_blocks.py tests/test_autopilot.py tests/test_autopilot_loop.py
git commit -m "Replace the battle receipt fence with an in-memory run tally

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Burst sizing and `AutopilotPolicy.burst_price_ceiling`

**Files:**
- Modify: `config.py` (after the Task 1 block)
- Modify: `policy.py` (fields 137-152; validation after line 202; `from_dict` line 230)
- Modify: `autopilot.py` (imports 11 and 20; `__init__` after line 178; module function before `class Search` at 145; tap block 624-640)
- Test: `tests/test_autopilot.py`, `tests/test_autopilot_policy.py`

**Interfaces:**
- Consumes: `device.tap_burst` (Task 1); `config.BATTLE_BURST_ENABLED`, `config.BATTLE_BURST_TAP_GAP_SECONDS`.
- Produces:
  - `config.BATTLE_BURST_MAX: int = 10`
  - `AutopilotPolicy.burst_price_ceiling: int | None = None`: runtime only, a positive int, `None` means no burst.
  - `autopilot.burst_size(curve: Sequence[int], index: int, budget: int, ceiling: int) -> int`
  - `BattleAutopilot._pending_curve: tuple[int, ...]` (empty unless the tap was curve-backed with the switch on), `_pending_index: int | None`, `_pending_k: int`
  - Test helpers in `tests/test_autopilot.py`: `Device.tap_burst` (records `("burst", x, y, n)`), `curve_of(upgrade_id)`, `modeled(policy, row, index, *, sequence=0, ceiling=None, wave=1)`, `shown(observation, row, index, at, **changes)`

- [ ] **Step 1: Write the failing test**

`tests/test_autopilot.py`: add to the `Device` class (after `swipe`, line 22):

```python
    def tap_burst(self, x: int, y: int, n: int, gap_s: float) -> None:
        self.actions.append(("burst", x, y, n))
```

Append:

```python
def curve_of(upgrade_id: str) -> list[int]:
    from fleet.battle_prices import catalog
    return catalog()['curves'][upgrade_id]


def modeled(policy: Any, row: Any, index: int, *, sequence: int = 0,
            ceiling: int | None = None, wave: int = 1) -> Any:
    """A Blender-style single-purchase policy holding a verified model quote."""
    from policy import UpgradeRule
    curve = curve_of(row.upgrade_id)
    return replace(policy, rules=(UpgradeRule(row.upgrade_id),), single_purchase=True,
        decision_token=f'a:1:7:{sequence}', modeled_pool=True, burst_price_ceiling=ceiling,
        battle_price_quote=dict(account_id='a', run_id=7, upgrade_id=row.upgrade_id,
            source='model', status='available', verified=True, price=curve[index],
            index=index, value=row.value + index * .05, wave=wave, sequence=sequence,
            batch_size=5, rule_id='cheap'))


def shown(observation: Any, row: Any, index: int, at: float, **changes: Any) -> Any:
    """`observation` showing only `row`, priced at curve level `index`."""
    price = curve_of(row.upgrade_id)[index]
    fields = dict(price=price, raw_price=f'${price}', value=row.value + index * .05,
                  observed_at=at)
    return replace(observation, observed_at=at, rows=(replace(row, **{**fields, **changes}),))


def test_burst_size_stops_at_cash_ceiling_curve_end_and_the_maximum() -> None:
    from autopilot import burst_size
    curve = [5, 7, 10, 15, 21, 28, 36]
    assert burst_size(curve, 0, 100, 10_000) == 6  # 86 fits, 122 does not
    assert burst_size(curve, 0, 80, 10_000) == 5   # 58 fits, 86 does not
    assert burst_size(curve, 0, 100, 20) == 4      # 21 is over the ceiling
    assert burst_size(curve, 5, 10_000, 10_000) == 2  # only two levels left
    assert burst_size([1] * 30, 0, 10_000, 10_000) == config.BATTLE_BURST_MAX
    assert burst_size(curve, 0, 4, 10_000) == 0


@pytest.mark.parametrize('reserve, spend_pct, ceiling, taps', [
    (0, 100, 10_000, 6),   # 5+7+10+15+21+28 = 86 of 100
    (20, 100, 10_000, 5),  # the reserve applies to the whole burst
    (0, 50, 10_000, 4),    # so does the route spend limit
    (0, 100, 20, 4),       # no level above the ceiling
    (0, 100, None, 1),     # no ceiling: one tap, as today
])
def test_a_modeled_purchase_bursts_within_cash_reserve_and_ceiling(
        reserve: int, spend_pct: int, ceiling: int | None, taps: int) -> None:
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    policy = replace(modeled(policy, row, 0, ceiling=ceiling), cash_reserve=reserve,
                     cash_spend_limit_pct=spend_pct)
    bot.step(frame, device, policy, cash=100, observation=shown(obs, row, 0, at=100), run_id=7)
    assert device.actions == [('tap', *row.tap) if taps == 1 else ('burst', *row.tap, taps)]
    assert (bot._pending_index, bot._pending_k) == (0, taps)


def test_a_ceiling_below_the_next_level_saves_instead_of_tapping() -> None:
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    bot.step(frame, device, modeled(policy, row, 0, ceiling=4), cash=100,
             observation=shown(obs, row, 0, at=100), run_id=7)
    assert device.actions == []
    assert bot.state.snapshot()['phase'] == 'saving'


def test_the_kill_switch_keeps_one_tap_per_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', False)
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    bot.step(frame, device, modeled(policy, row, 0, ceiling=10_000), cash=100,
             observation=shown(obs, row, 0, at=100), run_id=7)
    assert device.actions == [('tap', *row.tap)]
    assert (bot._pending_curve, bot._pending_k) == ((), 1)
```

`tests/test_autopilot_policy.py`, append:

```python
def test_burst_price_ceiling_is_runtime_only_and_positive() -> None:
    assert AutopilotPolicy(burst_price_ceiling=5).burst_price_ceiling == 5
    assert 'burst_price_ceiling' not in AutopilotPolicy(burst_price_ceiling=5).to_dict()
    for bad in (0, -1, 2.5, True):
        with pytest.raises(PolicyError):
            AutopilotPolicy(burst_price_ceiling=bad)
    with pytest.raises(PolicyError):
        AutopilotPolicy.from_dict({'burst_price_ceiling': 5})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_autopilot.py -k "burst or ceiling or kill_switch" tests/test_autopilot_policy.py::test_burst_price_ceiling_is_runtime_only_and_positive -p no:allure_pytest -q`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'burst_price_ceiling'` and `ImportError: cannot import name 'burst_size'`.

- [ ] **Step 3: Write minimal implementation**

`config.py`, append to the Task 1 block:

```python
# Most levels one burst may buy.
BATTLE_BURST_MAX: int = 10
```

`policy.py`: after `battle_price_quote: Mapping[str, Any] | None = None` (line 150):

```python
    # Highest single-level price a battle burst may include. Runtime-only;
    # None buys one level per decision.
    burst_price_ceiling: int | None = None
```

After the `max_scrolls` checks (line 202):

```python
        if self.burst_price_ceiling is not None and (
                type(self.burst_price_ceiling) is not int or self.burst_price_ceiling <= 0):
            raise PolicyError("burst_price_ceiling", "burst price ceiling must be a positive integer")
```

Line 230:

```python
        known = {field.name for field in dataclasses.fields(cls)} - {
            "modeled_pool", "battle_price_quote", "burst_price_ceiling"}
```

`autopilot.py`:
- line 11: `from typing import Any, Callable, Sequence`
- line 20: `from device import Image, tap, tap_burst`
- in `__init__`, after `self._pending_rule_id: str | None = None` (line 178):

```python
        # A curve-backed modeled tap: the price curve, the level the tap
        # started from and how many taps the burst sent.
        self._pending_curve: tuple[int, ...] = ()
        self._pending_index: int | None = None
        self._pending_k = 1
```

- before `@dataclass class Search` (line 145):

```python
def burst_size(curve: Sequence[int], index: int, budget: int, ceiling: int) -> int:
    """Levels one burst may buy, starting at curve level `index`.

    The largest k up to config.BATTLE_BURST_MAX whose prices
    curve[index:index + k] each stay at or under `ceiling`, sum to at most
    `budget` and stay inside the curve. 0 means not even the next level fits.
    """
    count = spent = 0
    while count < config.BATTLE_BURST_MAX and index + count < len(curve):
        price = curve[index + count]
        if price > ceiling or spent + price > budget:
            break
        spent += price
        count += 1
    return count


```

- replace lines 624-626 (`tap(device, *row.tap)` / `self._mark_tapped(row)` / `self.pending = (row, now)`):

```python
        count, curve, index = 1, (), None
        if quote is not None and config.BATTLE_BURST_ENABLED:
            from fleet.battle_prices import catalog
            steps = tuple(catalog()['curves'].get(target, ()))
            position = quote.get('index')
            if type(position) is int and 0 <= position < len(steps) and steps[position] == quote['price']:
                curve, index = steps, position
                if policy.burst_price_ceiling is not None:
                    # The reserve and the spend limit bound the whole burst,
                    # not just its first level.
                    budget = min(actual_cash - policy.cash_reserve,
                                 actual_cash * policy.cash_spend_limit_pct // 100)
                    count = burst_size(curve, index, budget, policy.burst_price_ceiling)
                    if count == 0:
                        self._reset_batch()
                        self._decide("saving", f"Saving cash for {row.name}; above the burst price ceiling",
                                     target)
                        return False
        if count > 1:
            tap_burst(device, *row.tap, count, config.BATTLE_BURST_TAP_GAP_SECONDS)
        else:
            tap(device, *row.tap)
        self._mark_tapped(row)
        self.pending = (row, now)
        self._pending_curve, self._pending_index, self._pending_k = curve, index, count
```

Lines 627-644 (`self._pending_modeled = ...` through `return True`) stay unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_autopilot.py tests/test_autopilot_policy.py tests/test_perception.py -p no:allure_pytest -q`
Expected: all pass. `test_perception.py` runs because one of its tests drives a modeled tap through `step`.

- [ ] **Step 5: Commit**

```bash
git add config.py policy.py autopilot.py tests/test_autopilot.py tests/test_autopilot_policy.py
git commit -m "Size modeled battle purchases as curve-backed tap bursts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Burst confirmation from the price curve

**Files:**
- Modify: `autopilot.py` (module helper before `burst_size`; `step` signature 367-373; pending block 435-488; new methods after `_seek`)
- Modify: `tower_bot.py` (`autopilot.step` call at 2879-2886; closure next to `record_battle_receipt`)
- Test: `tests/test_autopilot.py`, `tests/test_autopilot_loop.py`

**Interfaces:**
- Consumes: `_pending_curve/_pending_index/_pending_k` (Task 4); `BattlePrices.invalidate` (Task 2); `record_receipt(sequence, upgrade_id, levels)` (Task 3); `price_matches` (fleet/battle_prices.py:21); `price_number` (perception.py:46).
- Produces:
  - `BattleAutopilot.step(..., invalidate_quote: Callable[[str], None] | None = None)`
  - `autopilot._readable_price(row: ObservedUpgrade | None) -> int | None`
  - `BattleAutopilot._burst_outcome(before, after, waited: float) -> tuple[int, bool] | None` returns `(levels, invalidate)`, or `None` to keep waiting.
  - `BattleAutopilot._confirm_burst(before, after, levels: int, record_receipt) -> None`
  - local `confirmed_now: bool` in `step` (Task 8 reads it)

- [ ] **Step 1: Write the failing test**

`tests/test_autopilot.py`: pin two legacy batch tests by adding a `monkeypatch: pytest.MonkeyPatch` parameter and this first line to each:

- `test_modeled_batch_never_replays_a_receipt_while_counter_is_stale` (line 561)
- `test_modeled_batch_stops_for_changed_evidence_or_controls` (line 580; keep `interruption: str`)

```python
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', False)  # pins the one-level receipt
```

Append:

```python
def after_burst(index: int = 0, ceiling: int = 10_000) -> tuple:
    """A bot that has just sent a burst on Attack Speed from curve level `index`."""
    from autopilot import BattleAutopilot
    _, device, frame, obs, policy = parts()
    bus = Bus()
    bot = BattleAutopilot(bus=bus)
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    bot.step(frame, device, modeled(policy, row, index, sequence=10, ceiling=ceiling),
             cash=100, observation=shown(obs, row, index, at=100), run_id=7)
    return bot, device, frame, obs, policy, row, bus


def confirm(bot: Any, device: Any, frame: Any, policy: Any, row: Any, observation: Any,
            receipts: list, invalidated: list, index: int = 0) -> None:
    bot.step(frame, device, modeled(policy, row, index, sequence=10, ceiling=10_000),
             cash=10, observation=observation, run_id=7,
             record_receipt=lambda *args: receipts.append(args),
             invalidate_quote=invalidated.append)


def purchases(bus: Bus) -> list[tuple[str, int | None]]:
    import events
    return [(e.upgrade_id, e.price) for e in bus.published if isinstance(e, events.BattlePurchased)]


def test_a_burst_counts_the_levels_its_price_jump_proves() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst()
    assert device.actions == [('burst', *row.tap, 6)]
    receipts: list = []
    invalidated: list = []
    confirm(bot, device, frame, policy, row, shown(obs, row, 4, at=101), receipts, invalidated)
    assert purchases(bus) == [('attack_speed', price) for price in (5, 7, 10, 15)]
    assert receipts == [(13, 'attack_speed', 4)]  # the last level's sequence
    assert invalidated == []
    assert bot.state.snapshot()['verified_purchases'] == 4
    assert bot.pending is None


def test_a_jump_past_the_burst_counts_at_most_its_taps() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst(ceiling=20)
    assert device.actions == [('burst', *row.tap, 4)]
    confirm(bot, device, frame, policy, row, shown(obs, row, 6, at=101), [], [])
    assert len(purchases(bus)) == 4


def test_a_maxed_row_confirms_the_whole_burst() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst()
    receipts: list = []
    maxed = shown(obs, row, 0, at=101, status='maxed', price=None, raw_price=None)
    confirm(bot, device, frame, policy, row, maxed, receipts, [])
    assert purchases(bus) == [('attack_speed', price) for price in (5, 7, 10, 15, 21, 28)]
    assert receipts == [(15, 'attack_speed', 6)]


@pytest.mark.parametrize('changes', [
    dict(price=None, raw_price=None, status='unreadable'),  # no price at all
    dict(price=6, raw_price='$6'),                          # matches no curve level
    dict(price=21, raw_price='$5'),                         # digits disagree with the parse
])
def test_an_undecidable_price_waits_then_falls_back_to_the_value(changes: dict[str, Any]) -> None:
    bot, device, frame, obs, policy, row, bus = after_burst()
    receipts: list = []
    invalidated: list = []
    for at in (101, 104, 107.9):
        confirm(bot, device, frame, policy, row, shown(obs, row, 0, at=at, **changes),
                receipts, invalidated)
        assert bot.pending is not None and purchases(bus) == []
    confirm(bot, device, frame, policy, row,
            shown(obs, row, 0, at=108, **{**changes, 'value': row.value + .3}), receipts, invalidated)
    assert purchases(bus) == [('attack_speed', 5)]
    assert receipts == [(10, 'attack_speed', 1)]
    assert invalidated == ['attack_speed']  # the next read re-indexes the row


def test_an_unconfirmed_burst_with_an_unchanged_value_blocks_the_row() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst()
    receipts: list = []
    invalidated: list = []
    confirm(bot, device, frame, policy, row,
            shown(obs, row, 0, at=108, price=None, raw_price=None, status='unreadable'),
            receipts, invalidated)
    assert purchases(bus) == [] and receipts == [] and invalidated == []
    assert bot.pending is None and bot._blocked['attack_speed'] == 168


def test_a_price_behind_the_model_counts_nothing_and_reconciles() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst(index=3)
    assert device.actions == [('burst', *row.tap, 4)]
    receipts: list = []
    invalidated: list = []
    confirm(bot, device, frame, policy, row, shown(obs, row, 1, at=101), receipts, invalidated,
            index=3)
    assert purchases(bus) == [] and receipts == []
    assert invalidated == ['attack_speed']
    assert bot.pending is None and 'attack_speed' not in bot._blocked


def test_a_wave_change_does_not_discard_a_pending_burst() -> None:
    bot, device, frame, obs, policy, row, bus = after_burst()
    later = shown(obs, row, 2, at=101)
    later = replace(later, combat={**later.combat, 'wave': later.combat['wave'] + 1})
    confirm(bot, device, frame, policy, row, later, [], [])
    assert purchases(bus) == [('attack_speed', 5), ('attack_speed', 7)]
```

`tests/test_autopilot_loop.py`, append:

```python
def test_a_burst_invalidation_reaches_the_price_model(
    bot_in_run_on: Callable[[str], TowerBot], monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = bot_in_run_on('in_run_lit')
    bot.controls.apply({'autopilot': {'enabled': True}})
    progress = _BattleProgress()
    bot.reroll_progress = progress
    monkeypatch.setattr(bot.autopilot, 'step',
                        lambda *args, **kwargs: bool(kwargs['invalidate_quote']('attack_speed')))
    bot.run_once()
    assert ('invalidate', 'attack_speed') in progress.calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_autopilot.py -k "burst or maxed or undecidable or behind or wave_change" tests/test_autopilot_loop.py::test_a_burst_invalidation_reaches_the_price_model -p no:allure_pytest -q`
Expected: FAIL with `TypeError: step() got an unexpected keyword argument 'invalidate_quote'` and `KeyError: 'invalidate_quote'`.

- [ ] **Step 3: Write minimal implementation**

`autopilot.py`, module helper before `burst_size`:

```python
def _readable_price(row: ObservedUpgrade | None) -> int | None:
    """The row's price when its text and its parse agree; else unreadable."""
    if row is None or type(row.price) is not int:
        return None
    if row.raw_price is not None:
        from perception import price_number
        if price_number(row.raw_price) != row.price:
            return None
    return row.price


```

`step` signature (lines 372-373):

```python
             refresh_policy: Callable[[int | None], AutopilotPolicy] | None = None,
             record_receipt: Callable[[int | None, str, int], None] | None = None,
             invalidate_quote: Callable[[str], None] | None = None) -> bool:
```

Replace lines 435-488, from `if self.pending:` through the `if not confirmed or was_manual:` / `return False` pair. Lines 489-507 (`if policy.single_purchase:` ...) stay inside the `if self.pending:` block unchanged:

```python
        confirmed_now = False
        if self.pending:
            before, sent_at = self.pending
            after = visible.get(before.upgrade_id)
            if now <= sent_at:
                return False
            # A manual buy is one purchase; the policy above is still its
            # single-rule stand-in, so falling through would buy it again.
            was_manual = self._manual is not None
            if self._pending_curve:
                outcome = self._burst_outcome(before, after, now - sent_at)
                if outcome is None:
                    self._decide("verifying", f"Checking {before.name} purchase", before.upgrade_id)
                    return False
                levels, invalidate = outcome
                if invalidate and invalidate_quote is not None:
                    invalidate_quote(before.upgrade_id)
                if not levels:
                    if invalidate:
                        self._decide("observing", f"{before.name} price is behind the model; reconciling",
                                     before.upgrade_id)
                    else:
                        self._blocked[before.upgrade_id] = now + 60
                        self._decide("blocked", f"{before.name} purchase was not confirmed",
                                     before.upgrade_id)
                    self.pending = None
                    self._manual = None
                    self._reset_batch()
                    return False
                self._confirm_burst(before, after, levels, record_receipt)
                confirmed_now = True
            else:
                price_receipt = False
                if self._pending_modeled and after and self._pending_next_price is not None and after.raw_price:
                    from fleet.battle_prices import price_matches
                    from perception import price_number
                    price_receipt = (price_number(after.raw_price) == after.price
                        and price_matches(self._pending_next_price, after.price, after.raw_price)
                        and not price_matches(before.price, after.price, after.raw_price))
                confirmed = after and (
                    price_receipt or
                    after.status == "maxed" or
                    (not self._pending_modeled and after.price is not None and before.price is not None and after.price > before.price) or
                    (after.value is not None and before.value is not None and after.value != before.value)
                )
                if self._pending_modeled and observation.combat.get('wave') != self._pending_wave:
                    # A wave transition may have granted a free upgrade. Reconcile
                    # instead of calling that change a paid purchase receipt.
                    self.pending = None
                    self._manual = None
                    self._reset_batch()
                    self._decide("observing", "Wave changed during purchase; reconciling")
                    return False
                if confirmed:
                    self.state.verified(after)
                    self._completed_decision_token = self._pending_decision_token
                    self._emit(events.BattlePurchased(item=after.name, upgrade_id=after.upgrade_id,
                                                      price=before.price, value=after.value))
                    if self._pending_modeled and record_receipt is not None:
                        record_receipt(self._pending_sequence, after.upgrade_id, 1)
                    self._decide("verified", f"Verified {after.name} upgrade", after.upgrade_id)
                    self.pending = None
                    self._manual = None
                    if self._pending_modeled:
                        self._batch_count += 1
                    # No return: the frame that proves the last purchase already
                    # shows the new prices and cash, so it can pick the next one.
                    # Stopping here spent a whole scan per purchase doing nothing.
                    confirmed_now = True
                elif now - sent_at >= 8:
                    self._blocked[before.upgrade_id] = now + 60
                    self._decide("blocked", f"{before.name} purchase was not confirmed", before.upgrade_id)
                    self.pending = None
                    self._manual = None
                    self._reset_batch()
                else:
                    self._decide("verifying", f"Checking {before.name} purchase", before.upgrade_id)
            if not confirmed_now or was_manual:
                return False
```

New methods after `_seek` (after line 323):

```python
    def _burst_outcome(self, before: ObservedUpgrade, after: ObservedUpgrade | None,
                       waited: float) -> tuple[int, bool] | None:
        """(levels bought, invalidate the row's quote), or None to keep waiting.

        1. MAX: every level the burst could still buy.
        2. A price matching exactly one curve level j: j - index, clamped
           to the burst; a j behind the starting level means the model is
           wrong, so nothing counts and the quote is invalidated. A price
           still at the starting level proves nothing yet.
        3. Unreadable, unmatched or ambiguous prices wait.
        4. After 8 s: a changed value counts one level and invalidates the
           quote so the next read re-indexes it; otherwise nothing counts
           and the caller blocks the row.
        """
        curve, index, size = self._pending_curve, self._pending_index, self._pending_k
        assert index is not None
        if after is not None and after.status == "maxed":
            return min(size, len(curve) - index), False
        price = _readable_price(after)
        if price is not None:
            from fleet.battle_prices import price_matches
            matches = [level for level, step in enumerate(curve)
                       if price_matches(step, price, after.raw_price)]
            if len(matches) == 1 and matches[0] < index:
                return 0, True
            if len(matches) == 1 and matches[0] > index:
                return min(matches[0] - index, size), False
        if waited < 8:
            return None
        if (after is not None and after.value is not None and before.value is not None
                and after.value != before.value):
            return 1, True
        return 0, False

    def _confirm_burst(self, before: ObservedUpgrade, after: ObservedUpgrade | None, levels: int,
                       record_receipt: Callable[[int | None, str, int], None] | None) -> None:
        """Record a confirmed burst as one BattlePurchased per level.

        Each level is priced from the curve, so purchase counts, run upgrades
        and dashboards keep meaning one level per event. Known cost: a free
        level a wave-end perk grants inside the confirmation window is counted
        as bought - at most one per wave per row, and never more than the
        burst's size.
        """
        shown = after if after is not None else before
        curve, index = self._pending_curve, self._pending_index
        assert index is not None
        for offset in range(levels):
            self.state.verified(shown)
            self._emit(events.BattlePurchased(
                item=before.name, upgrade_id=before.upgrade_id, price=curve[index + offset],
                value=shown.value if offset == levels - 1 else None))
        self._completed_decision_token = self._pending_decision_token
        if record_receipt is not None and self._pending_sequence is not None:
            record_receipt(self._pending_sequence + levels - 1, before.upgrade_id, levels)
        self._decide("verified", f"Verified {levels} {before.name} level{'s' if levels > 1 else ''}",
                     before.upgrade_id)
        self.pending = None
        self._manual = None
        self._batch_count += 1  # one batch decision, however many levels it bought
```

`tower_bot.py`: after `record_battle_receipt`, add:

```python
                    def invalidate_battle_quote(upgrade_id: str) -> None:
                        if self.reroll_progress is not None:
                            self.reroll_progress.battle_prices.invalidate(upgrade_id)
```

and in the `self.autopilot.step(...)` call (2879-2886), replace the last argument line:

```python
                                                   record_receipt=record_battle_receipt,
                                                   invalidate_quote=invalidate_battle_quote)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_autopilot.py tests/test_autopilot_loop.py tests/test_perception.py -p no:allure_pytest -q`
Expected: all pass, including the pinned legacy batch tests and `test_modeled_batch_reuses_confirmation_frames_and_stops_after_five`. That test now confirms through the curve path: its raw prices are exact, so `j - index == 1`.

- [ ] **Step 5: Commit**

```bash
git add autopilot.py tower_bot.py tests/test_autopilot.py tests/test_autopilot_loop.py
git commit -m "Confirm battle bursts from the price curve, one event per level

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Tab-sticky rich-mode choice and the burst ceiling on the policy

**Files:**
- Modify: `config.py` (append to the fast battle buying block)
- Modify: `fleet/build_route_eval.py` (`RouteFacts` after line 73; `BattleDecision` after line 107)
- Modify: `fleet/strategy_blocks.py` (`_Choice` after line 528; cheapest branch 1064-1077; pool `return _Choice(...)` at 1099-1102; `BattleDecision(...)` at 1154-1156)
- Modify: `fleet/reroll_progress.py` (`battle_policy` signature 680-687; `RouteFacts(...)` 726-742; blocks-mode `return replace(...)` 759-768)
- Modify: `tower_bot.py` (`battle_policy(...)` call inside `refresh_battle_policy`, 2860-2872)
- Test: `tests/test_strategy_blocks.py`, `tests/test_autopilot_loop.py`

**Interfaces:**
- Consumes: `_assigned_model_progress` (Task 3), `_BattleProgress` (Task 3), `AutopilotPolicy.burst_price_ceiling` (Task 4), `fleet.battle_prices.catalog`.
- Produces:
  - `config.BATTLE_RICH_MULTIPLE: int = 10`
  - `RouteFacts.battle_tab: str | None = None`
  - `_Choice.burst_price_ceiling: int | None = None`
  - `BattleDecision.burst_price_ceiling: int | None = None`
  - `RerollProgress.battle_policy(..., battle_tab: str | None = None)`
  - Rich ceiling = `max(curve of the chosen upgrade)`, so it is stable across frames. Otherwise ceiling = `chosen price * (100 + max_price_premium_pct) // 100`. `None` when the switch is off or the pool is not modeled.

- [ ] **Step 1: Write the failing test**

`tests/test_strategy_blocks.py`: the existing batch-tolerance test's cash of 100 is exactly 10 times its cheapest price, which now means rich mode. Keep it below the threshold. In `test_modeled_visible_batch_is_bounded_and_keeps_price_tolerance` (line 1293), replace `f = replace(battle_facts(), visible_upgrade_ids=('health',), ...` with:

```python
    # battle_cash 99 keeps this below the rich multiple (10 x 10), where the
    # premium-to-stay batch rule applies unchanged.
    f = replace(battle_facts(), battle_cash=99, visible_upgrade_ids=('health',), battle_batch_purchases=count, battle_batch_rule_id='cheap',
```

The rest of that statement stays. Append:

```python
def rich_pool() -> dict[str, Any]:
    return pool(upgrade_ids=['attack_speed', 'multishot_chance', 'health', 'thorns'],
                selection='cheapest', price_source='model', batch_size=5, max_price_premium_pct=25)


def rich_facts(cash: int, tab: str | None, visible: tuple[str, ...]) -> RouteFacts:
    quote = dict(account_id='account', run_id=7, status='available', source='model',
                 value=1, verified=True)
    prices = {'attack_speed': 50, 'multishot_chance': 40, 'health': 10, 'thorns': 30,
              'defense_absolute': 1}  # defense_absolute is not in the pool
    return replace(battle_facts(), battle_cash=cash, battle_tab=tab, visible_upgrade_ids=visible,
                   battle_price_quotes={uid: dict(quote, price=p) for uid, p in prices.items()})


@pytest.mark.parametrize('tab, visible, chosen', [
    ('ATTACK', ('attack_speed',), 'attack_speed'),          # visible on the open tab
    ('ATTACK', (), 'multishot_chance'),                       # elsewhere on the open tab
    ('UTILITY', (), 'health'),                                # nothing on the tab: global
    (None, ('attack_speed',), 'health'),                      # tab unknown: global
    ('DEFENSE', ('defense_absolute', 'thorns'), 'thorns'),    # never outside the pool
])
def test_rich_mode_prefers_visible_then_tab_then_global(
        monkeypatch: pytest.MonkeyPatch, tab: str | None, visible: tuple[str, ...], chosen: str) -> None:
    from fleet.battle_prices import catalog
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    program = blocks.validate_program([rich_pool()], 'battle')
    result = blocks.evaluate_program(route(list(program), lane='battle'),
                                     rich_facts(1000, tab, visible), None, 'battle')
    assert result.decision.upgrade_id == chosen
    assert result.decision.burst_price_ceiling == max(catalog()['curves'][chosen])


def test_rich_burst_ceiling_does_not_follow_live_cash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    program = blocks.validate_program([rich_pool()], 'battle')
    ceilings = {blocks.evaluate_program(route(list(program), lane='battle'),
                                        rich_facts(cash, None, ()), None, 'battle').decision.burst_price_ceiling
                for cash in (1000, 1234, 5000)}
    assert len(ceilings) == 1  # a policy that changed every frame would reset the row search


@pytest.mark.parametrize('enabled', [True, False])
def test_outside_rich_mode_the_cheapest_choice_is_unchanged(
        monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', enabled)
    program = blocks.validate_program([rich_pool()], 'battle')
    result = blocks.evaluate_program(route(list(program), lane='battle'),
                                     rich_facts(99, 'ATTACK', ('attack_speed',)), None, 'battle')
    assert result.decision.upgrade_id == 'health'
    assert result.decision.burst_price_ceiling == (12 if enabled else None)  # 10 x 125%


@pytest.mark.parametrize('enabled', [True, False])
def test_battle_policy_carries_the_open_tab_and_burst_ceiling(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    from fleet.battle_prices import catalog
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', enabled)
    progress, root, rows, base = _assigned_model_progress(tmp_path)
    shared = dict(run_id=2, wave=2, battle_tab='DEFENSE', visible_upgrade_ids=('health',))
    rich = progress.battle_policy(base, rows, cash=1000, **shared)
    poor = progress.battle_policy(base, rows, cash=40, **shared)
    if enabled:
        assert rich.rules[0].upgrade_id == 'health'
        assert rich.burst_price_ceiling == max(catalog()['curves']['health'])
        assert poor.rules[0].upgrade_id == 'attack_speed' and poor.burst_price_ceiling == 6
    else:
        assert rich.rules[0].upgrade_id == 'attack_speed' and rich.burst_price_ceiling is None
        assert poor.burst_price_ceiling is None
```

`tests/test_autopilot_loop.py`, append:

```python
@pytest.mark.parametrize('frame, tab', [('in_run_lit', 'ATTACK'), ('in_run_defense', 'DEFENSE')])
def test_the_battle_policy_learns_which_tab_is_open(
    bot_in_run_on: Callable[[str], TowerBot], monkeypatch: pytest.MonkeyPatch, frame: str, tab: str,
) -> None:
    bot = bot_in_run_on(frame)
    bot.controls.apply({'autopilot': {'enabled': True}})
    progress = _BattleProgress()
    bot.reroll_progress = progress
    monkeypatch.setattr(bot.autopilot, 'step', lambda *args, **kwargs: False)
    bot.run_once()
    assert ('policy', tab) in progress.calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_strategy_blocks.py -k "rich or open_tab or tab_is_open or modeled_visible_batch" tests/test_autopilot_loop.py::test_the_battle_policy_learns_which_tab_is_open -p no:allure_pytest -q`
Expected: FAIL with `TypeError: RouteFacts.__init__() got an unexpected keyword argument 'battle_tab'`, `TypeError: battle_policy() got an unexpected keyword argument 'battle_tab'`, and `('policy', None)` instead of the tab.

- [ ] **Step 3: Write minimal implementation**

`config.py`, append to the fast battle buying block:

```python
# Rich mode: battle cash at least this many times the cheapest pool candidate.
# The bot then stays on the open tab and bursts limited by cash alone.
BATTLE_RICH_MULTIPLE: int = 10
```

`fleet/build_route_eval.py`, `RouteFacts`, after `battle_batch_rule_id: str | None = None` (line 73):

```python
    # The battle panel's open category (ATTACK, DEFENSE or UTILITY).
    battle_tab: str | None = None
```

`BattleDecision`, after `price_source: str = "observed"` (line 107):

```python
    # Highest single-level price a burst may include; None buys one level.
    burst_price_ceiling: int | None = None
```

`fleet/strategy_blocks.py`, `_Choice`, after `price_source: str = "observed"` (line 528):

```python
    burst_price_ceiling: int | None = None
```

Replace lines 1064-1077 (from `chosen = next(iter(candidates))` through the batch block's inner `chosen = min(local, ...)`):

```python
                chosen = next(iter(candidates))
                selected = None
                burst_ceiling: int | None = None
                if block.get('selection', 'priority') == 'cheapest':
                    def cheapest(ids: Any) -> str:
                        return min(ids, key=lambda uid: (price_for(uid, source=source), block['upgrade_ids'].index(uid)))
                    chosen = cheapest(candidates)
                    if (not config.BATTLE_BURST_ENABLED and source == 'model'
                            and (quote := model_quote(chosen)) and not quote.get('verified')):
                        return _Choice(identity, chosen, 'Reconcile the cheapest candidate after a wave change',
                                       observation_ids=(chosen,), price_source='model')
                    bursting = config.BATTLE_BURST_ENABLED and source == 'model' and lane == 'battle'
                    if bursting and wallet >= config.BATTLE_RICH_MULTIPLE * price_for(chosen, source=source):
                        # Rich: work through the open tab, visible rows first.
                        # This only narrows the block's own candidates; it
                        # never adds an upgrade from outside the pool.
                        on_tab = [uid for uid in candidates if facts.battle_tab is not None
                                  and upgrades.by_id(uid).category == facts.battle_tab]
                        in_view = [uid for uid in on_tab if uid in facts.visible_upgrade_ids]
                        chosen = cheapest(in_view or on_tab or list(candidates))
                        # Limited by cash alone: the autopilot's budget caps the
                        # burst at cash - reserve. The top of the curve, not the
                        # live cash, keeps the policy stable across frames.
                        from fleet.battle_prices import catalog
                        burst_ceiling = max(catalog()['curves'][chosen])
                    else:
                        if (source == 'model' and facts.battle_batch_rule_id == identity
                                and 0 < facts.battle_batch_purchases < block.get('batch_size', 1)):
                            local_ceiling = price_for(chosen, source=source) * (100 + block.get('max_price_premium_pct', 0))
                            local = [uid for uid in candidates if uid in facts.visible_upgrade_ids
                                     and (local_quote := model_quote(uid)) and local_quote.get('verified')
                                     and price_for(uid, source=source) * 100 <= local_ceiling]
                            if local:
                                chosen = cheapest(local)
                        if bursting:
                            burst_ceiling = (price_for(chosen, source=source)
                                             * (100 + block.get('max_price_premium_pct', 0)) // 100)
```

In the pool's final `return _Choice(...)` (1099-1102), add `burst_price_ceiling=burst_ceiling`:

```python
                return _Choice(identity, chosen, 'Eligible pool after price, cap and affordability filters',
                               weights=candidates if selected else None, pending=selected,
                               target=block.get('targets', {}).get(chosen),
                               price_source='model' if source == 'model' and model_quote(chosen) else 'observed',
                               burst_price_ceiling=burst_ceiling)
```

In the battle `BattleDecision(...)` (1154-1156):

```python
        decision = BattleDecision(facts.account_id, 'battle', 'buy', upgrade.id, upgrade.name,
            upgrade.category, price_for(upgrade.id, source=choice.price_source), wallet, choice.reason,
            target=choice.target, price_source=choice.price_source,
            burst_price_ceiling=choice.burst_price_ceiling)
```

`fleet/reroll_progress.py`, `battle_policy` signature line 687:

```python
                      after_receipt_sequence: int | None = None,
                      battle_tab: str | None = None) -> AutopilotPolicy:
```

In `RouteFacts(...)` (726-742), after `battle_batch_rule_id=batch_rule_id,`:

```python
                battle_tab=battle_tab,
```

In the blocks-mode `return replace(...)`, after `modeled_pool=uses_modeled_prices(effective.battle.blocks),` (line 763):

```python
                    burst_price_ceiling=(evaluation.decision.burst_price_ceiling
                        if not observe_only and evaluation.decision.price_source == "model" else None),
```

`tower_bot.py`, inside `refresh_battle_policy`'s `battle_policy(...)` call, replace `after_receipt_sequence=after_sequence)` with:

```python
                            after_receipt_sequence=after_sequence,
                            battle_tab=observation.category)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_strategy_blocks.py tests/test_blender_battle_update.py tests/test_autopilot_loop.py tests/test_reroll_progress.py -p no:allure_pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add config.py fleet/build_route_eval.py fleet/strategy_blocks.py fleet/reroll_progress.py tower_bot.py tests/test_strategy_blocks.py tests/test_autopilot_loop.py
git commit -m "Stay on the open battle tab when rich and pass the burst ceiling

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Directional row seek in catalog order

**Files:**
- Modify: `autopilot.py` (`Search` dataclass 145-151; module function before it; `_seek` 304-306)
- Test: `tests/test_autopilot.py`

**Interfaces:**
- Consumes: `upgrades.CATALOG` (ordered as on screen per category; unlock entries excluded from the order); `config.BATTLE_BURST_ENABLED`.
- Produces:
  - `autopilot.catalog_direction(target: upgrades.Upgrade, rows: Sequence[ObservedUpgrade]) -> str | None` returns `"down"`, `"up"` or `None`.
  - `Search.mode: str = "directed"`, which becomes `"sweep"` on fallback.

- [ ] **Step 1: Write the failing test**

`tests/test_autopilot.py`, append:

```python
@pytest.mark.parametrize('enabled, down', [(True, True), (False, False)])
def test_seek_scrolls_toward_the_target_in_catalog_order(
        monkeypatch: pytest.MonkeyPatch, enabled: bool, down: bool) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', enabled)
    bot, device, frame, observation, policy = parts()
    # Multishot Chance is listed after every visible Attack row.
    bot.step(frame, device, replace(policy, rules=(UpgradeRule('multishot_chance'),)),
             cash=100, observation=observation)
    (kind, _, y, _, y2), = device.actions
    assert kind == 'swipe' and (y > y2) == down  # a downward scroll swipes upward


def test_seek_scrolls_up_for_a_target_listed_before_the_visible_rows(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    bot, device, frame, observation, policy = parts()
    lower = tuple(replace(r, upgrade_id=uid) for r, uid in zip(
        observation.rows, ('range', 'damage_per_meter', 'multishot_chance', 'multishot_targets')))
    bot.step(frame, device, replace(policy, rules=(UpgradeRule('damage'),)),
             cash=100, observation=replace(observation, rows=lower))
    (kind, _, y, _, y2), = device.actions
    assert kind == 'swipe' and y < y2


def test_seek_sweeps_when_catalog_order_cannot_place_the_target(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    bot, device, frame, observation, policy = parts()
    # Visible rows on both sides of Critical Chance, but not Critical Chance.
    rows = tuple(r for r in observation.rows if r.upgrade_id in ('damage', 'critical_factor'))
    bot.step(frame, device, replace(policy, rules=(UpgradeRule('critical_chance'),)),
             cash=100, observation=replace(observation, rows=rows))
    (kind, _, y, _, y2), = device.actions
    assert kind == 'swipe' and y < y2  # the sweep starts upward


def test_a_directed_seek_that_stops_moving_restarts_as_a_full_sweep(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    bot, device, frame, observation, policy = parts()
    policy = replace(policy, rules=(UpgradeRule('multishot_chance'),))
    for n in range(2):  # the panel never moves
        bot.step(frame, device, policy, cash=100, observation=replace(observation, observed_at=100 + n))
    (_, _, down_y, _, down_y2), (_, _, up_y, _, up_y2) = device.actions
    assert down_y > down_y2 and up_y < up_y2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_autopilot.py -k "seek" -p no:allure_pytest -q`
Expected: FAIL. `test_seek_scrolls_toward_the_target_in_catalog_order[True-True]` sees an upward sweep (`y < y2`), and `test_a_directed_seek_that_stops_moving...` sees an up then up (or down) pattern instead of down then up.

- [ ] **Step 3: Write minimal implementation**

`autopilot.py`, before `@dataclass class Search`:

```python
def catalog_direction(target: upgrades.Upgrade, rows: Sequence[ObservedUpgrade]) -> str | None:
    """Which way the panel must scroll to reach `target`, from catalog order.

    upgrades.CATALOG lists each tab's standard rows in on-screen order, so a
    target ranked after the last visible row is further down and one ranked
    before the first is further up. None when no visible row is in the
    catalog order or the target sits inside the visible span; the up-then-
    down sweep decides those.
    """
    order = [entry.id for entry in upgrades.CATALOG
             if entry.category == target.category and not entry.unlock]
    if target.id not in order:
        return None
    seen = [order.index(row.upgrade_id) for row in rows if row.upgrade_id in order]
    if not seen:
        return None
    position = order.index(target.id)
    if position > max(seen):
        return "down"
    if position < min(seen):
        return "up"
    return None


```

`Search`, after `tab_attempts: int = 0`:

```python
    # "directed" follows catalog order; "sweep" is the up-then-down fallback.
    mode: str = "directed"
```

`_seek`: after `fingerprint = tuple(r.upgrade_id for r in observation.rows)` (line 304) and before `at_end = ...` (line 305), insert:

```python
        if search.mode == "directed":
            direction = (catalog_direction(entry, observation.rows)
                         if config.BATTLE_BURST_ENABLED else None)
            stuck = search.fingerprint is not None and (
                fingerprint == search.fingerprint or search.scrolls >= policy.max_scrolls)
            if direction is None or stuck or (search.scrolls and direction != search.direction):
                # Catalog order cannot place the target, or following it
                # stopped moving: fall back to today's full sweep, from the top.
                search.mode, search.direction, search.scrolls, search.fingerprint = "sweep", "up", 0, None
            else:
                search.direction = direction
```

The existing `at_end` logic below stays unchanged. A directed search is never `at_end`: its fingerprint moved and its scrolls are under the budget, or it already switched to `"sweep"` with a cleared fingerprint.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_autopilot.py tests/test_autopilot_api.py -p no:allure_pytest -q`
Expected: all pass. `test_unknown_search_has_a_finite_scroll_budget` and `test_cached_offscreen_target_search_still_has_a_scroll_bound` now make 3 swipes (directed down, then sweep up, then end), which is within their `<= 4` bound.

- [ ] **Step 5: Commit**

```bash
git add autopilot.py tests/test_autopilot.py
git commit -m "Seek battle rows in catalog order before falling back to the sweep

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Faster buying scans and no cooldown after a same-frame confirmation

**Files:**
- Modify: `config.py` (append to the fast battle buying block)
- Modify: `autopilot.py` (`__init__`; new `buying` property after `fast_followup`, 252-254; `suspend` 223-234; `step`: start ~378, after the pending block, cooldown gate 515-516, target seek 579-584, tap end 640-644)
- Modify: `tower_bot.py` (scan interval, 3226-3233)
- Test: `tests/test_autopilot.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `confirmed_now` (Task 5), `modeled`/`shown` helpers (Task 4), `strategy.MIN_INTERVAL`, `jitter.spread`.
- Produces:
  - `config.BATTLE_SCAN_INTERVAL_SECONDS: float = 0.6`
  - `BattleAutopilot.buying -> bool` (property backed by `_buying`)

- [ ] **Step 1: Write the failing test**

`tests/test_autopilot.py`, append:

```python
@pytest.mark.parametrize('enabled', [True, False])
def test_a_confirmed_purchase_skips_the_post_tap_cooldown(
        monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', enabled)
    bot, device, frame, obs, policy = parts()
    row = next(r for r in obs.rows if r.upgrade_id == 'attack_speed')
    bot.step(frame, device, modeled(policy, row, 0), cash=100,
             observation=shown(obs, row, 0, at=100), run_id=7, cooldown=1.0)
    bot.step(frame, device, modeled(policy, row, 0), cash=95,
             observation=shown(obs, row, 1, at=100.4), run_id=7, cooldown=1.0,
             refresh_policy=lambda sequence: modeled(policy, row, 1, sequence=1),
             record_receipt=lambda *args: None)
    # The confirming frame buys again only when the cooldown is skipped.
    assert len(device.actions) == (2 if enabled else 1)


def test_buying_follows_the_last_battle_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    from policy import UpgradeRule
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    bot, device, frame, obs, policy = parts()
    bot.step(frame, device, policy, cash=100, observation=obs)
    assert bot.buying  # tapped a purchase
    bot.suspend("Run ended", clear_battle=True)
    assert not bot.buying
    bot.step(frame, device, replace(policy, cash_reserve=95), cash=100,
             observation=replace(obs, observed_at=105))
    assert not bot.buying  # saving
    damage = next(r for r in obs.rows if r.upgrade_id == 'damage')
    bot.state.observe(replace(obs, rows=(replace(damage, upgrade_id='health', name='Health',
                                                 category='DEFENSE'),)))  # cached off-tab
    bot.step(frame, device, replace(policy, rules=(UpgradeRule('health'),)), cash=100,
             observation=replace(obs, observed_at=110))
    assert bot.buying  # opening the tab that holds the target
    bot.step(frame, device, replace(policy, rules=(UpgradeRule('health'),)), cash=100,
             observation=replace(obs, observed_at=110.3), cooldown=1.0)
    assert bot.buying  # a cooldown-skipped scan keeps the previous pace
    bot.step(frame, device, replace(policy, observe_only=True), cash=100,
             observation=replace(obs, observed_at=115))
    assert not bot.buying  # observing only
```

`tests/test_cli.py`, append:

```python
@pytest.mark.parametrize('enabled', [True, False])
def test_run_forever_scans_at_the_battle_pace_while_buying(monkeypatch, enabled: bool) -> None:
    """A step that bought, confirmed or headed for a purchase is followed
    by a 0.6 s scan; saving, observing and idle steps keep the strategy pace."""
    import events
    import vision
    from strategy import MAX_TIMING_JITTER
    from tower_bot import TowerBot

    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', enabled)
    bot = TowerBot(
        device=MagicMock(),
        templates=vision.TemplateCache(Path(__file__).parent.parent / "templates"),
        bus=events.EventBus(),
    )
    bot.controls.apply({"interval": 5.0})
    waits: list[float] = []

    def run_once(max_runs=None) -> bool:
        bot._last_scan_in_battle = True
        bot.autopilot._buying = not waits
        return True

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) == 2:
            bot.stop()
        return False

    monkeypatch.setattr(bot, "run_once", run_once)
    monkeypatch.setattr(bot._stopping, "wait", wait)
    bot.run_forever()

    if enabled:
        assert waits[0] <= config.BATTLE_SCAN_INTERVAL_SECONDS * (1 + MAX_TIMING_JITTER)
    else:
        assert waits[0] > 4.0
    assert waits[1] > 4.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_autopilot.py tests/test_cli.py -k "cooldown or buying_follows or battle_pace" -p no:allure_pytest -q`
Expected: FAIL. `[True]` has one action instead of two, `'BattleAutopilot' object has no attribute 'buying'`, and `config` has no `BATTLE_SCAN_INTERVAL_SECONDS`.

- [ ] **Step 3: Write minimal implementation**

`config.py`, append to the fast battle buying block:

```python
# Battle scan interval while buying: the last step tapped or confirmed a
# purchase, or headed for a buyable target. Saving, observing, idle and
# blocked steps keep the strategy interval.
BATTLE_SCAN_INTERVAL_SECONDS: float = 0.6
```

`autopilot.py`:
- `__init__`, after `self._last_action = float("-inf")` (line 195): `self._buying = False`
- after the `fast_followup` property (line 254):

```python

    @property
    def buying(self) -> bool:
        """The last battle step tapped, confirmed or headed for a purchase."""
        return self._buying
```

- `suspend`, first line of the body: `self._buying = False`
- `step`, right after `self._counter_followups = max(0, self._counter_followups - 1)` (line 378):

```python
        buying_before, self._buying = self._buying, False
```

- inside `if self.pending:`, directly after the Task 5 `if not confirmed_now or was_manual:` / `return False` pair:

```python
            self._buying = True
```

- replace the cooldown gate (lines 515-516):

```python
        if (not (config.BATTLE_BURST_ENABLED and confirmed_now)
                and now - self._last_action < max(.75, cooldown)):
            # Skipped scans keep the pace the last decision set.
            self._buying = self._buying or buying_before
            return False
```

- in the decision-target seek (lines 579-584), replace `if moved:` / `self._last_action = now` with:

```python
            if moved:
                self._last_action = now
                self._buying = not policy.observe_only
```

- before the final `return True` of `step`:

```python
        self._buying = True
```

`tower_bot.py`, insert immediately before `if interval is None and self.autopilot.fast_followup:` (line 3230):

```python
            if (interval is None and config.BATTLE_BURST_ENABLED
                    and self._last_scan_in_battle and self.autopilot.buying):
                # The last battle step bought, confirmed or headed for a
                # purchase, and the next frame decides the next one.
                current_interval = min(current_interval, max(
                    MIN_INTERVAL,
                    jitter.spread(config.BATTLE_SCAN_INTERVAL_SECONDS, live.timing_jitter),
                ))
```

`live` is bound in the `if interval is None:` branch just above. This new block runs only when `interval is None`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_autopilot.py -p no:allure_pytest -q` and `uv run pytest tests/test_cli.py -k "run_forever" -p no:allure_pytest -q`
Expected: all pass, including `test_run_forever_rescans_promptly_while_a_battle_purchase_is_pending`.

- [ ] **Step 5: Commit**

```bash
git add config.py autopilot.py tower_bot.py tests/test_autopilot.py tests/test_cli.py
git commit -m "Scan battles at 0.6s while buying and skip the cooldown after confirmation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Live probe, rollout and measurement (REQUIRES THE USER'S GO-AHEAD BEFORE TOUCHING A LIVE WORKER)

**Files:**
- Create: `tools/probe_tap_burst.py`
- Create: `tools/battle_purchase_rate.py`. This is a parameterised copy of the session analysis at `/private/tmp/claude-501/-Users-shahar-BifrostProjects-thetowerbot--claude-worktrees-turtle-template-balance-c5e01f/985ba6a6-9ddc-493f-a2a9-fbfdfc5ed15f/scratchpad/rate.py`. That copy is session scratch and may be gone; the script below is self-contained. It adds `--since` and the cash-spent share, which `rate.py` lacks.
- Modify: `config.py` (`BATTLE_BURST_TAP_GAP_SECONDS`, only if the probe drops taps)

**Interfaces:**
- Consumes: `device.connect_device`, `device.capture_screen`, `device.burst_command` (Task 1); `perception.observe_frame(screen, "battle")`; `fleet.battle_prices.catalog`, `price_matches`.
- Produces: a measured `BATTLE_BURST_TAP_GAP_SECONDS` and a before/after report against the success criteria.

- [ ] **Step 1: Write the tools (no device access yet)**

`tools/probe_tap_burst.py`:

```python
"""Time one tap burst on a live battle row and count the levels the game took.

Pause the worker first (dashboard Pause) so the bot sends no input, open its
battle upgrade panel on the tab showing UPGRADE with cash for every level,
then run:

    uv run python tools/probe_tap_burst.py 127.0.0.1:5555 attack_speed --taps 10 --gap 0
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device import burst_command, capture_screen, connect_device  # noqa: E402
from fleet.battle_prices import catalog, price_matches  # noqa: E402
from perception import ObservedUpgrade, observe_frame  # noqa: E402


def read_row(device: object, upgrade_id: str) -> ObservedUpgrade | None:
    observation = observe_frame(capture_screen(device), "battle")
    return next((row for row in observation.rows if row.upgrade_id == upgrade_id), None)


def level(row: ObservedUpgrade | None, curve: list[int]) -> int | None:
    if row is None or type(row.price) is not int:
        return None
    matches = [i for i, price in enumerate(curve) if price_matches(price, row.price, row.raw_price)]
    return matches[0] if len(matches) == 1 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", help="the worker's ADB endpoint, host:port")
    parser.add_argument("upgrade_id")
    parser.add_argument("--taps", type=int, default=10)
    parser.add_argument("--gap", type=float, default=0.0)
    args = parser.parse_args()
    host, _, port = args.endpoint.rpartition(":")
    device = connect_device(host=host, port=int(port))
    curve = catalog()["curves"][args.upgrade_id]
    before = read_row(device, args.upgrade_id)
    start = level(before, curve)
    if before is None or before.tap is None or start is None:
        print("The row is not visible with a readable price; open its tab and retry.")
        return 1
    print(f"start level {start}, price {curve[start]}, "
          f"cost of {args.taps} levels {sum(curve[start:start + args.taps])}")
    began = time.monotonic()
    device.shell(burst_command(*before.tap, args.taps, args.gap))
    took = time.monotonic() - began
    time.sleep(1.0)
    end = level(read_row(device, args.upgrade_id), curve)
    registered = None if end is None else end - start
    print(f"burst of {args.taps} taps, gap {args.gap}s: took {took:.2f}s, "
          f"registered {registered} levels")
    return 0 if registered == args.taps else 2


if __name__ == "__main__":
    raise SystemExit(main())
```

`tools/battle_purchase_rate.py`:

```python
"""Battle purchase rate before and after wave 20, per worker DB (read-only).

The measurement behind docs/superpowers/specs/2026-10-05-fast-battle-buying-design.md.
Cash spent after wave 20 is approximated as spent / (spent + growth of the
wallet seen at taps after wave 20); the DB records no wallet at death.

    uv run python tools/battle_purchase_rate.py --since 1791331200 \
        ~/.local/share/thetowerbot/fleet/workers/Tiramisu64_82/tower_bot.db \
        ~/.local/share/thetowerbot/fleet/workers/Tiramisu64_83/tower_bot.db
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from pathlib import Path


def report(path: str, since: float) -> None:
    connection = sqlite3.connect(f"file:{Path(path).expanduser()}?mode=ro", uri=True)
    runs = connection.execute(
        "SELECT id, started_at, ended_at, wave FROM runs WHERE started_at >= ? "
        "AND abandoned = 0 AND ended_at IS NOT NULL", (since,)).fetchall()
    known = [(ended - started) / wave for _, started, ended, wave in runs if wave]
    if not known:
        print(f"{path}: no completed runs since {since}")
        return
    per_wave_default = statistics.median(known)
    gaps: dict[str, list[float]] = {"<20": [], "20+": []}
    levels = {"<20": 0, "20+": 0}
    span = {"<20": 0.0, "20+": 0.0}
    ratio: dict[str, list[float]] = {"<20": [], "20+": []}
    shares: list[float] = []
    counted = 0
    for run_id, started, ended, wave in runs:
        per_wave = (ended - started) / wave if wave else per_wave_default
        wave_20 = started + 20 * per_wave
        rows = connection.execute(
            "SELECT ts, type, price, wallet FROM events WHERE run_id = ? "
            "AND type IN ('BattlePurchased', 'Tapped') ORDER BY seq", (run_id,)).fetchall()
        bought = [ts for ts, kind, _, _ in rows if kind == "BattlePurchased"]
        if len(bought) < 5:
            continue
        counted += 1
        span["<20"] += max(0.0, min(wave_20, ended) - started)
        span["20+"] += max(0.0, ended - wave_20)
        previous = started
        for ts in bought:
            key = "<20" if ts < wave_20 else "20+"
            gaps[key].append(ts - previous)
            levels[key] += 1
            previous = ts
        for ts, kind, price, wallet in rows:
            if kind == "Tapped" and price and wallet is not None:
                ratio["<20" if ts < wave_20 else "20+"].append(wallet / price)
        late = [(kind, price, wallet) for ts, kind, price, wallet in rows if ts >= wave_20]
        spent = sum(price or 0 for kind, price, _ in late if kind == "BattlePurchased")
        wallets = [wallet for kind, _, wallet in late if kind == "Tapped" and wallet is not None]
        growth = max(0, wallets[-1] - wallets[0]) if len(wallets) > 1 else 0
        if spent + growth:
            shares.append(spent / (spent + growth))
    print(f"\n### {path}: {counted} runs, sec/wave median {per_wave_default:.1f}")
    for key in ("<20", "20+"):
        if not levels[key]:
            print(f"{key}: no purchases")
            continue
        waves = span[key] / per_wave_default
        cash = statistics.median(ratio[key]) if ratio[key] else float("nan")
        print(f"{key}: levels={levels[key]} levels/wave={levels[key] / waves:.2f} "
              f"mean gap={statistics.mean(gaps[key]):.1f}s cash/price at tap median={cash:.1f}x")
    share = statistics.median(shares) if shares else float("nan")
    print(f"post-wave-20 cash spent share (approx.) median={share:.0%}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=float, required=True, help="unix seconds")
    parser.add_argument("dbs", nargs="+")
    args = parser.parse_args()
    for path in args.dbs:
        report(path, args.since)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Baseline the analysis (read-only, no device)**

Run: `uv run python tools/battle_purchase_rate.py --since $(( $(date +%s) - 86400 )) ~/.local/share/thetowerbot/fleet/workers/Tiramisu64_82/tower_bot.db ~/.local/share/thetowerbot/fleet/workers/Tiramisu64_83/tower_bot.db`
Expected: the `20+` rows show about 1.4 (82) and 1.8 (83) levels/wave, matching the spec's table. That confirms the script reproduces the original measurement before the feature runs.

- [ ] **Step 3: STOP and ask the user for the go-ahead to probe worker 82**

State exactly what will happen: worker 82 will be paused from the dashboard mid-battle, then one 10-tap burst goes to its open battle panel through ADB, spending in-run cash on about 10 levels of one upgrade. Wait for an explicit yes.

- [ ] **Step 4: Live probe (only after the go-ahead)**

1. On the fleet dashboard, open worker `Tiramisu64_82`, wait for a battle past wave 20 with cash at least 10 times a curve upgrade's price (e.g. Attack Speed on ATTACK), and press **Pause**. Copy the worker's ADB endpoint (host:port) from its dashboard card.
2. Run `uv run python tools/probe_tap_burst.py <endpoint from the card> attack_speed --taps 10 --gap 0`.
3. Exit code 0 and `registered 10 levels`: keep `BATTLE_BURST_TAP_GAP_SECONDS = 0.0`. Otherwise rerun with `--gap 0.05`, then `0.1`, then `0.2`, until two consecutive probes register all 10.
4. Note the `took` time. If a 10-tap burst takes more than 4 s, report it before continuing: the 8 s confirmation window counts from the frame before the burst.
5. Press **Resume** on the dashboard.

- [ ] **Step 5: Record the measured gap**

If Step 4 needed a gap, set it in `config.py` with the measurement:

```python
# Measured 2026-10-0X on Tiramisu64_82, Attack Speed: 10 taps with gap 0.1 s
# took N.NN s and registered 10/10 levels; gap 0 registered M/10.
BATTLE_BURST_TAP_GAP_SECONDS: float = 0.1
```

Replace the placeholders `0X`, `N.NN` and `M` in that comment with the date and numbers the probe printed. If no gap was needed, add the measurement comment above the existing `0.0`. Then run `uv run pytest tests/test_device.py tests/test_supervisor.py -p no:allure_pytest -q`.

- [ ] **Step 6: Commit**

```bash
git add tools/probe_tap_burst.py tools/battle_purchase_rate.py config.py
git commit -m "Add the tap burst probe and battle purchase rate report

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 7: Roll out to both workers and re-measure (user-driven)**

1. With the user's go-ahead, restart workers 82 and 83 on this build at the same time (`config.BATTLE_BURST_ENABLED = True`). Record the start time: `date +%s`.
2. After at least 10 completed runs per worker, run `uv run python tools/battle_purchase_rate.py --since <recorded start> ~/.local/share/thetowerbot/fleet/workers/Tiramisu64_82/tower_bot.db ~/.local/share/thetowerbot/fleet/workers/Tiramisu64_83/tower_bot.db`.
3. Success: on both workers, the `20+` row shows `levels/wave >= 4` and the cash spent share is at least 70%.
4. If either worker regresses (fewer levels/wave than baseline, blocked rows piling up in the decision feed, or levels counted without matching cash spend), set `config.BATTLE_BURST_ENABLED = False`, restart both workers, and report the numbers to the user.
