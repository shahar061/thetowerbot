"""Pure, bounded contracts for optional model-assisted recovery.

Only RecoveryProposal is model-authored. Candidates and their geometry are
created by verified local readers; a proposal selects their IDs, never pixels.
These types perform no I/O and contain no credentials or provider imports.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


RecoveryAction = Literal["observe", "close_overlay", "select_category", "open_screen", "pause"]
DeviceRecoveryAction = Literal["close_overlay", "select_category", "open_screen"]
RecoveryPostcondition = Literal[
    "fresh_observation", "overlay_absent", "category_visible", "screen_visible", "paused",
]
RecoveryError = Literal[
    "disabled", "credentials_missing", "unsupported_model", "timeout", "transport",
    "http", "authentication", "rate_limited", "invalid_response", "invalid_proposal",
    "model_mismatch", "cancelled", "budget_exhausted", "stale_request",
]

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
ScreenName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Generation = Annotated[int, Field(ge=0)]
MonotonicTime = Annotated[float, Field(ge=0)]

_POSTCONDITIONS: dict[str, str] = {
    "observe": "fresh_observation", "close_overlay": "overlay_absent",
    "select_category": "category_visible", "open_screen": "screen_visible", "pause": "paused",
}


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True,
                              allow_inf_nan=False, validate_default=True,
                              hide_input_in_errors=True)


class RecoveryProposal(_FrozenContract):
    """The complete allowlist of fields a model may return."""

    action_id: RecoveryAction
    candidate_id: Identifier | None
    expected_postcondition: RecoveryPostcondition
    explanation: ShortText

    @model_validator(mode="after")
    def consistent_action(self) -> Self:
        if self.expected_postcondition != _POSTCONDITIONS[self.action_id]:
            raise ValueError("postcondition_does_not_match_action")
        device_action = self.action_id not in {"observe", "pause"}
        if device_action != (self.candidate_id is not None):
            raise ValueError("device_actions_require_candidate_other_actions_forbid_it")
        return self


class RecoverySettings(_FrozenContract):
    """Non-secret settings; provider credentials remain outside this model."""

    mode: Literal["off", "shadow", "assist"] = "off"
    model: str = Field(default="openai/gpt-5.4-nano", min_length=3, max_length=160,
                       pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*/[A-Za-z0-9][A-Za-z0-9._:-]*$")
    deadline_seconds: float = Field(default=15.0, gt=0, le=15)
    max_calls: int = Field(default=2, ge=1, le=2)
    max_actions: int = Field(default=3, ge=1, le=3)
    cooldown_seconds: float = Field(default=600.0, ge=600, le=86_400)
    incident_limit_microusd: int = Field(default=50_000, ge=0, le=50_000)
    daily_limit_microusd: int = Field(default=1_000_000, ge=0, le=1_000_000)


class RecoveryScope(_FrozenContract):
    """Adapter to worker identity, not a second source of runtime identity.

    The scan owner copies these values from the foundation's current scope,
    including Attempt.generation directly into attempt_generation. The string
    UUID is independent of worker_generation; never replace it with a counter.
    Reconnects/manual input advance device_command_generation. Starting or
    resolving a purchase advances pending_transaction_generation even when
    there is no longer a pending purchase by the time the reply arrives.
    """

    worker: Identifier
    account_id: Identifier
    lease_id: Identifier
    attempt_id: Identifier
    attempt_generation: Identifier
    boot_id: Identifier
    worker_generation: Generation
    strategy_revision: Generation
    device_command_generation: Generation
    pending_transaction_generation: Generation


class VerifiedControl(_FrozenContract):
    """Host-owned bounding box in the original verified device frame."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_width: int = Field(gt=0)
    frame_height: int = Field(gt=0)

    @model_validator(mode="after")
    def inside_frame(self) -> Self:
        if self.x + self.width > self.frame_width or self.y + self.height > self.frame_height:
            raise ValueError("control_outside_verified_frame")
        return self


class RecoveryCandidate(_FrozenContract):
    """One locally verified, non-spending navigation control.

    target names the semantic destination/overlay for postcondition checks.
    The provider must omit control geometry when presenting candidates.
    control_generation changes when control identity or meaning changes,
    not for each screenshot or unrelated animation.
    """

    candidate_id: Identifier
    action_id: DeviceRecoveryAction
    screen: ScreenName
    control_generation: Generation
    target: ShortText
    expected_postcondition: RecoveryPostcondition
    control: VerifiedControl

    @model_validator(mode="after")
    def consistent_postcondition(self) -> Self:
        if self.expected_postcondition != _POSTCONDITIONS[self.action_id]:
            raise ValueError("candidate_postcondition_does_not_match_action")
        return self


class RecoveryContext(_FrozenContract):
    """A verified local observation; booleans reflect the action arbiter."""

    scope: RecoveryScope
    screen: ScreenName
    screen_generation: Generation
    observation_generation: Generation
    observed_at_monotonic: MonotonicTime
    candidates: tuple[RecoveryCandidate, ...] = Field(default=(), max_length=24)
    paused: bool = False
    identity_conflict: bool = False
    pending_transaction: bool = False

    @model_validator(mode="after")
    def unique_controls_for_screen(self) -> Self:
        identifiers = [candidate.candidate_id for candidate in self.candidates]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate_candidate_id")
        if any(candidate.screen != self.screen for candidate in self.candidates):
            raise ValueError("candidate_screen_does_not_match_context")
        return self


class RecoveryActionOutcome(_FrozenContract):
    """A short redacted local outcome, not a raw log or exception."""

    action: ScreenName
    outcome: Literal["confirmed", "failed", "unknown"]
    detail: ShortText


class RecoveryImage(_FrozenContract):
    """Caller-supplied cropped/redacted raster bytes; never a remote URL."""

    media_type: Literal["image/png", "image/jpeg"]
    data: bytes = Field(min_length=1, max_length=4_000_000, repr=False)
    width: int = Field(gt=0, le=4096)
    height: int = Field(gt=0, le=4096)


class RecoveryRequest(_FrozenContract):
    """A bounded request carrying the frozen context needed at dispatch.

    created/deadline times share the worker boot's monotonic clock. The service
    still checks its actual clock on dispatch; an old observation is not proof
    that the deadline has not passed. Do not serialize the scope to a provider.
    """

    request_id: Identifier
    incident_id: Identifier
    fingerprint: Identifier
    context: RecoveryContext
    created_at_monotonic: MonotonicTime
    deadline_at_monotonic: MonotonicTime
    objective: ShortText
    recent_actions: tuple[RecoveryActionOutcome, ...] = Field(default=(), max_length=12)
    image: RecoveryImage | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def bounded_deadline(self) -> Self:
        duration = self.deadline_at_monotonic - self.created_at_monotonic
        if not 0 < duration <= 15:
            raise ValueError("request_deadline_must_be_within_fifteen_seconds")
        if self.context.observed_at_monotonic > self.created_at_monotonic:
            raise ValueError("request_cannot_precede_its_observation")
        return self


class RecoveryUsage(_FrozenContract):
    """Unknown provider counts stay None; never substitute an invented zero."""

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class RecoveryReply(_FrozenContract):
    """A parsed proposal or classified failure, never unrestricted HTTP data."""

    request_id: Identifier
    proposal: RecoveryProposal | None = None
    actual_model: Identifier | None = None
    usage: RecoveryUsage | None = None
    cost_microusd: int | None = Field(default=None, ge=0)
    error: RecoveryError | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)

    @model_validator(mode="after")
    def success_or_error(self) -> Self:
        if (self.proposal is None) == (self.error is None):
            raise ValueError("reply_requires_exactly_one_proposal_or_error")
        return self


def _blocked(context: RecoveryContext) -> bool:
    return context.paused or context.identity_conflict or context.pending_transaction


def validate_proposal(proposal: RecoveryProposal, context: RecoveryContext) -> RecoveryCandidate | None:
    """Validate semantic membership; return only host-owned control metadata.

    Call once against the offered context, and again against the fresh context
    after proposal_is_current succeeds. This does not authorize execution:
    shadow mode, budgets and the serial device arbiter remain separate gates.
    """
    if _blocked(context):
        raise ValueError("recovery_blocked_by_local_safety_state")
    if proposal.candidate_id is None:
        return None
    candidate = next((item for item in context.candidates
                      if item.candidate_id == proposal.candidate_id), None)
    if (candidate is None or candidate.action_id != proposal.action_id
            or candidate.expected_postcondition != proposal.expected_postcondition):
        raise ValueError("proposal_does_not_match_offered_candidate")
    return candidate


def proposal_is_current(request: RecoveryRequest, current: RecoveryContext) -> bool:
    """Require a newer compatible scan before accepting any delayed proposal.

    Compare semantic epochs and verified controls, not screenshot digests. A
    same-screen animation may advance observation_generation freely. A screen
    transition away and back must advance screen_generation. Conservatively
    require all offered candidates to remain verified; extra controls are fine.
    The caller must supply the scan being used now, not cached telemetry.
    """
    previous = request.context
    if _blocked(previous) or _blocked(current) or current.scope != previous.scope:
        return False
    if current.screen != previous.screen or current.screen_generation != previous.screen_generation:
        return False
    if current.observation_generation <= previous.observation_generation:
        return False
    if not request.created_at_monotonic < current.observed_at_monotonic < request.deadline_at_monotonic:
        return False
    candidates = {candidate.candidate_id: candidate for candidate in current.candidates}
    return all(candidates.get(candidate.candidate_id) == candidate for candidate in previous.candidates)
