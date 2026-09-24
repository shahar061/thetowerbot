"""Recorded, redacted OCR from the approved Air 2 clone."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import cv2
import pytest
from PIL import Image as PILImage
from types import SimpleNamespace

from config import Rect
from ocr import TextBox
from fleet.account_observer import (parse_account_popup, parse_google_play_profile,
                                    parse_tower_consent,
                                    parse_new_account_warning,
                                    parse_game_stats_home,
                                    parse_settings, parse_home, parse_inbox,
                                    parse_link_account_prompt,
                                    StagingAccountObserver)
import vision
import config
import game_over


FIXTURE = Path(__file__).parent / "fixtures" / "r00" / "account_before_ocr.json"
WARNING_FIXTURE = Path(__file__).parent / "fixtures" / "r00" / "new_account_warning_ocr.json"
AFTER_FIXTURE = Path(__file__).parent / "fixtures" / "r00" / "account_after_ocr.json"
ACCOUNT_FIXTURES = Path(__file__).parent / "fixtures" / "account_screens"


def consent_boxes() -> tuple[TextBox, ...]:
    return tuple(TextBox(label, .99, Rect(*rect)) for label, rect in (
        ("THETOWER", (351, 569, 381, 56)),
        ("This game uses 3rd party analytics", (189, 723, 701, 52)),
        ("and advertising services", (290, 779, 497, 43)),
        ("Please refer to all privacy policies", (201, 856, 678, 48)),
        ("EULA", (471, 1091, 108, 42)),
        ("Privacy Policy", (380, 1243, 282, 56)),
        ('By tapping "I Agree," you confirm you\'ve', (144, 1460, 793, 43)),
        ("I Agree", (449, 1735, 164, 58)),
    ))


def test_first_launch_consent_exposes_only_i_agree() -> None:
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    reading = parse_tower_consent(frame, consent_boxes(), observed_at=101.,
                                  app_version="29.0.3", evidence_ref="capture://consent")
    assert reading is not None
    assert reading.screen == "tower_consent"
    assert reading.controls == {"i_agree": (531, 1764)}


def test_bluestacks_native_first_launch_consent_uses_the_observed_button() -> None:
    frame = cv2.imread(str(Path(__file__).parent / "fixtures" /
                           "tower_consent_bluestacks_1920.png"))
    rows = json.loads((Path(__file__).parent / "fixtures" / "ocr" /
                       "tower_consent_bluestacks_1920.json").read_text())
    observed = tuple(TextBox(row["text"], row["confidence"], Rect(*row["rect"]))
                     for row in rows)
    reading = parse_tower_consent(frame, observed, observed_at=101.,
                                  app_version="29.0.3", evidence_ref="capture://air22")
    assert reading is not None
    assert reading.controls == {"i_agree": (531, 1525)}


@pytest.mark.parametrize("removed", ["THETOWER", "EULA", "Privacy Policy", "I Agree"])
def test_incomplete_consent_never_exposes_agree(removed: str) -> None:
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    assert parse_tower_consent(
        frame, tuple(box for box in consent_boxes() if box.text != removed),
        observed_at=101., app_version="29.0.3", evidence_ref="capture://consent",
    ) is None


def boxes() -> tuple[TextBox, ...]:
    rows = json.loads(FIXTURE.read_text())["boxes"]
    return tuple(TextBox(row["text"], row["confidence"], Rect(*row["rect"])) for row in rows)


def warning_boxes() -> tuple[TextBox, ...]:
    rows = json.loads(WARNING_FIXTURE.read_text())["boxes"]
    return tuple(TextBox(row["text"], row["confidence"], Rect(*row["rect"])) for row in rows)


def google_play_profile_boxes() -> tuple[TextBox, ...]:
    return (
        TextBox("Create a Play Games profile", .99, Rect(139, 1335, 780, 63)),
        TextBox("No profile", .99, Rect(283, 1619, 203, 52)),
        TextBox("Cancel", .99, Rect(108, 2268, 136, 42)),
        TextBox("Next", .99, Rect(866, 2266, 100, 46)),
    )


def test_google_play_profile_exposes_only_cancel() -> None:
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    reading = parse_google_play_profile(
        frame, google_play_profile_boxes(), observed_at=101.,
        app_version="29.0.2", evidence_ref="capture://google-play-profile",
    )
    assert reading is not None
    assert reading.screen == "google_play_profile"
    assert reading.controls == {"dismiss_google_play_profile": (176, 2289)}


def test_google_play_no_profile_sheet_after_consent_exposes_cancel() -> None:
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    boxes = (
        TextBox("Google Play Games", .99, Rect(397, 670, 382, 52)),
        TextBox("No profile", .99, Rect(283, 1725, 203, 49)),
        TextBox("Cancel", .99, Rect(109, 2268, 135, 42)),
    )
    reading = parse_google_play_profile(
        frame, boxes, observed_at=101., app_version="29.0.3",
        evidence_ref="capture://no-profile",
    )
    assert reading is not None
    assert reading.controls == {"dismiss_google_play_profile": (176, 2289)}


@pytest.mark.parametrize("removed", ["Create a Play Games profile", "No profile", "Cancel", "Next"])
def test_incomplete_google_play_profile_never_exposes_cancel(removed: str) -> None:
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    observed = tuple(box for box in google_play_profile_boxes() if box.text != removed)
    assert parse_google_play_profile(
        frame, observed, observed_at=101., app_version="29.0.2",
        evidence_ref="capture://google-play-profile",
    ) is None


def test_measured_warning_exposes_only_confirm_new_account() -> None:
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    reading = parse_new_account_warning(frame, warning_boxes(), observed_at=101.,
                                        app_version="29.0.2", evidence_ref="capture://warning")
    assert reading is not None
    assert reading.screen == "new_account_warning"
    assert reading.account_id == "AAAAAAAAAAAAAAAA"
    assert reading.controls == {"confirm_new_account": (728, 1439)}


@pytest.mark.parametrize("removed", ["Warning", "continue?", "No", "Yes",
                                       "account and go back to the loading"])
def test_incomplete_warning_never_exposes_yes(removed: str) -> None:
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    observed = tuple(box for box in warning_boxes() if box.text != removed)
    assert parse_new_account_warning(frame, observed, observed_at=101.,
                                     app_version="29.0.2", evidence_ref="capture://warning") is None


def test_game_stats_exposes_only_measured_home() -> None:
    frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "game_over_newhigh.png"))
    observed = (TextBox("GAMESTATS", .99, Rect(333, 621, 411, 52)),
                TextBox("RETRY", .99, Rect(226, 1687, 152, 44)),
                TextBox("HOME", .99, Rect(709, 1687, 143, 42)))
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    reading = parse_game_stats_home(frame, observed, cache, observed_at=101.,
                                    app_version="29.0.2", evidence_ref="capture://game-over")
    assert reading is not None
    assert reading.controls == {"home_from_game_over": (780, 1708)}
    assert parse_game_stats_home(frame, observed[:-1], cache, observed_at=101.,
                                 app_version="29.0.2", evidence_ref="capture://game-over") is None


def test_game_stats_new_layout_exposes_home() -> None:
    frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "game_over_newhigh.png"))
    observed = (TextBox("GAMESTATS", .99, Rect(333, 668, 413, 56)),
                TextBox("RETRY", 1., Rect(225, 1636, 153, 46)),
                TextBox("HOME", 1., Rect(708, 1636, 145, 46)))
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    reading = parse_game_stats_home(frame, observed, cache, observed_at=101.,
                                    app_version="29.0.2", evidence_ref="capture://game-over")
    assert reading is not None
    assert reading.controls == {"home_from_game_over": (780, 1659)}


def test_bluestacks_native_game_over_reads_result_and_home() -> None:
    fixture_root = Path(__file__).parent / "fixtures"
    frame = cv2.imread(str(fixture_root / "game_over_bluestacks_1920.png"))
    rows = json.loads((fixture_root / "ocr" /
                       "game_over_bluestacks_1920.json").read_text())
    observed = tuple(TextBox(row["text"], row["confidence"], Rect(*row["rect"]))
                     for row in rows)
    result = game_over.parse_frame(frame, observed, now=101.)
    assert result is not None
    assert (result.frame_width, result.frame_height) == (1080, 1920)
    fields = {field.key: field for field in result.fields}
    assert fields["wave"].raw_value == "2"
    assert fields["coins_earned"].raw_value == "5"
    home = parse_game_stats_home(
        frame, observed, vision.TemplateCache(config.TEMPLATE_DIR),
        observed_at=101., app_version="29.0.3", evidence_ref="capture://air22",
    )
    assert home is not None
    assert home.controls == {"home_from_game_over": (781, 1468)}


def parse(observed: tuple[TextBox, ...] | None = None, *, shape: tuple[int, int, int] = (2400, 1080, 3)):
    return parse_account_popup(np.zeros(shape, dtype=np.uint8), boxes() if observed is None else observed,
                               observed_at=101., app_version="29.0.2", evidence_ref="capture://before")


def test_native_account_popup_reads_id_and_only_new_account_control() -> None:
    reading = parse()
    assert reading is not None
    assert reading.screen == "account"
    assert reading.account_id == "AAAAAAAAAAAAAAAA"
    assert reading.popup_title == "ACCOUNT" and reading.id_label == "ID:"
    assert reading.controls == {"new_account": (541, 1759)}


def test_bluestacks_native_account_popup_uses_centered_dialog_bounds() -> None:
    observed = (
        TextBox("ACCOUNT", .99, Rect(398, 344, 282, 47)),
        TextBox("ID: AAAAAAAAAAAAAAAA", .99, Rect(318, 455, 395, 30)),
        TextBox("New Account", .99, Rect(400, 1498, 283, 43)),
    )
    reading = parse(observed, shape=(1920, 1080, 3))
    assert reading is not None
    assert reading.account_id == "AAAAAAAAAAAAAAAA"
    assert reading.controls == {"new_account": (541, 1519)}


def test_redacted_before_and_after_account_captures_have_distinct_ids() -> None:
    rows = json.loads(AFTER_FIXTURE.read_text())["boxes"]
    after_boxes = tuple(TextBox(row["text"], row["confidence"], Rect(*row["rect"]))
                        for row in rows)
    after = parse(after_boxes)
    before = parse()
    assert after is not None and before is not None
    assert after.account_id == "BBBBBBBBBBBBBBBB"
    assert before.account_id == "AAAAAAAAAAAAAAAA"
    assert after.account_id != before.account_id
    assert set(after.controls) == {"new_account"}


@pytest.mark.parametrize("change", [
    lambda b: tuple(replace(x, confidence=.7) if x.text.startswith("ID:") else x for x in b),
    lambda b: b + (next(x for x in b if x.text.startswith("ID:")),),
    lambda b: tuple(replace(x, text="ID: ???") if x.text.startswith("ID:") else x for x in b),
])
def test_unreadable_ambiguous_or_invalid_id_refused(change) -> None:
    assert parse(change(boxes())) is None


def test_wrong_geometry_or_title_refused() -> None:
    assert parse(shape=(1200, 540, 3)) is None
    assert parse(tuple(b for b in boxes() if b.text != "ACCOUNT")) is None


def test_new_account_target_requires_unique_trusted_measured_label() -> None:
    original = boxes()
    button = next(b for b in original if b.text == "New Account")
    for altered in (original + (button,),
                    tuple(replace(b, confidence=.5) if b == button else b for b in original),
                    tuple(replace(b, rect=Rect(100, 100, 100, 30)) if b == button else b for b in original)):
        reading = parse(altered)
        assert reading is not None and reading.controls == {}


def test_recorded_settings_locates_account_without_other_controls() -> None:
    frame = cv2.imread(str(ACCOUNT_FIXTURES / "settings_redacted.png"))
    rows = json.loads((ACCOUNT_FIXTURES / "settings_redacted.json").read_text())
    observed = tuple(TextBox(row["text"], row["confidence"], Rect(*row["rect"])) for row in rows)
    reading = parse_settings(frame, observed, observed_at=101.,
                             app_version="29.0.1", evidence_ref="capture://settings")
    assert reading is not None and reading.screen == "settings"
    assert set(reading.controls) == {"account"}


def test_bluestacks_native_settings_locates_account() -> None:
    observed = (
        TextBox("SETTINGS", .99, Rect(408, 251, 263, 43)),
        TextBox("Account", .99, Rect(281, 569, 161, 35)),
        TextBox("v29.0.3", .99, Rect(819, 1671, 129, 34)),
    )
    reading = parse_settings(np.zeros((1920, 1080, 3), dtype=np.uint8), observed,
                             observed_at=101., app_version="29.0.3",
                             evidence_ref="capture://bluestacks-settings")
    assert reading is not None
    assert reading.controls == {"account": (361, 586)}


def test_recorded_home_locates_settings_without_account_popup() -> None:
    frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "menu_main.png"))
    reading = parse_home(frame, (), vision.TemplateCache(config.TEMPLATE_DIR),
                         observed_at=101., app_version="29.0.2", evidence_ref="capture://home")
    assert reading is not None and reading.screen == "home"
    assert set(reading.controls) == {"settings"}


def test_bluestacks_native_home_locates_settings_on_its_own_frame() -> None:
    frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "menu_main_bluestacks_1920.png"))
    reading = parse_home(frame, (), vision.TemplateCache(config.TEMPLATE_DIR),
                         observed_at=101., app_version="29.0.3",
                         evidence_ref="capture://bluestacks-home")
    assert reading is not None
    x, y = reading.controls["settings"]
    assert 900 < x < 1080 and 250 < y < 400


def test_popup_never_counts_as_home_or_settings() -> None:
    frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "menu_main.png"))
    observed = boxes()
    assert parse_home(frame, observed, vision.TemplateCache(config.TEMPLATE_DIR),
                      observed_at=101., app_version="29.0.2", evidence_ref="capture://popup") is None
    assert parse_settings(frame, observed, observed_at=101.,
                          app_version="29.0.2", evidence_ref="capture://popup") is None


def test_live_observer_saves_private_frame_and_returns_popup(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    import ocr
    monkeypatch.setattr(ocr, "read", lambda *_, **__: boxes())
    device = SimpleNamespace(
        serial="127.0.0.1:5575",
        app_current=lambda: SimpleNamespace(package="com.TechTreeGames.TheTower"),
        app_info=lambda _: SimpleNamespace(version_name="29.0.2"),
        screenshot=lambda **_: PILImage.fromarray(np.zeros((2400, 1080, 3), dtype=np.uint8)),
    )
    observer = StagingAccountObserver(tmp_path, endpoint=device.serial,
                                      allowed_versions=frozenset({"29.0.2"}))
    reading = observer(device)
    assert reading.account_id == "AAAAAAAAAAAAAAAA"
    saved = Path(reading.evidence_ref)
    assert saved.exists() and saved.stat().st_mode & 0o777 == 0o600


def test_live_observer_identifies_battle_without_exposing_controls(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ocr
    monkeypatch.setattr(ocr, "read", lambda *_, **__: ())
    frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "in_run_utility.png"))
    device = SimpleNamespace(
        serial="127.0.0.1:5575",
        app_current=lambda: SimpleNamespace(package="com.TechTreeGames.TheTower"),
        app_info=lambda _: SimpleNamespace(version_name="29.0.2"),
        screenshot=lambda **_: PILImage.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)),
    )
    reading = StagingAccountObserver(tmp_path, endpoint=device.serial,
                                     allowed_versions=frozenset({"29.0.2"}))(device)
    assert reading.screen == "battle"
    assert reading.controls == {}


def test_live_inbox_exposes_only_its_measured_return_caption(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ocr
    frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "menu_mail_empty_live_39.jpg"))
    observed = ocr.read(frame, strict=True, min_confidence=0.)
    reading = parse_inbox(frame, observed, observed_at=101.,
                          app_version="29.0.3", evidence_ref="capture://inbox")
    assert reading is not None
    assert reading.screen == "inbox"
    assert reading.controls == {"return_to_game": (540, 2301)}

    monkeypatch.setattr(ocr, "read", lambda *_, **__: observed)
    device = SimpleNamespace(
        serial="127.0.0.1:5775",
        app_current=lambda: SimpleNamespace(package="com.TechTreeGames.TheTower"),
        app_info=lambda _: SimpleNamespace(version_name="29.0.3"),
        screenshot=lambda **_: PILImage.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)),
    )
    observed_frame = StagingAccountObserver(
        tmp_path, endpoint=device.serial, allowed_versions=frozenset({"29.0.3"}),
    )(device)
    assert observed_frame.screen == "inbox"
    assert observed_frame.controls == reading.controls


def test_inbox_requires_anchored_header_and_bottom_return_caption() -> None:
    frame = np.zeros((2400, 1080, 3), dtype=np.uint8)
    observed = (
        TextBox("INBOX", .99, Rect(27, 110, 173, 46)),
        TextBox("Mail", .94, Rect(209, 199, 118, 52)),
        TextBox("News", .99, Rect(737, 198, 152, 56)),
        TextBox("Tap To Return To Game", .98, Rect(231, 2270, 618, 62)),
    )
    for removed in (observed[0], observed[-1]):
        assert parse_inbox(frame, tuple(box for box in observed if box is not removed),
                           observed_at=101., app_version="29.0.3",
                           evidence_ref="capture://incomplete") is None
    assert parse_inbox(frame, (observed[0], observed[-1]),
                       observed_at=101., app_version="29.0.3",
                       evidence_ref="capture://news-detail") is not None
    assert parse_inbox(frame, observed[:-1] + (
        TextBox("Tap To Return To Game", .98, Rect(231, 1000, 618, 62)),),
        observed_at=101., app_version="29.0.3",
        evidence_ref="capture://wrong-position") is None
    assert parse_inbox(frame, observed + (observed[-1],),
                       observed_at=101., app_version="29.0.3",
                       evidence_ref="capture://ambiguous-footer") is None
    assert parse_inbox(frame, observed[:-1] + (
        TextBox("Tap To Return To Game", .89, observed[-1].rect),),
        observed_at=101., app_version="29.0.3",
        evidence_ref="capture://weak-footer") is None


def test_live_observer_identifies_native_bluestacks_workshop(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ocr
    root = Path(__file__).parent / "fixtures"
    frame = cv2.imread(str(root / "menu_workshop_attack_1920.png"))
    rows = json.loads((root / "ocr" / "menu_workshop_attack_1920.json").read_text())
    observed = tuple(TextBox(row["text"], row["confidence"], Rect(*row["rect"]))
                     for row in rows)
    monkeypatch.setattr(ocr, "read", lambda *_, **__: observed)
    device = SimpleNamespace(
        serial="127.0.0.1:5775",
        app_current=lambda: SimpleNamespace(package="com.TechTreeGames.TheTower"),
        app_info=lambda _: SimpleNamespace(version_name="29.0.3"),
        screenshot=lambda **_: PILImage.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)),
    )
    reading = StagingAccountObserver(
        tmp_path, endpoint=device.serial, allowed_versions=frozenset({"29.0.3"}),
    )(device)
    assert reading.screen == "workshop"
    assert 50 <= reading.controls["battle_tab"][0] <= 180
    assert 1750 <= reading.controls["battle_tab"][1] <= 1900


def test_live_observer_recognizes_only_the_measured_google_play_profile(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ocr
    monkeypatch.setattr(ocr, "read", lambda *_, **__: google_play_profile_boxes())
    device = SimpleNamespace(
        serial="127.0.0.1:5575",
        app_current=lambda: SimpleNamespace(package="com.google.android.gms"),
        app_info=lambda _: SimpleNamespace(version_name="29.0.2"),
        screenshot=lambda **_: PILImage.fromarray(np.zeros((2400, 1080, 3), dtype=np.uint8)),
    )
    reading = StagingAccountObserver(tmp_path, endpoint=device.serial,
                                     allowed_versions=frozenset({"29.0.2"}))(device)
    assert reading.screen == "google_play_profile"
    assert reading.controls == {"dismiss_google_play_profile": (176, 2289)}


def test_loading_play_games_overlay_exposes_no_control(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ocr
    monkeypatch.setattr(ocr, "read", lambda *_, **__: (google_play_profile_boxes()[0],))
    device = SimpleNamespace(
        serial="127.0.0.1:5575",
        app_current=lambda: SimpleNamespace(package="com.google.android.gms"),
        app_info=lambda _: SimpleNamespace(version_name="29.0.3"),
        screenshot=lambda **_: PILImage.fromarray(np.zeros((2400, 1080, 3), dtype=np.uint8)),
    )
    reading = StagingAccountObserver(tmp_path, endpoint=device.serial,
                                     allowed_versions=frozenset({"29.0.3"}))(device)
    assert reading.screen == "unknown"
    assert reading.controls == {}


def test_live_observer_rejects_wrong_package_before_screenshot(tmp_path: Path) -> None:
    called: list[str] = []
    device = SimpleNamespace(
        serial="127.0.0.1:5575",
        app_current=lambda: SimpleNamespace(package="other.package"),
        screenshot=lambda **_: called.append("screenshot"),
    )
    observer = StagingAccountObserver(tmp_path, endpoint=device.serial,
                                      allowed_versions=frozenset({"29.0.2"}))
    with pytest.raises(ValueError, match="package"):
        observer(device)
    assert called == []


def test_live_observer_marks_conflict_and_exposes_no_tap(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    import ocr
    conflict = TextBox("New session detected", .99, Rect(320, 800, 440, 50))
    monkeypatch.setattr(ocr, "read", lambda *_, **__: boxes() + (conflict,))
    device = SimpleNamespace(
        serial="127.0.0.1:5575",
        app_current=lambda: SimpleNamespace(package="com.TechTreeGames.TheTower"),
        app_info=lambda _: SimpleNamespace(version_name="29.0.2"),
        screenshot=lambda **_: PILImage.fromarray(np.zeros((2400, 1080, 3), dtype=np.uint8)),
    )
    reading = StagingAccountObserver(tmp_path, endpoint=device.serial,
                                     allowed_versions=frozenset({"29.0.2"}))(device)
    assert reading.conflict_dialog == "new session detected"
    assert reading.controls == {}


def test_link_account_prompt_exposes_only_its_close_over_a_dimmed_home() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    frame = cv2.imread(str(fixtures / "link_account_prompt.png"))
    observed = tuple(TextBox(row["text"], row["confidence"], Rect(*row["rect"])) for row in
                     json.loads((fixtures / "ocr" / "link_account_prompt.json").read_text()))
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    reading = parse_link_account_prompt(frame, observed, cache, observed_at=101.,
                                        app_version="29.0.3", evidence_ref="capture://air37")
    assert reading is not None
    assert reading.screen == "link_account_prompt"
    assert reading.controls == {"close": (881, 770)}
    # Home still matches through the dim, which is why the prompt is read first.
    assert parse_home(frame, observed, cache, observed_at=101., app_version="29.0.3",
                      evidence_ref="capture://air37") is not None
    assert parse_link_account_prompt(
        frame, tuple(box for box in observed if box.text != "TAKE ME THERE"), cache,
        observed_at=101., app_version="29.0.3", evidence_ref="capture://air37") is None
