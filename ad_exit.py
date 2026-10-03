"""Locate a rewarded ad's exit without depending on the creative's artwork.

Ad SDK controls are often accessible even when their pixels change. Inspect
the foreground ad's accessibility tree first; keep the witnessed image
templates for ad providers that do not expose a labelled control.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

import cv2

import battle_menu
from device import Image
from vision import TemplateCache


_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_AD_ACTIVITY = ("adactivity", "rewardedactivity", "interstitialactivity")
_CLOSE_LABELS = {"close", "close ad", "close video", "dismiss", "dismiss ad"}
_TOP_LEFT_TEMPLATE = "in_game_ad/end_close_top_left.png"


def _ad_has_focus(window_dump: str) -> bool:
    """Require the foreground window to belong to an ad Activity."""
    focus = next((line.lower() for line in window_dump.splitlines()
                  if "mCurrentFocus=" in line), "")
    return any(name in focus for name in _AD_ACTIVITY)


def ad_foreground(device: Any) -> bool:
    """Whether Android reports a full-screen ad above the game."""
    read_window = getattr(device, "ad_window_state", None)
    if not callable(read_window):
        shell = getattr(device, "shell", None)
        if not callable(shell):
            return False
        read_window = lambda: shell("dumpsys window")
    try:
        return _ad_has_focus(read_window())
    except Exception:  # noqa: BLE001 - an ADB failure is not evidence of an ad
        return False


def _accessible_close(hierarchy: str, screen: Image) -> tuple[str, tuple[int, int] | None]:
    """Read small labelled close controls at an ad viewport's outer edge."""
    end = hierarchy.find("</hierarchy>")
    if end < 0:
        return ("absent", None)
    try:
        root = ET.fromstring(hierarchy[:end + len("</hierarchy>")])
    except ET.ParseError:
        return ("absent", None)
    nodes = list(root.iter("node"))
    width, height = screen.shape[1], screen.shape[0]
    for node in nodes:
        match = _BOUNDS.fullmatch(node.get("bounds", ""))
        if match and int(match[1]) == int(match[2]) == 0:
            width, height = int(match[3]), int(match[4])
            break
    points: list[tuple[int, int]] = []
    for node in nodes:
        if node.get("clickable") != "true":
            continue
        labels = {" ".join(value.lower().split())
                  for child in node.iter()
                  for value in (child.get("text", ""), child.get("content-desc", ""))}
        resource = node.get("resource-id", "").rsplit("/", 1)[-1].lower()
        if not (labels & _CLOSE_LABELS) and resource not in {"close", "close_button", "ad_close"}:
            continue
        bounds = _BOUNDS.fullmatch(node.get("bounds", ""))
        if bounds is None:
            continue
        x0, y0, x1, y1 = map(int, bounds.groups())
        x, y = (x0 + x1) // 2, (y0 + y1) // 2
        if (not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height)
                or x1 - x0 > width * .22 or y1 - y0 > height * .13
                or not (x < width * .27 or x > width * .73)
                or not (y < height * .2 or y > height * .8)):
            continue
        if all(abs(x - px) > 40 or abs(y - py) > 40 for px, py in points):
            points.append((x, y))
    if len(points) > 1:
        return ("ambiguous", None)
    if points:
        return ("located", points[0])
    return ("absent", None)


def find_close(screen: Image, templates: TemplateCache, device: Any) -> tuple[int, int] | None:
    """Return one witnessed close button, or None when evidence is unclear."""
    read_hierarchy = getattr(device, "ad_accessibility_hierarchy", None)
    if not callable(read_hierarchy):
        shell = getattr(device, "shell", None)
        if callable(shell):
            read_hierarchy = lambda: shell("uiautomator dump /dev/tty")
    if callable(read_hierarchy) and ad_foreground(device):
        try:
            status, point = _accessible_close(read_hierarchy(), screen)
            if status == "ambiguous":
                return None
            if point is not None:
                return point
        except Exception:  # noqa: BLE001 - ADB and UI hierarchy failures need visual fallback
            pass
    close = battle_menu.ad_end_card_close(screen, templates)
    if close is not None:
        return close
    template = templates.get(_TOP_LEFT_TEMPLATE)
    if template is None:
        return None
    region = screen[:260, :220]
    if region.shape[0] < template.shape[0] or region.shape[1] < template.shape[1]:
        return None
    _, score, _, (x, y) = cv2.minMaxLoc(
        cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))
    if score < .82:
        return None
    return (x + template.shape[1] // 2, y + template.shape[0] // 2)
