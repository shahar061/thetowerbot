"""Measured vertical layout changes between the two BlueStacks sizes."""

import numpy as np
import pytest

from geometry import anchored_point, anchored_y, supported_frame


def test_anchored_positions_match_real_milestones_captures() -> None:
    assert anchored_y(255, 1920, 'top') == 255
    assert anchored_y(1860, 1920, 'center') == 1620
    assert anchored_y(2270, 1920, 'bottom') == 1790
    assert anchored_y(255, 2400, 'top') == 255
    assert anchored_y(1860, 2400, 'center') == 1860
    assert anchored_y(2270, 2400, 'bottom') == 2270


def test_only_measured_frame_sizes_are_tappable() -> None:
    assert supported_frame(1080, 1920)
    assert supported_frame(1080, 2400)
    assert not supported_frame(1080, 2160)
    assert not supported_frame(720, 1280)


def test_a_top_anchored_tap_uses_the_live_frame_without_stretching() -> None:
    frame = np.zeros((1920, 1080, 3), dtype=np.uint8)
    assert anchored_point(frame, (180, 265), 'top') == (180, 265)


def test_a_bottom_anchored_tap_follows_the_live_footer() -> None:
    frame = np.zeros((1920, 1080, 3), dtype=np.uint8)
    assert anchored_point(frame, (540, 2270), 'bottom') == (540, 1790)


def test_an_unmeasured_or_outside_tap_is_refused() -> None:
    with pytest.raises(ValueError, match='unsupported frame size'):
        anchored_point(np.zeros((2160, 1080, 3), dtype=np.uint8), (180, 265), 'top')
    with pytest.raises(ValueError, match='outside framebuffer'):
        anchored_point(np.zeros((1920, 1080, 3), dtype=np.uint8), (1200, 265), 'top')
