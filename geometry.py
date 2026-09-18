"""Frame-local input geometry for measured BlueStacks portrait layouts.

The game keeps its header at the top, centres reward ceremonies, and pins
footer navigation to the bottom when the display height changes. A single
percentage scale cannot map all three. Most taps should use an OCR or template
match on the current frame; these anchors are for the few measured offsets.
"""

from __future__ import annotations

from typing import Any, Literal

REFERENCE_HEIGHT = 2400
SUPPORTED_SIZES = frozenset({(1080, 1920), (1080, 2400)})
VerticalAnchor = Literal['top', 'center', 'bottom']


def supported_frame(width: int, height: int) -> bool:
    """Whether this framebuffer size has measured tap geometry."""
    return (width, height) in SUPPORTED_SIZES


def anchored_y(reference_y: int, height: int, anchor: VerticalAnchor) -> int:
    """Map a measured 2400-height y to a matching vertical anchor."""
    if not supported_frame(1080, height):
        raise ValueError(f'unsupported frame height: {height}')
    delta = height - REFERENCE_HEIGHT
    if anchor == 'top':
        return reference_y
    if anchor == 'center':
        return reference_y + delta // 2
    if anchor == 'bottom':
        return reference_y + delta
    raise ValueError(f'unknown vertical anchor: {anchor}')


def anchored_point(frame: Any, reference_point: tuple[int, int],
                   anchor: VerticalAnchor) -> tuple[int, int]:
    """Resolve a measured point against the captured Android framebuffer."""
    height, width = frame.shape[:2]
    if not supported_frame(width, height):
        raise ValueError(f'unsupported frame size: {width}x{height}')
    x, y = reference_point
    mapped_y = anchored_y(y, height, anchor)
    if not 0 <= x < width or not 0 <= mapped_y < height:
        raise ValueError('tap point outside framebuffer')
    return x, mapped_y
