"""The main menu's Events icon - the calendar below the tournament trophy.

`located` finds the icon itself; `badge_visible` says whether its red dot is
lit at the icon's top-right corner. The template stops short of the dot, so
one template finds the icon with and without it (and the padlocked icon of an
account that has not unlocked Events, which never carries a dot).

The game lights the dot while a mission tier is claimable. The claim can be
anywhere down a list that outgrows the screen - once measured twenty cards
below the opening frame - which is why the walk scrolls the whole list.
"""

from __future__ import annotations

from account_collection import locate_control
from account_screens import ControlTarget
from device import Image
from milestones_badge import red_pixels
from vision import TemplateCache

EVENTS_TEMPLATE = 'nav/events.png'
# Relative to the template's top-left. The recorded dot spans dx 100..133,
# dy 2..35 (a ~33 px disc, ~870 red pixels); the window leaves margin on
# every side and reaches nothing but the icon's own blue art.
DOT_DX, DOT_DY, DOT_W, DOT_H = 90, -12, 60, 60
DOT_MIN_PIXELS = 300


def locate(screen: Image, templates: TemplateCache) -> ControlTarget:
    try:
        template = templates.get(EVENTS_TEMPLATE)
    except (OSError, ValueError, AttributeError):
        template = None
    return locate_control(screen, template, 'events_control', .9)


def badge_visible(screen: Image, templates: TemplateCache) -> bool | None:
    """True/False for the dot beside a located Events icon, else None."""
    control = locate(screen, templates)
    if control.status != 'located' or control.rect is None:
        return None
    x, y = control.rect[0] + DOT_DX, control.rect[1] + DOT_DY
    height, width = screen.shape[:2]
    patch = screen[max(0, y):min(height, y + DOT_H), max(0, x):min(width, x + DOT_W)]
    if patch.size == 0:
        return None
    return red_pixels(patch) >= DOT_MIN_PIXELS
