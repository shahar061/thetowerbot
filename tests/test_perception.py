from __future__ import annotations

import json
from pathlib import Path

import cv2
import pytest

import config
import ocr

FIXTURES = Path(__file__).parent / "fixtures"


def recorded(name: str) -> tuple[ocr.TextBox, ...]:
    return tuple(ocr.TextBox(b["text"], b["confidence"], config.Rect(*b["rect"]))
                 for b in json.loads((FIXTURES / "ocr" / f"{name}.json").read_text()))


def test_battle_values_are_distinct_from_costs_and_combat() -> None:
    from perception import parse_frame
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    result = parse_frame(frame, recorded("in_run_lit"), "battle", now=100)
    rows = {r.upgrade_id: r for r in result.rows}
    assert result.category == "ATTACK"
    assert rows["critical_chance"].value == 1
    assert rows["critical_chance"].price == 4
    assert rows["critical_factor"].value == 1.2
    assert result.combat["wave"] == 1
    assert result.combat["health"] == 5
    assert result.combat["max_health"] == 5
    assert result.combat["enemy_damage"] == 1.18
    # The OCR read this as reversed "68 $". Refusing is better than spending.
    assert result.cash is None


@pytest.mark.parametrize("text,want", [("1.20K",1200),("x1.20",1.2),("51%",51),
                                      ("2.3T",2.3e12),("10.0s",10),("5 sec",5),
                                      ("MAX",None),("NaN",None)])
def test_stat_number_handles_units_without_guessing(text: str, want: float | None) -> None:
    from perception import stat_number
    assert stat_number(text) == want


def test_duration_stat_is_not_a_purchase_price() -> None:
    from perception import price_number
    assert price_number("10.0s") is None


def test_max_and_lock_markers_do_not_become_part_of_upgrade_name() -> None:
    from perception import parse_frame
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    boxes = tuple(ocr.TextBox("MAX" if b.text == "$4" else b.text, b.confidence, b.rect)
                  for b in recorded("in_run_lit"))
    result = parse_frame(frame, boxes, "battle", now=100)
    row = next(r for r in result.rows if r.upgrade_id == "critical_chance")
    assert row.status == "maxed"
    assert row.price is None


def test_workshop_unlock_keeps_its_own_identity() -> None:
    from perception import parse_frame
    frame = cv2.imread(str(FIXTURES / "menu_workshop_utility.png"))
    result = parse_frame(frame, recorded("menu_workshop_utility"), "workshop", now=100)
    assert result.rows[0].upgrade_id == "unlock_cash_bonuses"
    assert result.rows[0].price == 40
    assert result.rows[0].value is None


def test_observations_keep_unknown_ocr_visible_without_canonical_execution_identity() -> None:
    from perception import parse_frame
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    boxes = tuple(ocr.TextBox("Unfamiliar Power" if b.text == "Damage" else b.text,
                              b.confidence, b.rect) for b in recorded("in_run_lit"))
    result = parse_frame(frame, boxes, "battle", now=100)
    unknown = next(r for r in result.rows if r.name == "Unfamiliar Power")
    assert unknown.upgrade_id == "discovered:unfamiliarpower"
    assert unknown.payload()["concept_id"] is None
    known = next(r for r in result.rows if r.upgrade_id == "critical_chance")
    assert known.payload()["concept_id"] == "stats.critical_chance"


def test_wallet_crop_ocr_reads_recorded_balance_without_a_digit_atlas() -> None:
    from perception import read_cash
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    assert read_cash(frame, (12, 1646)) == 89


def test_wallet_ocr_refuses_ambiguous_or_low_confidence_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    from perception import read_cash
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    box = config.Rect(1, 1, 30, 30)
    monkeypatch.setattr(ocr, "read", lambda image: (ocr.TextBox("$89", .89, box),))
    assert read_cash(frame, (12, 1646)) is None
    monkeypatch.setattr(ocr, "read", lambda image: (ocr.TextBox("8", .99, box), ocr.TextBox("9", .99, box)))
    assert read_cash(frame, (12, 1646)) is None


def test_frame_evidence_is_honest_and_low_confidence_tile_blocks_promotion(monkeypatch: pytest.MonkeyPatch) -> None:
    from perception import parse_frame
    import tiles
    import numpy as np
    screen = np.zeros((250, 250, 3), dtype=np.uint8)
    tile = config.Rect(10, 60, 220, 130)
    monkeypatch.setattr(tiles, 'find_tiles', lambda image: (tile,))
    boxes = (ocr.TextBox('Defense Upgrades', .99, config.Rect(5, 5, 200, 30)),
             ocr.TextBox('Health', .99, config.Rect(20, 70, 70, 20)),
             ocr.TextBox('Regen', .4, config.Rect(20, 95, 70, 20)),
             ocr.TextBox('100', .98, config.Rect(160, 75, 50, 20)),
             ocr.TextBox('10', .96, config.Rect(160, 165, 50, 20)))
    result = parse_frame(screen, boxes, 'workshop', now=1.)
    assert result.context == 'workshop'
    assert len(result.frame_digest) == 64
    assert (result.frame_width, result.frame_height) == (250, 250)
    assert result.rows[0].confidence == .4
    assert result.rows[0].raw_value == '100'


@pytest.mark.parametrize("name,speed,regen,paused", [
    ("in_run_lit", 1., 0., False),
    ("in_run_early", 1.5, .04, False),
    ("in_run_attack_paused", 0., .04, True),
    ("in_run_defense", 0., .04, True),
])
def test_hud_speed_recovery_and_pause_are_read_from_recorded_evidence(
    name: str, speed: float, regen: float, paused: bool
) -> None:
    from perception import parse_frame
    frame = cv2.imread(str(FIXTURES / f"{name}.png"))
    result = parse_frame(frame, recorded(name), "battle", now=100)
    assert result.combat["game_speed"] == speed
    assert result.combat["health_regen"] == pytest.approx(regen)
    assert result.paused is paused


def test_hud_readers_refuse_a_band_holding_two_candidates() -> None:
    """A second x-multiplier or rate in the band means it is not the widget."""
    from perception import parse_frame
    frame = cv2.imread(str(FIXTURES / "in_run_early.png"))
    boxes = recorded("in_run_early")
    speed = next(b for b in boxes if b.text == "x1.5")
    rate = next(b for b in boxes if b.text == "0.04/s")
    doubled = (*boxes,
               ocr.TextBox("x2.0", .99, config.Rect(speed.rect.x - 200, speed.rect.y, *speed.rect[2:])),
               ocr.TextBox("0.09/s", .99, config.Rect(rate.rect.x, rate.rect.y + 20, *rate.rect[2:])))
    result = parse_frame(frame, doubled, "battle", now=100)
    assert "game_speed" not in result.combat  # absent, never averaged or zeroed
    assert "health_regen" not in result.combat


def test_pause_is_unknown_on_a_frame_that_never_identified_itself() -> None:
    from perception import parse_frame
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    result = parse_frame(frame, (), "battle", now=100)
    assert result.paused is None  # not False: nothing was read


def test_a_single_digit_value_the_frame_read_misses_is_re_read_off_a_crop() -> None:
    """Damage "9" gets no box from a whole-frame read, and a row without a
    value is never bought - so a fresh account's battles bought only Attack
    Speed. observe_frame re-reads that value off a padded crop."""
    from perception import observe_frame, parse_frame
    frame = cv2.imread(str(FIXTURES / "in_run_damage_single_digit.png"))
    whole_frame = {r.upgrade_id: r for r in parse_frame(frame, ocr.read(frame), "battle").rows}
    assert whole_frame["damage"].value is None
    rows = {r.upgrade_id: r for r in observe_frame(frame, "battle").rows}
    assert rows["damage"].value == 9
    assert rows["damage"].price == 12
    assert rows["damage"].status == "available"
    assert rows["attack_speed"].value == 1.0


def test_parse_frame_uses_a_supplied_digest_instead_of_hashing() -> None:
    from perception import parse_frame
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    result = parse_frame(frame, recorded("in_run_lit"), "battle", now=100, digest="d" * 64)
    assert result.frame_digest == "d" * 64


def test_observe_frame_with_the_scans_reads_parses_the_same_frame() -> None:
    from perception import observe_frame
    from tests.parity import observation_diff
    frame = cv2.imread(str(FIXTURES / "in_run_damage_single_digit.png"))
    reads = ocr.FrameReads(frame)
    shared = observe_frame(frame, "battle", reads=reads)
    alone = observe_frame(frame, "battle")
    assert observation_diff(alone, shared) == (set(), set())
    assert shared.frame_digest == reads.digest == alone.frame_digest


def test_reads_for_another_frame_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    from perception import observe_frame
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    other = ocr.FrameReads(frame.copy())
    monkeypatch.setattr(other, "full", lambda: pytest.fail("used another frame's reads"))
    monkeypatch.setattr(other, "battle", lambda: pytest.fail("used another frame's reads"))
    assert observe_frame(frame, "battle", reads=other).category == "ATTACK"


def test_a_failed_shared_read_degrades_to_an_unread_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    from perception import observe_frame
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    reads = ocr.FrameReads(frame)

    def fail() -> tuple:
        raise RuntimeError("OCR inference failed")

    monkeypatch.setattr(reads, "full", fail)
    monkeypatch.setattr(reads, "battle", fail)
    result = observe_frame(frame, "battle", reads=reads)
    assert result.category is None and result.rows == ()


def test_the_panel_is_visible_when_exactly_one_heading_was_read(monkeypatch: pytest.MonkeyPatch) -> None:
    import battle_tab
    from perception import panel_visible
    monkeypatch.setattr(battle_tab, "classify_frame", lambda screen: None)
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    boxes = recorded("in_run_lit")
    heading = next(b for b in boxes if b.text == "ATTACKUPGRADES")
    assert panel_visible(frame, boxes)
    assert not panel_visible(frame, (heading, heading))
    assert not panel_visible(frame, tuple(ocr.TextBox(b.text, .5, b.rect) if b is heading else b
                                          for b in boxes))


def _without_heading(name: str) -> tuple[ocr.TextBox, ...]:
    return tuple(b for b in recorded(name) if "UPGRADES" not in b.text.upper())


def test_colour_that_agrees_with_the_heading_reads_as_today() -> None:
    from perception import parse_frame
    from tests.parity import observation_diff
    frame = cv2.imread(str(FIXTURES / "in_run_defense.png"))
    today = parse_frame(frame, recorded("in_run_defense"), "battle", now=1)
    agreed = parse_frame(frame, recorded("in_run_defense"), "battle", now=1, tab_colour="DEFENSE")
    assert agreed.category == "DEFENSE"
    assert observation_diff(today, agreed) == (set(), set())


@pytest.mark.parametrize("name,size", [("in_run_defense", (1080, 2400)),
                                       ("in_run_defense_1920", (1080, 1920))])
def test_colour_alone_names_the_tab_and_places_the_heading_from_config(
    name: str, size: tuple[int, int],
) -> None:
    from perception import parse_frame
    from tests.parity import observation_diff
    frame = cv2.imread(str(FIXTURES / f"{name}.png"))
    today = parse_frame(frame, recorded(name), "battle", now=1)
    colour_only = parse_frame(frame, _without_heading(name), "battle", now=1, tab_colour="DEFENSE")
    assert colour_only.category == "DEFENSE"
    assert colour_only.heading_y == config.BATTLE_BANDS[size].heading_y
    assert observation_diff(today, colour_only) == (set(), set())


def test_colour_that_disagrees_with_the_heading_refuses_the_frame() -> None:
    from perception import parse_frame
    frame = cv2.imread(str(FIXTURES / "in_run_defense.png"))
    result = parse_frame(frame, recorded("in_run_defense"), "battle", now=1, tab_colour="ATTACK")
    assert result.category is None and result.rows == ()


def test_observe_frame_falls_back_to_colour_when_ocr_misses_the_heading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from perception import observe_frame
    frame = cv2.imread(str(FIXTURES / "in_run_defense.png"))
    reads = ocr.FrameReads(frame)
    monkeypatch.setattr(reads, "battle", lambda: _without_heading("in_run_defense"))
    result = observe_frame(frame, "battle", reads=reads)
    assert result.category == "DEFENSE" and result.rows


def test_an_empty_read_never_falls_back_to_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    from perception import observe_frame
    frame = cv2.imread(str(FIXTURES / "in_run_defense.png"))
    reads = ocr.FrameReads(frame)
    monkeypatch.setattr(reads, "battle", lambda: ())
    result = observe_frame(frame, "battle", reads=reads)
    assert result.category is None and result.rows == ()


def test_the_panel_is_visible_by_colour_alone() -> None:
    from perception import panel_visible
    frame = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    assert panel_visible(frame, ())
    covered = frame.copy()
    covered[1640:1720] = 0
    assert not panel_visible(covered, ())
