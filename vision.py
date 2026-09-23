"""Vision layer: template loading and matching.

    screen capture  ->  device.capture_screen()
    template match  ->  cv2.matchTemplate
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

import cv2

import config
from device import Image

logger = logging.getLogger("tower_bot")


class Match(NamedTuple):
    """Where a template was found on screen, and how well it scored."""

    center: tuple[int, int]
    score: float
    top_left: tuple[int, int]


class TemplateCache:
    """Loads template images once and keeps them in memory."""

    def __init__(self, template_dir: Path) -> None:
        self._dir = template_dir
        self._cache: dict[str, Image] = {}
        self._coarse: dict[str, Image] = {}

    def get(self, template_path: str | Path) -> Image:
        key = str(template_path)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        path = Path(template_path)
        if not path.is_absolute() and not path.exists():
            path = self._dir / path
        if not path.exists():
            raise FileNotFoundError(f"Template not found: {path}")

        template = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if template is None:
            raise ValueError(f"Could not read template image: {path}")

        self._cache[key] = template
        logger.debug("Loaded template %s (%dx%d)", path.name, template.shape[1], template.shape[0])
        return template

    def coarse(self, template_path: str | Path) -> Image:
        """The template as two_step_score's coarse pass sees it, built once."""
        key = str(template_path)
        cached = self._coarse.get(key)
        if cached is None:
            cached = self._coarse[key] = coarse_image(self.get(template_path))
        return cached


def best_score(screen: Image, template: Image) -> tuple[float, tuple[int, int]]:
    """Best match score and its top-left position, ignoring any threshold."""
    result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    return float(max_val), max_loc


def coarse_image(image: Image) -> Image:
    """Greyscale at half size: two_step_score's cheap first pass."""
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return cv2.resize(grey, (max(grey.shape[1] // 2, 1), max(grey.shape[0] // 2, 1)),
                      interpolation=cv2.INTER_AREA)


def two_step_score(
    screen: Image, template: Image, *, coarse_screen: Image | None = None,
    coarse_template: Image | None = None, margin: int | None = None,
) -> tuple[float, tuple[int, int]]:
    """best_score's answer, found coarse-then-fine.

    The coarse pass only chooses where to look; the returned score and
    top-left come from full-resolution BGR TM_CCOEFF_NORMED in a window
    around it, so thresholds and tap coordinates keep their meaning. A
    template too small to halve, or larger than the frame, gets the plain
    full search.
    """
    margin = config.ANCHOR_FINE_MARGIN if margin is None else margin
    height, width = screen.shape[:2]
    tpl_h, tpl_w = template.shape[:2]
    small_screen = coarse_image(screen) if coarse_screen is None else coarse_screen
    small_template = coarse_image(template) if coarse_template is None else coarse_template
    if (min(small_template.shape[:2]) < 4 or tpl_h > height or tpl_w > width
            or small_template.shape[0] > small_screen.shape[0]
            or small_template.shape[1] > small_screen.shape[1]):
        return best_score(screen, template)
    result = cv2.matchTemplate(small_screen, small_template, cv2.TM_CCOEFF_NORMED)
    _, _, _, (cx, cy) = cv2.minMaxLoc(result)
    x0, y0 = max(cx * 2 - margin, 0), max(cy * 2 - margin, 0)
    x1, y1 = min(cx * 2 + tpl_w + margin, width), min(cy * 2 + tpl_h + margin, height)
    score, (fx, fy) = best_score(screen[y0:y1, x0:x1], template)
    return score, (fx + x0, fy + y0)


def locate_template(screen: Image, template: Image, threshold: float) -> Match | None:
    """Return the best match above threshold, or None."""
    screen_h, screen_w = screen.shape[:2]
    tpl_h, tpl_w = template.shape[:2]
    if tpl_h > screen_h or tpl_w > screen_w:
        logger.warning(
            "Template (%dx%d) is larger than the screen (%dx%d) - was it "
            "captured at a different emulator resolution?",
            tpl_w, tpl_h, screen_w, screen_h,
        )
        return None

    score, top_left = best_score(screen, template)
    if score < threshold:
        return None

    center = (top_left[0] + tpl_w // 2, top_left[1] + tpl_h // 2)
    return Match(center=center, score=score, top_left=top_left)


def mean_brightness(image: Image) -> float:
    """Mean grey level of ``image`` (0-255)."""
    return float(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).mean())


def brightness_ratio(screen: Image, match: Match, template: Image) -> float:
    """Brightness of the matched region relative to the template's own.

    TM_CCOEFF_NORMED is blind to brightness, so a dimmed button scores the
    same as a lit one. This is what tells them apart.
    """
    tpl_h, tpl_w = template.shape[:2]
    x, y = match.top_left
    region = screen[y : y + tpl_h, x : x + tpl_w]
    template_level = mean_brightness(template)
    if template_level <= 0.0:  # all-black template: nothing to compare against
        return 1.0
    return mean_brightness(region) / template_level
