"""Validated nonsecret recovery settings with atomic O4 budget policy updates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from recovery_budget import (BudgetLimits, BudgetPolicyStatus, BudgetSummary,
                             BudgetSettingsConflictError, RecoveryBudget)
from recovery_capabilities import CapabilityRegistry
from recovery_policy import RecoverySettings


@dataclass(frozen=True)
class RecoverySettingsState:
    settings: RecoverySettings
    shadow_worker: str | None
    settings_revision: int
    policy: BudgetPolicyStatus
    daily_budget: BudgetSummary
    budget_observed_at_utc: str
    assist_allowed_actions: tuple[str, ...] = ()


class RecoverySettingsConflict(RuntimeError):
    def __init__(self, state: RecoverySettingsState) -> None:
        super().__init__("recovery_settings_revision_changed")
        self.state = state


class RecoverySettingsStore:
    """Read/write a revisioned fleet snapshot; no credentials or device I/O."""

    def __init__(self, fleet_root: Path) -> None:
        self.root = Path(fleet_root)

    def _budget(self) -> RecoveryBudget:
        return RecoveryBudget(self.root)

    def read(self) -> RecoverySettingsState:
        budget = self._budget()
        revision, raw, active = budget.settings_snapshot()
        if raw is None:
            settings, shadow_worker = RecoverySettings(), None
        else:
            document = json.loads(raw)
            settings = RecoverySettings.model_validate(document["settings"])
            shadow_worker = document.get("shadow_worker")
            if shadow_worker is not None and (type(shadow_worker) is not str or not shadow_worker
                                              or len(shadow_worker) > 160):
                raise ValueError("invalid shadow worker")
        requested = BudgetLimits(settings.incident_limit_microusd,
                                 settings.daily_limit_microusd, settings.max_calls)
        actual = BudgetLimits(active.incident_limit_microusd,
                              active.daily_limit_microusd, active.max_calls)
        now = datetime.now(timezone.utc)
        daily = budget.summary(day=now.date().isoformat())
        return RecoverySettingsState(settings, shadow_worker, revision,
                                     BudgetPolicyStatus(active, requested, actual != requested),
                                     daily, now.isoformat(), self._assist_actions(settings.model))

    def _assist_actions(self, model: str) -> tuple[str, ...]:
        # Only validated, operator-activated replay + recorded-canary evidence.
        return CapabilityRegistry(self.root).allowed_actions(model=model, now=time.time())

    def effective_settings(self, worker: str) -> RecoverySettings:
        """Only the selected stable worker may make shadow provider calls."""
        state = self.read()
        if state.settings.mode == "shadow" and state.shadow_worker != worker:
            return state.settings.model_copy(update={"mode": "off"})
        return state.settings

    def save(self, settings: RecoverySettings, *, shadow_worker: str | None = None,
             expected_settings_revision: int,
             expected_policy_revision: int) -> RecoverySettingsState:
        if settings.mode == "assist" and not self._assist_actions(settings.model):
            # Assist needs active calibration evidence for this model (see
            # recovery_capabilities). Mock replay alone never grants it; each
            # worker is further gated on its own worker/account/action grant.
            raise ValueError("assist_uncalibrated")
        if settings.mode == "shadow" and (type(shadow_worker) is not str or not shadow_worker
                                          or shadow_worker != shadow_worker.strip()
                                          or len(shadow_worker) > 160):
            raise ValueError("shadow_worker_required")
        if settings.mode == "off":
            shadow_worker = None
        budget = self._budget()
        try:
            budget.update_settings(
                expected_settings_revision=expected_settings_revision,
                expected_policy_revision=expected_policy_revision,
                settings_json=json.dumps({"settings": settings.model_dump(mode="json"),
                                          "shadow_worker": shadow_worker}),
                incident_limit_microusd=settings.incident_limit_microusd,
                daily_limit_microusd=settings.daily_limit_microusd,
                max_calls=settings.max_calls,
            )
        except BudgetSettingsConflictError as exc:
            raise RecoverySettingsConflict(self.read()) from exc
        return self.read()
