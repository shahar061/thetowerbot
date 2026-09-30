# Tournament Play Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a tournament is open and a free ticket is available, the bot enters it automatically. It buys only attack and defense upgrades from an editable priority list, records the result (runs, tournament entries, ledger), and returns to farming. The UI filters tournament runs and draws them with a yellow theme.

**Architecture:**
- **Screen readers:** a new pure-reader module (`tournament_screen.py`) reads the tournament screens from OCR boxes.
- **Walk:** a bounded walk (`tournament_visit.py`) drives the menu → tournament page → entry flow, and the post-run stats → leaderboard → home flow.
- **Bot state:** a small state holder (`tournament_mode.py`) tracks whether the current run is a tournament run.
- **Policy:** the tournament policy is a `TournamentSettings` value (in `policy.py`). The main bot's `Strategy` and the fleet's `RouteBaseline` both embed it. It turns into a `manual` `AutopilotPolicy` whose rules are all attack or defense upgrades.
- **Recording:** extends the existing `runs` table, events, store sink and ledger.

**Tech Stack:** Python 3 (sqlite3, dataclasses, OpenCV and RapidOCR through the existing `ocr.py`, adbutils through `device.py`), FastAPI (`web/app.py`), Next.js + Tailwind + vitest (`web/ui`).

**Spec:** `docs/superpowers/specs/2026-09-30-tournament-play-design.md`

## Global Constraints

- Python test command (run only the named files, never a directory):
  `PY=/Users/shahar/BifrostProjects/thetowerbot/.venv/bin/python; $PY -m pytest -p no:allure_pytest tests/<file>.py -q`
- UI test command (named files only): `npm test --prefix web/ui -- <path relative to web/ui>`. UI type check: `npm run build --prefix web/ui`.
- Entry cost: free tickets only. `BATTLE` on the tournament page is tapped only with a same-frame reading of `tickets >= 1`, and at most once per visit. An unreadable ticket count never leads to entry.
- Tournament rules may contain only upgrades whose `upgrades.by_id(id).category` is `ATTACK` or `DEFENSE`. Unlock tiles are never allowed.
- Public name = `name_prefix` + the first 6 alphanumeric characters of the account id (e.g. `Tower8B9CEF`). The default `name_prefix` is `"Tower"`. The prefix matches `^[A-Za-z0-9]{1,10}$`.
- Default rule order: `damage, attack_speed, health, defense_absolute, critical_chance, critical_factor, defense_percent, health_regen, thorns, lifesteal, range, multishot_chance`.
- A tournament run's `runs.tier` stays NULL. Tournament runs are excluded from the wave and coin records, from best wave, and from pace statistics.
- Ledger kinds: `TOURNAMENT_ENTRY` (currency `tickets`, delta −1), `TOURNAMENT_PRIZE` (one line per currency, `gems` / `stones`), and `RUN_PAYOUT` with `detail.tournament = true`.
- Yellow theme: the amber of the existing record badge is `oklch(0.82 0.14 85)`. The row tint is that colour at 12% alpha.
- Commits: the repository owner approves every commit. At each commit step, stage the change, show `git diff --cached --stat`, and commit only after approval. The owner may give one blanket approval for the whole plan at the start. Never push to `main`.
- Type hints on every new function. No new dependencies.

## Review Focus

1. **The ticket counter misread as a digit that isn't there.** A single-glyph "1" or "0" is where OCR is weakest. A misread must never cause entry at 0: only an exact digits-only box in the header counter area counts. Pinned in Task 3 (`test_page_ticket_counter_needs_the_header_position`).
2. **The normal Game Over or normal HUD mistaken for tournament screens.** A farm run flagged as a tournament would corrupt records. Pinned in Task 3 (`test_normal_game_over_is_not_tournament_stats`, `test_normal_hud_has_no_tournament_marker`).
3. **The bot restarting while the stats modal is up.** After a restart the run has no armed state, but the modal must still end the run and be dismissed. Pinned in Task 7 (`test_stats_modal_ends_a_run_armed_only_by_the_hud`).
4. **A new run opened on the stats-modal frame.** The classifier still says IN_RUN after the forced end, so `runs.transition` must not start a phantom run. Pinned in Task 7 (`test_forced_end_holds_until_the_run_screen_is_left`).
5. **A worker DB that predates the migration.** Readers open worker databases read-only, without the `tournament` column or table. `list_runs`, records and best-wave queries must still work. Pinned in Task 4 (`test_list_runs_tolerates_a_db_without_tournament_schema`).

---

## File Structure

| File | Responsibility |
|---|---|
| `policy.py` (modify) | `TournamentSettings`: validation, defaults, `policy()`, `player_name()`, to/from dict |
| `strategy.py` (modify) | `Strategy.tournament` field and its round-trip and patch support |
| `fleet/build_route.py` (modify) | `RouteBaseline.tournament`, `EffectiveRoute.tournament` |
| `fleet/reroll_progress.py` (modify) | `tournament_settings()`, which resolves the per-account settings |
| `tournament_screen.py` (create) | Pure OCR readers for the menu entry, page, name prompt, profile popup, stats modal and HUD marker |
| `device.py` (modify) | `type_text`, `press_key` and key constants |
| `tournament_visit.py` (create) | `TournamentVisit` walk (entry flow and finish flow), `visit_due` |
| `tournament_mode.py` (create) | `TournamentMode`: armed state, run stamping, the `TournamentEntered` event |
| `runs.py` (modify) | `RunTracker.end()` forced end, and a hold until the run screen is left |
| `events.py` (modify) | `tournament`, `league` and `rank` on run events; three new tournament events |
| `db.py` (modify) | `runs.tournament`, the `tournament_entries` table, record functions, `farm_filter`, `list_runs` join |
| `sinks/store.py` (modify) | Persist the new events |
| `ledger.py` (modify) | Three ledger kinds |
| `fleet/reroll_metrics.py` (modify) | Exclude tournament runs from pace and recent runs |
| `tower_bot.py` (modify) | Wiring: build, schedule, arm, stats-modal end, policy swap, walk lists |
| `web/ui/lib/tournament.ts` (create) | Theme classes, `isTournamentRun`, `isTournamentLine` |
| `web/ui/lib/types.ts` (modify) | `RunRow` fields, `TournamentSettings`, `Strategy.tournament` |
| `web/ui/lib/buildRoute.ts` (modify) | `baseline.tournament` |
| `web/ui/app/fleet/reroll/runs/*` (modify) | Tint, trophy/league cell, "Tournaments" chip, detail label |
| `web/ui/components/RunTable.tsx`, `web/ui/app/runs/page.tsx` (modify) | Tint and chip on the main-bot runs page |
| `web/ui/components/LedgerEntries.tsx`, both ledger pages (modify) | Tint and chip |
| `web/ui/components/TournamentEditor.tsx` (create) | Enable toggle, prefix, spend limit, ordered attack/defense list |
| `web/ui/app/strategy/page.tsx`, `web/ui/app/fleet/reroll/strategies/StrategyStudio.tsx` (modify) | Mount `TournamentEditor` |

---

### Task 1: `TournamentSettings` and `Strategy.tournament`

**Files:**
- Modify: `policy.py` (after `AutopilotPolicy`, ~line 245)
- Modify: `strategy.py` (field list ~630-683, `_STRATEGY_TYPES` ~612-618, `to_dict` ~800, `from_dict` ~807-863, `merged` ~865-904)
- Test: `tests/test_tournament_settings.py` (create)

**Interfaces:**
- Produces:
  - `policy.TournamentSettings(enabled: bool = True, name_prefix: str = "Tower", rules: tuple[UpgradeRule, ...] = DEFAULT_TOURNAMENT_RULES, cash_reserve: int = 0, cash_spend_limit_pct: int = 100)`
  - `TournamentSettings.policy(purpose: str = "farm") -> AutopilotPolicy`
  - `TournamentSettings.player_name(account_id: str) -> str | None`
  - `TournamentSettings.to_dict() -> dict[str, Any]`
  - `TournamentSettings.from_dict(raw: Mapping[str, Any]) -> TournamentSettings`
  - `policy.DEFAULT_TOURNAMENT_RULES`
  - `Strategy.tournament: TournamentSettings`
- It raises `PolicyError`, a `ValueError` subclass (`policy.py:34`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tournament_settings.py
from __future__ import annotations

import json

import pytest

from policy import DEFAULT_TOURNAMENT_RULES, PolicyError, TournamentSettings, UpgradeRule
from strategy import ControlError, Strategy


def test_default_rules_are_the_agreed_attack_and_defense_order() -> None:
    assert [rule.upgrade_id for rule in DEFAULT_TOURNAMENT_RULES] == [
        "damage", "attack_speed", "health", "defense_absolute", "critical_chance",
        "critical_factor", "defense_percent", "health_regen", "thorns", "lifesteal",
        "range", "multishot_chance",
    ]
    assert TournamentSettings().rules == DEFAULT_TOURNAMENT_RULES


@pytest.mark.parametrize("upgrade_id", ["cash_per_wave", "coins_per_kill_bonus", "cash_bonus"])
def test_utility_upgrades_are_rejected(upgrade_id: str) -> None:
    with pytest.raises(PolicyError, match="attack or defense"):
        TournamentSettings(rules=(UpgradeRule("damage"), UpgradeRule(upgrade_id)))


def test_unlock_tiles_are_rejected_at_parse_time() -> None:
    with pytest.raises(PolicyError):
        TournamentSettings.from_dict({"rules": [{"upgrade_id": "unlock_thorns"}]})


def test_rules_must_be_unique_and_non_empty() -> None:
    with pytest.raises(PolicyError, match="unique"):
        TournamentSettings(rules=(UpgradeRule("damage"), UpgradeRule("damage")))
    with pytest.raises(PolicyError, match="at least one"):
        TournamentSettings(rules=())


@pytest.mark.parametrize("prefix", ["", "has space", "x" * 11, "émoji"])
def test_name_prefix_is_short_ascii_alphanumeric(prefix: str) -> None:
    with pytest.raises(PolicyError, match="name_prefix"):
        TournamentSettings(name_prefix=prefix)


def test_spend_limit_and_reserve_are_bounded() -> None:
    with pytest.raises(PolicyError, match="cash_spend_limit_pct"):
        TournamentSettings(cash_spend_limit_pct=0)
    with pytest.raises(PolicyError, match="cash_reserve"):
        TournamentSettings(cash_reserve=-1)


def test_player_name_uses_prefix_and_first_six_alphanumerics() -> None:
    assert TournamentSettings().player_name("8B9CEFFE1F5C4D6") == "Tower8B9CEF"
    assert TournamentSettings(name_prefix="Bot").player_name("ab-12:cd34ef") == "Botab12cd"
    assert TournamentSettings().player_name("") is None


def test_policy_is_manual_enabled_and_keeps_order() -> None:
    settings = TournamentSettings(rules=(UpgradeRule("health"), UpgradeRule("damage", target=50)),
                                  cash_reserve=5, cash_spend_limit_pct=80)
    policy = settings.policy(purpose="milestone")
    assert policy.enabled and policy.preset == "manual" and policy.purpose == "milestone"
    assert [rule.upgrade_id for rule in policy.rules] == ["health", "damage"]
    assert policy.rules[1].target == 50
    assert (policy.cash_reserve, policy.cash_spend_limit_pct) == (5, 80)


def test_round_trip_through_json() -> None:
    original = TournamentSettings(enabled=False, name_prefix="Tw",
                                  rules=(UpgradeRule("thorns", target=30),), cash_spend_limit_pct=90)
    assert TournamentSettings.from_dict(json.loads(json.dumps(original.to_dict()))) == original


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(PolicyError, match="unknown tournament field"):
        TournamentSettings.from_dict({"league": "copper"})


def test_strategy_carries_tournament_settings() -> None:
    raw = Strategy(name="t", actions=()).to_dict()
    assert raw["tournament"] == TournamentSettings().to_dict()
    raw["tournament"] = {"enabled": False, "rules": [{"upgrade_id": "health"}]}
    parsed = Strategy.from_dict(raw)
    assert parsed.tournament.enabled is False
    assert [rule.upgrade_id for rule in parsed.tournament.rules] == ["health"]


def test_strategy_rejects_utility_tournament_rule_as_control_error() -> None:
    raw = Strategy(name="t", actions=()).to_dict()
    raw["tournament"] = {"rules": [{"upgrade_id": "cash_bonus"}]}
    with pytest.raises(ControlError):
        Strategy.from_dict(raw)


def test_strategy_patch_replaces_tournament() -> None:
    merged = Strategy(name="t", actions=()).merged({"tournament": {"enabled": False}})
    assert merged.tournament.enabled is False
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_settings.py -q`
Expected: FAIL with `ImportError: cannot import name 'DEFAULT_TOURNAMENT_RULES'`.

- [ ] **Step 3: Implement `TournamentSettings` in `policy.py`**

Add `import re` to the imports if it's missing. `upgrades.by_id` is already imported as `by_id`; confirm with `grep -n "^from upgrades\|^import upgrades" policy.py`. Append after the `AutopilotPolicy` class:

```python
TOURNAMENT_CATEGORIES: tuple[str, ...] = ("ATTACK", "DEFENSE")
_NAME_PREFIX = re.compile(r"[A-Za-z0-9]{1,10}")
DEFAULT_TOURNAMENT_RULES: tuple[UpgradeRule, ...] = tuple(UpgradeRule(uid) for uid in (
    "damage", "attack_speed", "health", "defense_absolute", "critical_chance",
    "critical_factor", "defense_percent", "health_regen", "thorns", "lifesteal",
    "range", "multishot_chance",
))


@dataclass(frozen=True)
class TournamentSettings:
    """What a tournament run may buy, and whether tournaments are entered at all."""

    enabled: bool = True
    name_prefix: str = "Tower"
    rules: tuple[UpgradeRule, ...] = DEFAULT_TOURNAMENT_RULES
    cash_reserve: int = 0
    cash_spend_limit_pct: int = 100

    def __post_init__(self) -> None:
        try:
            rules = tuple(self.rules)
        except TypeError:
            raise PolicyError("rules", "rules must be a list or tuple") from None
        object.__setattr__(self, "rules", rules)
        if not isinstance(self.enabled, bool):
            raise PolicyError("enabled", "enabled must be a boolean")
        if not isinstance(self.name_prefix, str) or not _NAME_PREFIX.fullmatch(self.name_prefix):
            raise PolicyError("name_prefix", "name_prefix must be 1-10 ASCII letters or digits")
        if not rules:
            raise PolicyError("rules", "tournament rules need at least one upgrade")
        if any(not isinstance(rule, UpgradeRule) for rule in rules):
            raise PolicyError("rules", "rules must contain UpgradeRule values")
        ids = [rule.upgrade_id for rule in rules]
        if len(ids) != len(set(ids)):
            raise PolicyError("rules", "rule upgrade_id values must be unique")
        for uid in ids:
            if by_id(uid).category not in TOURNAMENT_CATEGORIES:
                raise PolicyError("rules", f"{uid} is not an attack or defense upgrade")
        if isinstance(self.cash_reserve, bool) or not isinstance(self.cash_reserve, int) \
                or self.cash_reserve < 0:
            raise PolicyError("cash_reserve", "cash_reserve must be a non-negative integer")
        if isinstance(self.cash_spend_limit_pct, bool) or not isinstance(self.cash_spend_limit_pct, int) \
                or not 1 <= self.cash_spend_limit_pct <= 100:
            raise PolicyError("cash_spend_limit_pct", "cash_spend_limit_pct must be 1-100")

    def policy(self, purpose: str = "farm") -> AutopilotPolicy:
        return AutopilotPolicy(
            enabled=True, preset="manual", rules=self.rules, economy_until_wave=0,
            cash_reserve=self.cash_reserve, cash_spend_limit_pct=self.cash_spend_limit_pct,
            purpose=purpose,
        )

    def player_name(self, account_id: str) -> str | None:
        suffix = "".join(char for char in account_id if char.isascii() and char.isalnum())[:6]
        return f"{self.name_prefix}{suffix}" if suffix else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "name_prefix": self.name_prefix,
            "rules": [rule.to_dict() for rule in self.rules],
            "cash_reserve": self.cash_reserve,
            "cash_spend_limit_pct": self.cash_spend_limit_pct,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TournamentSettings:
        if not isinstance(raw, Mapping):
            raise PolicyError("tournament", "tournament must be a mapping")
        known = {field.name for field in dataclasses.fields(cls)}
        for key in raw:
            if key not in known:
                raise PolicyError(key, f"unknown tournament field {key!r}")
        values = {key: value for key, value in raw.items() if key != "rules"}
        if "rules" in raw:
            if not isinstance(raw["rules"], (list, tuple)):
                raise PolicyError("rules", "rules must be a list or tuple")
            values["rules"] = tuple(UpgradeRule.from_dict(rule) for rule in raw["rules"])
        try:
            return cls(**values)
        except PolicyError:
            raise
        except TypeError as exc:
            raise PolicyError("tournament", str(exc)) from None
```

- [ ] **Step 4: Add `Strategy.tournament` in `strategy.py`**

1. Import: change `from policy import AutopilotPolicy, PolicyError` to `from policy import AutopilotPolicy, PolicyError, TournamentSettings`.
2. Field: after `tier_promotion: TierPromotion = TierPromotion()`, add `tournament: TournamentSettings = TournamentSettings()`.
3. Type table: after `_STRATEGY_TYPES["tier_promotion"] = (TierPromotion,)`, add `_STRATEGY_TYPES["tournament"] = (TournamentSettings,)`.
4. `to_dict`: after `"tier_promotion": self.tier_promotion.to_dict(),`, add `"tournament": self.tournament.to_dict(),`.
5. `from_dict`: before `values = {`, add:
   ```python
           try:
               tournament = (
                   TournamentSettings.from_dict(raw["tournament"])
                   if "tournament" in raw else TournamentSettings()
               )
           except PolicyError as exc:
               raise ControlError(exc.field, str(exc)) from None
   ```
   Add `"tournament"` to the excluded-keys tuple in the `values` comprehension, and pass `tournament=tournament` to `cls(...)`.
6. `merged`: before `if not updates:`, add:
   ```python
           if "tournament" in patch:
               try:
                   updates["tournament"] = TournamentSettings.from_dict(patch["tournament"])
               except PolicyError as exc:
                   raise ControlError(exc.field, str(exc)) from None
   ```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_settings.py tests/test_strategy.py tests/test_autopilot_policy.py -q`
Expected: all PASS. If `tests/test_strategy.py` compares a full `to_dict()` snapshot, add the `"tournament"` key to that expected dict, and nothing else.

- [ ] **Step 6: Commit** (after approval)

```bash
git add policy.py strategy.py tests/test_tournament_settings.py tests/test_strategy.py
git commit -m "Add tournament settings with an attack/defense-only priority list"
```

---

### Task 2: Fleet strategy `tournament` section

**Files:**
- Modify: `fleet/build_route.py` (`RouteBaseline` ~462-493, `EffectiveRoute` ~667-674, every `EffectiveRoute(` construction in `resolve_route`/`apply_rule_patches`)
- Modify: `fleet/reroll_progress.py` (add a method near `battle_policy` ~654)
- Test: `tests/test_build_route_tournament.py` (create)

**Interfaces:**
- Consumes: `policy.TournamentSettings` (Task 1).
- Produces:
  - `RouteBaseline.tournament: TournamentSettings`
  - `EffectiveRoute.tournament: TournamentSettings`
  - `RerollProgress.tournament_settings() -> TournamentSettings | None`, which returns `None` when there is no route runtime or the route is unavailable.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_build_route_tournament.py
from __future__ import annotations

import pytest

from fleet.build_route import RouteDocument, resolve_route
from policy import TournamentSettings


def _route() -> dict[str, object]:
    return RouteDocument.compatibility().to_dict()


def test_baseline_without_tournament_uses_defaults() -> None:
    raw = _route()
    raw["baseline"].pop("tournament", None)
    assert RouteDocument.from_dict(raw).baseline.tournament == TournamentSettings()


def test_baseline_tournament_round_trips() -> None:
    raw = _route()
    raw["baseline"]["tournament"] = {"enabled": False, "rules": [{"upgrade_id": "health"}]}
    document = RouteDocument.from_dict(raw)
    assert document.baseline.tournament.enabled is False
    assert RouteDocument.from_dict(document.to_dict()) == document


def test_baseline_rejects_utility_tournament_rule() -> None:
    raw = _route()
    raw["baseline"]["tournament"] = {"rules": [{"upgrade_id": "cash_per_wave"}]}
    with pytest.raises(ValueError, match="attack or defense"):
        RouteDocument.from_dict(raw)


def test_effective_route_carries_the_baseline_tournament() -> None:
    raw = _route()
    raw["baseline"]["tournament"] = {"rules": [{"upgrade_id": "thorns"}]}
    effective = resolve_route(RouteDocument.from_dict(raw), "Tiramisu64_75", "8B9CEFFE1F5C4D6")
    assert [rule.upgrade_id for rule in effective.tournament.rules] == ["thorns"]
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `$PY -m pytest -p no:allure_pytest tests/test_build_route_tournament.py -q`
Expected: FAIL with `AttributeError: 'RouteBaseline' object has no attribute 'tournament'`.

- [ ] **Step 3: Implement it in `fleet/build_route.py`**

1. Add `from policy import TournamentSettings` to the imports.
2. On `RouteBaseline`, after `rules: RouteRules = field(default_factory=RouteRules)`, add `tournament: TournamentSettings = field(default_factory=TournamentSettings)`.
3. In `RouteBaseline.from_dict`, change `_keys(raw, {"workshop", "battle", "gems", "labs", "rules"})` to include `"tournament"`. Parse it before the return with `tournament = TournamentSettings.from_dict(raw["tournament"]) if "tournament" in raw else TournamentSettings()`, and append `tournament` as the sixth positional argument of the final `cls(...)` call, after `rules`.
4. In `RouteBaseline.to_dict`, add `"tournament": self.tournament.to_dict()`.
5. On `EffectiveRoute`, add `tournament: TournamentSettings = field(default_factory=TournamentSettings)` as the last field.
6. Run `grep -n "EffectiveRoute(" fleet/build_route.py`. At every construction that has a baseline in scope, pass `tournament=<that baseline>.tournament`. At constructions that copy another `EffectiveRoute` (for example `replace(...)` in `apply_rule_patches`), leave the field to carry over.

- [ ] **Step 4: Add `tournament_settings` in `fleet/reroll_progress.py`**

Add it directly above `def battle_policy`:

```python
    def tournament_settings(self) -> TournamentSettings | None:
        """This account's resolved tournament settings, or None without a readable route."""
        if self.route_runtime is None:
            return None
        try:
            route = self.route_runtime.current()
        except RouteUnavailable:
            return None
        return resolve_route(route, self.root.name, self.account_id).tournament
```

Add `from policy import TournamentSettings` to the imports. The file already imports `resolve_route` and `RouteUnavailable`; confirm with grep.

Append this test to `tests/test_build_route_tournament.py`:

```python
def test_reroll_progress_without_route_runtime_has_no_tournament_settings(tmp_path) -> None:
    from fleet.reroll_progress import RerollProgress
    progress = RerollProgress.__new__(RerollProgress)
    progress.route_runtime = None
    assert progress.tournament_settings() is None
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `$PY -m pytest -p no:allure_pytest tests/test_build_route_tournament.py tests/test_build_route.py -q`
Expected: all PASS. If `test_build_route.py` asserts an exact baseline `to_dict()` key set, add `"tournament"` to it.

- [ ] **Step 6: Commit** (after approval)

```bash
git add fleet/build_route.py fleet/reroll_progress.py tests/test_build_route_tournament.py tests/test_build_route.py
git commit -m "Let fleet strategies carry per-account tournament settings"
```

---

### Task 3: Tournament screen readers

**Files:**
- Create: `tournament_screen.py`
- Create: `tests/fixtures/ocr/tournament/*.json` (recorded once from the committed PNGs)
- Test: `tests/test_tournament_screen.py` (create)

**Interfaces:**
- Produces (every reader is `(frame: Image, boxes: tuple[TextBox, ...]) -> Reading | None`):
  - `read_menu_entry -> MenuEntry(open: tuple[int, int])`
  - `read_page -> TournamentPage(tickets: int | None, league: str | None, battle: tuple[int, int] | None, join_seconds: int | None, tournament_id: str | None, return_to_game: tuple[int, int] | None)`
  - `read_username_prompt -> UsernamePrompt(field: tuple[int, int], save: tuple[int, int], close: tuple[int, int], text: str | None)`
  - `read_profile_popup -> ProfilePopup(close: tuple[int, int])`
  - `read_stats_modal -> TournamentStats(league: str | None, wave: int | None, rank: int | None, coins: int | None, ad_coins: int | None, killed_by: str | None, ok: tuple[int, int])`
  - `read_hud_marker(boxes: tuple[TextBox, ...]) -> bool`

- [ ] **Step 1: Record the OCR fixtures**

```bash
mkdir -p tests/fixtures/ocr/tournament
$PY - <<'EOF'
import json
from pathlib import Path
import cv2
import ocr
for png in sorted(Path("tests/fixtures/tournament").glob("*.png")):
    boxes = ocr.read(cv2.imread(str(png)), strict=True)
    out = Path("tests/fixtures/ocr/tournament") / f"{png.stem}.json"
    out.write_text(json.dumps([{"text": b.text, "confidence": round(b.confidence, 3),
                                "rect": [b.rect.x, b.rect.y, b.rect.w, b.rect.h]} for b in boxes], indent=1))
    print(out, len(boxes))
EOF
```

Then check the recorded texts the tests depend on:

```bash
grep -h '"text"' tests/fixtures/ocr/tournament/*.json | sort | uniq | head -120
```

Confirm that entries exist for:
- `OPEN`, `TOURNAMENT`, `BATTLE`, `Copper League`, `Tap To Return To Game`
- a time-left text, a `Tournament ID:` text, `USER NAME`, `Save`, `PLAYER PROFILE`
- `TOURNAMENT STATS`, `Wave 8`, `currently at rank: 30`, `103`, `Tier 1+`
- the ticket digits `1` and `0`

If the OCR engine splits or merges any of these differently (for example `Tournament ID:` and the id as two boxes), adapt the reader's regex or its neighbour lookup to the recorded boxes. **Do not change the expected values in the tests.**

For the normal-Game-Over negative test, record OCR for an existing non-tournament game-over fixture: `ls tests/fixtures | grep -i game_over`. Pick one and record it the same way into `tests/fixtures/ocr/tournament/normal_game_over.json`, copying the PNG to `tests/fixtures/tournament/normal_game_over.png`. For the normal-HUD negative, use an existing in-run fixture from `ls tests/fixtures | grep -i run` and record `normal_in_run.json` the same way.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_tournament_screen.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2

import config
import ocr
import tournament_screen as ts

FIXTURES = Path(__file__).parent / "fixtures"


def recorded(name: str) -> tuple[Any, ...]:
    raw = json.loads((FIXTURES / "ocr" / "tournament" / f"{name}.json").read_text())
    return tuple(ocr.TextBox(b["text"], b["confidence"], config.Rect(*b["rect"])) for b in raw)


def frame(name: str) -> Any:
    return cv2.imread(str(FIXTURES / "tournament" / f"{name}.png"))


def read(reader: Any, name: str) -> Any:
    return reader(frame(name), recorded(name))


def box(text: str, rect: tuple[int, int, int, int], confidence: float = .98) -> Any:
    return ocr.TextBox(text, confidence, config.Rect(*rect))


def test_menu_entry_is_the_open_label_under_the_trophy() -> None:
    entry = read(ts.read_menu_entry, "menu_tournament_open")
    assert entry is not None
    x, y = entry.open
    assert 60 <= x <= 200 and 500 <= y <= 610


def test_menu_entry_absent_on_the_tournament_page() -> None:
    assert read(ts.read_menu_entry, "tournament_join_ticket_1") is None


def test_join_page_with_one_ticket() -> None:
    page = read(ts.read_page, "tournament_join_ticket_1")
    assert page is not None
    assert page.tickets == 1
    assert page.league == "Copper League"
    assert page.battle is not None and abs(page.battle[0] - 540) < 80 and 1950 < page.battle[1] < 2100
    assert page.join_seconds is not None and 5 * 3600 <= page.join_seconds <= 6 * 3600
    assert page.return_to_game is not None and page.return_to_game[1] > 2200
    assert page.tournament_id is None


def test_leaderboard_page_after_entry() -> None:
    page = read(ts.read_page, "tournament_leaderboard_ticket_0")
    assert page is not None
    assert page.tickets == 0
    assert page.tournament_id == "FHFFFVJVAWEVESCU"
    assert page.league == "Copper League"


def test_page_ticket_counter_needs_the_header_position() -> None:
    boxes = tuple(b for b in recorded("tournament_join_ticket_1") if b.text.strip() != "1")
    moved = boxes + (box("1", (500, 1000, 20, 40)),)
    page = ts.read_page(frame("tournament_join_ticket_1"), moved)
    assert page is not None and page.tickets is None


def test_page_is_not_read_under_a_modal() -> None:
    assert read(ts.read_page, "tournament_username_prompt") is None
    assert read(ts.read_page, "tournament_player_profile") is None


def test_username_prompt_on_first_visit() -> None:
    prompt = read(ts.read_username_prompt, "tournament_username_prompt")
    assert prompt is not None
    assert prompt.text is None
    assert abs(prompt.field[1] - 1362) < 60 and abs(prompt.save[1] - 1530) < 60
    assert abs(prompt.close[0] - 882) < 60 and abs(prompt.close[1] - 812) < 60


def test_username_prompt_absent_on_the_join_page() -> None:
    assert read(ts.read_username_prompt, "tournament_join_ticket_1") is None


def test_profile_popup_close() -> None:
    popup = read(ts.read_profile_popup, "tournament_player_profile")
    assert popup is not None
    assert abs(popup.close[0] - 924) < 60 and abs(popup.close[1] - 642) < 60


def test_tournament_stats_modal() -> None:
    stats = read(ts.read_stats_modal, "tournament_stats")
    assert stats is not None
    assert (stats.league, stats.wave, stats.rank) == ("Copper League", 8, 30)
    assert (stats.coins, stats.ad_coins) == (103, 0)
    assert stats.killed_by == "Basic"
    assert abs(stats.ok[0] - 540) < 80 and abs(stats.ok[1] - 1718) < 80


def test_normal_game_over_is_not_tournament_stats() -> None:
    assert read(ts.read_stats_modal, "normal_game_over") is None


def test_hud_marker_on_tournament_runs_only() -> None:
    assert ts.read_hud_marker(recorded("tournament_run_start"))
    assert ts.read_hud_marker(recorded("tournament_run_midway"))


def test_normal_hud_has_no_tournament_marker() -> None:
    assert not ts.read_hud_marker(recorded("normal_in_run"))
    assert not ts.read_hud_marker((box("Tier 1", (560, 1480, 90, 40)),))
```

- [ ] **Step 3: Run the tests and watch them fail**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_screen.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tournament_screen'`.

- [ ] **Step 4: Implement `tournament_screen.py`**

```python
"""Tournament screens, read from a frame and its OCR boxes.

Pure: no taps, no I/O. Positions are fractions of the frame, taken from the
1080x2400 fixtures in tests/fixtures/tournament/. Every reader returns None
unless its screen is unmistakably present.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from device import Image
from ocr import TextBox

_MIN_CONFIDENCE = .8
Point = tuple[int, int]

_LEAGUE = re.compile(r"\s*([a-z]+)\s+league\s*", re.I)
_TICKETS = re.compile(r"\s*(\d{1,2})\s*")
_JOIN = re.compile(r"\s*time\s*left\s*to\s*join:?\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*", re.I)
_TOURNAMENT_ID = re.compile(r"\s*tournament\s*id:?\s*([a-z0-9]{6,})\s*", re.I)
_RETURN = re.compile(r"\s*tap\s*to\s*return\s*to\s*game\s*", re.I)
_USER_NAME = re.compile(r"\s*user\s*name\s*", re.I)
_PLAYER_PROFILE = re.compile(r"\s*player\s*profile\s*", re.I)
_STATS_TITLE = re.compile(r"\s*tournament\s*stats\s*", re.I)
_WAVE = re.compile(r"\s*wave\s*(\d+)\s*", re.I)
_RANK = re.compile(r"\s*currently\s*at\s*rank:?\s*(\d+)\s*", re.I)
_KILLED = re.compile(r"\s*killed\s*by\s*(.+?)\s*", re.I)
_LEADING_NUMBER = re.compile(r"\s*(\d[\d,]*)")
_HUD_MARKER = re.compile(r"\s*tier\s*\d+\s*\+\s*", re.I)
_MODAL_TITLES = (_USER_NAME, _PLAYER_PROFILE, _STATS_TITLE)


@dataclass(frozen=True)
class MenuEntry:
    open: Point


@dataclass(frozen=True)
class TournamentPage:
    tickets: int | None
    league: str | None
    battle: Point | None
    join_seconds: int | None
    tournament_id: str | None
    return_to_game: Point | None


@dataclass(frozen=True)
class UsernamePrompt:
    field: Point
    save: Point
    close: Point
    text: str | None


@dataclass(frozen=True)
class ProfilePopup:
    close: Point


@dataclass(frozen=True)
class TournamentStats:
    league: str | None
    wave: int | None
    rank: int | None
    coins: int | None
    ad_coins: int | None
    killed_by: str | None
    ok: Point


def _trusted(box: TextBox) -> bool:
    return (math.isfinite(box.confidence) and box.confidence >= _MIN_CONFIDENCE
            and box.rect.w > 0 and box.rect.h > 0)


def _centre(box: TextBox) -> Point:
    return box.rect.x + box.rect.w // 2, box.rect.y + box.rect.h // 2


def _matching(boxes: tuple[TextBox, ...], pattern: re.Pattern[str]) -> list[TextBox]:
    return [box for box in boxes if _trusted(box) and pattern.fullmatch(box.text)]


def _exact(boxes: tuple[TextBox, ...], word: str) -> list[TextBox]:
    return [box for box in boxes if _trusted(box) and box.text.strip().lower() == word]


def _league(boxes: tuple[TextBox, ...]) -> str | None:
    found = sorted(_matching(boxes, _LEAGUE), key=lambda box: box.rect.y)
    if not found:
        return None
    name = _LEAGUE.fullmatch(found[0].text).group(1)
    return f"{name.capitalize()} League"


def _number(text: str) -> int | None:
    match = _LEADING_NUMBER.match(text)
    return int(match.group(1).replace(",", "")) if match else None


def _below(boxes: tuple[TextBox, ...], caption: TextBox, height: int, width: int) -> int | None:
    cx, cy = _centre(caption)
    candidates = [box for box in boxes if _trusted(box)
                  and abs(_centre(box)[0] - cx) <= width * .1
                  and 0 < _centre(box)[1] - cy <= height * .06
                  and _number(box.text) is not None]
    candidates.sort(key=lambda box: _centre(box)[1])
    return _number(candidates[0].text) if candidates else None


def read_menu_entry(frame: Image, boxes: tuple[TextBox, ...]) -> MenuEntry | None:
    height, width = frame.shape[:2]
    found = [box for box in _exact(boxes, "open")
             if _centre(box)[0] < width * .25 and height * .15 < _centre(box)[1] < height * .32]
    return MenuEntry(_centre(found[0])) if len(found) == 1 else None


def read_page(frame: Image, boxes: tuple[TextBox, ...]) -> TournamentPage | None:
    height, width = frame.shape[:2]
    header = [box for box in _exact(boxes, "tournament")
              if _centre(box)[1] < height * .08 and _centre(box)[0] < width * .5]
    if len(header) != 1 or any(_matching(boxes, title) for title in _MODAL_TITLES):
        return None
    counters = [box for box in _matching(boxes, _TICKETS)
                if _centre(box)[0] > width * .85 and _centre(box)[1] < height * .08]
    tickets = int(_TICKETS.fullmatch(counters[0].text).group(1)) if len(counters) == 1 else None
    battles = [box for box in _exact(boxes, "battle") if _centre(box)[1] > height * .75]
    join = _matching(boxes, _JOIN)
    join_seconds = None
    if join:
        days, hours, minutes = (int(part or 0) for part in _JOIN.fullmatch(join[0].text).groups())
        join_seconds = ((days * 24 + hours) * 60 + minutes) * 60
    ids = _matching(boxes, _TOURNAMENT_ID)
    returns = _matching(boxes, _RETURN)
    return TournamentPage(
        tickets=tickets,
        league=_league(boxes),
        battle=_centre(battles[0]) if len(battles) == 1 else None,
        join_seconds=join_seconds,
        tournament_id=_TOURNAMENT_ID.fullmatch(ids[0].text).group(1).upper() if ids else None,
        return_to_game=_centre(returns[0]) if returns else None,
    )


def read_username_prompt(frame: Image, boxes: tuple[TextBox, ...]) -> UsernamePrompt | None:
    height, width = frame.shape[:2]
    titles = _matching(boxes, _USER_NAME)
    saves = _exact(boxes, "save")
    if len(titles) != 1 or len(saves) != 1:
        return None
    title_x, title_y = _centre(titles[0])
    save = _centre(saves[0])
    field = (save[0], save[1] - round(height * .07))
    typed = [box for box in boxes if _trusted(box)
             and abs(_centre(box)[1] - field[1]) <= height * .02
             and "enter name" not in box.text.lower()]
    return UsernamePrompt(
        field=field, save=save,
        close=(round(width * .817), title_y - round(height * .01)),
        text=typed[0].text.strip() if len(typed) == 1 else None,
    )


def read_profile_popup(frame: Image, boxes: tuple[TextBox, ...]) -> ProfilePopup | None:
    height, width = frame.shape[:2]
    titles = _matching(boxes, _PLAYER_PROFILE)
    if len(titles) != 1:
        return None
    return ProfilePopup(close=(round(width * .855), _centre(titles[0])[1]))


def read_stats_modal(frame: Image, boxes: tuple[TextBox, ...]) -> TournamentStats | None:
    height, width = frame.shape[:2]
    titles = _matching(boxes, _STATS_TITLE)
    oks = _exact(boxes, "ok")
    if len(titles) != 1 or len(oks) != 1:
        return None
    waves = _matching(boxes, _WAVE)
    ranks = _matching(boxes, _RANK)
    killed = _matching(boxes, _KILLED)
    coins_caption = _exact(boxes, "coins earned")
    ad_caption = _exact(boxes, "ad coins earned")
    return TournamentStats(
        league=_league(boxes),
        wave=int(_WAVE.fullmatch(waves[0].text).group(1)) if len(waves) == 1 else None,
        rank=int(_RANK.fullmatch(ranks[0].text).group(1)) if len(ranks) == 1 else None,
        coins=_below(boxes, coins_caption[0], height, width) if len(coins_caption) == 1 else None,
        ad_coins=_below(boxes, ad_caption[0], height, width) if len(ad_caption) == 1 else None,
        killed_by=_KILLED.fullmatch(killed[0].text).group(1) if len(killed) == 1 else None,
        ok=_centre(oks[0]),
    )


def read_hud_marker(boxes: tuple[TextBox, ...]) -> bool:
    return bool(_matching(boxes, _HUD_MARKER))
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_screen.py -q`
Expected: all PASS. When a fixture assertion fails, print that fixture's recorded boxes and fix the reader's pattern or geometry to match them. Only the reader changes: the expected values come from the screenshots and stay as they are.

- [ ] **Step 6: Commit** (after approval)

```bash
git add tournament_screen.py tests/test_tournament_screen.py tests/fixtures/ocr/tournament tests/fixtures/tournament
git commit -m "Read the tournament menu entry, page, name prompt, stats modal and HUD marker"
```

---

### Task 4: Storing tournament runs (events, db, store sink, farm-only stats)

**Files:**
- Modify: `events.py` (~147-162, plus new classes after `RunEnded`)
- Modify: `db.py` (schema DDL, migration in `connect` ~163-173, `start_run` ~277, `finish_run` ~289-334, `_schema_probe` ~343, `list_runs` ~362, `_records` ~386, `best_wave` ~246)
- Modify: `sinks/store.py` (~115-137)
- Modify: `fleet/reroll_metrics.py` (~336-337, ~387)
- Test: `tests/test_tournament_storage.py` (create)

**Interfaces:**
- Produces:
  - `events.RunStarted.tournament: bool = False`
  - `events.RunEnded.tournament: bool = False`, `league: str | None = None`, `rank: int | None = None`
  - `events.TournamentEntered(run_id: int, league: str | None, tickets_before: int)`
  - `events.TournamentResult(run_id: int, league: str | None, wave: int | None, rank: int | None, coins: int | None, ad_coins: int | None, tournament_id: str | None)`
  - `events.TournamentPrizeClaimed(tournament_id: str | None, rank: int | None, gems: int | None, stones: int | None)`
  - `db.start_run(..., tournament: bool = False)`
  - `db.finish_run(..., tournament: bool = False)`
  - `db.record_tournament_entry(conn, run_id: int, entered_at: float, league: str | None) -> None`
  - `db.record_tournament_result(conn, run_id: int, *, league, wave, rank, coins, ad_coins, tournament_id) -> None`
  - `db.record_tournament_prize(conn, tournament_id: str, *, gems: int | None, stones: int | None, claimed_at: float) -> None`
  - `db.farm_filter(conn) -> str`
  - `list_runs` rows gain `tournament`, `league`, `rank`, `tournament_id`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tournament_storage.py
from __future__ import annotations

import sqlite3
from pathlib import Path

import db
import events


def make_db(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "bot.db")


def _finish(conn: sqlite3.Connection, run_id: int, **overrides: object) -> None:
    fields: dict[str, object] = dict(started_at=0.0, ended_at=100.0 + run_id, wave=10, coins=50, tier=1,
                                     abandoned=False, scan_count=0, tap_count=0)
    fields.update(overrides)
    db.finish_run(conn, run_id, **fields)  # type: ignore[arg-type]


def test_run_events_default_to_farm() -> None:
    assert events.RunStarted(run_id=1).tournament is False
    ended = events.RunEnded(run_id=1, duration=1.0)
    assert (ended.tournament, ended.league, ended.rank) == (False, None, None)


def test_tournament_run_is_flagged_and_joined_with_its_entry(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.start_run(conn, 1, started_at=10.0, tournament=True)
    db.record_tournament_entry(conn, 1, entered_at=10.0, league="Copper League")
    _finish(conn, 1, tier=None, wave=8, coins=103, tournament=True)
    db.record_tournament_result(conn, 1, league="Copper League", wave=8, rank=30, coins=103,
                                ad_coins=0, tournament_id="FHFFFVJVAWEVESCU")
    run = db.list_runs(conn)[0]
    assert run["tournament"] == 1
    assert (run["league"], run["rank"], run["tournament_id"]) == ("Copper League", 30, "FHFFFVJVAWEVESCU")


def test_finish_never_clears_a_tournament_flag(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.start_run(conn, 1, started_at=10.0, tournament=True)
    _finish(conn, 1)
    assert db.list_runs(conn)[0]["tournament"] == 1


def test_result_without_entry_creates_the_entry(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _finish(conn, 2, tier=None, tournament=True)
    db.record_tournament_result(conn, 2, league=None, wave=8, rank=None, coins=None,
                                ad_coins=None, tournament_id=None)
    assert db.list_runs(conn)[0]["tournament_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM tournament_entries").fetchone()[0] == 1


def test_prize_is_recorded_once_per_tournament(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _finish(conn, 1, tier=None, tournament=True)
    db.record_tournament_result(conn, 1, league="Copper League", wave=8, rank=30, coins=103,
                                ad_coins=0, tournament_id="T1")
    db.record_tournament_prize(conn, "T1", gems=10, stones=5, claimed_at=500.0)
    db.record_tournament_prize(conn, "T1", gems=99, stones=99, claimed_at=600.0)
    row = conn.execute("SELECT prize_gems, prize_stones, claimed_at FROM tournament_entries").fetchone()
    assert tuple(row) == (10, 5, 500.0)


def test_tournament_runs_never_hold_or_break_records(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _finish(conn, 1, wave=20, coins=100)
    _finish(conn, 2, wave=99, coins=9999, tier=None, tournament=True)
    runs = {run["id"]: run for run in db.list_runs(conn)}
    assert runs[1]["wave_record"] == "standing" and runs[1]["coin_record"] == "standing"
    assert runs[2]["wave_record"] is None and runs[2]["coin_record"] is None
    assert db.best_wave(conn) == 20


def test_farm_filter_depends_on_the_column(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    assert db.farm_filter(conn) == " AND tournament = 0"
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at REAL, ended_at REAL, wave INTEGER)")
    assert db.farm_filter(old) == ""


def test_list_runs_tolerates_a_db_without_tournament_schema(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _finish(conn, 1)
    conn.execute("DROP TABLE tournament_entries")
    conn.execute("ALTER TABLE runs DROP COLUMN tournament")
    run = db.list_runs(conn)[0]
    assert (run["tournament"], run["league"], run["rank"], run["tournament_id"]) == (0, None, None, None)


def test_connect_migrates_an_old_runs_table(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, "
                "wave INTEGER, coins INTEGER, tier INTEGER, abandoned INTEGER NOT NULL DEFAULT 0, "
                "scan_count INTEGER NOT NULL DEFAULT 0, tap_count INTEGER NOT NULL DEFAULT 0)")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    assert "tournament" in columns
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='tournament_entries'").fetchone()


def test_store_sink_persists_tournament_events(tmp_path: Path) -> None:
    from sinks.store import StoreSink
    sink = StoreSink(tmp_path / "bot.db")
    conn = db.connect(tmp_path / "bot.db")
    sink._conn = conn
    for event in (
        events.RunStarted(run_id=1, tournament=True, ts=10.0),
        events.TournamentEntered(run_id=1, league="Copper League", tickets_before=1, ts=11.0),
        events.RunEnded(run_id=1, duration=5.0, wave=8, coins=103, tournament=True,
                        league="Copper League", rank=30, ts=15.0),
        events.TournamentResult(run_id=1, league="Copper League", wave=8, rank=30, coins=103,
                                ad_coins=0, tournament_id="T1", ts=16.0),
    ):
        sink.handle(event)
    run = db.list_runs(conn)[0]
    assert (run["tournament"], run["rank"], run["tournament_id"]) == (1, 30, "T1")
```

Before running, check how `tests/test_store_sink.py` builds and drives a `StoreSink` (constructor arguments, whether the method is `handle` or `_handle`, and how the connection is set). Adjust only the setup lines of `test_store_sink_persists_tournament_events` to match; keep its assertions.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_storage.py -q`
Expected: FAIL with `TypeError: start_run() got an unexpected keyword argument 'tournament'` (plus errors about the missing events).

- [ ] **Step 3: Add the events in `events.py`**

Replace `RunStarted` and `RunEnded`, and add three events after `RunEnded`:

```python
@dataclass(frozen=True, kw_only=True)
class RunStarted(Event):
    run_id: int
    purpose: str = "farm"
    tournament: bool = False


@dataclass(frozen=True, kw_only=True)
class RunEnded(Event):
    run_id: int
    duration: float
    wave: int | None = None
    coins: int | None = None
    tier: int | None = None
    abandoned: bool = False
    killed_by: str | None = None
    ad_coins: int | None = None
    tournament: bool = False
    league: str | None = None
    rank: int | None = None


@dataclass(frozen=True, kw_only=True)
class TournamentEntered(Event):
    run_id: int
    league: str | None
    tickets_before: int


@dataclass(frozen=True, kw_only=True)
class TournamentResult(Event):
    run_id: int
    league: str | None
    wave: int | None
    rank: int | None
    coins: int | None
    ad_coins: int | None
    tournament_id: str | None


@dataclass(frozen=True, kw_only=True)
class TournamentPrizeClaimed(Event):
    tournament_id: str | None
    rank: int | None
    gems: int | None
    stones: int | None
```

- [ ] **Step 4: Add the storage in `db.py`**

1. Next to the `CREATE TABLE IF NOT EXISTS runs` DDL, in the same schema script that `connect` executes, add:
   ```sql
   CREATE TABLE IF NOT EXISTS tournament_entries (
       id            INTEGER PRIMARY KEY AUTOINCREMENT,
       run_id        INTEGER,
       entered_at    REAL,
       league        TEXT,
       tournament_id TEXT,
       wave          INTEGER,
       rank          INTEGER,
       coins         INTEGER,
       ad_coins      INTEGER,
       prize_gems    INTEGER,
       prize_stones  INTEGER,
       claimed_at    REAL
   );
   ```
2. In `connect`, after the `ad_coins` ALTER:
   ```python
       if "tournament" not in columns:
           conn.execute("ALTER TABLE runs ADD COLUMN tournament INTEGER NOT NULL DEFAULT 0")
   ```
3. `start_run` gains `tournament: bool = False`. The SQL becomes:
   ```python
       conn.execute(
           """INSERT INTO runs (id, started_at, purpose, tournament) VALUES (?, ?, ?, ?)
              ON CONFLICT(id) DO UPDATE SET started_at = excluded.started_at,
                                           purpose = excluded.purpose,
                                           tournament = MAX(runs.tournament, excluded.tournament)""",
           (run_id, started_at, purpose, int(tournament)),
       )
   ```
4. `finish_run` gains `tournament: bool = False` after `ad_coins`. Add `tournament` to its INSERT column list and values (`int(tournament)`), and add `tournament = MAX(runs.tournament, excluded.tournament)` to its `DO UPDATE SET`.
5. Add the record functions and `farm_filter` after `finish_run`:
   ```python
   def record_tournament_entry(conn: sqlite3.Connection, run_id: int, entered_at: float,
                               league: str | None) -> None:
       conn.execute("INSERT INTO tournament_entries (run_id, entered_at, league) VALUES (?, ?, ?)",
                    (run_id, entered_at, league))
       conn.execute("UPDATE runs SET tournament = 1 WHERE id = ?", (run_id,))
       conn.commit()


   def record_tournament_result(conn: sqlite3.Connection, run_id: int, *, league: str | None,
                                wave: int | None, rank: int | None, coins: int | None,
                                ad_coins: int | None, tournament_id: str | None) -> None:
       values = (league, wave, rank, coins, ad_coins, tournament_id)
       updated = conn.execute(
           """UPDATE tournament_entries SET league = COALESCE(?, league), wave = ?, rank = ?,
                  coins = ?, ad_coins = ?, tournament_id = ? WHERE run_id = ?""",
           (*values, run_id)).rowcount
       if not updated:
           conn.execute(
               """INSERT INTO tournament_entries (league, wave, rank, coins, ad_coins, tournament_id, run_id)
                  VALUES (?, ?, ?, ?, ?, ?, ?)""", (*values, run_id))
       conn.execute("UPDATE runs SET tournament = 1 WHERE id = ?", (run_id,))
       conn.commit()


   def record_tournament_prize(conn: sqlite3.Connection, tournament_id: str, *, gems: int | None,
                               stones: int | None, claimed_at: float) -> None:
       conn.execute(
           """UPDATE tournament_entries SET prize_gems = ?, prize_stones = ?, claimed_at = ?
              WHERE tournament_id = ? AND claimed_at IS NULL""",
           (gems, stones, claimed_at, tournament_id))
       conn.commit()


   def farm_filter(conn: sqlite3.Connection) -> str:
       """SQL to append to a runs WHERE clause so tournament runs never count as farming."""
       columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
       return " AND tournament = 0" if "tournament" in columns else ""
   ```
6. In `_schema_probe`'s returned dict, add:
   ```python
           "tournament": "tournament" in columns,
           "tournament_entries": conn.execute(
               "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tournament_entries'"
           ).fetchone() is not None,
   ```
7. `list_runs`: replace the query and add the defaults:
   ```python
       entry_select, entry_join = (
           ("te.league AS league, te.rank AS rank, te.tournament_id AS tournament_id",
            "LEFT JOIN tournament_entries te ON te.run_id = runs.id")
           if schema["tournament_entries"] else
           ("NULL AS league, NULL AS rank, NULL AS tournament_id", ""))
       rows = conn.execute(
           f"SELECT runs.*, {buys_select}, {entry_select} FROM runs {entry_join} "
           "ORDER BY runs.id DESC LIMIT ?",
           (limit,),
       ).fetchall()
   ```
   Inside the loop, next to the `killed_by` default, add `if not schema["tournament"]: run["tournament"] = 0`.
8. `_records`: append ` AND tournament = 0` to its `WHERE ended_at IS NOT NULL AND abandoned = 0`, but only when `schema["tournament"]` is true. Build it as `farm = " AND tournament = 0" if schema.get("tournament") else ""` and insert `{farm}` into the f-string.
9. `best_wave`: change the query to `f"SELECT MAX(wave) FROM runs WHERE wave IS NOT NULL{farm_filter(conn)}"`.

- [ ] **Step 5: Persist the events in `sinks/store.py`**

In the `match` block:

```python
            case events.RunStarted():
                self._run_id = event.run_id
                self._scans = 0
                self._taps = 0
                db.start_run(conn, event.run_id, event.ts, purpose=event.purpose,
                             tournament=event.tournament)
```

Add `tournament=event.tournament,` to the `db.finish_run(...)` call in the `RunEnded` case, then add:

```python
            case events.TournamentEntered():
                db.record_tournament_entry(conn, event.run_id, event.ts, event.league)
            case events.TournamentResult():
                db.record_tournament_result(
                    conn, event.run_id, league=event.league, wave=event.wave, rank=event.rank,
                    coins=event.coins, ad_coins=event.ad_coins, tournament_id=event.tournament_id)
            case events.TournamentPrizeClaimed() if event.tournament_id is not None:
                db.record_tournament_prize(conn, event.tournament_id, gems=event.gems,
                                           stones=event.stones, claimed_at=event.ts)
```

- [ ] **Step 6: Keep tournament runs out of pace statistics in `fleet/reroll_metrics.py`**

Add `from db import farm_filter`; the file uses a local connection variable named `db`, so import the function by name. Then change the two queries:
- ~336-337: `"SELECT started_at, ended_at, tier, wave FROM runs WHERE ended_at IS NOT NULL" + farm_filter(<conn>) + " ORDER BY id"`
- ~387: `"SELECT ended_at, tier, wave, coins FROM runs WHERE ended_at IS NOT NULL" + farm_filter(<conn>) + " ORDER BY id DESC LIMIT 3"`

Here `<conn>` is the connection variable used on those lines. The `tier=1` best-wave query needs no change, because tournament runs have `tier` NULL.

- [ ] **Step 7: Run the tests and watch them pass**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_storage.py tests/test_db.py tests/test_store_sink.py tests/test_reroll_metrics.py -q`
Expected: all PASS.

- [ ] **Step 8: Commit** (after approval)

```bash
git add events.py db.py sinks/store.py fleet/reroll_metrics.py tests/test_tournament_storage.py
git commit -m "Store tournament runs and entries, and keep them out of farming records"
```

---

### Task 5: Tournament ledger kinds

**Files:**
- Modify: `ledger.py` (currency constants ~34-35, `KINDS` ~49-72, `classify` ~126-171)
- Test: `tests/test_tournament_ledger.py` (create)

**Interfaces:**
- Consumes: the events from Task 4.
- Produces:
  - `ledger.TICKETS = "tickets"`, `ledger.STONES = "stones"`
  - kinds `TOURNAMENT_ENTRY` and `TOURNAMENT_PRIZE`
  - `RUN_PAYOUT.detail` carrying `{"tournament": True, "league": ..., "rank": ...}` for tournament runs

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tournament_ledger.py
from __future__ import annotations

import events
import ledger


def test_entry_spends_one_ticket() -> None:
    (line,) = ledger.classify(events.TournamentEntered(run_id=7, league="Copper League",
                                                       tickets_before=1, ts=5.0, seq=3))
    assert (line.kind, line.currency, line.delta, line.run_id) == ("TOURNAMENT_ENTRY", "tickets", -1, 7)
    assert line.detail == {"league": "Copper League", "tickets_before": 1}


def test_tournament_run_payout_is_tagged() -> None:
    (line,) = ledger.classify(events.RunEnded(run_id=7, duration=1.0, coins=103, tournament=True,
                                              league="Copper League", rank=30))
    assert line.kind == "RUN_PAYOUT" and line.delta == 103
    assert line.detail == {"tournament": True, "league": "Copper League", "rank": 30}


def test_farm_run_payout_has_no_tournament_detail() -> None:
    (line,) = ledger.classify(events.RunEnded(run_id=8, duration=1.0, coins=50))
    assert line.detail == {}


def test_prize_writes_one_line_per_paid_currency() -> None:
    lines = ledger.classify(events.TournamentPrizeClaimed(tournament_id="T1", rank=23, gems=10, stones=5))
    assert [(l.kind, l.currency, l.delta) for l in lines] == [
        ("TOURNAMENT_PRIZE", "gems", 10), ("TOURNAMENT_PRIZE", "stones", 5)]
    assert all(l.detail == {"tournament_id": "T1", "rank": 23} for l in lines)
    assert ledger.classify(events.TournamentPrizeClaimed(tournament_id="T1", rank=23, gems=0, stones=None)) == ()


def test_new_kinds_are_registered() -> None:
    assert {"TOURNAMENT_ENTRY", "TOURNAMENT_PRIZE"} <= set(ledger.KINDS)
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_ledger.py -q`
Expected: FAIL with `AttributeError` or an empty tuple for `TournamentEntered`.

- [ ] **Step 3: Implement it in `ledger.py`**

1. After `GEMS = "gems"`, add `TICKETS = "tickets"` and `STONES = "stones"`. Leave `BALANCED_CURRENCIES` unchanged.
2. Append `"TOURNAMENT_ENTRY", "TOURNAMENT_PRIZE",` to `KINDS`.
3. Replace the `RunEnded` branch in `classify` and add the two new cases:
   ```python
           case events.RunEnded():
               return (LedgerLine(
                   kind="RUN_PAYOUT",
                   currency=COINS,
                   delta=event.coins,
                   run_id=event.run_id,
                   reason="abandoned" if event.abandoned else None,
                   detail=({"tournament": True, "league": event.league, "rank": event.rank}
                           if event.tournament else {}),
                   **base,
               ),)
           case events.TournamentEntered():
               return (LedgerLine(
                   kind="TOURNAMENT_ENTRY", currency=TICKETS, delta=-1, run_id=event.run_id,
                   detail={"league": event.league, "tickets_before": event.tickets_before}, **base,
               ),)
           case events.TournamentPrizeClaimed():
               return tuple(
                   LedgerLine(kind="TOURNAMENT_PRIZE", currency=currency, delta=amount,
                              detail={"tournament_id": event.tournament_id, "rank": event.rank}, **base)
                   for currency, amount in ((GEMS, event.gems), (STONES, event.stones)) if amount
               )
   ```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_ledger.py tests/test_ledger.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit** (after approval)

```bash
git add ledger.py tests/test_tournament_ledger.py
git commit -m "Record tournament entries, tagged payouts and prizes in the ledger"
```

---

### Task 6: Device text input and the `TournamentVisit` walk

**Files:**
- Modify: `device.py` (after `tap` ~101)
- Create: `tournament_visit.py`
- Test: `tests/test_tournament_visit.py` (create)

**Interfaces:**
- Consumes: `tournament_screen` readers (Task 3), `events.TournamentResult` (Task 4), `account_collection.CollectionAction` / `CollectionResult`, and `screens.ScreenState`.
- Produces:
  - `device.type_text(device, text: str) -> None`, `device.press_key(device, keycode: int) -> None`, and the constants `KEY_BACK = 4`, `KEY_ENTER = 66`, `KEY_DEL = 67`.
  - `tournament_visit.Entry(league: str | None, tickets_before: int)`
  - `TournamentVisit(frame_budget: int = 4)`, with:
    - `.active: bool` and `.name_failed: bool`
    - `.request(*, name: str | None, now: float | None = None) -> bool`
    - `.request_finish(*, run_id: int, stats: TournamentStats, now: float | None = None) -> bool`
    - `.advance(*, screen: Image, device: Any, boxes: tuple[TextBox, ...], state: str, bus: Any, now: float | None = None) -> CollectionAction | None`
    - `.take_entry() -> Entry | None` and `.cancel(reason: str, detail: str, now: float | None = None) -> None`
    - `.snapshot() -> dict[str, Any]` and `.battle_taps: int`
  - `tournament_visit.visit_due(*, enabled: bool, name_failed: bool, armed: bool, last_at: float | None, now: float) -> bool`
  - `tournament_visit.RETRY_AFTER_S = 1800`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tournament_visit.py
from __future__ import annotations

from typing import Any

import numpy as np

import config
import events
import ocr
import tournament_screen as ts
import tournament_visit as tv

FRAME = np.zeros((2400, 1080, 3), dtype=np.uint8)
MENU, RUN, UNKNOWN = "MAIN_MENU", "IN_RUN", "UNKNOWN"


class FakeDevice:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.shells: list[str] = []

    def click(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def shell(self, command: str) -> str:
        self.shells.append(command)
        return ""


class FakeBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)


def b(text: str, x: int, y: int) -> Any:
    return ocr.TextBox(text, .99, config.Rect(x - 40, y - 20, 80, 40))


MENU_BOXES = (b("OPEN", 120, 558),)
PAGE_1 = (b("TOURNAMENT", 200, 133), b("1", 1008, 134), b("Copper League", 540, 670),
          b("BATTLE", 560, 2034), b("Time left to join: 5h 59m", 540, 2148),
          b("Tap To Return To Game", 540, 2298))
PAGE_0 = tuple(x if x.text != "1" else b("0", 1008, 134) for x in PAGE_1) + (
    b("Tournament ID: FHFFFVJVAWEVESCU", 540, 2195),)
PROMPT = (b("USER NAME", 540, 836), b("Save", 540, 1530))
PROMPT_TYPED = PROMPT + (b("Tower8B9CEF", 420, 1362),)
PROFILE = (b("PLAYER PROFILE", 540, 644),)
HUD = (b("Tier 1+", 640, 1500),)
STATS = ts.TournamentStats("Copper League", 8, 30, 103, 0, "Basic", (540, 1718))


class Walk:
    def __init__(self) -> None:
        self.visit, self.device, self.bus = tv.TournamentVisit(frame_budget=2), FakeDevice(), FakeBus()

    def step(self, boxes: tuple[Any, ...], state: str = UNKNOWN) -> Any:
        return self.visit.advance(screen=FRAME, device=self.device, boxes=boxes, state=state,
                                  bus=self.bus, now=1.0)


def test_happy_path_enters_once_and_reports_the_entry() -> None:
    walk = Walk()
    assert walk.visit.request(name="Tower8B9CEF", now=0)
    walk.step(MENU_BOXES, MENU)
    walk.step(PAGE_1)
    assert walk.visit.battle_taps == 1
    walk.step((), UNKNOWN)
    walk.step(HUD, RUN)
    assert not walk.visit.active
    assert walk.visit.snapshot()["result"]["reason"] == "entered"
    assert walk.visit.take_entry() == tv.Entry("Copper League", 1)
    assert walk.visit.take_entry() is None
    assert walk.device.taps == [(120, 558), (560, 2034)]


def test_zero_tickets_returns_without_battle() -> None:
    walk = Walk()
    walk.visit.request(name="Tower8B9CEF", now=0)
    walk.step(MENU_BOXES, MENU)
    walk.step(PAGE_0)
    walk.step((), MENU)
    assert walk.visit.battle_taps == 0
    assert (540, 2298) in walk.device.taps
    assert walk.visit.snapshot()["result"]["reason"] == "no_free_entry"
    assert walk.visit.take_entry() is None


def test_unreadable_tickets_never_enter() -> None:
    walk = Walk()
    walk.visit.request(name="Tower8B9CEF", now=0)
    walk.step(MENU_BOXES, MENU)
    walk.step(tuple(x for x in PAGE_1 if x.text != "1"))
    walk.step((), MENU)
    assert walk.visit.battle_taps == 0
    assert walk.visit.snapshot()["result"]["reason"] == "tickets_unknown"


def test_name_prompt_types_verifies_and_saves() -> None:
    walk = Walk()
    walk.visit.request(name="Tower8B9CEF", now=0)
    walk.step(MENU_BOXES, MENU)
    walk.step(PROMPT)
    walk.step(PROMPT)
    assert "input text Tower8B9CEF" in walk.device.shells
    walk.step(PROMPT_TYPED)
    assert (540, 1530) in walk.device.taps
    walk.step(PROFILE)
    walk.step(PAGE_1)
    assert walk.visit.battle_taps == 1


def test_wrong_name_is_retried_once_then_abandoned() -> None:
    walk = Walk()
    walk.visit.request(name="Tower8B9CEF", now=0)
    walk.step(MENU_BOXES, MENU)
    garbled = PROMPT + (b("Tower8B9CFF", 420, 1362),)
    for _ in range(6):
        walk.step(garbled)
    walk.step((), MENU)
    assert walk.visit.name_failed
    assert (540, 1530) not in walk.device.taps
    assert walk.visit.snapshot()["result"]["reason"] == "name_failed"
    assert walk.device.shells.count("input text Tower8B9CEF") == 2


def test_without_a_name_the_prompt_is_closed() -> None:
    walk = Walk()
    walk.visit.request(name=None, now=0)
    walk.step(MENU_BOXES, MENU)
    walk.step(PROMPT)
    walk.step((), MENU)
    assert walk.visit.snapshot()["result"]["reason"] == "name_required"
    assert not any(shell.startswith("input text") for shell in walk.device.shells)


def test_unconfirmed_entry_never_taps_again() -> None:
    walk = Walk()
    walk.visit.request(name="Tower8B9CEF", now=0)
    walk.step(MENU_BOXES, MENU)
    walk.step(PAGE_1)
    for _ in range(tv.ENTRY_FRAMES + 2):
        walk.step(PAGE_1)
    assert walk.visit.battle_taps == 1
    assert walk.visit.snapshot()["result"]["reason"] == "entry_unconfirmed"
    assert walk.visit.take_entry() is None


def test_finish_flow_taps_ok_reads_the_id_and_returns() -> None:
    walk = Walk()
    assert walk.visit.request_finish(run_id=12, stats=STATS, now=0)
    walk.step((b("TOURNAMENT STATS", 540, 616), b("OK", 540, 1718)), RUN)
    walk.step(PAGE_0)
    walk.step((), MENU)
    assert walk.device.taps == [(540, 1718), (540, 2298)]
    (result,) = [e for e in walk.bus.published if isinstance(e, events.TournamentResult)]
    assert (result.run_id, result.rank, result.tournament_id) == (12, 30, "FHFFFVJVAWEVESCU")
    assert walk.visit.snapshot()["result"]["status"] == "completed"


def test_finish_flow_publishes_result_even_without_an_id() -> None:
    walk = Walk()
    walk.visit.request_finish(run_id=12, stats=STATS, now=0)
    walk.step((b("TOURNAMENT STATS", 540, 616), b("OK", 540, 1718)), RUN)
    for _ in range(4):
        walk.step((), UNKNOWN)
    walk.step((), MENU)
    (result,) = [e for e in walk.bus.published if isinstance(e, events.TournamentResult)]
    assert result.tournament_id is None
    assert "input keyevent 4" in walk.device.shells


def test_cancel_mid_visit_stops_without_entry() -> None:
    walk = Walk()
    walk.visit.request(name="Tower8B9CEF", now=0)
    walk.step(MENU_BOXES, MENU)
    walk.visit.cancel("paused", "Operator paused the bot.")
    assert not walk.visit.active and walk.visit.take_entry() is None


def test_visit_due_backs_off_and_respects_state() -> None:
    assert tv.visit_due(enabled=True, name_failed=False, armed=False, last_at=None, now=0)
    assert not tv.visit_due(enabled=True, name_failed=False, armed=False, last_at=0, now=60)
    assert tv.visit_due(enabled=True, name_failed=False, armed=False, last_at=0, now=tv.RETRY_AFTER_S)
    assert not tv.visit_due(enabled=False, name_failed=False, armed=False, last_at=None, now=0)
    assert not tv.visit_due(enabled=True, name_failed=True, armed=False, last_at=None, now=0)
    assert not tv.visit_due(enabled=True, name_failed=False, armed=True, last_at=None, now=0)


def test_type_text_rejects_shell_metacharacters() -> None:
    import pytest
    import device
    with pytest.raises(ValueError):
        device.type_text(FakeDevice(), "a; rm -rf /")
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_visit.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tournament_visit'`.

- [ ] **Step 3: Add the device helpers in `device.py`**

Add `import re` if it's missing, then append after `tap`:

```python
KEY_BACK = 4
KEY_ENTER = 66
KEY_DEL = 67
_TYPABLE = re.compile(r"[A-Za-z0-9]{1,32}")


def type_text(device: AdbDevice, text: str) -> None:
    """Type into the focused field. Only plain ASCII letters and digits reach the shell."""
    if not _TYPABLE.fullmatch(text):
        raise ValueError(f"refusing to type {text!r}")
    device.shell(f"input text {text}")


def press_key(device: AdbDevice, keycode: int) -> None:
    device.shell(f"input keyevent {int(keycode)}")
```

- [ ] **Step 4: Implement `tournament_visit.py`**

```python
"""One bounded tournament visit: enter on a free ticket, or finish a played tournament run.

Entry flow:  OPEN -> PAGE [-> NAME_TYPE -> NAME_CHECK] -> CONFIRM_ENTRY
Finish flow: STATS -> LEADERBOARD -> CONFIRM_HOME
Any exit through the page goes RETURN -> CONFIRM_HOME.

BATTLE is tapped only from PAGE, on a frame that itself reads tickets >= 1, and at
most once per visit. Nothing here ever taps BATTLE a second time.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from enum import Enum, auto
from typing import Any

import events
import tournament_screen as ts
from account_collection import CollectionAction, CollectionResult
from device import KEY_BACK, KEY_DEL, KEY_ENTER, Image, press_key, tap, type_text
from ocr import TextBox
from screens import ScreenState

RETRY_AFTER_S = 30 * 60
ENTRY_FRAMES = 10
MAX_VISIT_FRAMES = 120
NAME_ATTEMPTS = 2
_MENU = ScreenState.MAIN_MENU.value
_RUN = ScreenState.IN_RUN.value


class Step(Enum):
    IDLE = auto()
    OPEN = auto()
    PAGE = auto()
    NAME_TYPE = auto()
    NAME_CHECK = auto()
    CONFIRM_ENTRY = auto()
    STATS = auto()
    LEADERBOARD = auto()
    RETURN = auto()
    CONFIRM_HOME = auto()


@dataclass(frozen=True)
class Entry:
    league: str | None
    tickets_before: int


def visit_due(*, enabled: bool, name_failed: bool, armed: bool, last_at: float | None,
              now: float) -> bool:
    if not enabled or name_failed or armed:
        return False
    return last_at is None or now - last_at >= RETRY_AFTER_S


class TournamentVisit:
    def __init__(self, *, frame_budget: int = 4) -> None:
        self._budget = frame_budget
        self._step = Step.IDLE
        self._waited = self._frames = 0
        self._result: CollectionResult | None = None
        self._status, self._reason = "completed", "entered"
        self._name: str | None = None
        self._name_attempts = 0
        self._pending_entry: Entry | None = None
        self._entry: Entry | None = None
        self._run_id: int | None = None
        self._stats: ts.TournamentStats | None = None
        self._result_published = False
        self._bus: Any = None
        self.name_failed = False
        self.battle_taps = 0

    @property
    def active(self) -> bool:
        return self._step is not Step.IDLE

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": "running" if self.active else self._result.status if self._result else "idle",
            "step": self._step.name.lower(),
            "battle_taps": self.battle_taps,
            "name_failed": self.name_failed,
            "result": asdict(self._result) if self._result else None,
        }

    def request(self, *, name: str | None, now: float | None = None) -> bool:
        if self.active:
            return False
        self._reset()
        self._name = name
        self._enter(Step.OPEN)
        return True

    def request_finish(self, *, run_id: int, stats: ts.TournamentStats,
                       now: float | None = None) -> bool:
        if self.active:
            return False
        self._reset()
        self._run_id, self._stats = run_id, stats
        self._status, self._reason = "completed", "finished"
        self._enter(Step.STATS)
        return True

    def take_entry(self) -> Entry | None:
        entry, self._entry = self._entry, None
        return entry

    def cancel(self, reason: str, detail: str, now: float | None = None) -> None:
        if self.active:
            self._pending_entry = None
            self._finish("failed", reason, detail, time.time() if now is None else now)

    def advance(self, *, screen: Image, device: Any, boxes: tuple[TextBox, ...], state: str,
                bus: Any, now: float | None = None) -> CollectionAction | None:
        if not self.active:
            return None
        self._bus = bus
        moment = time.time() if now is None else now
        self._frames += 1
        if self._frames > MAX_VISIT_FRAMES and self._step is not Step.CONFIRM_HOME:
            return self._escape(device, "visit_too_long", moment)

        if self._step is Step.OPEN:
            if state != _MENU:
                return self._wait("menu_not_confirmed", "Tournament needs the main menu.", moment)
            entry = ts.read_menu_entry(screen, boxes)
            if entry is None:
                return self._finish("failed", "menu_entry_missing", "No tournament button on the menu.", moment)
            return self._tap(device, "tournament_open", entry.open, Step.PAGE)

        if self._step is Step.CONFIRM_ENTRY:
            if state == _RUN and ts.read_hud_marker(boxes):
                self._entry, self._pending_entry = self._pending_entry, None
                return self._finish("completed", "entered", "Tournament run started.", moment)
            self._waited += 1
            if self._waited > ENTRY_FRAMES:
                self._pending_entry = None
                return self._finish("failed", "entry_unconfirmed",
                                    "BATTLE was tapped but no tournament run appeared.", moment)
            return None

        if self._step is Step.STATS:
            stats = ts.read_stats_modal(screen, boxes)
            if stats is None:
                return self._wait("stats_not_visible", "Waiting for the tournament stats.", moment)
            return self._tap(device, "tournament_stats_ok", stats.ok, Step.LEADERBOARD)

        if self._step is Step.CONFIRM_HOME:
            if state == _MENU:
                return self._finish(self._status, self._reason, "Back on the main menu.", moment)
            return self._wait("home_not_restored", "Waiting for the main menu.", moment)

        if self._step in (Step.PAGE, Step.NAME_TYPE, Step.NAME_CHECK):
            prompt = ts.read_username_prompt(screen, boxes)
            if prompt is not None:
                return self._name_step(device, prompt, moment)
            popup = ts.read_profile_popup(screen, boxes)
            if popup is not None:
                return self._tap(device, "tournament_profile_close", popup.close, Step.PAGE)

        page = ts.read_page(screen, boxes)
        if self._step is Step.LEADERBOARD:
            if page is None:
                return self._wait("leaderboard_not_readable", "Waiting for the leaderboard.", moment)
            self._publish_result(page.tournament_id)
            return self._leave(device, page, moment)
        if self._step is Step.RETURN:
            if page is None:
                return self._wait("page_not_readable", "Waiting to leave the tournament page.", moment)
            return self._leave(device, page, moment)
        if page is None:
            return self._wait("page_not_readable", "Waiting for the tournament page.", moment)
        if page.tickets is None:
            return self._return(device, page, "tickets_unknown")
        if page.tickets < 1:
            return self._return(device, page, "no_free_entry")
        if page.battle is None:
            return self._return(device, page, "battle_missing")
        if self.battle_taps:
            return self._return(device, page, "entry_unconfirmed")
        self.battle_taps += 1
        self._pending_entry = Entry(page.league, page.tickets)
        return self._tap(device, "tournament_battle", page.battle, Step.CONFIRM_ENTRY)

    # -- steps -----------------------------------------------------------
    def _name_step(self, device: Any, prompt: ts.UsernamePrompt, moment: float) -> CollectionAction | None:
        if self._name is None:
            self._status, self._reason = "failed", "name_required"
            return self._tap(device, "tournament_name_close", prompt.close, Step.CONFIRM_HOME)
        if self._step is Step.PAGE:
            return self._tap(device, "tournament_name_field", prompt.field, Step.NAME_TYPE)
        if self._step is Step.NAME_TYPE:
            type_text(device, self._name)
            press_key(device, KEY_ENTER)
            self._name_attempts += 1
            self._enter(Step.NAME_CHECK)
            return None
        if prompt.text == self._name:
            return self._tap(device, "tournament_name_save", prompt.save, Step.PAGE)
        if prompt.text is None:
            return self._wait("name_not_read", "Waiting to read the typed name.", moment)
        if self._name_attempts < NAME_ATTEMPTS:
            tap(device, *prompt.field)
            for _ in range(len(prompt.text) + 4):
                press_key(device, KEY_DEL)
            self._enter(Step.NAME_TYPE)
            return None
        self.name_failed = True
        self._status, self._reason = "failed", "name_failed"
        return self._tap(device, "tournament_name_close", prompt.close, Step.CONFIRM_HOME)

    def _return(self, device: Any, page: ts.TournamentPage, reason: str) -> CollectionAction | None:
        self._status, self._reason = ("failed" if reason == "entry_unconfirmed" else "completed"), reason
        return self._leave(device, page, 0.0)

    def _leave(self, device: Any, page: ts.TournamentPage, moment: float) -> CollectionAction | None:
        if page.return_to_game is None:
            press_key(device, KEY_BACK)
            self._enter(Step.CONFIRM_HOME)
            return None
        return self._tap(device, "tournament_return", page.return_to_game, Step.CONFIRM_HOME)

    def _escape(self, device: Any, reason: str, moment: float) -> None:
        if self._step is Step.LEADERBOARD:
            self._publish_result(None)
        if self._status == "completed":
            self._status, self._reason = "failed", reason
        press_key(device, KEY_BACK)
        self._enter(Step.CONFIRM_HOME)

    def _publish_result(self, tournament_id: str | None) -> None:
        if self._result_published or self._stats is None or self._run_id is None:
            return
        self._result_published = True
        if self._bus is not None:
            s = self._stats
            self._bus.publish(events.TournamentResult(
                run_id=self._run_id, league=s.league, wave=s.wave, rank=s.rank, coins=s.coins,
                ad_coins=s.ad_coins, tournament_id=tournament_id))

    # -- plumbing ----------------------------------------------------------
    def _tap(self, device: Any, name: str, point: tuple[int, int], following: Step) -> CollectionAction:
        step = self._step.name.lower()
        tap(device, *point)
        self._enter(following)
        return CollectionAction(step, name, point[0], point[1], 1.0, None)

    def _wait(self, reason: str, detail: str, moment: float) -> None:
        self._waited += 1
        if self._waited <= self._budget:
            return None
        if self._step in (Step.PAGE, Step.NAME_CHECK, Step.LEADERBOARD, Step.RETURN, Step.STATS):
            return self._escape(self._device_for_escape, reason, moment)
        return self._finish("failed", reason, detail, moment)

    def _enter(self, step: Step) -> None:
        self._step, self._waited = step, 0

    def _reset(self) -> None:
        self._waited = self._frames = self._name_attempts = 0
        self._result, self._pending_entry, self._stats, self._run_id = None, None, None, None
        self._status, self._reason = "completed", "entered"
        self._result_published = False
        self.battle_taps = 0

    def _finish(self, status: str, reason: str, detail: str, moment: float) -> None:
        self._result = CollectionResult(status, reason, detail, "tournament", moment)
        self._step = Step.IDLE
        return None
```

`_wait` needs the device for its escape path. At the top of `advance`, right after `self._bus = bus`, add `self._device_for_escape = device`, and initialise `self._device_for_escape: Any = None` in `__init__`.

- [ ] **Step 5: Run the tests and watch them pass**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_visit.py -q`
Expected: all PASS. Trace any failure against the step table in the module docstring. Change the walk, not the tests.

- [ ] **Step 6: Commit** (after approval)

```bash
git add device.py tournament_visit.py tests/test_tournament_visit.py
git commit -m "Add the tournament visit walk with free-ticket-only entry"
```

---

### Task 7: Tournament mode, forced run end, and `tower_bot` wiring

**Files:**
- Create: `tournament_mode.py`
- Modify: `runs.py` (`RunTracker` ~65-99; confirm the class name with `grep -n "^class" runs.py`)
- Modify: `tower_bot.py`, at these call sites:
  - construction ~195-202
  - `_any_walk_active` 1113
  - `_cancel_walks` 1289
  - the walk lists at 838, 973-974, 1614, 1811, 2089, 2157, 2174-2178, 2206, 2536
  - run transition 1939-1966
  - the walk dispatch gate 2262 and chain ~2296
  - the battle autopilot 2553-2623
  - the menu offer sites 2693 and 2719
- Test: `tests/test_tournament_mode.py` (create)

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces:
  - `tournament_mode.TournamentMode`, with:
    - `.armed: bool`
    - `.arm(entry: Entry | None) -> None` and `.disarm() -> None`
    - `.stamp_started(event: RunStarted, hud_marker: bool) -> RunStarted`
    - `.entered_event(run_id: int | None) -> TournamentEntered | None`
    - `.observe_hud(hud_marker: bool) -> None`
  - `RunTracker.end(now: float) -> RunEnded | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tournament_mode.py
from __future__ import annotations

import events
import runs
from screens import ScreenState
from tournament_mode import TournamentMode
from tournament_visit import Entry


def tracker() -> runs.RunTracker:
    return runs.RunTracker()


def test_entry_arms_and_stamps_the_next_run() -> None:
    mode = TournamentMode()
    mode.arm(Entry("Copper League", 1))
    started = mode.stamp_started(events.RunStarted(run_id=4), hud_marker=False)
    assert started.tournament is True
    entered = mode.entered_event(4)
    assert (entered.run_id, entered.league, entered.tickets_before) == (4, "Copper League", 1)
    assert mode.entered_event(4) is None


def test_hud_marker_arms_after_a_restart_without_a_ledger_entry() -> None:
    mode = TournamentMode()
    started = mode.stamp_started(events.RunStarted(run_id=5), hud_marker=True)
    assert started.tournament and mode.armed
    assert mode.entered_event(5) is None


def test_farm_runs_stay_farm() -> None:
    mode = TournamentMode()
    assert mode.stamp_started(events.RunStarted(run_id=6), hud_marker=False).tournament is False
    assert not mode.armed


def test_observe_hud_arms_a_run_already_open() -> None:
    mode = TournamentMode()
    mode.observe_hud(True)
    assert mode.armed
    mode.disarm()
    assert not mode.armed


def test_forced_end_closes_the_open_run() -> None:
    t = tracker()
    t.transition(ScreenState.IN_RUN, 10.0)
    ended = t.end(25.0)
    assert ended is not None and ended.duration == 15.0 and not ended.abandoned
    assert t.end(26.0) is None


def test_forced_end_holds_until_the_run_screen_is_left() -> None:
    t = tracker()
    t.transition(ScreenState.IN_RUN, 10.0)
    t.end(25.0)
    assert t.transition(ScreenState.IN_RUN, 26.0) is None
    assert t.transition(ScreenState.UNKNOWN, 27.0) is None
    assert t.transition(ScreenState.IN_RUN, 28.0) is None
    assert t.transition(ScreenState.MAIN_MENU, 29.0) is None
    started = t.transition(ScreenState.IN_RUN, 30.0)
    assert isinstance(started, events.RunStarted)


def test_stats_modal_ends_a_run_armed_only_by_the_hud() -> None:
    mode, t = TournamentMode(), tracker()
    started = t.transition(ScreenState.IN_RUN, 10.0)
    mode.stamp_started(started, hud_marker=True)
    assert mode.armed
    assert t.end(20.0) is not None
```

The hold only releases on a *known* non-run screen (`MAIN_MENU` or `GAME_OVER`), not on `UNKNOWN`. The leaderboard page classifies as `UNKNOWN` and sits between the modal and the menu.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_mode.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'tournament_mode'`. If `runs.RunTracker` is not the class name, update the `tracker()` helper to the real class and constructor, and nothing else.

- [ ] **Step 3: Implement `tournament_mode.py`**

```python
"""Whether the current run is a tournament run, and the one entry event it owes the ledger."""
from __future__ import annotations

import dataclasses

import events
from tournament_visit import Entry


class TournamentMode:
    def __init__(self) -> None:
        self.armed = False
        self._entry: Entry | None = None

    def arm(self, entry: Entry | None) -> None:
        self.armed = True
        self._entry = entry

    def disarm(self) -> None:
        self.armed = False
        self._entry = None

    def observe_hud(self, hud_marker: bool) -> None:
        if hud_marker and not self.armed:
            self.arm(None)

    def stamp_started(self, event: events.RunStarted, hud_marker: bool) -> events.RunStarted:
        self.observe_hud(hud_marker)
        return dataclasses.replace(event, tournament=True) if self.armed else event

    def entered_event(self, run_id: int | None) -> events.TournamentEntered | None:
        if self._entry is None or run_id is None:
            return None
        entry, self._entry = self._entry, None
        return events.TournamentEntered(run_id=run_id, league=entry.league,
                                        tickets_before=entry.tickets_before)
```

- [ ] **Step 4: Add `end()` and the hold in `runs.py`**

In `__init__`, add `self._hold_in_run = False`. At the top of `transition`, after the UNKNOWN early return, add:

```python
        if self._hold_in_run:
            if curr is ScreenState.IN_RUN:
                return None
            self._hold_in_run = False
```

Keep the UNKNOWN early return *before* this block, so UNKNOWN never releases the hold. Then add:

```python
    def end(self, now: float) -> events.RunEnded | None:
        """Close the open run although the screen still reads IN_RUN (a tournament stats modal)."""
        if self.current_id is None or self._started_at is None:
            return None
        run_id, started = self.current_id, self._started_at
        self.current_id = None
        self._started_at = None
        self.completed += 1
        self._hold_in_run = True
        return events.RunEnded(run_id=run_id, duration=now - started, abandoned=False)
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `$PY -m pytest -p no:allure_pytest tests/test_tournament_mode.py tests/test_runs.py -q`
Expected: all PASS.

- [ ] **Step 6: Wire it into `tower_bot.py`**

Make each edit at the call site named. Read ±15 lines around each one before editing.

1. **Imports:** `import tournament_screen`, `from tournament_mode import TournamentMode`, and `from tournament_visit import TournamentVisit, visit_due`.
2. **Construction** (~195-202, beside the other walks):
   ```python
           self.tournament_visit = TournamentVisit()
           self.tournament = TournamentMode()
           self._tournament_last_at: float | None = None
   ```
3. **Settings and name helpers** (new methods beside `_offer_claim`):
   ```python
       def _tournament_settings(self, settings: Any) -> TournamentSettings:
           resolved = (self.reroll_progress.tournament_settings()
                       if self.reroll_progress is not None else None)
           return resolved if resolved is not None else settings.strategy.tournament

       def _tournament_name(self, tournament: TournamentSettings) -> str | None:
           account = getattr(self.supervisor, "current_account", None) or (
               self.reroll_progress.account_id if self.reroll_progress is not None else None)
           return tournament.player_name(account) if account else None

       def _offer_tournament(self, settings: Any) -> bool:
           if settings.paused or self._any_walk_active():
               return False
           tournament = self._tournament_settings(settings)
           now = time.time()
           if not visit_due(enabled=tournament.enabled, name_failed=self.tournament_visit.name_failed,
                            armed=self.tournament.armed, last_at=self._tournament_last_at, now=now):
               return False
           self._tournament_last_at = now
           return self.tournament_visit.request(name=self._tournament_name(tournament), now=now)
   ```
   Import `TournamentSettings` from `policy`.
4. **Menu offer** (2693 and 2719): change `armed = self._offer_claim(settings)` to `armed = self._offer_claim(settings) or self._offer_tournament(settings)`.
5. **Walk lists:**
   - Add `or self.tournament_visit.active` to `_any_walk_active` and to the gate at 2262.
   - Add `self.tournament_visit` to the tuple in `_cancel_walks`.
   - At each of 838, 973-974, 1614, 1811, 2089, 2157, 2174-2178, 2206 and 2536: where the line lists the other walks' `.active`, add `or self.tournament_visit.active` in the same form. Where it lists walks to cancel, add `self.tournament_visit`.
6. **Walk dispatch** (the `elif` chain ~2296):
   ```python
           elif self.tournament_visit.active:
               walking = "tournament_visit"
               action = self.tournament_visit.advance(
                   screen=self.screen, device=self.device, boxes=reads.full(),
                   state=state.value, bus=self.bus)
               entry = self.tournament_visit.take_entry()
               if entry is not None:
                   self.tournament.arm(entry)
                   entered = self.tournament.entered_event(self.runs.current_id)
                   if entered is not None:
                       self.bus.publish(entered)
   ```
   Here `reads` is the frame's `FrameReads`; use the variable the neighbouring code uses for it (see `reads.battle()` around 1679).
7. **HUD arming and run start** (1939-1966): before `run_event = self.runs.transition(...)`, compute `hud_marker = state is screens.ScreenState.IN_RUN and tournament_screen.read_hud_marker(tuple(boxes))`, using the frame's already-read battle boxes. Then call `self.tournament.observe_hud(hud_marker)`. In the `RunStarted` branch, after the purpose stamp, add:
   ```python
                       run_event = self.tournament.stamp_started(run_event, hud_marker)
   ```
   After `self.bus.publish(run_event)`, when the event is `RunStarted`:
   ```python
                   if isinstance(run_event, events.RunStarted):
                       entered = self.tournament.entered_event(run_event.run_id)
                       if entered is not None:
                           self.bus.publish(entered)
   ```
8. **Stats modal:** in the same frame handler, *before* `self.runs.transition(...)`, add:
   ```python
               if (self.tournament.armed and state is screens.ScreenState.IN_RUN
                       and not self.tournament_visit.active):
                   stats = tournament_screen.read_stats_modal(self.screen, reads.full())
                   if stats is not None:
                       ended = self.runs.end(time.monotonic())
                       if ended is not None:
                           ended = dataclasses.replace(
                               ended, wave=stats.wave, coins=stats.coins, ad_coins=stats.ad_coins,
                               killed_by=stats.killed_by, tournament=True, league=stats.league,
                               rank=stats.rank)
                           self.bus.publish(ended)
                           self.autopilot.suspend("Tournament run ended", clear_battle=True)
                           self.tournament_visit.request_finish(run_id=ended.run_id, stats=stats)
                       self.tournament.disarm()
   ```
   The existing best-wave and ladder-tier bookkeeping stays inside the normal `RunEnded` branch. The forced end skips it on purpose, so a tournament never raises farming bests.
9. **Autopilot** (2553-2623):
   - Change the gate `if settings.strategy.autopilot.enabled or self.autopilot.has_work:` to `if settings.strategy.autopilot.enabled or self.autopilot.has_work or self.tournament.armed:`.
   - Replace the `battle_policy = (...)` expression with:
     ```python
                         if self.tournament.armed:
                             battle_policy = self._tournament_settings(settings).policy(
                                 purpose=settings.strategy.autopilot.purpose)
                         else:
                             battle_policy = (<the existing reroll/default expression, unchanged>)
     ```
   - The legacy `else:` branch (`strategy.actions`) is then unreachable while armed, because the gate is open.

- [ ] **Step 7: Import and smoke checks**

Run: `$PY -c "import tower_bot" && $PY -m pytest -p no:allure_pytest tests/test_tournament_mode.py tests/test_tournament_visit.py tests/test_bot_reporting.py -q`
Expected: the import succeeds and all tests PASS.

Also run `grep -n "milestones_claim.active" tower_bot.py | wc -l` and `grep -n "tournament_visit.active" tower_bot.py | wc -l`. The second count should be at least the first. If it is lower, find the walk list you missed.

- [ ] **Step 8: Commit** (after approval)

```bash
git add tournament_mode.py runs.py tower_bot.py tests/test_tournament_mode.py
git commit -m "Enter tournaments between runs and play them with the tournament policy"
```

---

### Task 8: Runs UI (yellow tournament rows and filter)

**Files:**
- Create: `web/ui/lib/tournament.ts`
- Modify: `web/ui/lib/types.ts` (`RunRow` ~146)
- Modify: `web/ui/app/fleet/reroll/runs/RunsTable.tsx`, `web/ui/app/fleet/reroll/runs/page.tsx`, `web/ui/app/fleet/reroll/runs/RunDetailPanel.tsx`
- Modify: `web/ui/components/RunTable.tsx`, `web/ui/app/runs/page.tsx`
- Test: `web/ui/lib/tournament.test.ts` (create), `web/ui/components/RunTable.test.tsx` (extend), `web/ui/app/fleet/reroll/runs/page.test.tsx` (extend)

**Interfaces:**
- Consumes: the `/api/runs` fields `tournament`, `league`, `rank` and `tournament_id` (Task 4).
- Produces:
  - `TOURNAMENT_TINT`, `TOURNAMENT_TEXT`
  - `isTournamentRun(run: { tournament?: number | boolean | null }): boolean`
  - `isTournamentLine(line: { kind: string; detail?: Record<string, unknown> }): boolean`

- [ ] **Step 1: Write the failing tests**

```ts
// web/ui/lib/tournament.test.ts
import { describe, expect, it } from "vitest";
import { isTournamentLine, isTournamentRun } from "./tournament";

describe("tournament helpers", () => {
  it("recognises tournament runs", () => {
    expect(isTournamentRun({ tournament: 1 })).toBe(true);
    expect(isTournamentRun({ tournament: 0 })).toBe(false);
    expect(isTournamentRun({})).toBe(false);
  });
  it("recognises tournament ledger lines", () => {
    expect(isTournamentLine({ kind: "TOURNAMENT_ENTRY" })).toBe(true);
    expect(isTournamentLine({ kind: "TOURNAMENT_PRIZE" })).toBe(true);
    expect(isTournamentLine({ kind: "RUN_PAYOUT", detail: { tournament: true } })).toBe(true);
    expect(isTournamentLine({ kind: "RUN_PAYOUT", detail: {} })).toBe(false);
  });
});
```

Add to `web/ui/components/RunTable.test.tsx`:

```tsx
it("tints tournament runs and shows their league", () => {
  const rows: RunRow[] = [
    { id: 1, started_at: 0, ended_at: 30, wave: 12, coins: 500, tier: 1, abandoned: 0, scan_count: 1, tap_count: 1 },
    { id: 2, started_at: 0, ended_at: 30, wave: 8, coins: 103, tier: null, abandoned: 0, scan_count: 1, tap_count: 1,
      tournament: 1, league: "Copper League", rank: 30 },
  ];
  render(<RunTable runs={rows} onSelect={() => {}} />);
  const row = screen.getByText("Copper League").closest("tr")!;
  expect(row).toHaveAttribute("data-tournament", "true");
  expect(screen.getByText("12").closest("tr")).not.toHaveAttribute("data-tournament");
});
```

Add to `web/ui/app/fleet/reroll/runs/page.test.tsx`, using the file's existing `run(id, over)` factory and mocks:

```tsx
it("filters to tournament runs with the Tournaments chip", async () => {
  // Arrange the mocked worker runs so run 1 is a farm run and run 2 has tournament: 1, league: "Copper League", rank: 30.
  const chip = await screen.findByRole("button", { name: "Tournaments" });
  expect(await screen.findAllByRole("button", { name: /^Run #/ })).toHaveLength(2);
  fireEvent.click(chip);
  const rows = await screen.findAllByRole("button", { name: /^Run #/ });
  expect(rows).toHaveLength(1);
  expect(rows[0]).toHaveAttribute("data-tournament", "true");
  expect(within(rows[0]).getByText("Copper League")).toBeInTheDocument();
});
```

Arrange this test the way the neighbouring tests set up their mocked run list. Copy the closest existing test's setup and give its second run `tournament: 1, league: "Copper League", rank: 30`.

- [ ] **Step 2: Run the tests and watch them fail**

Run: `npm test --prefix web/ui -- lib/tournament.test.ts components/RunTable.test.tsx app/fleet/reroll/runs/page.test.tsx`
Expected: FAIL (the module is missing, and there's no `data-tournament` or chip).

- [ ] **Step 3: Implement it**

`web/ui/lib/tournament.ts`:

```ts
export const TOURNAMENT_TINT = "bg-[oklch(0.82_0.14_85/0.12)]";
export const TOURNAMENT_TEXT = "text-[oklch(0.82_0.14_85)]";
export const TOURNAMENT_CHIP_ON = "border-[oklch(0.82_0.14_85)] text-[oklch(0.82_0.14_85)]";

export function isTournamentRun(run: { tournament?: number | boolean | null }): boolean {
  return Boolean(run.tournament);
}

export function isTournamentLine(line: { kind: string; detail?: Record<string, unknown> | null }): boolean {
  return line.kind.startsWith("TOURNAMENT_") || line.detail?.tournament === true;
}
```

In `web/ui/lib/types.ts`, add these to `RunRow`:

```ts
  tournament?: number;
  league?: string | null;
  rank?: number | null;
  tournament_id?: string | null;
```

`RunsTable.tsx`:
- Import `{ TOURNAMENT_TEXT, TOURNAMENT_TINT, isTournamentRun }` from `@/lib/tournament`.
- In the row, compute `const tournament = isTournamentRun(run);` and add `data-tournament={tournament || undefined}` to the `<button>`.
- Change the className's inactive branch from `"hover:bg-muted/50"` to ``${tournament ? TOURNAMENT_TINT : ""} hover:bg-muted/50``.
- Replace the tier cell with:
  ```tsx
            <span className="font-mono">{tournament
              ? <span className={`inline-flex items-center gap-1 truncate font-sans text-xs ${TOURNAMENT_TEXT}`}><Trophy aria-hidden="true" className="size-3" />{run.league ?? "Tournament"}</span>
              : run.tier === null ? "—" : `T${run.tier}`}</span>
  ```

`app/fleet/reroll/runs/page.tsx`:
- Add `const [tournamentsOnly, setTournamentsOnly] = useState(false);`.
- Extend the filter chain with `.filter(run => !tournamentsOnly || isTournamentRun(run))`.
- After the "Records only" button, add:
  ```tsx
          <button type="button" aria-pressed={tournamentsOnly} onClick={() => setTournamentsOnly(value => !value)}
            className={`h-8 rounded-full border px-3 ${tournamentsOnly ? TOURNAMENT_CHIP_ON : ""}`}>Tournaments</button>
  ```

`RunDetailPanel.tsx`: replace `{run.purpose === "milestone" ? "Milestone run" : "Farm run"}` with:

```tsx
{isTournamentRun(run)
  ? `Tournament run${run.league ? ` · ${run.league}` : ""}${run.rank != null ? ` · rank ${run.rank}` : ""}${run.tournament_id ? ` · ${run.tournament_id}` : ""}`
  : run.purpose === "milestone" ? "Milestone run" : "Farm run"}
```

`components/RunTable.tsx`:
- Keep the headers unchanged. Add the import `import { TOURNAMENT_TEXT, TOURNAMENT_TINT, isTournamentRun } from "@/lib/tournament";`.
- Change the `<tr>` to:
  ```tsx
            <tr key={run.id} onClick={() => onSelect(run.id)} data-tournament={isTournamentRun(run) || undefined}
              className={`cursor-pointer hover:bg-accent/50 ${isTournamentRun(run) ? TOURNAMENT_TINT : ""}`}>
  ```
- Change the tier cell to `{isTournamentRun(run) ? <span className={TOURNAMENT_TEXT}>{run.league ?? "Tournament"}</span> : run.tier ?? "-"}`.

`app/runs/page.tsx`: add the same `tournamentsOnly` state and chip above `<RunTable>`, and pass `runs={tournamentsOnly ? runs.filter(isTournamentRun) : runs}`.

- [ ] **Step 4: Run the tests and the type check, and watch them pass**

Run: `npm test --prefix web/ui -- lib/tournament.test.ts components/RunTable.test.tsx app/fleet/reroll/runs/page.test.tsx app/runs/page.test.tsx && npm run build --prefix web/ui`
Expected: tests PASS and the build succeeds.

- [ ] **Step 5: Commit** (after approval)

```bash
git add web/ui/lib/tournament.ts web/ui/lib/tournament.test.ts web/ui/lib/types.ts web/ui/app/fleet/reroll/runs web/ui/components/RunTable.tsx web/ui/components/RunTable.test.tsx web/ui/app/runs/page.tsx
git commit -m "Show tournament runs in yellow with a Tournaments filter"
```

---

### Task 9: Ledger UI (yellow tournament lines and filter)

**Files:**
- Modify: `web/ui/components/LedgerEntries.tsx` (`KIND_TONE` ~10-25, row ~91-92)
- Modify: `web/ui/app/ledger/page.tsx`, `web/ui/app/fleet/reroll/ledger/page.tsx`
- Test: `web/ui/app/ledger/page.test.tsx` (extend), `web/ui/app/fleet/reroll/ledger/page.test.tsx` (extend)

**Interfaces:**
- Consumes: `isTournamentLine`, `TOURNAMENT_TINT`, `TOURNAMENT_CHIP_ON` (Task 8), and the ledger kinds (Task 5).

- [ ] **Step 1: Write the failing test**

Add to `web/ui/app/ledger/page.test.tsx`, using its existing fetch mock:

```tsx
it("tints tournament lines and filters to them", async () => {
  // Mock fetchLedger to return three lines:
  //   { kind: "RUN_PAYOUT", delta: 50, currency: "coins", detail: {} },
  //   { kind: "RUN_PAYOUT", delta: 103, currency: "coins", detail: { tournament: true, league: "Copper League", rank: 30 } },
  //   { kind: "TOURNAMENT_ENTRY", delta: -1, currency: "tickets", detail: { league: "Copper League", tickets_before: 1 } }
  const rows = await screen.findAllByRole("row");
  const tinted = rows.filter(row => row.getAttribute("data-tournament") === "true");
  expect(tinted).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "Tournaments" }));
  const after = (await screen.findAllByRole("row")).filter(row => row.querySelector("td"));
  expect(after.every(row => row.getAttribute("data-tournament") === "true")).toBe(true);
});
```

Build the three mocked lines with the file's existing line factory, filling in every field `LedgerLine` needs. Add the same test to `app/fleet/reroll/ledger/page.test.tsx` with that file's mock shape (one member).

- [ ] **Step 2: Run the tests and watch them fail**

Run: `npm test --prefix web/ui -- app/ledger/page.test.tsx app/fleet/reroll/ledger/page.test.tsx`
Expected: FAIL (no `data-tournament`, no chip).

- [ ] **Step 3: Implement it**

`LedgerEntries.tsx`:
- Add to `KIND_TONE`:
  ```tsx
  TOURNAMENT_ENTRY: "bg-[oklch(0.82_0.14_85/0.18)] text-[oklch(0.82_0.14_85)]",
  TOURNAMENT_PRIZE: "bg-[oklch(0.82_0.14_85/0.18)] text-[oklch(0.82_0.14_85)]",
  ```
- Change the row to:
  ```tsx
  {entries.map(({ key, head, financial, rehearsal, source, balanced: ownBalanced }) => {
    const tournament = isTournamentLine(head) || financial.some(isTournamentLine);
    return (
    <tr key={key} data-tournament={tournament || undefined} className={cn("border-t", tournament && TOURNAMENT_TINT)}>
  ```
  Close the arrow function body with `);})}` where the `<tr>` ends.

Both ledger pages:
- Add `const [tournamentsOnly, setTournamentsOnly] = useState(false);`.
- Add the chip next to the existing kind chips or select:
  ```tsx
  <button type="button" aria-pressed={tournamentsOnly} onClick={() => setTournamentsOnly(value => !value)}
    className={cn("rounded-full border px-2 py-0.5 text-xs", tournamentsOnly ? TOURNAMENT_CHIP_ON : "border-border text-muted-foreground hover:bg-muted")}>Tournaments</button>
  ```
- Pass `entries={tournamentsOnly ? entries.filter(entry => isTournamentLine(entry.head) || entry.financial.some(isTournamentLine)) : entries}`.

- [ ] **Step 4: Run the tests and the type check, and watch them pass**

Run: `npm test --prefix web/ui -- app/ledger/page.test.tsx app/fleet/reroll/ledger/page.test.tsx && npm run build --prefix web/ui`
Expected: PASS, and the build succeeds.

- [ ] **Step 5: Commit** (after approval)

```bash
git add web/ui/components/LedgerEntries.tsx web/ui/app/ledger web/ui/app/fleet/reroll/ledger
git commit -m "Show tournament ledger lines in yellow with a Tournaments filter"
```

---

### Task 10: Tournament editor in the strategy pages

**Files:**
- Create: `web/ui/components/TournamentEditor.tsx`
- Modify: `web/ui/lib/types.ts` (add `TournamentSettings`, `Strategy.tournament?`)
- Modify: `web/ui/lib/buildRoute.ts` (`BuildRouteDocument.baseline.tournament?`)
- Modify: `web/ui/app/strategy/page.tsx` (mount below `AutopilotEditor` ~223)
- Modify: `web/ui/app/fleet/reroll/strategies/StrategyStudio.tsx` (mount in the baseline editor area)
- Test: `web/ui/components/TournamentEditor.test.tsx` (create)

**Interfaces:**
- Consumes: the `Upgrade` type (`lib/types.ts:291`) and the server defaults (Task 1). A saved strategy without `tournament` gets the defaults on the server.
- Produces:
  - `TournamentSettings` TS interface `{ enabled: boolean; name_prefix: string; rules: UpgradeRule[]; cash_reserve: number; cash_spend_limit_pct: number }`
  - `DEFAULT_TOURNAMENT: TournamentSettings`
  - `TournamentEditor({ value, onChange, catalog, disabled })`

- [ ] **Step 1: Write the failing test**

```tsx
// web/ui/components/TournamentEditor.test.tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Upgrade } from "@/lib/types";
import { DEFAULT_TOURNAMENT, TournamentEditor } from "./TournamentEditor";

const catalog: Upgrade[] = [
  { id: "damage", name: "Damage", category: "ATTACK", aliases: [], unlock: false },
  { id: "health", name: "Health", category: "DEFENSE", aliases: [], unlock: false },
  { id: "cash_bonus", name: "Cash Bonus", category: "UTILITY", aliases: [], unlock: false },
  { id: "unlock_thorns", name: "Thorns", category: "DEFENSE", aliases: [], unlock: true },
];
const value = { ...DEFAULT_TOURNAMENT, rules: [{ upgrade_id: "damage", enabled: true, target: null }] };

describe("TournamentEditor", () => {
  it("offers only attack and defense upgrades that are not already listed", () => {
    render(<TournamentEditor value={value} onChange={() => {}} catalog={catalog} />);
    const options = Array.from(screen.getByLabelText("Add tournament upgrade").querySelectorAll("option")).map(o => o.textContent);
    expect(options).toEqual(["Add upgrade…", "Health"]);
  });

  it("adds, moves and removes rules", () => {
    const onChange = vi.fn();
    render(<TournamentEditor value={value} onChange={onChange} catalog={catalog} />);
    fireEvent.change(screen.getByLabelText("Add tournament upgrade"), { target: { value: "health" } });
    expect(onChange).toHaveBeenLastCalledWith({ ...value, rules: [...value.rules, { upgrade_id: "health", enabled: true, target: null }] });
    fireEvent.click(screen.getByRole("button", { name: "Remove Damage" }));
    expect(onChange).toHaveBeenLastCalledWith({ ...value, rules: [] });
  });

  it("toggles tournament entry", () => {
    const onChange = vi.fn();
    render(<TournamentEditor value={value} onChange={onChange} catalog={catalog} />);
    fireEvent.click(screen.getByLabelText("Enter tournaments"));
    expect(onChange).toHaveBeenLastCalledWith({ ...value, enabled: false });
  });
});
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `npm test --prefix web/ui -- components/TournamentEditor.test.tsx`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement it**

`web/ui/lib/types.ts`: add

```ts
export interface TournamentSettings {
  enabled: boolean;
  name_prefix: string;
  rules: UpgradeRule[];
  cash_reserve: number;
  cash_spend_limit_pct: number;
}
```

Add `tournament?: TournamentSettings;` to `Strategy`. In `web/ui/lib/buildRoute.ts`, add `tournament?: TournamentSettings;` inside `BuildRouteDocument["baseline"]` and import the type.

`web/ui/components/TournamentEditor.tsx`:

```tsx
"use client";
import type { TournamentSettings, Upgrade, UpgradeRule } from "@/lib/types";

const DEFAULT_IDS = ["damage", "attack_speed", "health", "defense_absolute", "critical_chance", "critical_factor",
  "defense_percent", "health_regen", "thorns", "lifesteal", "range", "multishot_chance"];

export const DEFAULT_TOURNAMENT: TournamentSettings = {
  enabled: true, name_prefix: "Tower", cash_reserve: 0, cash_spend_limit_pct: 100,
  rules: DEFAULT_IDS.map(upgrade_id => ({ upgrade_id, enabled: true, target: null })),
};

export function TournamentEditor({ value, onChange, catalog, disabled = false }: {
  value: TournamentSettings;
  onChange: (next: TournamentSettings) => void;
  catalog: Upgrade[];
  disabled?: boolean;
}): React.JSX.Element {
  const names = new Map(catalog.map(upgrade => [upgrade.id, upgrade.name]));
  const listed = new Set(value.rules.map(rule => rule.upgrade_id));
  const addable = catalog.filter(upgrade => !upgrade.unlock && upgrade.category !== "UTILITY" && !listed.has(upgrade.id));
  const setRules = (rules: UpgradeRule[]) => onChange({ ...value, rules });
  const move = (index: number, by: number) => {
    const rules = [...value.rules];
    const [rule] = rules.splice(index, 1);
    rules.splice(index + by, 0, rule);
    setRules(rules);
  };
  return (
    <section aria-label="Tournament" className="space-y-3 rounded-xl border p-4">
      <header className="flex items-center justify-between">
        <h3 className="font-semibold">Tournament</h3>
        <label className="inline-flex items-center gap-2 text-sm">
          <input type="checkbox" aria-label="Enter tournaments" checked={value.enabled} disabled={disabled}
            onChange={event => onChange({ ...value, enabled: event.target.checked })} />Enter free tournaments
        </label>
      </header>
      <div className="flex flex-wrap gap-4 text-sm">
        <label className="inline-flex items-center gap-2">Name prefix
          <input aria-label="Tournament name prefix" className="w-28 rounded border px-2 py-1" maxLength={10}
            value={value.name_prefix} disabled={disabled}
            onChange={event => onChange({ ...value, name_prefix: event.target.value })} />
        </label>
        <label className="inline-flex items-center gap-2">Spend limit %
          <input aria-label="Tournament spend limit" type="number" min={1} max={100} className="w-20 rounded border px-2 py-1"
            value={value.cash_spend_limit_pct} disabled={disabled}
            onChange={event => onChange({ ...value, cash_spend_limit_pct: Number(event.target.value) })} />
        </label>
      </div>
      <ol className="space-y-1 text-sm">
        {value.rules.map((rule, index) => {
          const name = names.get(rule.upgrade_id) ?? rule.upgrade_id;
          return (
            <li key={rule.upgrade_id} className="flex items-center gap-2">
              <span className="w-6 text-right font-mono text-muted-foreground">{index + 1}</span>
              <span className="flex-1">{name}</span>
              <button type="button" aria-label={`Move ${name} up`} disabled={disabled || index === 0} onClick={() => move(index, -1)}>↑</button>
              <button type="button" aria-label={`Move ${name} down`} disabled={disabled || index === value.rules.length - 1} onClick={() => move(index, 1)}>↓</button>
              <button type="button" aria-label={`Remove ${name}`} disabled={disabled}
                onClick={() => setRules(value.rules.filter(other => other.upgrade_id !== rule.upgrade_id))}>✕</button>
            </li>
          );
        })}
      </ol>
      <select aria-label="Add tournament upgrade" value="" disabled={disabled || !addable.length}
        className="rounded border px-2 py-1 text-sm"
        onChange={event => event.target.value && setRules([...value.rules, { upgrade_id: event.target.value, enabled: true, target: null }])}>
        <option value="">Add upgrade…</option>
        {addable.map(upgrade => <option key={upgrade.id} value={upgrade.id}>{upgrade.name}</option>)}
      </select>
    </section>
  );
}
```

Mounting:
- `app/strategy/page.tsx`, below `<AutopilotEditor ... />`:
  ```tsx
  <TournamentEditor value={draft.tournament ?? DEFAULT_TOURNAMENT} catalog={catalog}
    onChange={tournament => setDraft({ ...draft, tournament })} disabled={saving} />
  ```
  Use the page's actual state names for the draft, setter, catalog and saving flag. Read how `AutopilotEditor` is fed and mirror it.
- `StrategyStudio.tsx`: in the baseline editor area, next to where the battle lane is edited:
  ```tsx
  <TournamentEditor value={strategy.baseline.tournament ?? DEFAULT_TOURNAMENT} catalog={catalog}
    onChange={tournament => <update the working strategy's baseline>({ ...strategy.baseline, tournament })} />
  ```
  Use the component's existing baseline-update function, the one the battle or workshop lanes call to change `strategy.baseline`.

- [ ] **Step 4: Run the tests and the type check, and watch them pass**

Run: `npm test --prefix web/ui -- components/TournamentEditor.test.tsx app/fleet/reroll/strategies/StrategyStudio.test.tsx && npm run build --prefix web/ui`
Expected: PASS, and the build succeeds.

- [ ] **Step 5: Commit** (after approval)

```bash
git add web/ui/components/TournamentEditor.tsx web/ui/components/TournamentEditor.test.tsx web/ui/lib/types.ts web/ui/lib/buildRoute.ts web/ui/app/strategy/page.tsx web/ui/app/fleet/reroll/strategies/StrategyStudio.tsx
git commit -m "Edit tournament priorities in the strategy pages"
```

---

### Task 11: Live acceptance on one fleet worker

This task is manual, with no code. Run it at the next tournament where a fleet account holds a ticket (the menu trophy shows `OPEN` and the page counter reads ≥ 1).

- [ ] **Step 1:** Rebuild and restart the coordinator from the branch, following the repo's normal procedure (`run.sh` rebuilds a stale UI bundle).
- [ ] **Step 2:** In Strategy Studio, confirm the assigned strategy shows the Tournament panel with the default list. Save it unchanged.
- [ ] **Step 3:** Watch one worker reach the main menu between runs. Pass criteria:
  - The visit opens the tournament page.
  - It sets the name if asked.
  - It taps BATTLE exactly once.
  - The HUD shows `Tier 1+`.
- [ ] **Step 4:** During the run, confirm through the worker's run purchases (`/api/runs/<id>/purchases`) that every purchase is an ATTACK or DEFENSE upgrade.
- [ ] **Step 5:** At death, confirm:
  - The bot taps OK on TOURNAMENT STATS, reads the leaderboard, and returns to the main menu.
  - The next run is a normal farming run (the HUD shows `Tier 1`).
- [ ] **Step 6:** In the UI, confirm:
  - The run row is yellow with the league.
  - The Tournaments chip filters to it.
  - The detail panel shows the rank and tournament id.
  - The ledger shows `TOURNAMENT_ENTRY` (−1 tickets) and a yellow `RUN_PAYOUT`.
  - The account's best tier-1 wave and records did not change.
- [ ] **Step 7:** Record the evidence (run id, screenshots, ledger rows) in the PR description.
