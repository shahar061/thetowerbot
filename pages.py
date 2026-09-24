"""Which MENU page is on screen?

Deliberately not screens.py. That module models the run lifecycle as an enum
and does ScreenState(winner), which raises on any name the enum lacks - so a
menu page can never become a member of it without turning every workshop
frame into a crash. config.py has said so since the anchors were cut; this is
the module that makes use of them without breaking that rule.

Only consulted while a shopping visit is live. Outside one, a menu page
reading UNKNOWN to the screen tracker is correct behaviour, not a gap.
"""

from __future__ import annotations

from dataclasses import dataclass

import config
import vision
from device import Image

UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PageReading:
    """One frame's page classification, with every anchor's score.

    `scores` is kept for the same reason screens.ScreenReading keeps it: when
    a page stops being recognised, the useful question is what it nearly was.
    """

    page: str
    confidence: float
    scores: dict[str, float]
    top_left: tuple[int, int] | None = None


def classify_page(
    screen: Image,
    cache: vision.TemplateCache,
    threshold: float = config.ANCHOR_THRESHOLD,
) -> PageReading:
    """Best-scoring menu page, or UNKNOWN below threshold.

    Every anchor over the whole frame, coarse-then-fine like screens.classify().
    """
    scores: dict[str, float] = {}
    positions: dict[str, tuple[int, int]] = {}

    coarse = vision.coarse_image(screen)
    for name, template_path in config.PAGE_ANCHORS.items():
        score, top_left = vision.two_step_score(
            screen, cache.get(template_path),
            coarse_screen=coarse, coarse_template=cache.coarse(template_path))
        scores[name] = score
        positions[name] = top_left

    winner = max(scores, key=lambda name: scores[name])
    confidence = scores[winner]

    if confidence < threshold:
        return PageReading(UNKNOWN, confidence, scores)

    return PageReading(winner, confidence, scores, top_left=positions[winner])
