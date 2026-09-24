"""Recognise the in-battle upgrade explanation overlay before any game action."""

from __future__ import annotations

from collections.abc import Sequence

import ocr


def dismiss_point(
    boxes: Sequence[ocr.TextBox], frame_shape: tuple[int, ...],
) -> tuple[int, int] | None:
    """Return an inert point left of a measured Current/Max Level popup.

    The game can draw this panel over a still-visible battle HUD or death
    dialog. Two labels in the centre, in their known vertical order, are the
    evidence; a dim frame or a surviving HUD alone is never permission to tap.
    The caller must additionally establish a battle/death context.
    """
    height, width = frame_shape[:2]
    labels = {box.text.strip().casefold(): box for box in boxes if box.confidence >= .85}
    current = labels.get("current level")
    maximum = labels.get("max level")
    if current is None or maximum is None:
        return None
    if not (.15 * width <= current.rect.x <= .65 * width
            and .25 * height <= current.rect.y <= .7 * height
            and .15 * width <= maximum.rect.x <= .65 * width
            and 0 < maximum.rect.y - current.rect.y <= .12 * height
            and abs(maximum.rect.x - current.rect.x) <= .2 * width):
        return None
    return round(width * .06), round(height * .45)


def death_controls_visible(boxes: Sequence[ocr.TextBox], frame_shape: tuple[int, ...]) -> bool:
    """The two dimmed death buttons distinguish this from a Workshop popup."""
    height, width = frame_shape[:2]
    labels = {box.text.strip().casefold(): box for box in boxes if box.confidence >= .85}
    retry, home = labels.get("retry"), labels.get("home")
    return bool(
        retry is not None and home is not None
        and .6 * height <= retry.rect.y <= .9 * height
        and .6 * height <= home.rect.y <= .9 * height
        and retry.rect.x < .5 * width < home.rect.x
        and abs(retry.rect.y - home.rect.y) <= .05 * height
    )
