"""Each fresh reroll account is given one opening variant, once."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import builds
import db as bot_db
from fleet.reroll_variants import (VARIANT_FILE, assign_variant, caps_summary, compare,
                                  opening_variants, read_variant, variant_name)

A, B, C = (builds.Variant(i, i.upper()) for i in ("a", "b", "c"))


def _stored(workers: Path, name: str, variant: str) -> None:
    (workers / name).mkdir(parents=True)
    (workers / name / VARIANT_FILE).write_text(json.dumps({"variant": variant}))


def assign(workers: Path, name: str, variants=(A, B, C)) -> str:
    return assign_variant(workers / name, workers_dir=workers, variants=variants,
                          now=lambda: "2026-09-23T18:00:00Z")


def test_assign_creates_the_worker_folder(tmp_path: Path) -> None:
    assert assign(tmp_path, "Tiramisu64_30") == "a"
    stored = json.loads((tmp_path / "Tiramisu64_30" / VARIANT_FILE).read_text())
    assert stored == {"variant": "a", "assigned_at": "2026-09-23T18:00:00Z"}
    assert read_variant(tmp_path / "Tiramisu64_30") == "a"


def test_assign_picks_the_variant_with_the_fewest_accounts(tmp_path: Path) -> None:
    _stored(tmp_path, "one", "a")
    _stored(tmp_path, "two", "a")
    _stored(tmp_path, "three", "b")
    assert assign(tmp_path, "four") == "c"
    assert assign(tmp_path, "five") == "b"   # b and c tie at 1 -> pack order


def test_assign_is_idempotent(tmp_path: Path) -> None:
    _stored(tmp_path, "one", "b")
    assert assign(tmp_path, "one") == "b"


def test_ids_no_longer_in_the_pack_are_not_counted(tmp_path: Path) -> None:
    _stored(tmp_path, "one", "retired_idea")
    _stored(tmp_path, "two", "retired_idea")
    assert assign(tmp_path, "three") == "a"


def test_an_unreadable_file_reads_as_no_variant(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / VARIANT_FILE).write_text("{not json")
    assert read_variant(tmp_path / "w") is None
    assert read_variant(tmp_path / "missing") is None


def test_the_opening_variants_come_from_the_pack() -> None:
    assert [v.id for v in opening_variants()] == ["baseline", "income_first", "attack_heavy"]


def _played(workers: Path, name: str, variant: str, runs: list[tuple],
            registered_as: str | None = None) -> None:
    _stored(workers, name, variant)
    (workers / name / "fleet-registration.json").write_text(
        json.dumps({"account_id": registered_as or name}))
    db = workers / name / "tower_bot.db"
    bot_db.bind_account(db, name)
    with sqlite3.connect(db) as conn:
        for number, (start, end, tier, wave) in enumerate(runs, 1):
            conn.execute("INSERT INTO runs (id, started_at, ended_at, tier, wave) "
                         "VALUES (?, ?, ?, ?, ?)", (number, start, end, tier, wave))


def test_compare_rows_per_variant(tmp_path: Path) -> None:
    _played(tmp_path, "one", "a", [(0, 100, 1, 20)])
    _played(tmp_path, "two", "a", [(0, 300, 1, 25)])
    _played(tmp_path, "three", "a", [(0, 50, 1, 8)])
    _played(tmp_path, "four", "b", [(0, 70, 1, 3)])
    rows = {row["id"]: row for row in compare(tmp_path, (A, B, C))}
    assert list(rows) == ["a", "b", "c"]
    assert (rows["a"]["accounts"], rows["a"]["reached"]) == (3, 2)
    assert (rows["a"]["median_seconds"], rows["a"]["fastest_seconds"]) == (200, 100)
    assert (rows["b"]["accounts"], rows["b"]["reached"], rows["b"]["median_seconds"]) == (1, 0, None)
    assert rows["c"]["accounts"] == 0


def test_compare_counts_an_account_without_a_database(tmp_path: Path) -> None:
    _stored(tmp_path, "fresh", "a")
    row = compare(tmp_path, (A,))[0]
    assert (row["accounts"], row["reached"]) == (1, 0)


def test_compare_lists_an_unknown_variant_last(tmp_path: Path) -> None:
    _played(tmp_path, "old", "retired_idea", [(0, 10, 1, 21)])
    rows = compare(tmp_path, (A,))
    assert [row["name"] for row in rows] == ["A", "Unknown (retired_idea)"]
    assert rows[1]["reached"] == 1 and rows[1]["caps"] == ""
    assert variant_name("retired_idea", (A,)) == "Unknown (retired_idea)"


def test_caps_summary_groups_rows_with_the_same_rule() -> None:
    summary = caps_summary(builds.by_id("opening").variant("income_first").level_caps)
    assert summary == ("Damage, Attack Speed \u2264 1 + 1 per Coins / Wave"
                       " \u00b7 Coins / Wave \u2264 5 \u00b7 Defense Absolute \u2264 2")


def test_compare_ignores_a_database_left_by_an_earlier_account(tmp_path: Path) -> None:
    # A recreated emulator reusing an old name: its folder still holds the
    # previous account's database, which must not count as this account's play.
    _played(tmp_path, "reused", "a", [(0, 10, 1, 21)], registered_as="new_account")
    row = compare(tmp_path, (A,))[0]
    assert (row["accounts"], row["reached"]) == (1, 0)
