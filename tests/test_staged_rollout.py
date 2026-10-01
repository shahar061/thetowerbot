"""The locked JSON file every staged rollout shares."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from staged_rollout import LockedJsonFile


def _file(root: Path) -> LockedJsonFile:
    return LockedJsonFile(root / "x.json", root / ".x.lock", "Test rollout")


def _parse(document: object) -> dict:
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("bad schema")
    return document


def test_a_missing_file_loads_as_none(tmp_path: Path) -> None:
    assert _file(tmp_path).load(_parse, quarantine=True) is None


def test_a_written_document_loads_back(tmp_path: Path) -> None:
    store = _file(tmp_path)
    with store.locked():
        store.write({"schema_version": 1, "n": 3})
    assert store.load(_parse, quarantine=True) == {"schema_version": 1, "n": 3}


def test_a_corrupt_file_is_moved_aside_only_when_quarantining(tmp_path: Path) -> None:
    store = _file(tmp_path)
    store.path.write_text("{not json")
    assert store.load(_parse, quarantine=False) is None
    assert store.path.exists()
    assert store.load(_parse, quarantine=True) is None
    assert not store.path.exists()
    assert len(list(tmp_path.glob("x.json.corrupt-*"))) == 1


def test_a_schema_the_parser_refuses_is_corrupt(tmp_path: Path) -> None:
    store = _file(tmp_path)
    store.path.write_text(json.dumps({"schema_version": 9}))
    assert store.load(_parse, quarantine=True) is None
    assert len(list(tmp_path.glob("x.json.corrupt-*"))) == 1


def test_an_unreadable_file_raises_only_for_a_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _file(tmp_path)
    store.path.write_text(json.dumps({"schema_version": 1}))

    def unreadable(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert store.load(_parse, quarantine=True) is None
    with pytest.raises(PermissionError):
        store.load(_parse, quarantine=True, for_write=True)


def test_the_lock_serializes_read_modify_write(tmp_path: Path) -> None:
    store = _file(tmp_path)

    def bump(_: int) -> None:
        with store.locked():
            current = store.load(_parse, quarantine=True) or {"schema_version": 1, "n": 0}
            store.write({**current, "n": current["n"] + 1})

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(bump, range(40)))
    assert store.load(_parse, quarantine=True)["n"] == 40
