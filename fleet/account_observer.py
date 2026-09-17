"""Measured Tower account and first-launch screen OCR."""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import account_collection
import cv2
import config
import ocr
import pages
import screens
import vision
from config import Rect
from device import Image, capture_screen
from fleet.account_creation import AccountFrame
from fleet.tutorial import workshop_coin_claim
from ocr import TextBox


_TITLE = Rect(365, 555, 360, 105)
_IDENTITY = Rect(285, 655, 465, 105)
_NEW_ACCOUNT = Rect(365, 1705, 370, 115)
_SETTINGS_TITLE = Rect(350, 460, 370, 110)
_SETTINGS_ACCOUNT = Rect(235, 775, 275, 120)
_VERSION = Rect(790, 1875, 205, 105)
_ID = re.compile(r"ID:\s*([0-9A-F]{16})", re.IGNORECASE)
_WARNING_LINES = (
    ("Warning", Rect(385, 835, 310, 120)),
    ("You will be logged out of the current", Rect(140, 945, 800, 100)),
    ("account and go back to the loading", Rect(150, 1000, 780, 100)),
    ("screen.", Rect(430, 1055, 220, 100)),
    ("Are you sure that you want to", Rect(200, 1130, 680, 110)),
    ("continue?", Rect(395, 1190, 290, 90)),
    ("No", Rect(265, 1370, 175, 125)),
    ("Yes", Rect(625, 1370, 205, 125)),
)
_GAME_STATS_TITLE = Rect(300, 580, 480, 130)
_GAME_STATS_RETRY = Rect(180, 1650, 250, 120)
_GAME_STATS_HOME = Rect(660, 1650, 260, 120)
_GOOGLE_PLAY_PROFILE = (
    ("Create a Play Games profile", Rect(80, 1240, 920, 180)),
    ("No profile", Rect(180, 1510, 400, 180)),
    ("Cancel", Rect(50, 2200, 260, 120)),
    ("Next", Rect(800, 2200, 220, 120)),
)
_GOOGLE_PLAY_NO_PROFILE = (
    ("Google Play Games", Rect(300, 600, 520, 160)),
    ("No profile", Rect(180, 1660, 400, 180)),
    ("Cancel", Rect(50, 2200, 260, 120)),
)
_TOWER_CONSENT = (
    ("THETOWER", Rect(300, 525, 470, 135)),
    ("This game uses 3rd party analytics", Rect(135, 680, 810, 130)),
    ("and advertising services", Rect(225, 745, 620, 100)),
    ("Please refer to all privacy policies", Rect(150, 815, 770, 105)),
    ("EULA", Rect(425, 1050, 205, 105)),
    ("Privacy Policy", Rect(325, 1190, 380, 130)),
    ('By tapping "I Agree," you confirm you\'ve', Rect(95, 1410, 890, 115)),
    ("I Agree", Rect(390, 1690, 300, 155)),
)
_TOWER_PACKAGE = "com.TechTreeGames.TheTower"
_GOOGLE_PLAY_SERVICES_PACKAGE = "com.google.android.gms"


def _inside(box: TextBox, bounds: Rect) -> bool:
    rect = box.rect
    return (rect.w > 0 and rect.h > 0 and bounds.x <= rect.x and bounds.y <= rect.y
            and rect.x + rect.w <= bounds.x + bounds.w
            and rect.y + rect.h <= bounds.y + bounds.h)


def _trusted(box: TextBox) -> bool:
    return math.isfinite(box.confidence) and .9 <= box.confidence <= 1.


def parse_account_popup(
    frame: Image, boxes: tuple[TextBox, ...], *, observed_at: float,
    app_version: str, evidence_ref: str,
) -> AccountFrame | None:
    """Read one native frame, refusing ambiguous or unmeasured identities."""
    if (frame.shape[:2] != (2400, 1080) or not app_version.strip()
            or not evidence_ref.strip() or not math.isfinite(observed_at)):
        return None
    titles = tuple(box for box in boxes if box.text.strip().upper() == "ACCOUNT"
                   and _inside(box, _TITLE))
    identities = tuple((box, match) for box in boxes
                       if _inside(box, _IDENTITY)
                       for match in [_ID.fullmatch(box.text.strip())] if match is not None)
    if (len(titles) != 1 or not _trusted(titles[0])
            or len(identities) != 1 or not _trusted(identities[0][0])):
        return None
    buttons = tuple(box for box in boxes if box.text.strip().lower() == "new account"
                    and _inside(box, _NEW_ACCOUNT))
    controls: dict[str, tuple[int, int]] = {}
    if len(buttons) == 1 and _trusted(buttons[0]):
        rect = buttons[0].rect
        controls["new_account"] = (rect.x + rect.w // 2, rect.y + rect.h // 2)
    return AccountFrame(
        screen="account", account_id=identities[0][1].group(1).upper(),
        app_version=app_version, digest=hashlib.sha256(frame.tobytes()).hexdigest(),
        observed_at=observed_at, evidence_ref=evidence_ref, controls=controls,
        popup_title="ACCOUNT", id_label="ID:",
    )


def parse_new_account_warning(
    frame: Image, boxes: tuple[TextBox, ...], *, observed_at: float,
    app_version: str, evidence_ref: str,
) -> AccountFrame | None:
    """Expose Yes only for the measured New Account logout warning."""
    if (frame.shape[:2] != (2400, 1080) or not app_version.strip()
            or not evidence_ref.strip() or not math.isfinite(observed_at)):
        return None
    identity = tuple((box, match) for box in boxes if _inside(box, _IDENTITY)
                     for match in [_ID.fullmatch(box.text.strip())] if match is not None)
    account_titles = tuple(box for box in boxes if box.text.strip() == "ACCOUNT"
                           and _inside(box, _TITLE))
    if (len(identity) != 1 or not _trusted(identity[0][0])
            or len(account_titles) != 1 or not _trusted(account_titles[0])):
        return None
    matched: dict[str, TextBox] = {}
    for label, bounds in _WARNING_LINES:
        found = tuple(box for box in boxes if box.text.strip() == label
                      and _inside(box, bounds) and _trusted(box))
        if len(found) != 1:
            return None
        matched[label] = found[0]
    yes = matched["Yes"].rect
    return AccountFrame(
        screen="new_account_warning", account_id=identity[0][1].group(1).upper(),
        app_version=app_version, digest=hashlib.sha256(frame.tobytes()).hexdigest(),
        observed_at=observed_at, evidence_ref=evidence_ref,
        controls={"confirm_new_account": (yes.x + yes.w // 2, yes.y + yes.h // 2)},
        popup_title="Warning", id_label="ID:",
    )


def parse_google_play_profile(
    frame: Image, boxes: tuple[TextBox, ...], *, observed_at: float,
    app_version: str, evidence_ref: str,
) -> AccountFrame | None:
    """Expose Cancel only for the measured optional Play Games profile prompt."""
    if (frame.shape[:2] != (2400, 1080) or not app_version.strip()
            or not evidence_ref.strip() or not math.isfinite(observed_at)):
        return None
    found: dict[str, TextBox] = {}
    for layout in (_GOOGLE_PLAY_PROFILE, _GOOGLE_PLAY_NO_PROFILE):
        matches_by_label: dict[str, TextBox] = {}
        for label, bounds in layout:
            matches = tuple(box for box in boxes if box.text.strip() == label
                            and _inside(box, bounds) and _trusted(box))
            if len(matches) != 1:
                break
            matches_by_label[label] = matches[0]
        if len(matches_by_label) == len(layout):
            found = matches_by_label
            break
    if not found:
        return None
    cancel = found["Cancel"].rect
    return AccountFrame(
        screen="google_play_profile", account_id=None, app_version=app_version,
        digest=hashlib.sha256(frame.tobytes()).hexdigest(),
        observed_at=observed_at, evidence_ref=evidence_ref,
        controls={"dismiss_google_play_profile": (
            cancel.x + cancel.w // 2, cancel.y + cancel.h // 2,
        )},
        popup_title="Create a Play Games profile",
    )


def parse_tower_consent(
    frame: Image, boxes: tuple[TextBox, ...], *, observed_at: float,
    app_version: str, evidence_ref: str,
) -> AccountFrame | None:
    """Expose I Agree only on the measured first-launch Tower consent popup."""
    if (frame.shape[:2] != (2400, 1080) or not app_version.strip()
            or not evidence_ref.strip() or not math.isfinite(observed_at)):
        return None
    found: dict[str, TextBox] = {}
    for label, bounds in _TOWER_CONSENT:
        matches = tuple(box for box in boxes if box.text.strip() == label
                        and _inside(box, bounds) and _trusted(box))
        if len(matches) != 1:
            return None
        found[label] = matches[0]
    agree = found["I Agree"].rect
    return AccountFrame(
        screen="tower_consent", account_id=None, app_version=app_version,
        digest=hashlib.sha256(frame.tobytes()).hexdigest(),
        observed_at=observed_at, evidence_ref=evidence_ref,
        controls={"i_agree": (agree.x + agree.w // 2, agree.y + agree.h // 2)},
        popup_title="THETOWER",
    )


def parse_settings(
    frame: Image, boxes: tuple[TextBox, ...], *, observed_at: float,
    app_version: str, evidence_ref: str,
) -> AccountFrame | None:
    """Locate only the measured Settings -> Account row."""
    if frame.shape[:2] != (2400, 1080) or not app_version or not evidence_ref:
        return None
    titles = tuple(box for box in boxes if box.text.strip().upper() == "SETTINGS"
                   and _inside(box, _SETTINGS_TITLE))
    accounts = tuple(box for box in boxes if box.text.strip().lower() == "account"
                     and _inside(box, _SETTINGS_ACCOUNT))
    versions = tuple(box for box in boxes if box.text.strip() == f"v{app_version}"
                     and _inside(box, _VERSION))
    if (any(len(found) != 1 or not _trusted(found[0])
            for found in (titles, accounts, versions))
            or any(box.text.strip().upper() == "ACCOUNT" and _inside(box, _TITLE)
                   for box in boxes)):
        return None
    rect = accounts[0].rect
    return AccountFrame(
        screen="settings", account_id=None, app_version=app_version,
        digest=hashlib.sha256(frame.tobytes()).hexdigest(),
        observed_at=observed_at, evidence_ref=evidence_ref,
        controls={"account": (rect.x + rect.w // 2, rect.y + rect.h // 2)},
    )


def parse_home(
    frame: Image, boxes: tuple[TextBox, ...], cache: vision.TemplateCache, *,
    observed_at: float, app_version: str, evidence_ref: str,
) -> AccountFrame | None:
    """Require the main-menu anchor and one undimmed Settings icon."""
    if frame.shape[:2] != (2400, 1080) or not app_version or not evidence_ref:
        return None
    if any(box.text.strip().upper() in {"ACCOUNT", "SETTINGS"} for box in boxes):
        return None
    if screens.classify(frame, cache).state is not screens.ScreenState.MAIN_MENU:
        return None
    target = account_collection.locate_control(
        frame, cache.get(account_collection.SETTINGS_TEMPLATE), "settings",
    )
    if target.status != "located" or target.point is None:
        return None
    return AccountFrame(
        screen="home", account_id=None, app_version=app_version,
        digest=hashlib.sha256(frame.tobytes()).hexdigest(),
        observed_at=observed_at, evidence_ref=evidence_ref,
        controls={"settings": target.point},
    )


def parse_game_stats_home(
    frame: Image, boxes: tuple[TextBox, ...], cache: vision.TemplateCache, *,
    observed_at: float, app_version: str, evidence_ref: str,
) -> AccountFrame | None:
    """Locate only Home on the measured post-creation Game Stats screen."""
    if frame.shape[:2] != (2400, 1080) or not app_version or not evidence_ref:
        return None
    if screens.classify(frame, cache).state is not screens.ScreenState.GAME_OVER:
        return None
    expected = (("GAMESTATS", _GAME_STATS_TITLE), ("RETRY", _GAME_STATS_RETRY),
                ("HOME", _GAME_STATS_HOME))
    found: dict[str, TextBox] = {}
    for label, bounds in expected:
        candidates = tuple(box for box in boxes if box.text.strip() == label
                           and _inside(box, bounds) and _trusted(box))
        if len(candidates) != 1:
            return None
        found[label] = candidates[0]
    rect = found["HOME"].rect
    return AccountFrame(
        screen="game_over", account_id=None, app_version=app_version,
        digest=hashlib.sha256(frame.tobytes()).hexdigest(),
        observed_at=observed_at, evidence_ref=evidence_ref,
        controls={"home_from_game_over": (rect.x + rect.w // 2, rect.y + rect.h // 2)},
    )


class StagingAccountObserver:
    """Read one leased ADB device frame and retain private, exact evidence."""

    def __init__(self, evidence_root: Path, *, endpoint: str,
                 allowed_versions: frozenset[str]) -> None:
        if not endpoint or not allowed_versions:
            raise ValueError("endpoint and approved app versions are required")
        self.evidence_root = Path(evidence_root)
        self.endpoint = endpoint
        self.allowed_versions = allowed_versions
        self.cache = vision.TemplateCache(config.TEMPLATE_DIR)

    def __call__(self, device: Any) -> AccountFrame:
        if getattr(device, "serial", None) != self.endpoint:
            raise ValueError("observer endpoint mismatch")
        current = device.app_current()
        package = getattr(current, "package", None)
        if package not in {_TOWER_PACKAGE, _GOOGLE_PLAY_SERVICES_PACKAGE}:
            raise ValueError("unexpected game package")
        info = device.app_info(_TOWER_PACKAGE)
        version = getattr(info, "version_name", None)
        if version not in self.allowed_versions:
            raise ValueError("unapproved game version")
        frame = capture_screen(device)
        observed_at = time.time()
        if frame.shape[:2] != (2400, 1080):
            raise ValueError("unsupported native frame geometry")
        boxes = ocr.read(frame, strict=True, min_confidence=0.)
        self.evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded, data = cv2.imencode(".png", frame)
        if not encoded:
            raise ValueError("frame could not be encoded")
        evidence_path = self.evidence_root / f"account-{uuid4().hex}.png"
        descriptor = os.open(evidence_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(data.tobytes())
            file.flush()
            os.fsync(file.fileno())
        evidence_ref = str(evidence_path.resolve())
        text = " ".join(box.text.lower() for box in boxes)
        conflict = next((phrase for phrase in (
            "new session detected", "cloud session different than local session",
        ) if phrase in text), None)
        if conflict is not None:
            return AccountFrame("unknown", None, version,
                                hashlib.sha256(frame.tobytes()).hexdigest(),
                                observed_at, evidence_ref, {}, conflict)
        if package == _GOOGLE_PLAY_SERVICES_PACKAGE:
            prompt = parse_google_play_profile(frame, boxes, observed_at=observed_at,
                                               app_version=version, evidence_ref=evidence_ref)
            if prompt is not None:
                return prompt
            return AccountFrame("unknown", None, version,
                                hashlib.sha256(frame.tobytes()).hexdigest(),
                                observed_at, evidence_ref, {})
        consent = parse_tower_consent(frame, boxes, observed_at=observed_at,
                                      app_version=version, evidence_ref=evidence_ref)
        if consent is not None:
            return consent
        tutorial_claim = workshop_coin_claim(frame, boxes)
        if tutorial_claim is not None:
            return AccountFrame("workshop_tutorial_claim", None, version,
                                hashlib.sha256(frame.tobytes()).hexdigest(),
                                observed_at, evidence_ref, {"claim": tutorial_claim})
        warning = parse_new_account_warning(frame, boxes, observed_at=observed_at,
                                            app_version=version, evidence_ref=evidence_ref)
        if warning is not None:
            return warning
        if ("are you sure" in text or
                (re.search(r"\byes\b", text) and re.search(r"\bno\b", text))):
            return AccountFrame("unknown", None, version,
                                hashlib.sha256(frame.tobytes()).hexdigest(),
                                observed_at, evidence_ref, {}, "ambiguous confirmation")
        for reading in (
            parse_account_popup(frame, boxes, observed_at=observed_at,
                                app_version=version, evidence_ref=evidence_ref),
            parse_settings(frame, boxes, observed_at=observed_at,
                           app_version=version, evidence_ref=evidence_ref),
        ):
            if reading is not None:
                return reading
        home = parse_home(frame, boxes, self.cache, observed_at=observed_at,
                          app_version=version, evidence_ref=evidence_ref)
        if home is not None:
            return home
        game_over = parse_game_stats_home(frame, boxes, self.cache,
                                          observed_at=observed_at,
                                          app_version=version, evidence_ref=evidence_ref)
        if game_over is not None:
            return game_over
        workshop = pages.classify_page(frame, self.cache)
        if workshop.page == "WORKSHOP" and workshop.confidence >= .95:
            titles = tuple(box for box in boxes if box.text.strip() == "WORKSHOP"
                           and _trusted(box) and _inside(box, Rect(0, 65, 400, 130)))
            battle = self.cache.get(config.NAV_TARGETS["BATTLE_TAB"])
            score, position = vision.best_score(frame, battle)
            if (len(titles) == 1 and score >= .95 and position is not None
                    and 0 <= position[0] <= 180 and 2200 <= position[1] <= 2320):
                return AccountFrame("workshop", None, version,
                                    hashlib.sha256(frame.tobytes()).hexdigest(),
                                    observed_at, evidence_ref,
                                    {"battle_tab": (position[0] + battle.shape[1] // 2,
                                                    position[1] + battle.shape[0] // 2)})
        return AccountFrame("unknown", None, version,
                            hashlib.sha256(frame.tobytes()).hexdigest(),
                            observed_at, evidence_ref, {})
