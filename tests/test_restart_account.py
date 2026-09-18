"""A restart proves the live account before the worker resumes bot taps."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from fleet.account_creation import AccountFrame
from fleet.restart_account import verify_restart_account
from supervisor import RecoveryBlocked, RecoveryState


def frame(screen: str, *, account_id: str | None = None,
          controls: dict[str, tuple[int, int]] | None = None) -> AccountFrame:
    return AccountFrame(screen, account_id, "29.0", f"digest-{screen}", 100.,
                        f"capture://{screen}", controls or {}, None,
                        "ACCOUNT" if screen == "account" else None,
                        "ID:" if screen == "account" else None)


class Device:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def click(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class Supervisor:
    def __init__(self, expected: str) -> None:
        self.expected = expected
        self.verified: str | None = None

    def verify_account(self, account_id: str, *, observed_at: float) -> None:
        assert observed_at == 100.
        if account_id != self.expected:
            raise RecoveryBlocked("wrong account")
        self.verified = account_id

    def observe(self, *, frame_digest: str, observed_at: float,
                screen: str, account_id: str) -> RecoveryState:
        assert (frame_digest, observed_at, screen, account_id) == (
            "digest-account", 100., "account", self.expected)
        return RecoveryState.READY


def observer(rows: list[AccountFrame]) -> Any:
    frames: Iterator[AccountFrame] = iter(rows)
    return lambda _device: next(frames)


def test_verified_restart_walk_returns_to_home() -> None:
    device = Device()
    supervisor = Supervisor("ACCOUNT-A")
    verify_restart_account(device=device, supervisor=supervisor,
        expected_account="ACCOUNT-A", clock=lambda: 101., sleep=lambda _: None,
        observe=observer([
            frame("home", controls={"settings": (1, 2)}),
            frame("home", controls={"settings": (1, 2)}),
            frame("settings", controls={"account": (3, 4)}),
            frame("settings", controls={"account": (3, 4)}),
            frame("account", account_id="ACCOUNT-A", controls={"close": (940, 585)}),
            frame("settings", controls={"close": (910, 490)}), frame("home"),
        ]))
    assert supervisor.verified == "ACCOUNT-A"
    assert device.taps == [(1, 2), (3, 4), (940, 585), (910, 490)]


def test_wrong_account_stops_before_closing_popup() -> None:
    device = Device()
    with pytest.raises(RecoveryBlocked, match="wrong account"):
        verify_restart_account(device=device, supervisor=Supervisor("ACCOUNT-A"),
            expected_account="ACCOUNT-A", clock=lambda: 101., sleep=lambda _: None,
            observe=observer([frame("account", account_id="ACCOUNT-B")]))
    assert device.taps == []


def test_missing_close_target_never_guesses_a_fixed_coordinate() -> None:
    device = Device()
    with pytest.raises(RecoveryBlocked, match="account close control unavailable"):
        verify_restart_account(device=device, supervisor=Supervisor("ACCOUNT-A"),
            expected_account="ACCOUNT-A", clock=lambda: 101., sleep=lambda _: None,
            observe=observer([frame("account", account_id="ACCOUNT-A")]))
    assert device.taps == []


def test_restart_from_workshop_tutorial_claim() -> None:
    device = Device()
    supervisor = Supervisor("ACCOUNT-A")
    verify_restart_account(device=device, supervisor=supervisor,
        expected_account="ACCOUNT-A", clock=lambda: 101., sleep=lambda _: None,
        observe=observer([
            frame("workshop_tutorial_claim", controls={"claim": (5, 6)}),
            frame("workshop", controls={"battle_tab": (7, 8)}),
            frame("home", controls={"settings": (1, 2)}),
            frame("settings", controls={"account": (3, 4)}),
            frame("account", account_id="ACCOUNT-A", controls={"close": (940, 585)}),
            frame("settings", controls={"close": (910, 490)}), frame("home"),
        ]))
    assert device.taps == [(5, 6), (7, 8), (1, 2), (3, 4),
                           (940, 585), (910, 490)]


def test_restart_from_finished_run_uses_observed_home() -> None:
    device = Device()
    supervisor = Supervisor("ACCOUNT-A")
    verify_restart_account(device=device, supervisor=supervisor,
        expected_account="ACCOUNT-A", clock=lambda: 101., sleep=lambda _: None,
        observe=observer([
            frame("game_over", controls={"home_from_game_over": (780, 1659)}),
            frame("home", controls={"settings": (1, 2)}),
            frame("settings", controls={"account": (3, 4)}),
            frame("account", account_id="ACCOUNT-A", controls={"close": (940, 585)}),
            frame("settings", controls={"close": (910, 490)}), frame("home"),
        ]))
    assert supervisor.verified == "ACCOUNT-A"
    assert device.taps[0] == (780, 1659)


def test_restart_waits_for_battle_without_tapping_it() -> None:
    device = Device()
    supervisor = Supervisor("ACCOUNT-A")
    verify_restart_account(device=device, supervisor=supervisor,
        expected_account="ACCOUNT-A", clock=lambda: 101., sleep=lambda _: None,
        observe=observer([
            frame("battle"), frame("battle"),
            frame("game_over", controls={"home_from_game_over": (780, 1659)}),
            frame("home", controls={"settings": (1, 2)}),
            frame("settings", controls={"account": (3, 4)}),
            frame("account", account_id="ACCOUNT-A", controls={"close": (940, 585)}),
            frame("settings", controls={"close": (910, 490)}), frame("home"),
        ]))
    assert supervisor.verified == "ACCOUNT-A"
    assert device.taps == [(780, 1659), (1, 2), (3, 4), (940, 585), (910, 490)]


def test_ambiguous_home_control_never_taps() -> None:
    device = Device()
    with pytest.raises(RecoveryBlocked, match="navigation evidence"):
        verify_restart_account(device=device, supervisor=Supervisor("ACCOUNT-A"),
            expected_account="ACCOUNT-A", clock=lambda: 101., sleep=lambda _: None,
            observe=observer([frame("home", controls={"settings": (1, 2),
                                                   "battle": (3, 4)})]))
    assert device.taps == []
