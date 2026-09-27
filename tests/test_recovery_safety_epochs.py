from __future__ import annotations
from pathlib import Path
import pytest


def test_control_revision_catches_change_and_change_back() -> None:
    from control import Controls
    from tests.test_runner import a_strategy
    controls = Controls(a_strategy())
    initial = controls.recovery_revision
    controls.apply({'paused': True})
    controls.apply({'paused': False})
    assert controls.recovery_revision == initial + 2
    controls.request('speed_up')
    assert controls.recovery_revision == initial + 3


def test_purchase_cache_starts_unknown_and_invalidates_on_attempt(tmp_path: Path) -> None:
    from transactions import TransactionJournal
    journal = TransactionJournal(tmp_path / 'account.db')
    assert journal.recovery_snapshot()[1] is True
    journal.refresh_recovery_snapshot()
    epoch, pending = journal.recovery_snapshot()
    assert pending is False
    with pytest.raises(Exception):
        journal.record_action('absent')
    assert journal.recovery_snapshot()[0] > epoch
    assert journal.recovery_snapshot()[1] is True
    journal.refresh_recovery_snapshot()
    assert journal.recovery_snapshot()[1] is False


def test_final_guard_runs_after_checkpoint_and_command_epoch_tracks_input(tmp_path: Path) -> None:
    from tests.test_supervisor import Clock, Device, supervisor, observed
    from supervisor import RecoveryPreflightBlocked
    clock, device = Clock(), Device()
    sut = supervisor(tmp_path / 'supervisor.json', clock, [device])
    sut.recover()
    observed(sut, clock, 'before')
    initial = sut.command_generation
    with pytest.raises(RecoveryPreflightBlocked):
        sut.recovery_tap(1, 2, guard=lambda: False)
    assert device.taps == []
    assert sut.command_generation == initial
    clock.now += 1
    observed(sut, clock, 'fresh')
    sut.recovery_tap(1, 2, guard=lambda: True)
    assert device.taps == [(1, 2)]
    assert sut.command_generation == initial + 1
    sut.disconnected()
    assert sut.command_generation == initial + 2
