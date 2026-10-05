from pathlib import Path
from typing import Any

import pytest

import config
from fleet.battle_prices import BattlePrices


def row(price: int | None = None, value: float = 1.0, status: str = "available", at: float = 100) -> dict[str, Any]:
    return dict(price=price, value=value, status=status, observed_at=at)


def test_confirmed_counts_advance_without_price_ocr_and_survive_reload(tmp_path: Path) -> None:
    model = BattlePrices(tmp_path, "a")
    quotes = model.update(run_id=7, wave=4, counts={}, rows={"health": row(10)}, now=100)
    assert quotes["health"]["price"] == 10
    quotes = model.update(run_id=7, wave=4, counts={"health": 4}, rows={}, now=200)
    assert quotes["health"]["price"] == 23
    model = BattlePrices(tmp_path, "a")
    assert model.update(run_id=7, wave=4, counts={"health": 4}, rows={}, now=300)["health"]["price"] == 23


def test_restart_new_run_requires_calibration_and_max_is_terminal(tmp_path: Path) -> None:
    model = BattlePrices(tmp_path, "a")
    model.update(run_id=7, wave=40, counts={}, rows={"health": row(33)}, now=100)
    assert not model.update(run_id=8, wave=40, counts={}, rows={}, now=101)
    q = model.update(run_id=8, wave=40, counts={}, rows={"health": row(33)}, now=101)
    assert q["health"]["index"] == 6
    q = model.update(run_id=8, wave=40, counts={}, rows={"health": row(status="maxed", at=102)}, now=102)
    assert q["health"]["status"] == "maxed"
    assert model.update(run_id=8, wave=40, counts={}, rows={}, now=200)["health"]["status"] == "maxed"


def test_pending_frame_cannot_double_advance_and_uncertain_wave_resyncs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "BATTLE_BURST_ENABLED", False)  # pins the per-wave expiry
    model = BattlePrices(tmp_path, "a")
    model.update(run_id=7, wave=4, counts={}, rows={"attack_speed": row(5)}, now=100)
    q = model.update(run_id=7, wave=4, counts={}, rows={"attack_speed": row(7)}, now=101, pending=True)
    assert q["attack_speed"]["price"] == 5
    q = model.update(run_id=7, wave=4, counts={"attack_speed": 1}, rows={}, now=102)
    assert q["attack_speed"]["price"] == 7
    assert not model.update(run_id=7, wave=5, counts={"attack_speed": 1}, rows={}, now=103)["attack_speed"]["verified"]


def test_unknown_prices_and_identity_never_generate_base_quotes(tmp_path: Path) -> None:
    model = BattlePrices(tmp_path, "a")
    assert not model.update(run_id=None, wave=1, counts={}, rows={"health": row(10)}, now=100)
    assert not model.update(run_id=7, wave=1, counts={}, rows={"health": row(987654321)}, now=100)
    assert not model.update(run_id=7, wave=1, counts=None, rows={"health": row(10)}, now=100)


def test_abbreviated_price_calibrates_only_when_the_index_is_unique(tmp_path: Path) -> None:
    model = BattlePrices(tmp_path, 'a')
    from fleet.battle_prices import catalog
    curve = catalog()['curves']['cash_bonus']
    assert curve[37] != 1020  # display precision must not become the price table
    q = model.update(run_id=7, wave=3, counts={}, rows={
        'cash_bonus': dict(row(1020), raw_price='1.02K')}, now=100)
    assert q['cash_bonus']['price'] == curve[37]
    assert q['cash_bonus']['index'] == 37


def test_wave_regression_discards_previous_battle_and_unknown_value_changes_resync(tmp_path: Path) -> None:
    model = BattlePrices(tmp_path, 'a')
    model.update(run_id=7, wave=8, counts={}, rows={'health': row(10)}, now=100)
    assert not model.update(run_id=7, wave=1, counts={}, rows={}, now=101)
    model.update(run_id=7, wave=1, counts={}, rows={'health': row(10, at=102)}, now=102)
    assert not model.update(run_id=7, wave=1, counts={}, rows={'health': row(value=99, at=103)}, now=103)


def test_quotes_stay_verified_across_waves_until_a_fresh_read_disagrees(tmp_path: Path) -> None:
    model = BattlePrices(tmp_path, "a")
    model.update(run_id=7, wave=4, counts={}, rows={"attack_speed": row(5)}, now=100)
    quote = model.update(run_id=7, wave=9, counts={"attack_speed": 1}, rows={}, now=103)["attack_speed"]
    assert quote["verified"] and quote["price"] == 7
    # A fresh read that matches no level of the curve drops the quote.
    assert "attack_speed" not in model.update(run_id=7, wave=9, counts={"attack_speed": 1},
        rows={"attack_speed": row(987654321, at=104)}, now=104)


def test_invalidate_forgets_one_row_until_its_next_fresh_read(tmp_path: Path) -> None:
    model = BattlePrices(tmp_path, "a")
    model.update(run_id=7, wave=4, counts={}, rows={"attack_speed": row(5), "health": row(10)}, now=100)
    model.invalidate("attack_speed")
    quotes = model.update(run_id=7, wave=4, counts={}, rows={}, now=101)
    assert "attack_speed" not in quotes and quotes["health"]["price"] == 10
    assert "attack_speed" not in BattlePrices(tmp_path, "a").rows  # persisted
    quotes = model.update(run_id=7, wave=4, counts={}, rows={"attack_speed": row(7, at=102)}, now=102)
    assert quotes["attack_speed"]["index"] == 1
