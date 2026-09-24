"""Spec P4 parity: coarse-then-fine anchors classify every fixture as the
full search does. About a minute: the full search is the slow half."""
from __future__ import annotations

from pathlib import Path

import cv2
import pytest

import config
import pages
import run_hud
import screens
import vision

FIXTURES = Path(__file__).parent / "fixtures"
EVERY_FRAME = sorted(FIXTURES.glob("*.png")) + sorted((FIXTURES / "account_screens").glob("*.png"))


def _full_search(screen, template, **_kwargs):
    return vision.best_score(screen, template)


def _close(new: tuple[int, int] | None, old: tuple[int, int] | None) -> bool:
    if old is None or new is None:
        return new is old
    return max(abs(a - b) for a, b in zip(new, old)) <= 1


def test_classification_goes_through_the_two_step_matcher(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    monkeypatch.setattr(vision, "two_step_score",
                        lambda screen, template, **kw: (seen.append(1), (0.0, (0, 0)))[1])
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    image = cv2.imread(str(FIXTURES / "menu_main.png"), cv2.IMREAD_COLOR)
    screens.classify(image, cache)
    pages.classify_page(image, cache)
    assert len(seen) == len(config.SCREEN_ANCHORS) + len(config.PAGE_ANCHORS)


@pytest.mark.parametrize("path", EVERY_FRAME, ids=lambda p: p.name)
def test_two_step_classification_matches_the_full_search(
    path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    new_screen, new_page = screens.classify(image, cache), pages.classify_page(image, cache)
    with monkeypatch.context() as patched:
        patched.setattr(vision, "two_step_score", _full_search)
        old_screen, old_page = screens.classify(image, cache), pages.classify_page(image, cache)
    assert new_screen.state is old_screen.state
    assert new_page.page == old_page.page
    assert _close(new_screen.top_left, old_screen.top_left)
    assert _close(new_page.top_left, old_page.top_left)
    assert new_screen.cash_top_left == old_screen.cash_top_left
    for new, old in ((new_screen.scores, old_screen.scores), (new_page.scores, old_page.scores)):
        for name, score in old.items():
            if score >= config.ANCHOR_THRESHOLD - 0.1:
                assert new[name] == pytest.approx(score, abs=0.01), name
            else:
                assert new[name] < config.ANCHOR_THRESHOLD, name


def test_find_cash_still_searches_its_region_at_full_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant 1."""
    monkeypatch.setattr(vision, "two_step_score", lambda *a, **k: pytest.fail("find_cash went coarse"))
    image = cv2.imread(str(FIXTURES / "in_run_lit.png"), cv2.IMREAD_COLOR)
    assert run_hud.find_cash(image, vision.TemplateCache(config.TEMPLATE_DIR)).top_left is not None
