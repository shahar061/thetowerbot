"""In-process generation handoff must be coordinated or explicitly quarantined."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fleet.identity import Attempt
from fleet.input_lease import InputLease, InputLeaseExpired
from fleet.worker_generation import rotate_registered_attempt
from tests.test_reroll_supervisor import _harness


NAME = "Tiramisu64_20"


def test_interrupted_generation_transition_stays_fenced_and_quarantined(tmp_path: Path) -> None:
    make, spawned, _, _ = _harness(tmp_path)
    coordinator = make()
    coordinator.start(NAME)
    root = tmp_path / "workers" / NAME
    registration = json.loads((root / "fleet-registration.json").read_text())
    binding = json.loads(Path(registration["binding"]).read_text())
    previous = Attempt(**{key: binding[key] for key in (
        "worker_id", "endpoint", "lease_id", "attempt_id", "generation", "created_at")})
    process = coordinator._read(NAME)
    process["pid"] = os.getpid()
    coordinator._save(NAME, process)
    next_attempt = Attempt.new(NAME, previous.endpoint, previous.lease_id, previous.attempt_id)

    def crash_before_handoff(attempt: Attempt, path: Path) -> None:
        assert coordinator._read(NAME)["input_generation"] == attempt.generation
        raise SystemExit("simulated process crash")

    with pytest.raises(SystemExit, match="simulated process crash"):
        rotate_registered_attempt(root, previous, next_attempt, crash_before_handoff)
    lease = InputLease(root / "input-lease.json")
    assert lease._read()["generation"] == previous.generation
    for generation in (previous.generation, next_attempt.generation):
        with pytest.raises(InputLeaseExpired):
            lease.assert_current(generation)
    transition = json.loads((root / "generation-transition.json").read_text())
    assert transition["from_generation"] == previous.generation
    assert transition["to_generation"] == next_attempt.generation
    status = coordinator.start(NAME)
    assert (status["state"], status["error"]) == (
        "identity_changed", "generation_transition_incomplete")
    assert len(spawned) == 1
    with pytest.raises(InputLeaseExpired, match="generation_transition_incomplete"):
        rotate_registered_attempt(root, previous, next_attempt, crash_before_handoff)
