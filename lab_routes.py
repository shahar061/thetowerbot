"""Route authorization is independent of Labs observation and planning support.

General research no longer depends on an offline-validated recorded sequence:
the starter rollout (lab_starter_rollout.py) gates each slot through a live
dry run, canary and fleet promotion, plus one clean rehearsal per lab. Only
slot 1's Game Speed keeps the legacy fixture-regression gate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RouteGate:
    enabled: bool
    evidence: str
    reason: str


LEGACY_GAME_SPEED = RouteGate(True, 'legacy_fixture_regression',
    'Existing slot-one Game Speed executor; no continuous live canary is claimed')


def route_gates() -> dict[str, dict[str, object]]:
    return {
        'game_speed_slot_1': asdict(LEGACY_GAME_SPEED),
        'general_research': asdict(RouteGate(True, 'starter_rollout',
            'Per-slot dry run → canary → fleet, plus one clean rehearsal per lab '
            '(lab-starter-rollout.json)')),
        'native_repeat': asdict(RouteGate(False, 'missing_recorded_sequence',
            'Repeat controls and persisted on/off state are uncalibrated')),
        **{f'in_battle_{page}': asdict(RouteGate(False, 'missing_recorded_sequence',
            'Pending work waits for MAIN_MENU/GAME_OVER; no calibrated return to the same run'))
           for page in ('labs', 'missions')},
    }
