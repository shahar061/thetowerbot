from __future__ import annotations

import copy
from typing import Any

from fleet.build_route import RouteBaseline, RouteDocument
from fleet.resource_blocks import template_lab_list
from tools import migrate_lab_lists as m


def _baseline(labs: dict[str, Any]) -> dict[str, Any]:
    raw = RouteDocument.compatibility().to_dict()["baseline"]
    raw["labs"] = labs
    return raw


STEPS = {"slot1_research": "game_speed", "steps": ["research_game_speed"], "mode": "steps", "blocks": []}

TRACKS = [
    {"id": "t1", "type": "slot_track", "slots": [1], "on_blocked": "skip", "children": [
        {"id": "t1.gs", "type": "research", "lab_id": "labs.game-speed", "to_level": 7},
        {"id": "t1.pool", "type": "lab_pool", "lab_ids": ["labs.coins-kill-bonus", "labs.cash-bonus"],
         "selection": "cheapest", "caps": {"labs.coins-kill-bonus": 30}},
        {"id": "t1.c", "type": "condition", "field": "game_speed_maxed", "cmp": "eq", "value": 1,
         "then": [{"id": "t1.as", "type": "research", "lab_id": "labs.attack-speed", "to_level": 50}],
         "else": []}]},
    {"id": "t2", "type": "slot_track", "slots": [2], "on_blocked": "skip", "children": [
        {"id": "t2.ls", "type": "research", "lab_id": "labs.labs-speed", "to_level": 99}]},
    {"id": "t3", "type": "slot_track", "slots": [3, 4], "on_blocked": "skip", "children": [
        {"id": "t3.cw", "type": "research", "lab_id": "labs.coins-wave", "to_level": 20},
        {"id": "t3.dup", "type": "research", "lab_id": "labs.coins-kill-bonus", "to_level": 10}]},
]


def test_steps_strategy_gets_template_list_and_rules() -> None:
    new, notes = m.migrate_baseline(_baseline(STEPS))
    assert new["labs"]["mode"] == "blocks"
    assert new["labs"]["blocks"] == list(template_lab_list())
    assert new["rules"]["coins"]["lab_share"] == {"mode": "just_in_time", "pct": 25}
    assert new["rules"]["labs"]["direct_start"] is True
    assert new["rules"]["labs"]["filler"]["enabled"] is True
    RouteBaseline.from_dict(new)  # passes server-side validation


def test_slot_tracks_flatten_with_pins_order_and_caps() -> None:
    entries, notes = m.flatten_slot_tracks(copy.deepcopy(TRACKS))
    ids = [(e["lab_id"], e["to_level"], e.get("pin_slot")) for e in entries]
    assert ids[:3] == [("labs.game-speed", 7, 1), ("labs.attack-speed", 50, 1), ("labs.labs-speed", 99, 2)]
    assert ("labs.coins-kill-bonus", 30, None) in ids
    assert ("labs.coins-wave", 20, None) in ids
    # Coins/Kill L10 after L30 is not a higher level: dropped with a note.
    assert ("labs.coins-kill-bonus", 10, None) not in ids
    assert any("coins-kill-bonus" in n for n in notes)
    assert entries[0]["tier"] == "S+"


def test_slot_track_strategy_migrates_and_validates() -> None:
    new, _ = m.migrate_baseline(_baseline({"slot1_research": "game_speed", "steps": ["research_game_speed"],
                                           "mode": "blocks", "blocks": copy.deepcopy(TRACKS)}))
    assert new["labs"]["blocks"][0]["type"] == "lab_list"
    RouteBaseline.from_dict(new)


def test_existing_lab_list_keeps_entries() -> None:
    blocks = list(template_lab_list())
    new, _ = m.migrate_baseline(_baseline({"slot1_research": "game_speed", "steps": ["research_game_speed"],
                                           "mode": "blocks", "blocks": copy.deepcopy(blocks)}))
    assert new["labs"]["blocks"] == blocks
    assert new["rules"]["labs"]["direct_start"] is True


def test_flatten_drops_non_increasing_repeats_and_unpriced_labs() -> None:
    tracks = copy.deepcopy(TRACKS)
    tracks[2]["children"].append({"id": "t3.bh", "type": "research", "lab_id": "labs.black-hole-damage",
                                  "to_level": 1})
    entries, notes = m.flatten_slot_tracks(tracks)
    assert all("black-hole" not in e["lab_id"] for e in entries)
    assert any("black-hole" in n for n in notes)


def test_rules_leave_workshop_and_gem_limits_alone() -> None:
    base = _baseline(STEPS)
    base["rules"]["coins"]["workshop_spend_limit_pct"] = 40
    new, _ = m.migrate_baseline(base)
    assert new["rules"]["coins"]["workshop_spend_limit_pct"] == 40
    assert new["rules"]["gems"] == base["rules"]["gems"]


def test_template_rules_default_to_direct_start() -> None:
    from fleet.resource_blocks import template_lab_list_rules
    assert template_lab_list_rules()["labs"]["direct_start"] is True


def test_slot_3_plus_research_is_unpinned_and_ordered_after_slot_2_pins() -> None:
    tracks = copy.deepcopy(TRACKS)
    tracks.append({"id": "t5", "type": "slot_track", "slots": [5], "on_blocked": "skip", "children": [
        {"id": "t5.up", "type": "research", "lab_id": "labs.unlock-perks", "to_level": 1}]})
    entries, _ = m.flatten_slot_tracks(tracks)
    assert all(e.get("pin_slot") in (None, 1, 2) for e in entries)
    ids = [(e["lab_id"], e.get("pin_slot")) for e in entries]
    assert ids[:4] == [("labs.game-speed", 1), ("labs.attack-speed", 1), ("labs.labs-speed", 2),
                       ("labs.unlock-perks", None)]
    # Pool and multi-slot research follow the former slot-3+ research.
    assert ids.index(("labs.unlock-perks", None)) < ids.index(("labs.coins-kill-bonus", None))


# ---- main() against a fake coordinator (no network) -------------------------------------------

def _row(name: str, baseline: dict[str, Any]) -> dict[str, Any]:
    return {"id": name.lower(), "name": name, "version": 1, "source_template": "t", "baseline": baseline}


class _Fake:
    """Stands in for m._request: serves a library and records POSTs, chaining revisions."""

    def __init__(self, rows: list[dict[str, Any]], fail_on_post: dict[int, Exception] | None = None) -> None:
        self.rows, self.revision = rows, 10
        self.gets: list[str] = []
        self.posts: list[dict[str, Any]] = []
        self.fail_on_post = fail_on_post or {}

    def __call__(self, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if body is None:
            self.gets.append(url)
        else:
            self.posts.append(body)
            if len(self.posts) in self.fail_on_post:
                raise self.fail_on_post[len(self.posts)]
            self.revision += 1
        return {"revision": self.revision, "strategies": self.rows}


def _http_error(code: int) -> m.urllib.error.HTTPError:
    import io
    return m.urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"nope"))  # type: ignore[arg-type]


def _run(monkeypatch, fake: _Fake, *argv: str) -> int:
    monkeypatch.setattr(m, "_request", fake)
    return m.main(list(argv))


def test_main_chains_revisions_between_saves(monkeypatch) -> None:
    fake = _Fake([_row("A", _baseline(STEPS)), _row("B", _baseline(STEPS))])
    assert _run(monkeypatch, fake) == 0
    assert [p["expected_revision"] for p in fake.posts] == [10, 11]


def test_main_dry_run_never_posts(monkeypatch) -> None:
    fake = _Fake([_row("A", _baseline(STEPS))])
    assert _run(monkeypatch, fake, "--dry-run") == 0
    assert fake.posts == [] and len(fake.gets) == 1


def test_main_409_stops_the_loop(monkeypatch, capsys) -> None:
    fake = _Fake([_row(n, _baseline(STEPS)) for n in "ABC"], fail_on_post={2: _http_error(409)})
    assert _run(monkeypatch, fake) == 1
    assert len(fake.posts) == 2  # C is never attempted


def test_main_unchanged_baseline_is_not_posted(monkeypatch, capsys) -> None:
    done, _ = m.migrate_baseline(_baseline(STEPS))
    fake = _Fake([_row("A", done), _row("B", _baseline(STEPS))])
    assert _run(monkeypatch, fake) == 0
    assert [p["name"] for p in fake.posts] == ["B"]
    assert "unchanged" in capsys.readouterr().out


def test_main_url_error_midrun_reports_saved_and_fails(monkeypatch, capsys) -> None:
    fake = _Fake([_row(n, _baseline(STEPS)) for n in "ABC"],
                 fail_on_post={2: m.urllib.error.URLError("down")})
    assert _run(monkeypatch, fake) == 1
    out = capsys.readouterr().out
    assert "saved so far: A" in out
    assert len(fake.posts) == 2


def test_main_timeout_midrun_returns_one(monkeypatch, capsys) -> None:
    fake = _Fake([_row(n, _baseline(STEPS)) for n in "AB"], fail_on_post={1: TimeoutError("slow")})
    assert _run(monkeypatch, fake) == 1
    assert "saved so far: none" in capsys.readouterr().out


def test_main_malformed_baseline_is_skipped(monkeypatch, capsys) -> None:
    fake = _Fake([_row("Bad", {"labs": {"mode": "blocks", "blocks": [{"type": "slot_track", "slots": [1],
                                                                       "children": [{"type": "research"}]}]},
                               "rules": {}}), _row("Good", _baseline(STEPS))])
    assert _run(monkeypatch, fake) == 1
    assert "SKIP Bad" in capsys.readouterr().out
    assert [p["name"] for p in fake.posts] == ["Good"]
