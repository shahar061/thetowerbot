"""Numbered reward badges require both the control and a readable count."""
from pathlib import Path

import cv2
import pytest

import config
import menu_badges
import ocr
from account_collection import locate_control
from vision import TemplateCache

FIXTURES = Path(__file__).parent / 'fixtures'


def test_recorded_badges_are_attached_to_their_own_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_main_bluestacks_1920.png'))
    monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: (ocr.TextBox('2', .99, config.Rect(0, 0, 15, 20)),))
    cache = TemplateCache(config.TEMPLATE_DIR)
    assert menu_badges.read_badge(frame, cache, 'missions').count == 2
    assert menu_badges.read_badge(frame, cache, 'mail').count == 2


def test_plain_red_dot_is_not_a_numbered_badge(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_main_bluestacks_1920.png'))
    cv2.circle(frame, (925, 258), 20, (0, 0, 255), -1)
    monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: ())
    assert menu_badges.read_badge(frame, TemplateCache(config.TEMPLATE_DIR), 'missions') is None


@pytest.mark.parametrize('label', ['0', '12/35', 'CLAIM', '-2'])
def test_nonpositive_or_noninteger_counts_refuse(monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_main_bluestacks_1920.png'))
    monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: (ocr.TextBox(label, .99, config.Rect(0, 0, 15, 20)),))
    assert menu_badges.read_badge(frame, TemplateCache(config.TEMPLATE_DIR), 'missions') is None


def test_battle_frame_cannot_supply_a_menu_badge() -> None:
    frame = cv2.imread(str(FIXTURES / 'in_run_early.png'))
    assert menu_badges.read_badge(frame, TemplateCache(config.TEMPLATE_DIR), 'missions') is None


def test_real_ocr_reads_both_recorded_badge_counts() -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_main_bluestacks_1920.png'))
    cache = TemplateCache(config.TEMPLATE_DIR)
    assert menu_badges.read_badge(frame, cache, 'missions').count == 2
    assert menu_badges.read_badge(frame, cache, 'mail').count == 4


def test_two_matching_mail_controls_are_ambiguous() -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_main_bluestacks_1920.png'))
    frame[800:880, 980:1065] = frame[480:560, 980:1065]
    assert menu_badges.read_badge(frame, TemplateCache(config.TEMPLATE_DIR), 'mail') is None


@pytest.mark.parametrize('fixture', ['menu_main_labs_unlocked.png', 'menu_labs_active.png'])
def test_recorded_purple_lab_notifications(fixture: str) -> None:
    frame = cv2.imread(str(FIXTURES / fixture))
    assert menu_badges.read_notification(frame, TemplateCache(config.TEMPLATE_DIR), 'labs') is True


def test_lab_tab_without_dot_is_clear_not_unknown() -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_labs_active.png'))
    frame[2203:2259, 819:876] = (30, 20, 25)
    assert menu_badges.read_notification(frame, TemplateCache(config.TEMPLATE_DIR), 'labs') is False


def test_settings_dot_is_not_mission_notification() -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_main_bluestacks_1920.png'))
    frame[235:280, 895:955] = 0  # remove the Missions badge, retain settings dot and anchor
    assert menu_badges.read_notification(frame, TemplateCache(config.TEMPLATE_DIR), 'missions') is False


def test_unreadable_numbered_mission_badge_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_main_bluestacks_1920.png'))
    monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: ())
    assert menu_badges.read_notification(frame, TemplateCache(config.TEMPLATE_DIR), 'missions') is None


def test_absent_lab_anchor_is_unknown_not_clear() -> None:
    frame = cv2.imread(str(FIXTURES / 'in_run_early.png'))
    assert menu_badges.read_notification(frame, TemplateCache(config.TEMPLATE_DIR), 'labs') is None


def test_synthetic_plain_mission_dot_validates_geometry_only() -> None:
    frame = cv2.imread(str(FIXTURES / 'menu_main_bluestacks_1920.png'))
    frame[235:280, 895:955] = 0
    cache = TemplateCache(config.TEMPLATE_DIR)
    anchor = locate_control(frame, cache.get(config.NAV_TARGETS['MISSIONS']), 'missions')
    assert not menu_badges._plain_mission_dot(frame, anchor)
    cv2.circle(frame, (925, 258), 16, (0, 0, 255), -1)
    assert menu_badges._plain_mission_dot(frame, anchor)
    # No recorded positive plain mission dot exists yet; this geometry test
    # cannot enable a route to the claim walk.
    assert menu_badges.read_notification(frame, cache, 'missions') is None
