# Telegram Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The project's no-automatic-commit instruction overrides any commit step in those skills; stage the completed change and request permission before committing.

**Goal:** Add editable, persistent Telegram update frequency and message contents, with sample previews, to the single emulator and fleet settings pages.

**Architecture:** A validated JSON store owns separate single and fleet profiles without storing credentials. The existing reporter reads the active profile, renders the selected fields, and wakes when settings change; the coordinator alone renders fleet summaries. FastAPI exposes settings and preview endpoints, and both Next.js routes share one settings component.

**Tech Stack:** Python 3, FastAPI, Pydantic, pytest, React 19, Next.js 15 static export, TypeScript, Vitest.

**Spec:** `docs/superpowers/specs/2026-09-25-telegram-settings-design.md`

## Global Constraints

- Telegram token and chat ID stay in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Never persist or return the token.
- Single and fleet have separate enabled flags, intervals, and optional field selections. Minutes are whole numbers from 1 to 1,440.
- Status/uptime and fleet name/state/count are mandatory. Missing measurements say “unavailable,” never zero.
- Preview uses fixed sample data and the same server renderer as sending. Preview never contacts Telegram.
- `--no-telegram` suppresses sends; explicit `--telegram-interval` overrides the saved interval and is reported in the UI.
- Plain text messages remain at or below 4,096 characters. Network/snapshot errors do not kill the reporter.
- Fleet workers never send their own periodic Telegram messages; the coordinator sends one aggregate when its reroll pool has members.
- Only run the specific test files/functions changed here, with `-p no:allure_pytest`; never run a whole-directory test sweep.
- Do not commit automatically. Stage and display the final diff, then ask for commit permission.

## Review Focus

1. A corrupt settings file must yield an explicit error and preserve its bytes. Test in Task 1.
2. Rapid saves, including a disable just before a send, must not duplicate or continue messages. Test in Task 2.
3. A malformed fleet member must not suppress every other member's update. Test in Task 2.
4. Fleet settings must remain reachable without a selected/running account and keep fleet navigation active. Test in Task 4.
5. A mismatch between the dashboard and backend build must prevent a settings write. Test in Task 4's API helper test.

## File map

- `telegram_settings.py`: schema, defaults, validation, atomic store, and safe status fields.
- `telegram_report.py`: selected-field renderers, sample preview, dynamic scheduling, fleet aggregation.
- `tower_bot.py`, `fleet/reroll_supervisor.py`: reporter/store wiring and worker suppression.
- `web/app.py`: settings/preview API and `telegram` runtime capability; routes precede the API catch-all.
- `web/ui/lib/telegram.ts`, `web/ui/lib/api.ts`: client types and calls.
- `web/ui/components/TelegramSettings.tsx`, two settings page files, `Sidebar.tsx`, `AccountShell.tsx`: shared UI and navigation.
- `README.md`: new settings instructions and existing environment/CLI override behavior.

---

### Task 1: Validated settings store

**Files:** Create `telegram_settings.py`; create `tests/test_telegram_settings.py`.

**Interfaces:** Produce `TelegramMode = Literal["single", "fleet"]`, `TelegramProfile`, `TelegramSettingsStore(path, initial_interval_seconds=3600)`, `default_profile(mode, initial_interval_seconds)`, `legacy_telegram_interval_from_env(env=None)`, `SINGLE_FIELDS`, and `FLEET_FIELDS`. `load(mode) -> TelegramProfile` and `save(mode, profile) -> TelegramProfile` raise `TelegramSettingsError` on unreadable/corrupt storage and `ValueError` for invalid fields.

- [ ] **Step 1: Write focused failing tests.** Cover the default profile, both modes surviving a save/reload, unknown/duplicate fields, bool/fraction/out-of-range intervals, and corrupt JSON remaining untouched:

```python
def test_profiles_persist_independently(tmp_path: Path) -> None:
    store = TelegramSettingsStore(tmp_path / "telegram-settings.json")
    single = store.load("single").model_copy(update={"interval_minutes": 15})
    store.save("single", single)
    again = TelegramSettingsStore(tmp_path / "telegram-settings.json")
    assert again.load("single").interval_minutes == 15
    assert again.load("fleet").interval_minutes == 60

def test_corrupt_file_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "telegram-settings.json"
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(TelegramSettingsError):
        TelegramSettingsStore(path).load("single")
    assert path.read_text(encoding="utf-8") == "{bad"

def test_rejects_invalid_fields_without_changing_saved_profile(tmp_path: Path) -> None:
    store = TelegramSettingsStore(tmp_path / "telegram-settings.json")
    before = store.load("fleet")
    with pytest.raises(ValueError, match="invalid_telegram_fields"):
        store.save("fleet", TelegramProfile(enabled=True, interval_minutes=15,
                                            fields=["tier_wave", "tier_wave"]))
    assert store.load("fleet") == before

@pytest.mark.parametrize("interval", [True, 0, 1441, 2.5])
def test_interval_must_be_a_whole_minute_in_range(interval: object) -> None:
    with pytest.raises(ValidationError):
        TelegramProfile(enabled=True, interval_minutes=interval, fields=[])

def test_legacy_environment_sets_only_initial_default() -> None:
    seconds = legacy_telegram_interval_from_env({"TELEGRAM_SUMMARY_SECONDS": "900"})
    assert default_profile("single", seconds).interval_minutes == 15
```

- [ ] **Step 2: Run only this file and confirm the tests fail for missing implementation.** Run `uv run pytest -p no:allure_pytest tests/test_telegram_settings.py -q` with a 180-second command timeout.
- [ ] **Step 3: Implement the schema and store.** Use strict Pydantic fields and reject extra keys. Keep defaults as data, not hidden in the UI. Use a `threading.RLock` and an exclusive `fcntl.flock` on a stable sibling lock file so two dashboard processes cannot overwrite each other's mode. While locked, read the existing document, replace one mode, write a mode-0600 temp file, flush/fsync, then `os.replace`; release the lock and remove only the temp file. Missing settings file means defaults; malformed existing file raises:

```python
SINGLE_FIELDS = ("screen", "scans", "wallet", "run", "runs_completed", "taps", "skips", "last_error")
FLEET_FIELDS = ("tier_wave", "lifetime_coins", "milestone", "errors")
TelegramMode = Literal["single", "fleet"]

class TelegramProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(strict=True)
    interval_minutes: int = Field(ge=1, le=1440, strict=True)
    fields: list[str]

def validate_fields(mode: TelegramMode, profile: TelegramProfile) -> None:
    allowed = set(SINGLE_FIELDS if mode == "single" else FLEET_FIELDS)
    if len(profile.fields) != len(set(profile.fields)) or set(profile.fields) - allowed:
        raise ValueError("invalid_telegram_fields")

def default_profile(mode: TelegramMode, initial_interval_seconds: float = 3600) -> TelegramProfile:
    minutes = initial_interval_seconds / 60
    minutes = int(minutes) if minutes.is_integer() and 1 <= minutes <= 1440 else 60
    return TelegramProfile(enabled=True, interval_minutes=minutes,
                           fields=list(SINGLE_FIELDS if mode == "single" else FLEET_FIELDS))

def legacy_telegram_interval_from_env(env: Mapping[str, str] | None = None) -> float:
    raw = (os.environ if env is None else env).get("TELEGRAM_SUMMARY_SECONDS", "")
    try:
        return float(raw) if raw else 3600.0
    except ValueError:
        return 3600.0
```

- [ ] **Step 4: Run the same focused file until it passes.** Confirm file permissions and `git diff --check`.

### Task 2: Field selection, fleet renderer, and live scheduling

**Files:** Modify `telegram_report.py`; modify `tests/test_telegram_report.py`.

**Interfaces:** Consume Task 1's `TelegramProfile` and `TelegramSettingsStore`. Preserve `render_summary(snapshot, paused=None, label="The Tower bot")` and `TelegramReporter(state, settings, controls=None, send=None, label="The Tower bot")` callers. Extend the renderer with optional `fields` and the reporter with optional `preferences: TelegramSettingsStore | None`, `fleet`, and `interval_override_seconds`. Produce `render_fleet_summary(snapshot, fields=None)`, `render_sample(mode, profile)`, and `TelegramReporter.reconfigure()`.

- [ ] **Step 1: Add failing renderer and scheduler tests.** Pin the selected lines, unknown values, one broken fleet member, message limit with omitted count, sample rendering through the production formatter, disable/re-enable, wakeup after a frequency change, and corrupt shared settings during a running reporter:

```python
def test_selected_single_fields_omit_unselected_lines() -> None:
    body = render_summary(snapshot(screen="BATTLE", scans=9), fields=["screen"])
    assert "Screen: BATTLE" in body
    assert "Scans:" not in body
    assert body.startswith("The Tower bot - running")

def test_fleet_member_with_missing_metrics_does_not_hide_healthy_member() -> None:
    body = render_fleet_summary({"members": [
        {"name": "Air18", "state": "running", "tier": 1, "wave": 72},
        {"name": "Air19", "state": "error", "tier": "bad"},
    ]}, fields=["tier_wave"])
    assert "Air18" in body and "Tier 1 Wave 72" in body
    assert "Air19" in body and "unavailable" in body

def test_preview_does_not_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telegram_report, "post_message", lambda *a, **kw: pytest.fail("sent"))
    assert "Air18" in render_sample("fleet", default_profile("fleet"))

def test_disabling_a_running_reporter_stops_future_ticks(tmp_path: Path) -> None:
    store = TelegramSettingsStore(tmp_path / "telegram-settings.json")
    sent: list[str] = []
    reporter = TelegramReporter(FakeState(), SETTINGS, send=sent.append,
                                preferences=store, interval_override_seconds=0.2)
    reporter.start()
    try:
        deadline = time.monotonic() + 1
        while not sent and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(sent) == 1
        store.save("single", store.load("single").model_copy(update={"enabled": False}))
        reporter.reconfigure()
        time.sleep(0.3)
        assert len(sent) == 1
        store.save("single", store.load("single").model_copy(update={"enabled": True}))
        reporter.reconfigure()
        deadline = time.monotonic() + 1
        while len(sent) == 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(sent) == 2
    finally:
        reporter.close()

def test_fleet_message_bound_reports_omitted_members() -> None:
    members = [{"name": f"Air{index}-" + "x" * 100, "state": "running"}
               for index in range(100)]
    body = render_fleet_summary({"members": members})
    assert len(body) <= telegram_report.MAX_MESSAGE_CHARS
    assert "more emulators" in body

def test_corrupt_shared_settings_pauses_without_killing_reporter(tmp_path: Path) -> None:
    path = tmp_path / "telegram-settings.json"
    store = TelegramSettingsStore(path)
    store.save("single", store.load("single"))
    sent: list[str] = []
    reporter = TelegramReporter(FakeState(), SETTINGS, send=sent.append,
                                preferences=store, interval_override_seconds=0.2)
    reporter.start()
    try:
        path.write_text("{bad", encoding="utf-8")
        reporter.reconfigure()
        time.sleep(0.3)
        assert len(sent) <= 1
        assert reporter._thread is not None and reporter._thread.is_alive()
    finally:
        reporter.close()
```

- [ ] **Step 2: Run only `tests/test_telegram_report.py` and confirm new tests fail.** Run `uv run pytest -p no:allure_pytest tests/test_telegram_report.py -q` with a 180-second command timeout.
- [ ] **Step 3: Implement rendering and scheduling.** Filter optional lines by selected fields, use mandatory headers, and apply the current 4,096-character bound. For fleet, sort members by name, validate each optional value before formatting, append rows only while room remains for `… and N more emulators`. `render_sample` passes fixed snapshot dictionaries through these same two renderers. `TelegramReporter` reloads the active mode from the settings store when present and wakes on `reconfigure` (and at most every two seconds to observe another local process changing the shared file):

```python
def reconfigure(self) -> None:
    self._changed.set()

def _active_mode(self) -> TelegramMode:
    if self._fleet is None:
        return "single"
    try:
        members = json.loads((self._fleet.root / "reroll-pool.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "single"
    if not isinstance(members, list):
        raise ValueError("pool_state_unreadable")
    return "fleet" if members else "single"

def _safe_current_profile(self) -> tuple[TelegramMode, TelegramProfile] | None:
    try:
        mode = self._active_mode()
        profile = (self._preferences.load(mode) if self._preferences is not None
                   else default_profile(mode))
        return mode, profile
    except (TelegramSettingsError, OSError, ValueError):
        logger.exception("telegram reporting settings unavailable")
        return None

def _delay_seconds(self, profile: TelegramProfile) -> float:
    if self._interval_override_seconds is not None:
        return self._interval_override_seconds
    return profile.interval_minutes * 60 if self._preferences is not None else self._settings.interval

def _run(self) -> None:
    current = self._safe_current_profile()
    if current is not None and current[1].enabled:
        self.tick()
    due = (time.monotonic() + self._delay_seconds(current[1])
           if current is not None and current[1].enabled else None)
    while not self._stop.is_set():
        remaining = max(0.0, due - time.monotonic()) if due is not None else 2.0
        self._changed.wait(min(2.0, remaining))
        self._changed.clear()
        if self._stop.is_set():
            break
        newest = self._safe_current_profile()
        if newest is None:
            current, due = None, None
            continue
        if newest != current:
            current = newest
            due = (time.monotonic() + self._delay_seconds(current[1])
                   if current[1].enabled else None)
            continue
        if due is not None and time.monotonic() >= due:
            self.tick()
            due = time.monotonic() + self._delay_seconds(current[1])
```

The reporter's `close()` sets both stop and changed events so a disabled reporter exits promptly. `_safe_current_profile()` catches `TelegramSettingsError`, logs it, and returns `None`; a corrupt shared file pauses sends until repaired without killing the thread. Keep existing direct-constructor behavior when no store is passed. Add tests using short injected delays/fake send functions; do not sleep for real minutes.
- [ ] **Step 4: Run only `tests/test_telegram_report.py` again.** Confirm all existing and new cases pass, including token redaction and 4,096-character behavior.

### Task 3: Runtime wiring and Telegram HTTP API

**Files:** Modify `tower_bot.py`, `fleet/reroll_supervisor.py`, `web/app.py`, `tests/test_web_api.py`, and `tests/test_reroll_supervisor.py`; update the Telegram section in `README.md`.

**Interfaces:** Consume Task 1's store and Task 2's reporter/preview. Extend `create_app(..., telegram_store: TelegramSettingsStore | None = None, telegram_reporter: TelegramReporter | None = None, telegram_interval_override: float | None = None, telegram_suppressed: bool = False)`. GET and PUT `/api/telegram/settings?mode=...`; POST `/api/telegram/preview?mode=...`. Add `telegram` to status runtime capabilities when the store exists.

- [ ] **Step 1: Add failing API/runtime tests.** Construct `create_app` with a temp settings store. Verify a GET contains the profile and safe credential booleans but no token, PUT updates the store and calls `reconfigure`, preview returns sample text without calling `post_message`, invalid mode/fields/interval return 422, corrupt storage returns a non-200 explicit error, and the routes are reachable before the catch-all. Assert worker `_args()` includes `--no-telegram` and the main worker path suppresses a reporter.

```python
def test_telegram_settings_save_and_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = TelegramSettingsStore(tmp_path / "telegram-settings.json")
    reporter = Mock()
    app = create_app(state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
                     telegram_store=store, telegram_reporter=reporter)
    client = TestClient(app)
    profile = {"enabled": True, "interval_minutes": 15, "fields": ["screen"]}
    assert client.put("/api/telegram/settings?mode=single", json=profile).status_code == 200
    reporter.reconfigure.assert_called_once()
    assert client.post("/api/telegram/preview?mode=single", json=profile).json()["message"].startswith("The Tower bot")

def test_worker_launch_suppresses_telegram(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    make().start("Tiramisu64_20")
    assert "--no-telegram" in spawned[0]
```

- [ ] **Step 2: Run only the new tests in these files and confirm they fail.** Use `uv run pytest -p no:allure_pytest tests/test_web_api.py -k telegram -q` and `uv run pytest -p no:allure_pytest tests/test_reroll_supervisor.py::test_worker_launch_suppresses_telegram -q`, each with a 180-second command timeout.
- [ ] **Step 3: Implement the API and wiring.** Register routes above the catch-all. Use `async def` plus `asyncio.to_thread` for file reads/writes. Return `configured`, `token_present`, `chat_id_present`, `chat_id_masked`, `suppressed`, and `interval_overridden` without a token value. Validate fields via the store for PUT and preview. Add `telegram` capability; use it for UI write preflight. In `tower_bot.py`, choose the settings path beside a standalone DB or inside the fleet root, create the store even with missing credentials, and create one reporter only when credentials exist and the process is not suppressed. Determine active fleet mode from a nonempty `reroll-pool.json`; use `FleetSetupService.reroll_snapshot()` as the fleet source. Pass the reporter and store to `create_app` and append `--no-telegram` to new fleet worker arguments:

```python
telegram_store = TelegramSettingsStore(
    (fleet_controller.root if hasattr(fleet_controller, "reroll_snapshot")
     else db_path.parent) / "telegram-settings.json",
    initial_interval_seconds=legacy_telegram_interval_from_env(),
)
worker_telegram_suppressed = args.reroll_pool is not None
reporter = None if (args.no_telegram or worker_telegram_suppressed or telegram_settings is None) else TelegramReporter(
    state, telegram_settings, controls=controls,
    preferences=telegram_store,
    fleet=(fleet_controller if callable(getattr(fleet_controller, "reroll_snapshot", None)) else None),
    interval_override_seconds=(telegram_settings.interval if args.telegram_interval is not None else None),
)
```

Use `args.reroll_pool is not None` to suppress even an already running worker after its new code starts, independent of inherited credentials. Document how settings, credentials, and CLI overrides interact in `README.md`.
- [ ] **Step 4: Run only `tests/test_web_api.py -k telegram`, the new worker suppression test, and `tests/test_telegram_report.py`.** Run `git diff --check`; do not run a directory-wide sweep.

### Task 4: Shared settings UI and both sidebar entries

**Files:** Create `web/ui/lib/telegram.ts`, `web/ui/components/TelegramSettings.tsx`, `web/ui/app/settings/page.tsx`, `web/ui/app/fleet/reroll/settings/page.tsx`, and `web/ui/components/TelegramSettings.test.tsx`; modify `web/ui/lib/api.ts`, `web/ui/lib/api.test.ts`, `web/ui/components/Sidebar.tsx`, `web/ui/components/Sidebar.test.tsx`, `web/ui/components/AccountShell.tsx`, and `web/ui/components/AccountShell.test.tsx`.

**Interfaces:** Consume Task 3's JSON shapes. Define `TelegramMode`, `TelegramProfile`, and `TelegramSettingsResponse` in `lib/telegram.ts`. Produce `fetchTelegramSettings(mode)`, `saveTelegramSettings(mode, profile)`, `previewTelegramMessage(mode, profile)` in `lib/api.ts`. The shared `<TelegramSettings mode="single" | "fleet" />` receives mode from each route.

- [ ] **Step 1: Add failing client and UI tests.** Verify GET has no account-scope header, PUT preflights `telegram` capability and uses JSON, preview updates after a field toggle without saving, Save persists the draft and shows success, Reset restores saved fields, missing credentials show an actionable state, invalid interval prevents Save, and both sidebars include the correct bottom Settings link. Test both routes with no selected account.

```tsx
test("fleet settings stays in fleet navigation", () => {
  state.pathname = "/fleet/reroll/settings/";
  render(<Sidebar />);
  expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/fleet/reroll/settings/");
  expect(screen.getByRole("link", { name: "Fleet Live" })).toBeInTheDocument();
});

test("settings page is available without an account", () => {
  state.pathname = "/settings/";
  state.selected = null;
  render(<AccountShell><p>Telegram settings</p></AccountShell>);
  expect(screen.getByText("Telegram settings")).toBeInTheDocument();
  expect(screen.queryByText("No account selected")).not.toBeInTheDocument();
});

it("does not PUT settings against a mismatched backend", async () => {
  fetchMock.mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({
    ...validStatus, runtime: { ...validStatus.runtime,
      backend: { ...validStatus.runtime.backend, source_hash: "other-build" },
      capabilities: [...validStatus.runtime.capabilities, "telegram"] },
  }) });
  await expect(saveTelegramSettings("single", { enabled: true, interval_minutes: 15,
    fields: ["screen"] })).rejects.toThrow(ApiError);
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
```

- [ ] **Step 2: Run only the touched Vitest files and confirm the new tests fail.** From `web/ui`, run `npx vitest run lib/api.test.ts components/TelegramSettings.test.tsx components/Sidebar.test.tsx components/AccountShell.test.tsx` with a 180-second command timeout.
- [ ] **Step 3: Implement the client and shared page.** Use `send(..., "telegram")` for saves and read-only preview, and `getJson(..., false)` for GET. Add `telegram` to the `Capability` union, and make `preflight("telegram")` use `fetchHostStatus()` so no selected-account header can block settings. Keep `saved` and `draft` separate; call preview on draft change with cancellation/sequence protection so an older response cannot overwrite a newer one. The two page files are one-line wrappers. Add Settings to the shared sidebar footer and mark `/settings/` independent in `AccountShell`:

```tsx
export default function SingleSettingsPage(): React.JSX.Element {
  return <TelegramSettings mode="single" />;
}

export default function FleetSettingsPage(): React.JSX.Element {
  return <TelegramSettings mode="fleet" />;
}

const SETTINGS: Item = { href: reroll ? "/fleet/reroll/settings/" : "/settings/", label: "Settings", icon: Settings2 };
```

For each optional field, render a labeled checkbox/switch from the mode's field list. Show the server preview under “Sample message — no message sent”; preserve line breaks with `white-space: pre-wrap`. Show the masked chat ID and override/suppression state without exposing secrets. Disable Save while an invalid interval or request is pending; keep the draft and show a clear error if save fails.
- [ ] **Step 4: Run those same targeted Vitest files and a targeted TypeScript check.** Use `npx tsc --noEmit` in `web/ui` and `git diff --check`. Do not run the entire Vitest suite.

### Task 5: Final focused verification and reviewable handoff

**Files:** Only files from Tasks 1–4; no additional implementation files.

- [ ] **Step 1: Run each touched Python test file only** (`tests/test_telegram_settings.py`, `tests/test_telegram_report.py`, `tests/test_web_api.py -k telegram`, and `tests/test_reroll_supervisor.py::test_worker_launch_suppresses_telegram`), always with `-p no:allure_pytest` and a 180-second timeout.
- [ ] **Step 2: Run the four touched Vitest files and `npx tsc --noEmit`.** Resolve actual failures only; do not broaden testing to unrelated directories.
- [ ] **Step 3: Review the diff against the spec.** Check that the preview never sends, token never enters API responses or saved JSON, fleet workers do not send, sidebar links work in both workspaces, and settings apply while reporting is running.
- [ ] **Step 4: Stage the changed source, tests, README, spec, and plan; display `git diff --cached --check`, `git diff --cached --stat`, and the substantive staged diff.** Stop for the user's explicit commit permission. Never push to `main`/`master`.
