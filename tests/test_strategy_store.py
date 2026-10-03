"""The filesystem half of strategy.py.

Kept apart from tests/test_strategy.py on purpose: the model's tests are
pure, these need tmp_path, and a filesystem quirk should never look like a
validation bug.
"""

from __future__ import annotations

import json

import pytest

import config
from strategy import ControlError, Strategy, StrategyStore, validate_name
from tests.conftest import REAL_STRATEGY_DIR, seed_template_dir


@pytest.fixture
def store(tmp_path, monkeypatch) -> StrategyStore:
    """A store over tmp_path, whose templates all exist.

    save() runs validated(), so the fixture points TEMPLATE_DIR at a
    directory holding the files config.ACTIONS names - otherwise every save
    in this file would fail for a reason that has nothing to do with the
    store.
    """
    templates = tmp_path / "templates"
    templates.mkdir()
    seed_template_dir(templates)
    monkeypatch.setattr(config, "TEMPLATE_DIR", templates)
    return StrategyStore(tmp_path / "strategies")


def test_saving_then_loading_returns_an_equal_strategy(store) -> None:
    original = Strategy.from_config("mine")
    store.save(original)
    assert store.load("mine") == original


def test_save_creates_the_directory(tmp_path, store) -> None:
    # First run of a fresh clone: strategies/ does not exist yet, and a save
    # must not be the thing that discovers that.
    store.save(Strategy.from_config("mine"))
    assert (tmp_path / "strategies" / "mine.json").is_file()


def test_saved_json_is_human_readable(store) -> None:
    # These files are meant to be read in a diff and edited by hand, so the
    # formatting is part of the contract, not an accident of json.dumps.
    store.save(Strategy.from_config("mine"))
    text = store.path_for("mine").read_text()
    assert text.endswith("\n")
    assert "\n  " in text  # indented, not one line


def test_names_are_sorted_and_exclude_the_active_pointer(store) -> None:
    store.save(Strategy.from_config("zeta"))
    store.save(Strategy.from_config("alpha"))
    (store.directory / ".active").write_text("alpha\n")
    assert store.names() == ["alpha", "zeta"]


def test_names_is_empty_before_anything_is_saved(store) -> None:
    assert store.names() == []


def test_loading_an_absent_strategy_names_the_field(store) -> None:
    with pytest.raises(ControlError) as caught:
        store.load("ghost")
    assert caught.value.field == "name"


def test_loading_unparseable_json_names_the_file(store) -> None:
    store.directory.mkdir(parents=True, exist_ok=True)
    store.path_for("broken").write_text("{not json")
    with pytest.raises(ControlError) as caught:
        store.load("broken")
    assert caught.value.field == "name"
    assert "broken" in str(caught.value)


def test_save_is_atomic(store, monkeypatch) -> None:
    """A crash mid-write must not leave a profile that fails to parse.

    Simulated by making os.replace raise: save()'s finally clause always
    unlinks the temp file, so it never survives a failed replace either -
    what this proves is that the real path is left untouched, which is
    exactly the guarantee a write-then-rename gives and a write-in-place
    does not.
    """
    import os

    store.save(Strategy.from_config("mine"))
    before = store.path_for("mine").read_text()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    original = Strategy.from_config("mine")
    with pytest.raises(OSError):
        store.save(Strategy.from_dict({**original.to_dict(), "interval": 9.0}))
    assert store.path_for("mine").read_text() == before
    # The docstring's other half, now actually asserted: the finally clause
    # cleans up after a failed replace, so the directory holds exactly the
    # profile that was there before and nothing else.
    assert sorted(p.name for p in store.directory.iterdir()) == [".lock", "mine.json"]


def test_two_saves_of_one_profile_do_not_share_a_temp_path(store, monkeypatch) -> None:
    """Same-profile concurrency is the expected case, not the exotic one.

    A later stage saves on every settings change. With one temp path per
    target, two writers' write_text calls interleave into a single spliced
    file, the first replace promotes it, and the second writer dies on a
    FileNotFoundError from the first one's finally clause.
    """
    import os

    seen: list[str] = []
    real_replace = os.replace

    def recording(src, dst):
        seen.append(str(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording)
    store.save(Strategy.from_config("mine"))
    store.save(Strategy.from_config("mine"))
    assert len(set(seen)) == 2


def test_save_leaves_no_temp_files_behind(store) -> None:
    store.save(Strategy.from_config("mine"))
    assert [p.name for p in store.directory.iterdir() if p.suffix != ".json" and p.name != ".lock"] == []


def test_save_rejects_a_strategy_with_a_missing_template(store, monkeypatch) -> None:
    monkeypatch.setattr(config, "TEMPLATE_DIR", store.directory / "empty")
    with pytest.raises(ControlError):
        store.save(Strategy.from_config("mine"))


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/etc/passwd",
        "a/b",
        "",
        "x" * 65,
        "has space",
        "dot.name",
        "..",
        "mine\n",  # `$` matches before a trailing "\n"; must use fullmatch
        "tab\tname",
    ],
)
def test_unsafe_names_are_refused_before_they_become_paths(name: str) -> None:
    """A name arrives off a URL and becomes a filename.

    /api/unknown/{name} already learned this: resolve-and-check is the
    fallback, refusing the name outright is the fix.
    """
    with pytest.raises(ControlError) as caught:
        validate_name(name)
    assert caught.value.field == "name"


@pytest.mark.parametrize("name", ["default", "crit-build", "early_game", "T5", "x" * 64])
def test_reasonable_names_are_accepted(name: str) -> None:
    assert validate_name(name) == name


def test_the_store_refuses_unsafe_names_on_every_path(store) -> None:
    # Every entry point takes the name from a URL, so every entry point has
    # to refuse it - not just the two that happened to get tested first.
    store.ensure_seeded()
    with pytest.raises(ControlError):
        store.load("../../etc/passwd")
    with pytest.raises(ControlError):
        store.path_for("../escape")
    with pytest.raises(ControlError):
        store.delete("../escape")
    with pytest.raises(ControlError):
        store.set_active("../escape")
    assert store.exists("../escape") is False


def test_save_refuses_a_bad_name_before_creating_the_directory(store) -> None:
    """save()'s validate_name call looks redundant with path_for()'s. It isn't.

    It runs before mkdir, so a name that will be refused anyway cannot create
    the strategies directory as a side effect first. Without this assertion
    the redundancy has nothing pinning it, and reads like dead code.
    """
    escaping = Strategy.from_dict(
        {**Strategy.from_config("mine").to_dict(), "name": "../escape"}
    )
    with pytest.raises(ControlError) as caught:
        store.save(escaping)
    assert caught.value.field == "name"
    assert not store.directory.exists()


def test_names_omits_a_file_the_loader_would_refuse(store) -> None:
    # A listing that offers a name load() then rejects is a dead entry in the
    # dashboard's dropdown.
    store.save(Strategy.from_config("mine"))
    (store.directory / "my strategy.json").write_text("{}")
    assert store.names() == ["mine"]


def test_the_committed_default_matches_config() -> None:
    """The committed profile and config.py's defaults must not drift - both
    config.ACTIONS and config.SHOPPING_ROWS (Task 10), since this compares
    whole Strategy objects and Strategy.from_config() builds both from
    config.py.

    ensure_seeded() only writes default.json when strategies/ is empty, and
    it never is in a real clone - so config.py's defaults are never
    consulted there. Without this test, adding an upgrade to config.ACTIONS
    or reordering config.SHOPPING_ROWS would reach no clone, new or old, and
    nothing would say so.

    Deliberately reads tests/conftest.py's REAL_STRATEGY_DIR rather than
    config.STRATEGY_DIR: the session-scoped fenced_strategy_dir fixture in
    conftest.py repoints the latter at a throwaway directory for the whole
    suite (see its docstring), so config.STRATEGY_DIR here would read that
    empty stand-in instead of the tracked file this test exists to check -
    failing or passing vacuously depending on what other tests happened to
    seed into the shared fence first, either of which is wrong. This is the
    one test in the suite meant to see the real, committed directory.
    """
    raw = json.loads((REAL_STRATEGY_DIR / "default.json").read_text())
    assert Strategy.from_dict(raw) == Strategy.from_config("default")


def test_the_committed_default_leaves_claims_off() -> None:
    """Turning claims on is task 7's decision, made once, in the open."""
    raw = json.loads((REAL_STRATEGY_DIR / "default.json").read_text())
    assert Strategy.from_dict(raw).claims.enabled is False


def test_ensure_seeded_writes_the_shipped_defaults(store) -> None:
    seeded = store.ensure_seeded()
    assert seeded == Strategy.from_config("default")
    assert store.names() == ["default"]
    assert store.active_name() == "default"


def test_ensure_seeded_is_idempotent_and_does_not_overwrite(store) -> None:
    """A second launch must not undo your edits.

    This is the failure that would be worst to discover late: seeding on
    every start would silently reset a tuned profile back to config.ACTIONS.
    """
    store.ensure_seeded()
    edited = Strategy.from_config("default")
    edited = Strategy.from_dict({**edited.to_dict(), "interval": 9.0})
    store.save(edited)

    again = store.ensure_seeded()
    assert again.interval == 9.0
    assert store.load("default").interval == 9.0


def test_ensure_seeded_returns_the_active_profile_not_the_default(store) -> None:
    store.ensure_seeded()
    store.save(Strategy.from_config("crit"))
    store.set_active("crit")
    assert store.ensure_seeded().name == "crit"


def test_ensure_seeded_recovers_from_an_active_pointer_at_a_deleted_file(store) -> None:
    """The pointer can outlive its target - someone deletes a file by hand.

    Falling back to any surviving profile beats refusing to start: the bot
    is more useful running the wrong strategy than not running.
    """
    store.ensure_seeded()
    store.save(Strategy.from_config("crit"))
    store.set_active("crit")
    store.path_for("crit").unlink()

    recovered = store.ensure_seeded()
    assert recovered.name == "default"
    assert store.active_name() == "default"


def test_ensure_seeded_skips_a_profile_that_does_not_parse(store) -> None:
    """A hand-edit that breaks the JSON must not stop the bot starting.

    active_name() checks that the pointer's target exists, not that it
    parses, so without a skip here the load at the end of ensure_seeded()
    propagates straight out of startup - contradicting the fallback the
    method's own docstring promises.
    """
    store.ensure_seeded()
    store.save(Strategy.from_config("crit"))
    store.set_active("crit")
    store.path_for("crit").write_text("{not json")

    recovered = store.ensure_seeded()
    assert recovered.name == "default"
    assert store.active_name() == "default"


def test_ensure_seeded_raises_when_nothing_on_disk_parses(store) -> None:
    # The fallback stops here on purpose: quietly reseeding would overwrite
    # the very file whose contents its owner needs in order to repair it,
    # and would look exactly like a tuned profile having been reset.
    store.directory.mkdir(parents=True)
    store.path_for("default").write_text("{not json")
    with pytest.raises(ControlError) as caught:
        store.ensure_seeded()
    assert caught.value.code == "not_found"
    assert store.path_for("default").read_text() == "{not json"


def test_set_active_persists_across_stores(store) -> None:
    store.ensure_seeded()
    store.save(Strategy.from_config("crit"))
    store.set_active("crit")
    # A fresh store over the same directory is what a restart looks like.
    assert StrategyStore(store.directory).active_name() == "crit"


def test_set_active_refuses_a_name_with_no_file(store) -> None:
    store.ensure_seeded()
    with pytest.raises(ControlError) as caught:
        store.set_active("ghost")
    assert caught.value.field == "name"
    assert store.active_name() == "default"


def test_delete_removes_a_profile(store) -> None:
    store.ensure_seeded()
    store.save(Strategy.from_config("crit"))
    store.delete("crit")
    assert store.names() == ["default"]


def test_delete_refuses_the_active_profile(store) -> None:
    store.ensure_seeded()
    store.save(Strategy.from_config("crit"))
    with pytest.raises(ControlError) as caught:
        store.delete("default")
    assert caught.value.field == "name"
    # Both guards raise field == "name", so only the message tells them
    # apart - and the order they run in is a fix that nothing else pins.
    assert "is active" in str(caught.value)
    assert caught.value.code == "conflict"
    assert store.names() == ["crit", "default"]


def test_delete_refuses_the_last_profile(store) -> None:
    # Both guards exist because both leave the bot with no policy to load,
    # which has no recovery short of hand-editing the directory.
    store.ensure_seeded()
    store.set_active("default")
    with pytest.raises(ControlError) as caught:
        store.delete("default")
    # A lone profile is necessarily the active one, so this assertion is what
    # proves the last-profile guard ran first. Without it, flipping the order
    # back would silently make this branch unreachable and this test would
    # still pass against the active-profile guard.
    assert "the last strategy cannot be deleted" in str(caught.value)
    assert caught.value.code == "conflict"


def test_delete_refuses_an_absent_profile(store) -> None:
    store.ensure_seeded()
    store.save(Strategy.from_config("crit"))
    with pytest.raises(ControlError) as caught:
        store.delete("ghost")
    assert caught.value.code == "not_found"


def test_errors_carry_the_code_an_http_layer_maps_on(store) -> None:
    """404 vs 409 vs 422 must not be decided by string-matching a message.

    Every raise below carries field == "name"; only the code separates
    "no such profile" from "you may not delete this one" from "the file is
    corrupt", and a status a route derives from wording breaks the first
    time a message is reworded.
    """
    store.ensure_seeded()
    with pytest.raises(ControlError) as absent:
        store.load("ghost")
    assert absent.value.code == "not_found"

    with pytest.raises(ControlError) as pointed:
        store.set_active("ghost")
    assert pointed.value.code == "not_found"

    store.path_for("broken").write_text("{not json")
    with pytest.raises(ControlError) as corrupt:
        store.load("broken")
    # Corrupt content is a bad file, not a missing one: it stays the default.
    assert corrupt.value.code == "invalid"
