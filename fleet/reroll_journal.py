"""Durable shared event journal for manually selected reroll workers."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock


# Each color meets WCAG AA contrast for normal text on white.
_COLORS = ("#1d4ed8", "#7c3aed", "#9a3412", "#166534",
           "#0f766e", "#a21caf", "#9f1239", "#334155")


def instance_color(instance: str) -> str:
    """Return a stable, readable foreground color for an emulator name."""
    digest = hashlib.sha256(instance.encode("utf-8")).digest()
    return _COLORS[int.from_bytes(digest[:4], "big") % len(_COLORS)]


def _timestamp(at: str | datetime | None) -> str:
    if at is None:
        value = datetime.now(timezone.utc)
    elif isinstance(at, datetime):
        value = at
    else:
        value = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("journal timestamp must have a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class RerollJournal:
    """Append and query bounded events in one SQLite database per fleet root."""

    def __init__(self, root: Path, *, max_entries: int = 10_000) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.path = Path(root) / "reroll-journal.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_entries = max_entries
        self._lock = RLock()
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS journal (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                instance TEXT NOT NULL,
                level TEXT NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS journal_at_sequence ON journal(at, sequence)")
            db.execute("""CREATE TABLE IF NOT EXISTS instance_colors (
                instance TEXT PRIMARY KEY,
                color TEXT NOT NULL UNIQUE
            )""")

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict[str, int | str]:
        return {"sequence": row["sequence"], "at": row["at"],
                "instance": row["instance"], "level": row["level"],
                "kind": row["kind"], "message": row["message"],
                "color": row["color"] or instance_color(row["instance"])}

    @staticmethod
    def _color(db: sqlite3.Connection, instance: str) -> str:
        existing = db.execute("SELECT color FROM instance_colors WHERE instance=?",
                              (instance,)).fetchone()
        if existing is not None:
            return existing[0]
        used = {row[0] for row in db.execute("SELECT color FROM instance_colors")}
        preferred = _COLORS.index(instance_color(instance))
        color = next((candidate for candidate in
                      (_COLORS[(preferred + offset) % len(_COLORS)]
                       for offset in range(len(_COLORS))) if candidate not in used),
                     instance_color(instance))
        if color not in used:
            db.execute("INSERT INTO instance_colors(instance,color) VALUES (?,?)",
                       (instance, color))
        return color

    def append(self, *, instance: str, level: str, kind: str, message: str,
               at: str | datetime | None = None) -> dict[str, int | str]:
        """Persist one event and prune the oldest ingested entries."""
        if not all(isinstance(value, str) and value for value in
                   (instance, level, kind, message)):
            raise ValueError("journal fields must be nonempty strings")
        recorded_at = _timestamp(at)
        with self._lock, sqlite3.connect(self.path, timeout=5) as db:
            db.execute("BEGIN IMMEDIATE")
            color = self._color(db, instance)
            cursor = db.execute(
                "INSERT INTO journal(at, instance, level, kind, message) VALUES (?, ?, ?, ?, ?)",
                (recorded_at, instance, level, kind, message))
            sequence = cursor.lastrowid
            assert sequence is not None
            db.execute("DELETE FROM journal WHERE sequence <= ?", (sequence - self.max_entries,))
        return {"sequence": sequence, "at": recorded_at, "instance": instance,
                "level": level, "kind": kind, "message": message,
                "color": color}

    def list_entries(self, *, cursor: int | None = None,
                     instances: list[str] | None = None,
                     levels: list[str] | None = None,
                     limit: int = 200) -> list[dict[str, int | str]]:
        """Read events after an ingestion sequence, ordered by time and sequence."""
        if limit < 1 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if cursor is not None and (not isinstance(cursor, int) or cursor < 0):
            raise ValueError("cursor must be a nonnegative sequence")
        if instances == [] or levels == []:
            return []
        clauses: list[str] = []
        values: list[int | str] = []
        if cursor is not None:
            clauses.append("sequence > ?")
            values.append(cursor)
        for column, selected in (("instance", instances), ("level", levels)):
            if selected is not None:
                clauses.append(f"journal.{column} IN ({','.join('?' for _ in selected)})")
                values.extend(selected)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        with self._lock, sqlite3.connect(self.path, timeout=5) as db:
            db.row_factory = sqlite3.Row
            order = "ASC" if cursor is not None else "DESC"
            rows = db.execute("SELECT journal.*, instance_colors.color FROM journal "
                              "LEFT JOIN instance_colors USING(instance)" + where +
                              f" ORDER BY sequence {order} LIMIT ?", values).fetchall()
        return [self._entry(row) for row in sorted(rows,
                key=lambda row: (row["at"], row["sequence"]))]
