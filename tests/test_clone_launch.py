"""Guard the BlueStacks Game Center to Tower transition on staged clones."""

from types import SimpleNamespace

import numpy as np
import pytest

from config import Rect
from ocr import TextBox
from fleet.clone_launch import (complete_first_launch_onboarding,
                                launch_tower_from_game_center, wait_for_tower_ready)


class Device:
    serial = "127.0.0.1:5665"

    def __init__(self, packages: list[str]) -> None:
        self.packages = iter(packages)
        self.actions: list[object] = []

    def app_current(self) -> SimpleNamespace:
        return SimpleNamespace(package=next(self.packages))

    def shell(self, command: str) -> str:
        self.actions.append(command)
        return ""

    def click(self, x: int, y: int) -> None:
        self.actions.append((x, y))


def test_launch_tower_from_game_center_uses_home_then_labeled_icon() -> None:
    device = Device(["com.bluestacks.gamecenter", "com.uncube.launcher3",
                     "com.TechTreeGames.TheTower"])
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    launch_tower_from_game_center(
        device, capture=lambda _: frame,
        read_text=lambda _: (TextBox("The Tower", .95, Rect(801, 479, 157, 38)),),
        sleep=lambda _: None,
    )
    assert device.actions == ["input keyevent KEYCODE_HOME", (879, 369)]


def test_launch_accepts_measured_launcher_ocr_without_space() -> None:
    device = Device(["com.uncube.launcher3", "com.TechTreeGames.TheTower"])
    launch_tower_from_game_center(
        device, capture=lambda _: np.zeros((2400, 1080, 3), dtype=np.uint8),
        read_text=lambda _: (TextBox("TheTower", 1., Rect(800, 477, 158, 41)),),
        sleep=lambda _: None,
    )
    assert device.actions == [(879, 367)]


def test_launch_waits_for_game_center_after_boot_overlay() -> None:
    device = Device(["com.android.systemui", "com.bluestacks.gamecenter",
                     "com.uncube.launcher3", "com.TechTreeGames.TheTower"])
    launch_tower_from_game_center(
        device, capture=lambda _: np.zeros((2400, 1080, 3), dtype=np.uint8),
        read_text=lambda _: (TextBox("TheTower", 1., Rect(800, 477, 158, 41)),),
        sleep=lambda _: None,
    )
    assert device.actions == ["input keyevent KEYCODE_HOME", (879, 367)]


def test_launch_tower_from_game_center_refuses_unlabeled_icon() -> None:
    device = Device(["com.bluestacks.gamecenter"] + ["com.uncube.launcher3"] * 240)
    with pytest.raises(ValueError, match="Tower icon"):
        launch_tower_from_game_center(
            device, capture=lambda _: np.zeros((2400, 1080, 3), dtype=np.uint8),
            read_text=lambda _: (TextBox("Store", .99, Rect(100, 479, 100, 38)),),
            sleep=lambda _: None,
        )
    assert device.actions == ["input keyevent KEYCODE_HOME"]


def test_wait_for_tower_ready_ignores_loading_then_accepts_home() -> None:
    screens = iter(["unknown", "unknown", "home"])
    observed: list[str] = []

    def observe(_: object) -> SimpleNamespace:
        screen = next(screens)
        observed.append(screen)
        return SimpleNamespace(screen=screen, conflict_dialog=None)

    device = Device([])
    wait_for_tower_ready(device, observe, sleep=lambda _: None)
    assert observed == ["unknown", "unknown", "home"]


def test_wait_for_tower_ready_refuses_conflict() -> None:
    with pytest.raises(ValueError, match="session_conflict"):
        wait_for_tower_ready(
            Device([]),
            lambda _: SimpleNamespace(screen="unknown", conflict_dialog="new session detected"),
            sleep=lambda _: None,
        )


def test_first_launch_taps_measured_consent_then_game_stats_home() -> None:
    device = Device([])
    journaled: list[str] = []
    frames = iter([
        SimpleNamespace(screen="google_play_profile", conflict_dialog=None,
                        controls={"dismiss_google_play_profile": (176, 2289)}),
        SimpleNamespace(screen="tower_consent", conflict_dialog=None,
                        controls={"i_agree": (531, 1764)}),
        SimpleNamespace(screen="unknown", conflict_dialog=None, controls={}),
        SimpleNamespace(screen="game_over", conflict_dialog=None,
                        controls={"home_from_game_over": (780, 1708)}),
        SimpleNamespace(screen="home", conflict_dialog=None, controls={"settings": (1020, 230)}),
    ])
    complete_first_launch_onboarding(
        device, lambda _: next(frames), sleep=lambda _: None,
        before_action=lambda action, _: journaled.append(action),
    )
    assert device.actions == [(176, 2289), (531, 1764), (780, 1708)]
    assert journaled == ["dismiss_google_play_profile", "i_agree", "home_from_game_over"]


def test_first_launch_refuses_unmeasured_agree() -> None:
    device = Device([])
    with pytest.raises(ValueError, match="consent control"):
        complete_first_launch_onboarding(
            device,
            lambda _: SimpleNamespace(screen="tower_consent", conflict_dialog=None,
                                      controls={"new_account": (531, 1764)}),
            sleep=lambda _: None,
        )
    assert device.actions == []
