"""Repeat is an observed on/off preference, never an unverified toggle loop."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from lab_plan import LabVisitOptions
from lab_visit import LabVisit
import vision


def visit(mode: str = "enabled", *, authorize: bool = True) -> LabVisit:
    subject = LabVisit(vision.TemplateCache(Path("templates")),
                       authorize=lambda *args: authorize)
    subject.request(LabVisitOptions(start_research=False, native_repeat=mode))
    subject._reading = SimpleNamespace(jobs=[SimpleNamespace(
        slot=1, concept_id="labs.health", target_level=3, status="researching")])
    return subject


def advance(subject: LabVisit, state: str, *, scope: bool = True) -> bool:
    subject._capture_at += 1
    control = SimpleNamespace(slot=1, state=state, point=(74, 555))
    with patch("lab_visit.read_repeat_controls", return_value=() if state == "unknown" else (control,)), \
         patch.object(subject, "_scope", return_value=object() if scope else None):
        return subject._reconcile_repeat(np.zeros((2400, 1080, 3), dtype=np.uint8), object())


def test_repeat_requires_two_reads_one_tap_and_two_matching_readbacks() -> None:
    subject = visit()
    with patch.object(subject, "_tap") as tap:
        assert advance(subject, "disabled")
        tap.assert_not_called()
        assert advance(subject, "disabled")
        tap.assert_called_once()
        assert advance(subject, "enabled")
        assert not advance(subject, "enabled")
        assert not advance(subject, "enabled")
        tap.assert_called_once()


@pytest.mark.parametrize("mode,state", [("unchanged", "disabled"), ("enabled", "enabled"),
                                        ("disabled", "disabled"), ("enabled", "unknown")])
def test_repeat_never_taps_unknown_unchanged_or_matching_state(mode: str, state: str) -> None:
    subject = visit(mode)
    with patch.object(subject, "_tap") as tap:
        for _ in range(3):
            advance(subject, state)
        tap.assert_not_called()


@pytest.mark.parametrize("scope,authorized", [(False, True), (True, False)])
def test_repeat_requires_current_account_and_strategy_authorization(scope: bool, authorized: bool) -> None:
    subject = visit(authorize=authorized)
    with patch.object(subject, "_tap") as tap:
        for _ in range(3):
            advance(subject, "disabled", scope=scope)
        tap.assert_not_called()


def test_repeat_failed_readback_never_retries_the_toggle() -> None:
    subject = visit()
    with patch.object(subject, "_tap") as tap:
        for _ in range(10):
            advance(subject, "disabled")
        tap.assert_called_once()
        assert subject.recovery_status == "lab_repeat_unverified"


def test_repeat_fresh_capture_required_for_confirmation() -> None:
    subject = visit()
    with patch.object(subject, "_tap") as tap:
        assert advance(subject, "disabled")
        subject._capture_at -= 1
        assert advance(subject, "disabled")
        tap.assert_not_called()
