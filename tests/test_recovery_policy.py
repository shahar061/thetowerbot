"""Pure policy regressions: no provider, credentials, clock reads or device input."""

from __future__ import annotations

import importlib
from dataclasses import replace
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from fleet.identity import Attempt


def _policy() -> ModuleType:
    return importlib.import_module("recovery_policy")


def _scope(**changes: Any) -> Any:
    return _policy().RecoveryScope(**{
        "worker": "Air_1", "account_id": "account-a", "lease_id": "lease-a",
        "attempt_id": "attempt-a", "attempt_generation": "a" * 32,
        "boot_id": "boot-a", "worker_generation": 2,
        "strategy_revision": 7, "device_command_generation": 9,
        "pending_transaction_generation": 3, **changes,
    })


def _candidate(**changes: Any) -> Any:
    policy = _policy()
    return policy.RecoveryCandidate(**{
        "candidate_id": "overlay.close", "action_id": "close_overlay",
        "screen": "MAIN_MENU", "control_generation": 4,
        "target": "daily reward overlay", "expected_postcondition": "overlay_absent",
        "control": policy.VerifiedControl(x=850, y=90, width=80, height=80,
                                          frame_width=1080, frame_height=2400),
        **changes,
    })


def _context(**changes: Any) -> Any:
    return _policy().RecoveryContext(**{
        "scope": _scope(), "screen": "MAIN_MENU", "screen_generation": 5,
        "observation_generation": 10, "observed_at_monotonic": 100.0,
        "candidates": (_candidate(),), **changes,
    })


def _request(**changes: Any) -> Any:
    return _policy().RecoveryRequest(**{
        "request_id": "request-a", "incident_id": "incident-a",
        "fingerprint": "menu-overlay-stall", "context": _context(),
        "created_at_monotonic": 100.0, "deadline_at_monotonic": 115.0,
        "objective": "Return to the main menu", **changes,
    })


def _proposal(**changes: Any) -> Any:
    return _policy().RecoveryProposal(**{
        "action_id": "close_overlay", "candidate_id": "overlay.close",
        "expected_postcondition": "overlay_absent",
        "explanation": "Close the verified reward overlay.", **changes,
    })


def test_defaults_are_off_and_serialization_has_no_credential_field() -> None:
    settings = _policy().RecoverySettings()
    assert settings.mode == "off"
    assert settings.model == "openai/gpt-5.4-nano"
    assert (settings.deadline_seconds, settings.max_calls, settings.max_actions) == (15, 2, 3)
    assert (settings.incident_limit_microusd, settings.daily_limit_microusd) == (50_000, 1_000_000)
    assert settings.cooldown_seconds == 600
    assert "key" not in settings.model_dump_json().lower()
    assert "key" not in str(settings.model_json_schema()).lower()


@pytest.mark.parametrize("change", [
    {"mode": "automatic"}, {"model": ""}, {"model": "  "},
    {"model": "https://elsewhere.example/model"},
    {"deadline_seconds": float("nan")}, {"deadline_seconds": float("inf")},
    {"deadline_seconds": float("-inf")}, {"deadline_seconds": True},
    {"deadline_seconds": 0}, {"deadline_seconds": 16}, {"deadline_seconds": "5"},
    {"max_calls": True}, {"max_calls": 3}, {"max_actions": True}, {"max_actions": 4},
    {"incident_limit_microusd": True}, {"incident_limit_microusd": 50_001},
    {"incident_limit_microusd": -1}, {"daily_limit_microusd": True},
    {"daily_limit_microusd": 1_000_001}, {"cooldown_seconds": 599},
    {"api_key": "synthetic-test-value"},
])
def test_settings_reject_invalid_or_relaxed_limits(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _policy().RecoverySettings(**change)


@pytest.mark.parametrize("change", [
    {"x": 200}, {"y": 400}, {"code": "run something"},
    {"action_id": "buy"}, {"action_id": "shell"}, {"action_id": "change_account"},
    {"candidate_id": None}, {"candidate_id": ""}, {"candidate_id": " "},
    {"expected_postcondition": "paused"}, {"explanation": "x" * 241},
    {"explanation": ""}, {"explanation": "   "},
])
def test_model_proposal_cannot_supply_geometry_code_or_invalid_semantics(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _proposal(**change)


@pytest.mark.parametrize(("action", "postcondition"), [
    ("observe", "fresh_observation"), ("pause", "paused"),
    ("close_overlay", "overlay_absent"), ("select_category", "category_visible"),
    ("open_screen", "screen_visible"),
])
def test_all_allowed_proposals_have_matching_postconditions(action: str, postcondition: str) -> None:
    candidate_id = None if action in {"observe", "pause"} else "control-a"
    proposal = _proposal(action_id=action, candidate_id=candidate_id, expected_postcondition=postcondition)
    assert proposal.action_id == action
    assert _policy().RecoveryProposal.model_validate_json(proposal.model_dump_json()) == proposal


@pytest.mark.parametrize("action", ["observe", "pause"])
def test_non_device_actions_cannot_smuggle_a_control_target(action: str) -> None:
    with pytest.raises(ValidationError):
        _proposal(action_id=action, expected_postcondition="paused" if action == "pause" else "fresh_observation")


def test_candidate_membership_and_action_are_validated_locally() -> None:
    policy = _policy()
    context = _context()
    assert policy.validate_proposal(_proposal(), context) == context.candidates[0]
    with pytest.raises(ValueError, match="candidate"):
        policy.validate_proposal(_proposal(candidate_id="unoffered-control"), context)
    with pytest.raises(ValueError, match="candidate"):
        policy.validate_proposal(_proposal(action_id="open_screen", expected_postcondition="screen_visible"), context)
    assert policy.validate_proposal(_proposal(action_id="observe", candidate_id=None,
        expected_postcondition="fresh_observation"), context) is None


@pytest.mark.parametrize("flag", ["paused", "identity_conflict", "pending_transaction"])
def test_safety_gates_block_all_proposals(flag: str) -> None:
    context = _context(**{flag: True})
    with pytest.raises(ValueError):
        _policy().validate_proposal(_proposal(), context)
    assert not _policy().proposal_is_current(_request(), context)


@pytest.mark.parametrize("change", [
    {"x": -1}, {"y": -1}, {"x": 1050}, {"y": 2390},
    {"width": 0}, {"height": 0}, {"frame_width": True},
])
def test_host_control_geometry_must_fit_the_verified_frame(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _policy().VerifiedControl(**{"x": 850, "y": 90, "width": 80,
            "height": 80, "frame_width": 1080, "frame_height": 2400, **change})


def test_candidate_context_and_scope_are_frozen_and_unique() -> None:
    candidate = _candidate()
    with pytest.raises(ValidationError):
        candidate.candidate_id = "changed"
    with pytest.raises(ValidationError):
        candidate.control.x = 2
    with pytest.raises(ValidationError):
        _scope().account_id = "changed"
    with pytest.raises(ValidationError):
        _context(candidates=(candidate, candidate))
    with pytest.raises(ValidationError):
        _context(candidates=(_candidate(screen="LABS"),))
    with pytest.raises(ValidationError):
        _candidate(expected_postcondition="screen_visible")


def test_fresh_animation_frames_preserve_semantically_verified_controls() -> None:
    request = _request()
    current = _context(observation_generation=42, observed_at_monotonic=101.5)
    assert _policy().proposal_is_current(request, current)
    assert not _policy().proposal_is_current(request, request.context)


@pytest.mark.parametrize("change", [
    {"worker": "Air_2"}, {"account_id": "account-b"}, {"lease_id": "lease-b"},
    {"attempt_id": "attempt-b"}, {"attempt_generation": "b" * 32},
    {"boot_id": "boot-b"}, {"worker_generation": 3},
    {"strategy_revision": 8}, {"device_command_generation": 10},
    {"pending_transaction_generation": 4},
])
def test_changed_scope_rejects_a_late_response(change: dict[str, Any]) -> None:
    current = _context(scope=_scope(**change), observation_generation=11, observed_at_monotonic=101.0)
    assert not _policy().proposal_is_current(_request(), current)


def test_foundation_attempt_generation_rejects_reply_with_other_scope_fields_unchanged() -> None:
    attempt = Attempt(worker_id="Air_1", endpoint="127.0.0.1:5555", lease_id="lease-a",
                      attempt_id="attempt-a", generation="a" * 32, created_at=100.0)
    restarted = replace(attempt, generation="b" * 32)
    previous = _scope(attempt_generation=attempt.generation)
    current = _scope(attempt_generation=restarted.generation)
    assert previous.attempt_generation == attempt.generation
    assert current.attempt_generation == restarted.generation
    assert previous.model_dump(exclude={"attempt_generation"}) == current.model_dump(exclude={"attempt_generation"})
    request = _request(context=_context(scope=previous))
    assert not _policy().proposal_is_current(request, _context(scope=current,
        observation_generation=11, observed_at_monotonic=101.0))


@pytest.mark.parametrize("change", [
    {"candidates": ()}, {"screen_generation": 6}, {"observation_generation": 9},
    {"observed_at_monotonic": 99.0}, {"observed_at_monotonic": 115.0},
])
def test_disappearance_transition_old_frame_and_deadline_reject_proposals(change: dict[str, Any]) -> None:
    current = _context(**{"observation_generation": 11, "observed_at_monotonic": 101.0, **change})
    assert not _policy().proposal_is_current(_request(), current)


def test_changed_candidate_geometry_or_generation_rejects_proposals() -> None:
    moved = _candidate(control=_policy().VerifiedControl(x=840, y=90, width=80,
        height=80, frame_width=1080, frame_height=2400))
    for candidate in (moved, _candidate(control_generation=5)):
        current = _context(candidates=(candidate,), observation_generation=11, observed_at_monotonic=101.0)
        assert not _policy().proposal_is_current(_request(), current)
    current = _context(screen="LABS", candidates=(), observation_generation=11, observed_at_monotonic=101.0)
    assert not _policy().proposal_is_current(_request(), current)


@pytest.mark.parametrize("change", [
    {"deadline_at_monotonic": 116.0}, {"deadline_at_monotonic": 100.0},
    {"created_at_monotonic": float("nan")}, {"objective": "x" * 241},
    {"created_at_monotonic": 99.0}, {"recent_actions": tuple({} for _ in range(13))},
])
def test_request_context_is_bounded(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _request(**change)


def test_image_payload_is_bounded_and_omitted_from_representation() -> None:
    policy = _policy()
    image = policy.RecoveryImage(media_type="image/png", data=b"synthetic-image", width=100, height=100)
    request = _request(image=image)
    assert "synthetic-image" not in repr(request)
    with pytest.raises(ValidationError):
        policy.RecoveryImage(media_type="image/svg+xml", data=b"data", width=100, height=100)
    with pytest.raises(ValidationError):
        policy.RecoveryImage(media_type="image/png", data=b"x" * 4_000_001, width=100, height=100)


def test_success_and_failure_replies_preserve_unknown_usage_without_raw_responses() -> None:
    policy = _policy()
    reply = policy.RecoveryReply(request_id="request-a", proposal=_proposal(), actual_model="openai/gpt-5.4-nano")
    assert reply.usage is None
    assert reply.cost_microusd is None
    timeout = policy.RecoveryReply(request_id="request-a", error="timeout")
    assert timeout.proposal is None
    with pytest.raises(ValidationError):
        policy.RecoveryReply(request_id="request-a", error="timeout", raw_response="unsafe")
    with pytest.raises(ValidationError):
        policy.RecoveryReply(request_id="request-a", error="Authorization: synthetic-value")
    with pytest.raises(ValidationError):
        policy.RecoveryReply(request_id="request-a", error="timeout", proposal=_proposal())
    with pytest.raises(ValidationError):
        policy.RecoveryReply(request_id="request-a")


@pytest.mark.parametrize("change", [{"prompt_tokens": True}, {"completion_tokens": -1},
                                     {"reasoning_tokens": "10"}])
def test_usage_has_strict_nonnegative_counts(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _policy().RecoveryUsage(**change)
