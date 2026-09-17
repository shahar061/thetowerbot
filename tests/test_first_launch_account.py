"""Source template package checks must never launch Tower."""

from fleet.first_launch_account import tower_is_unopened


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
