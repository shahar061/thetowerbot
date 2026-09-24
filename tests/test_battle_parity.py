"""Spec P2 parity: the two-band battle read parses as the full-frame read does.

Real engine. Each pipeline starts from empty OCR caches: RapidOCR batches
recognition crops, and a cached crop keeps its first batch's answer, so a
warm cache would make the result depend on test order.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from pathlib import Path

import cv2
import pytest

import ocr
import screens
from perception import observe_frame
from tests.parity import observation_diff

FIXTURES = Path(__file__).parent / "fixtures"
IN_RUN = sorted(p.stem for p in FIXTURES.glob("in_run_*.png"))
# Fields the full-frame read leaves unknown that the band read fills, pinned
# as measured when P2 was planned. Anything else - in either direction -
# fails. Do not re-pin to make this pass: stop and report instead.
KNOWN_GAINS: dict[str, set[str]] = {
    "in_run_wallet_no_cutout": {"rows.attack_speed.price", "rows.attack_speed.status",
                                "rows.attack_speed.tap"},
}
# The value each pinned gain must read, not just that it changed.
GAINED_PRICES: dict[str, dict[str, int]] = {
    "in_run_wallet_no_cutout": {"attack_speed": 21},
}


def cold_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_frame_results", "_region_results", "_crop_results"):
        monkeypatch.setattr(ocr, name, OrderedDict())


@pytest.mark.parametrize("name", IN_RUN)
def test_two_band_battle_read_parses_what_the_full_frame_read_does(
    name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = cv2.imread(str(FIXTURES / f"{name}.png"), cv2.IMREAD_COLOR)
    cold_ocr(monkeypatch)
    old = observe_frame(frame, "battle")
    cold_ocr(monkeypatch)
    reads = ocr.FrameReads(frame)
    new = observe_frame(frame, "battle", reads=reads)
    assert reads._full is None, "the battle path must not fall back to the full read"
    changes, gains = observation_diff(old, new)
    assert changes == set()
    assert gains == KNOWN_GAINS.get(name, set())
    rows = {row.upgrade_id: row for row in new.rows}
    for upgrade_id, price in GAINED_PRICES.get(name, {}).items():
        assert rows[upgrade_id].price == price


# Named battle_popup_*, not in_run_*: the panel classifies these frames as
# IN_RUN (spec P2's risk case is a recovery modal drawn over an otherwise
# ordinary battle frame), but an in_run_* glob is what Tasks 12/14/16's
# fixtures and tests match on, and a popup fixture must never be picked up
# by those.
POPUPS = sorted(p.stem for p in FIXTURES.glob("battle_popup_*.png"))


@pytest.mark.skipif(
    not POPUPS,
    reason=(
        "no popup-over-battle fixture captured yet (spec P2 Risks). To capture one: "
        "when an online-required or session-conflict popup appears over a battle on a "
        "worker, `adb -s 127.0.0.1:<port> exec-out screencap -p > "
        "tests/fixtures/battle_popup_<name>.png` then `.venv/bin/python tools/record_ocr.py "
        "tests/fixtures/battle_popup_<name>.png`."
    ),
)
@pytest.mark.parametrize("name", POPUPS or ["none"])
def test_a_popup_over_a_battle_reaches_the_preflight(name: str, bot_in_run_on) -> None:
    """The spec's risk case: a popup mid-screen. Either the safety net fires
    on this frame, or the backstop's full read names the popup."""
    from tower_bot import popup_flags
    bot = bot_in_run_on(name)
    reading = screens.ScreenReading(screens.ScreenState.IN_RUN, 1.0, {})
    expected = popup_flags(ocr.FrameReads(bot._screen).full())
    assert any(expected), "a popup fixture must show a recovery modal"
    bot._battle_full_read_at = time.monotonic()
    between_backstops = popup_flags(bot._preflight_boxes(reading, ocr.FrameReads(bot._screen)))
    visible = bot._battle_panel_visible(ocr.FrameReads(bot._screen))
    if not visible:
        # The bands show no upgrade panel, so the P1 safety net - not the
        # backstop timer - must have routed this frame to the full read.
        assert between_backstops == expected, (
            "the safety net should have sent this frame to the full read")
    # Else the popup leaves the panel readable: the bands alone are not
    # guaranteed to catch it between backstops, so detection here is bounded
    # by the BATTLE_FULL_READ_EVERY backstop - the unconditional
    # at_backstop == expected assertion below is the guarantee.
    bot._battle_full_read_at = float("-inf")
    at_backstop = popup_flags(bot._preflight_boxes(reading, ocr.FrameReads(bot._screen)))
    assert at_backstop == expected
