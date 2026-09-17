"""The shared reroll journal keeps a durable, filterable event history."""

from pathlib import Path

from fleet.reroll_journal import RerollJournal, instance_color


def test_entries_persist_and_sort_by_recorded_time_then_sequence(tmp_path: Path) -> None:
    journal = RerollJournal(tmp_path)
    later = journal.append(instance="BlueStacks_21", level="info", kind="action",
                           message="started", at="2026-09-17T10:00:02Z")
    earlier = journal.append(instance="BlueStacks_20", level="warning", kind="milestone",
                             message="needs review", at="2026-09-17T10:00:01Z")
    tied = journal.append(instance="BlueStacks_22", level="error", kind="failure",
                          message="blocked", at="2026-09-17T10:00:02Z")

    reopened = RerollJournal(tmp_path)
    entries = reopened.list_entries()
    assert [entry["sequence"] for entry in entries] == [earlier["sequence"],
                                                         later["sequence"], tied["sequence"]]
    assert entries[0] == earlier
    assert set(entries[0]) == {"sequence", "at", "instance", "level", "kind",
                               "message", "color"}


def test_filters_and_cursor_select_only_requested_events(tmp_path: Path) -> None:
    journal = RerollJournal(tmp_path)
    first = journal.append(instance="BlueStacks_20", level="info", kind="action",
                           message="one", at="2026-09-17T10:00:01Z")
    journal.append(instance="BlueStacks_21", level="warning", kind="warning",
                   message="two", at="2026-09-17T10:00:02Z")
    wanted = journal.append(instance="BlueStacks_20", level="warning", kind="warning",
                            message="three", at="2026-09-17T10:00:03Z")

    assert journal.list_entries(cursor=first["sequence"], instances=["BlueStacks_20"],
                                levels=["warning"]) == [wanted]
    assert journal.list_entries(instances=["missing"]) == []
    assert journal.list_entries(levels=["error"]) == []


def test_cursor_pages_do_not_skip_late_arriving_older_timestamps(tmp_path: Path) -> None:
    journal = RerollJournal(tmp_path)
    first = journal.append(instance="Air_20", level="info", kind="action",
                           message="one", at="2026-09-17T10:00:03Z")
    second = journal.append(instance="Air_21", level="info", kind="action",
                            message="two", at="2026-09-17T10:00:02Z")
    third = journal.append(instance="Air_22", level="info", kind="action",
                           message="three", at="2026-09-17T10:00:01Z")

    page = journal.list_entries(cursor=0, limit=2)
    assert {entry["sequence"] for entry in page} == {first["sequence"], second["sequence"]}
    next_page = journal.list_entries(cursor=max(entry["sequence"] for entry in page), limit=2)
    assert [entry["sequence"] for entry in next_page] == [third["sequence"]]


def test_retention_removes_oldest_sequences_and_survives_reopen(tmp_path: Path) -> None:
    journal = RerollJournal(tmp_path, max_entries=2)
    for number in range(3):
        journal.append(instance="BlueStacks_20", level="info", kind="action",
                       message=str(number), at=f"2026-09-17T10:00:0{number}Z")

    assert [entry["message"] for entry in RerollJournal(tmp_path).list_entries()] == ["1", "2"]


def test_instance_color_is_stable_and_legible_on_white(tmp_path: Path) -> None:
    first = RerollJournal(tmp_path).append(instance="BlueStacks_20", level="info",
                                            kind="action", message="started")
    assert first["color"] == instance_color("BlueStacks_20")
    assert instance_color("BlueStacks_20") == instance_color("BlueStacks_20")
    assert instance_color("BlueStacks_20").startswith("#")
    rgb = [int(instance_color("BlueStacks_20")[offset:offset + 2], 16) / 255
           for offset in (1, 3, 5)]
    channels = [channel / 12.92 if channel <= 0.04045
                else ((channel + 0.055) / 1.055) ** 2.4 for channel in rgb]
    luminance = sum(weight * channel for weight, channel in
                    zip((0.2126, 0.7152, 0.0722), channels))
    assert 1.05 / (luminance + 0.05) >= 4.5


def test_simultaneous_emulator_names_receive_distinct_persistent_colors(tmp_path: Path) -> None:
    journal = RerollJournal(tmp_path)
    first = journal.append(instance="Tiramisu64_21", level="info", kind="action",
                           message="started")
    second = journal.append(instance="Tiramisu64_22", level="info", kind="action",
                            message="started")
    assert first["color"] != second["color"]
    reopened = RerollJournal(tmp_path)
    assert [entry["color"] for entry in reopened.list_entries()] == [
        first["color"], second["color"]]
