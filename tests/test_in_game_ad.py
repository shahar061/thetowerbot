"""The in-battle six-gem ad, using frames from a verified live claim."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import pytest

import battle_menu
import events
import in_game_ad
import ocr
from device import Image
from vision import TemplateCache


ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "in_game_ad"
TEMPLATES = TemplateCache(ROOT / "templates")
POLICY = SimpleNamespace(tap_jitter_px=0, tap_delay=0.0, timing_jitter=0.0)


def frame(name: str) -> Image:
    return cv2.imread(str(FIX / f"{name}.jpg"))


class Device:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.backs = 0

    def click(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def press_back(self) -> None:
        self.backs += 1


class Reader:
    def __init__(self, balances: list[int | None]) -> None:
        self.balances = iter(balances)

    def read(self, *args: Any) -> int | None:
        return next(self.balances)


class Bus:
    def __init__(self) -> None:
        self.events: list[events.Event] = []

    def publish(self, event: events.Event) -> None:
        self.events.append(event)


def test_tile_requires_the_lit_play_control_and_battle_hud() -> None:
    assert in_game_ad.find_tile(frame("battle_available"), (30, 35), TEMPLATES) == (200, 1368)
    assert in_game_ad.find_tile(frame("battle_claimed"), (30, 35), TEMPLATES) is None
    assert in_game_ad.find_tile(frame("reward"), None, TEMPLATES) is None


def test_reward_reader_is_specific_to_six_gems() -> None:
    reward = frame("reward")
    boxes = ocr.read(reward)
    assert battle_menu.ad_reward_claim(reward, boxes, amount=6) is not None
    assert battle_menu.ad_reward_claim(reward, boxes) is None


def test_live_ad_variant_closes_only_after_maturity() -> None:
    bus, device = Bus(), Device()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([164, 170]), sleep=lambda _: None)
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 0, 7, True)
    assert device.taps == [(200, 1368)]
    assert claim.observe(frame("end_card"), None, device, POLICY, 20, 7, False)
    assert len(device.taps) == 1
    assert claim.observe(frame("end_card"), None, device, POLICY, 31, 7, False)
    assert device.taps[-1] == (81, 104)
    assert claim.observe(frame("reward"), None, device, POLICY, 34, 7, False)
    assert len(device.taps) == 3
    assert claim.observe(frame("battle_claimed"), (30, 35), device, POLICY, 36, 7, True)
    assert not claim.active
    assert bus.events == [events.InGameAdGemClaimed(
        gems_before=164, gems_after=170, delta=6, run_id=7)]


def test_meta_audience_network_close_preserves_wait_and_verified_reward() -> None:
    class MetaDevice(Device):
        def shell(self, command: str) -> str:
            if command.startswith("dumpsys window"):
                return (FIX / "meta_audience_network_focus_82.txt").read_text()
            if command.startswith("uiautomator dump"):
                return (FIX / "meta_audience_network_end_82.xml").read_text()
            raise AssertionError(command)

    bus, device = Bus(), MetaDevice()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([164, 170]), sleep=lambda _: None)
    end_card = cv2.imread(str(FIX / "meta_audience_network_end_82.png"))
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 0, 7, True)
    assert claim.observe(end_card, None, device, POLICY, 20, 7, False)
    assert device.taps == [(200, 1368)]
    assert claim.observe(end_card, None, device, POLICY, 31, 7, False)
    assert device.taps[-1] == (77, 77)
    assert bus.events == []  # Closing an ad is not proof of earned gems.
    # Exercise the established reward flow; this expired live ad had no reward popup.
    assert claim.observe(frame("reward"), None, device, POLICY, 34, 7, False)
    assert claim.observe(frame("battle_claimed"), (30, 35), device, POLICY, 36, 7, True)
    assert not claim.active
    assert bus.events == [events.InGameAdGemClaimed(
        gems_before=164, gems_after=170, delta=6, run_id=7)]


def test_reward_granted_end_card_closes_after_maturity() -> None:
    bus, device = Bus(), Device()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([164]), sleep=lambda _: None)
    end_card = cv2.imread(str(ROOT / "tests" / "fixtures" / "battle_menu"
                              / "ad_reward_granted.png"))
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 0, 7, True)
    assert claim.observe(end_card, None, device, POLICY, 20, 7, False)
    assert device.taps == [(200, 1368)]
    assert claim.observe(end_card, None, device, POLICY, 31, 7, False)
    assert device.taps[-1] == (1014, 64)


@pytest.mark.parametrize("emulator", [82, 83])
def test_dark_reward_pill_closes_on_both_live_ad_creatives(emulator: int) -> None:
    class AdDevice(Device):
        def shell(self, command: str) -> str:
            if command.startswith("dumpsys window"):
                return ("mCurrentFocus=Window{123 u0 com.TechTreeGames.TheTower/"
                        "com.google.android.gms.ads.AdActivity}")
            if command.startswith("uiautomator dump"):
                return ('<hierarchy><node text="Reward granted" />'
                        '<node text="Close" clickable="true" '
                        'bounds="[981,33][1050,99]" /></hierarchy>')
            raise AssertionError(command)

    bus, device = Bus(), AdDevice()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([164]), sleep=lambda _: None)
    end_card = cv2.imread(str(FIX / f"ad_reward_granted_dark_{emulator}.jpg"))
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 0, 7, True)
    assert claim.observe(end_card, None, device, POLICY, 31, 7, False)
    assert device.taps[-1] == (1015, 66)
    assert claim.active


def test_unconfirmed_balance_is_uncertain_and_tile_is_not_retapped() -> None:
    bus, device = Bus(), Device()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([164, 164, 164]), sleep=lambda _: None)
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 0, 7, True)
    assert claim.observe(frame("reward"), None, device, POLICY, 35, 7, False)
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 36, 7, True)
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 52, 7, True)
    assert not claim.active
    assert len(device.taps) == 2
    assert isinstance(bus.events[0], events.ClaimUncertain)
    assert not claim.observe(frame("battle_available"), (30, 35), device, POLICY, 60, 7, True)
    assert len(device.taps) == 2


def test_late_six_gem_reward_is_claimed_after_ad_timeout() -> None:
    bus, device = Bus(), Device()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([164, 170]), sleep=lambda _: None)
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 0, 7, True)
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY,
                         181, 7, True)
    assert not claim.active

    assert claim.observe(frame("reward"), None, device, POLICY, 184, 7, False)
    assert claim.active
    assert claim.observe(frame("battle_claimed"), (30, 35), device, POLICY,
                         186, 7, True)
    assert device.taps == [(200, 1368), (541, 1891)]
    assert any(isinstance(event, events.InGameAdGemClaimed) for event in bus.events)


@pytest.mark.parametrize("focused_activity", [
    "com.TechTreeGames.TheTower/com.google.android.gms.ads.AdActivity",
    "com.android.vending/com.google.android.finsky.transparentmainactivity.HsdpAlias",
])
def test_ad_timeout_backs_out_of_foreground_ad_then_claims_reward(
        focused_activity: str) -> None:
    class AdDevice(Device):
        def shell(self, command: str) -> str:
            if command.startswith("dumpsys window"):
                return f"mCurrentFocus=Window{{123 u0 {focused_activity}}}"
            if command.startswith("uiautomator dump"):
                return "<hierarchy></hierarchy>"
            raise AssertionError(command)

    bus, device = Bus(), AdDevice()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([164, 170]), sleep=lambda _: None)
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 0, 7, True)
    dark_end_card = cv2.imread(str(FIX / "ad_reward_granted_dark_82.jpg"))
    assert claim.observe(dark_end_card, None, device, POLICY, 181, 7, False)
    assert device.backs == 1
    assert claim.active
    assert claim.observe(frame("reward"), None, device, POLICY, 184, 7, False)
    assert claim.observe(frame("battle_claimed"), (30, 35), device, POLICY, 186, 7, True)
    assert not claim.active
    assert any(isinstance(event, events.InGameAdGemClaimed) for event in bus.events)


def test_play_store_overlay_exit_resumes_the_game_from_recorded_dialogs() -> None:
    class PlayDevice(Device):
        focus = ""

        def shell(self, command: str) -> str:
            assert command.startswith("dumpsys window")
            return self.focus

    bus, device = Bus(), PlayDevice()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([164]), sleep=lambda _: None)
    device.focus = ("mCurrentFocus=Window{123 u0 com.TechTreeGames.TheTower/"
                    "com.unity3d.player.UnityPlayerActivity}")
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 0, 7, True)

    device.focus = ("mCurrentFocus=Window{8dc4b2c u0 com.android.vending/"
                    "com.google.android.finsky.transparentmainactivity.HsdpAlias}")
    assert claim.observe(frame("play_store_overlay_83"), None, device, POLICY, 31, 7, False)
    device.focus = ("mCurrentFocus=Window{123 u0 com.TechTreeGames.TheTower/"
                    "com.unity3d.player.UnityPlayerActivity}")
    assert claim.observe(frame("ad_return_resume_83"), None, device, POLICY, 34, 7, False)
    assert claim.observe(frame("ad_return_cloud_83"), None, device, POLICY, 37, 7, False)
    assert claim.observe(frame("battle_claimed"), (30, 35), device, POLICY, 41, 7, True)

    assert device.taps == [(200, 1368), (1000, 940), (722, 1425), (539, 1638)]
    assert device.backs == 0
    assert not claim.active
    assert any(isinstance(event, events.ClaimUncertain)
               and event.target == "in_game_ad_gems" for event in bus.events)


def test_scan_loop_starts_video_before_menu_or_upgrades(bot_with_frames: Any) -> None:
    bot = bot_with_frames(["in_run_early"], battle_menu_opt_in=True)
    available = frame("battle_available")

    def refresh() -> Image:
        bot._screen = available
        return available

    bot.refresh_screen = refresh
    bot.gem.observe = lambda **kwargs: False
    assert bot.run_once()
    assert len(bot.device.taps) == 1
    assert abs(bot.device.taps[0][0] - 200) <= 8
    assert abs(bot.device.taps[0][1] - 1368) <= 8
    assert bot.in_game_ad.active
    assert not bot.battle_menu.active


@pytest.mark.parametrize("supervised", [False, True])
def test_ad_walk_claims_across_unclassified_frames(
        bot_with_frames: Any, tmp_path: Path, supervised: bool) -> None:
    from tests.test_battle_menu_loop import _supervised

    bot = bot_with_frames(["in_run_early"], battle_menu_opt_in=True)
    images = [frame(name) for name in (
        "battle_available", "end_card", "reward", "battle_claimed")]
    index = 0

    def refresh() -> Image:
        nonlocal index
        bot._screen = images[min(index, len(images) - 1)]
        index += 1
        return bot._screen

    bot.refresh_screen = refresh
    bot.gem.observe = lambda **kwargs: False
    hardware = _supervised(bot, tmp_path) if supervised else bot.device
    assert bot.run_once()
    bot.in_game_ad._started -= 31
    for _ in range(3):
        assert bot.run_once()

    assert len(hardware.taps) == 3
    assert not bot.in_game_ad.active
    assert any(isinstance(event, events.InGameAdGemClaimed)
               for event in bot.bus.published)


@pytest.mark.parametrize("supervised", [False, True])
def test_scan_loop_claims_reward_after_ad_timeout(
        bot_with_frames: Any, tmp_path: Path, supervised: bool) -> None:
    from tests.test_battle_menu_loop import _supervised
    import config

    bot = bot_with_frames(["in_run_early"], battle_menu_opt_in=True)
    images = [frame(name) for name in (
        "battle_available", "end_card", "reward", "battle_claimed")]
    index = 0

    def refresh() -> Image:
        nonlocal index
        bot._screen = images[min(index, len(images) - 1)]
        index += 1
        return bot._screen

    bot.refresh_screen = refresh
    bot.gem.observe = lambda **kwargs: False
    hardware = _supervised(bot, tmp_path) if supervised else bot.device
    assert bot.run_once()
    bot.in_game_ad._started -= config.BATTLE_MENU_AD_TIMEOUT + 1
    bot.in_game_ad._closes = 3
    assert bot.run_once()
    assert not bot.in_game_ad.active
    assert bot.run_once()
    assert bot.run_once()
    assert len(hardware.taps) == 2
    assert any(isinstance(event, events.InGameAdGemClaimed)
               for event in bot.bus.published)


class UnityDevice(Device):
    """Emulator 82's Unity WebView ad, which ignores the back button."""

    def __init__(self) -> None:
        super().__init__()
        self.hierarchy = (FIX / "unity_playable_82.xml").read_text()

    def shell(self, command: str) -> str:
        if command.startswith("dumpsys window"):
            return (FIX / "unity_focus_82.txt").read_text()
        if command.startswith("uiautomator dump"):
            return self.hierarchy
        raise AssertionError(command)


def test_unity_ad_skips_then_closes_its_end_card() -> None:
    bus, device = Bus(), UnityDevice()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([164, 170]), sleep=lambda _: None)
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 0, 7, True)
    assert claim.observe(frame("unity_playable_82"), None, device, POLICY, 31, 7, False)
    x, y = device.taps[-1]
    assert abs(x - 995) <= 6 and abs(y - 110) <= 6
    device.hierarchy = (FIX / "unity_end_82.xml").read_text()
    assert claim.observe(frame("unity_end_82"), None, device, POLICY, 35, 7, False)
    x, y = device.taps[-1]
    assert abs(x - 999) <= 6 and abs(y - 105) <= 6
    assert claim.observe(frame("reward"), None, device, POLICY, 38, 7, False)
    assert claim.observe(frame("battle_claimed"), (30, 35), device, POLICY, 40, 7, True)
    assert bus.events == [events.InGameAdGemClaimed(
        gems_before=164, gems_after=170, delta=6, run_id=7)]


def test_recovery_closes_an_ad_that_ignores_back() -> None:
    bus, device = Bus(), UnityDevice()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([164, 170]), sleep=lambda _: None)
    playable = frame("unity_playable_82")
    assert claim.observe(frame("battle_available"), (30, 35), device, POLICY, 0, 7, True)
    for now in (31, 34, 37):  # Skip taps the ad did not act on.
        assert claim.observe(playable, None, device, POLICY, now, 7, False)
    assert claim.observe(playable, None, device, POLICY, 181, 7, False)
    assert device.backs == 1 and len(device.taps) == 4
    assert claim.observe(playable, None, device, POLICY, 184, 7, False)
    assert len(device.taps) == 5
    x, y = device.taps[-1]
    assert abs(x - 995) <= 6 and abs(y - 110) <= 6
    device.hierarchy = (FIX / "unity_end_82.xml").read_text()
    assert claim.observe(frame("unity_end_82"), None, device, POLICY, 188, 7, False)
    x, y = device.taps[-1]
    assert abs(x - 999) <= 6 and abs(y - 105) <= 6
    assert device.backs == 1
    assert claim.observe(frame("reward"), None, device, POLICY, 191, 7, False)
    assert claim.observe(frame("battle_claimed"), (30, 35), device, POLICY, 193, 7, True)
    assert not claim.active
    assert bus.events == [events.InGameAdGemClaimed(
        gems_before=164, gems_after=170, delta=6, run_id=7)]


def test_ad_free_pack_claims_immediately_and_verifies_six_gems() -> None:
    bus, device = Bus(), Device()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([843, 849]), sleep=lambda _: None)
    available = frame("battle_ad_free_available")
    assert claim.observe(available, (30, 35), device, POLICY, 0, 7, True)
    assert len(device.taps) == 1
    x, y = device.taps[0]
    assert 20 < x < 270 and 1290 < y < 1450
    assert claim.observe(frame("battle_claimed"), (30, 35), device, POLICY, 1, 7, True)
    assert not claim.active
    assert bus.events == [events.InGameAdGemClaimed(gems_before=843, gems_after=849, delta=6, run_id=7)]


def test_ad_free_claim_requires_battle_hud_and_available_button() -> None:
    assert in_game_ad.find_instant_claim(frame("battle_ad_free_available"), (30, 35), TEMPLATES) is not None
    assert in_game_ad.find_instant_claim(frame("battle_ad_free_available"), None, TEMPLATES) is None
    assert in_game_ad.find_instant_claim(frame("battle_claimed"), (30, 35), TEMPLATES) is None
    assert in_game_ad.find_instant_claim(frame("battle_available"), (30, 35), TEMPLATES) is None


def test_ad_free_claim_does_not_repeat_unverified_reward() -> None:
    bus, device = Bus(), Device()
    claim = in_game_ad.InGameAdClaim(bus, TEMPLATES, Reader([843, 843]), sleep=lambda _: None)
    available = frame("battle_ad_free_available")
    assert claim.observe(available, (30, 35), device, POLICY, 0, 7, True)
    assert claim.observe(available, (30, 35), device, POLICY, 20, 7, True)
    assert not claim.observe(available, (30, 35), device, POLICY, 21, 7, True)
    assert len(device.taps) == 1
    assert any(isinstance(event, events.ClaimUncertain) for event in bus.events)
