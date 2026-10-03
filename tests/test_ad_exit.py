"""Ad exit controls captured from two different rewarded video end cards."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import pytest

import ad_exit
from supervisor import GuardedDevice
from vision import TemplateCache


ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = TemplateCache(ROOT / "templates")
FOCUSED_AD = (
    "mCurrentFocus=Window{123 u0 com.TechTreeGames.TheTower/"
    "com.google.android.gms.ads.AdActivity}"
)
FOCUSED_GAME = (
    "mCurrentFocus=Window{123 u0 com.TechTreeGames.TheTower/"
    "com.unity3d.player.UnityPlayerActivity}"
)
END_CARD = """<hierarchy>
  <node text="Reward granted" clickable="false" bounds="[731,33][968,99]" />
  <node text="Close" clickable="true" bounds="[981,33][1050,99]" />
  <node text="Watch video" clickable="true" bounds="[214,1584][866,1702]" />
</hierarchy>"""


class Device:
    def __init__(self, focus: str, hierarchy: str) -> None:
        self.focus = focus
        self.hierarchy = hierarchy

    def shell(self, command: str) -> str:
        if command.startswith("dumpsys window"):
            return self.focus
        if command.startswith("uiautomator dump"):
            return self.hierarchy + "\nUI hierarchy dumped"
        raise AssertionError(command)


@pytest.mark.parametrize("emulator", [82, 83])
def test_live_dark_reward_pill_uses_accessible_close_across_ad_artwork(
        emulator: int) -> None:
    screen = cv2.imread(str(ROOT / "tests/fixtures/in_game_ad"
                            / f"ad_reward_granted_dark_{emulator}.jpg"))
    assert ad_exit.find_close(screen, TEMPLATES, Device(FOCUSED_AD, END_CARD)) == (1015, 66)


def test_accessible_close_is_ignored_when_game_is_foreground() -> None:
    screen = cv2.imread(str(ROOT / "tests/fixtures/in_game_ad"
                            / "ad_reward_granted_dark_83.jpg"))
    hierarchy = END_CARD.replace("Reward granted", "Advertisement")
    assert ad_exit.find_close(screen, TEMPLATES, Device(FOCUSED_GAME, hierarchy)) is None


def test_competing_accessible_close_buttons_are_ambiguous() -> None:
    screen = cv2.imread(str(ROOT / "tests/fixtures/in_game_ad"
                            / "ad_reward_granted_dark_82.jpg"))
    hierarchy = END_CARD.replace(
        "</hierarchy>",
        '<node text="Close" clickable="true" bounds="[30,33][100,99]" /></hierarchy>',
    )
    assert ad_exit.find_close(screen, TEMPLATES, Device(FOCUSED_AD, hierarchy)) is None


def test_unlabelled_top_left_ad_close_uses_witnessed_visual_fallback() -> None:
    screen = cv2.imread(str(ROOT / "tests/fixtures/in_game_ad/end_card.jpg"))
    assert ad_exit.find_close(screen, TEMPLATES, Device(FOCUSED_GAME, "")) == (81, 104)


def test_accessible_icon_with_close_label_on_child_is_located() -> None:
    screen = cv2.imread(str(ROOT / "tests/fixtures/in_game_ad"
                            / "ad_reward_granted_dark_83.jpg"))
    hierarchy = ('<hierarchy><node clickable="true" bounds="[981,33][1050,99]">'
                 '<node text="Close" clickable="false" bounds="[990,40][1040,90]" />'
                 '</node></hierarchy>')
    assert ad_exit.find_close(screen, TEMPLATES, Device(FOCUSED_AD, hierarchy)) == (1015, 66)


def test_supervised_worker_can_read_ad_close_without_a_general_shell() -> None:
    screen = cv2.imread(str(ROOT / "tests/fixtures/in_game_ad"
                            / "ad_reward_granted_dark_83.jpg"))
    hardware = Device(FOCUSED_AD, END_CARD)
    guarded = GuardedDevice(SimpleNamespace(device=hardware))
    assert not hasattr(guarded, "shell")
    assert ad_exit.find_close(screen, TEMPLATES, guarded) == (1015, 66)
