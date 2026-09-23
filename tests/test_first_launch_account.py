"""Source template package checks must never launch Tower."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import fleet.first_launch_account as module
from fleet.account_creation import AccountFrame
from fleet.first_launch_account import tower_is_unopened
from fleet.identity import Attempt


class Device:
    def __init__(self, state: str) -> None:
        self.state = state
        self.commands: list[str] = []

    def shell(self, command: str) -> str:
        self.commands.append(command)
        return self.state


def test_unopened_template_uses_only_package_metadata() -> None:
    device = Device("User 0: installed=true hidden=false stopped=true notLaunched=true enabled=0")
    assert tower_is_unopened(device)
    assert device.commands == ["dumpsys package com.TechTreeGames.TheTower"]


def test_launched_template_is_rejected() -> None:
    device = Device("User 0: installed=true hidden=false stopped=true notLaunched=false enabled=0")
    assert not tower_is_unopened(device)


def test_settings_close_button_does_not_block_account_navigation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The observer adds `close` to Settings whenever the X is visible."""
    attempt = Attempt("worker", "127.0.0.1:5845", "lease", "attempt")
    now = attempt.created_at + 1

    def frame(screen: str, controls: dict[str, tuple[int, int]], **extra: Any) -> AccountFrame:
        return AccountFrame(screen, extra.pop("account_id", None), "29.0.3", f"digest-{screen}",
                            now, f"/evidence/{screen}.png", controls, **extra)

    frames = iter([
        frame("home", {"settings": (1, 1)}),
        frame("settings", {"account": (2, 2), "close": (3, 3)}),
        frame("account", {}, account_id="DA95B1992D4FD246", popup_title="ACCOUNT", id_label="ID:"),
    ])

    def onboarding(device: Any, observe: Any, *, before_action: Any, **kwargs: Any) -> None:
        before_action("i_agree", frame("tower_consent", {"i_agree": (0, 0)}))

    monkeypatch.setattr(module, "launch_tower_from_game_center", lambda device: None)
    monkeypatch.setattr(module, "complete_first_launch_onboarding", onboarding)
    device = SimpleNamespace(serial=attempt.endpoint, clicks=[],
                             shell=lambda command: "stopped=true notLaunched=true")
    device.click = lambda x, y: device.clicks.append((x, y))
    runtime = SimpleNamespace(checkpoint_root=tmp_path / "w" / "checkpoints", root=tmp_path / "w",
                              reserve=lambda endpoint: nullcontext())
    adapter = SimpleNamespace(staging_lease=nullcontext, designated=lambda instance, attempt:
                              SimpleNamespace(state="running", source_lineage="lineage"))

    audit = module.create_first_launch_account(
        runtime=runtime, attempt=attempt, adapter=adapter, instance="Tiramisu64_29",
        source_lineage="lineage", protected_ids=frozenset(), connect=lambda: device,
        observe=lambda device: next(frames), registry=tmp_path / "registry.json",
        clock=lambda: now)

    assert audit["state"] == "verified"
    assert audit["account_id"] == "DA95B1992D4FD246"
    assert device.clicks == [(1, 1), (2, 2)]
