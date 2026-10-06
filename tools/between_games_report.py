"""Report the time between games and lab starts per hour for one fleet worker (read-only).

Usage:
    uv run python tools/between_games_report.py <worker> [--hours 24]

Reads ~/.local/share/thetowerbot/fleet/workers/<worker>/tower_bot.db read-only. A gap is the
seconds from a RunEnded to the next RunStarted; a RunEnded followed by another RunEnded counts
once, from the later one. Lab starts per hour is the LabResearchStarted count over the window.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import time
from pathlib import Path

WORKERS_DIR = Path.home() / ".local/share/thetowerbot/fleet/workers"


def gaps(rows: list[tuple[float, str]]) -> list[float]:
    """Seconds from each RunEnded to the next RunStarted, over (ts, type) rows sorted by time."""
    out: list[float] = []
    ended: float | None = None
    for ts, kind in rows:
        if kind == "RunEnded":
            ended = ts  # A second RunEnded before any RunStarted replaces the first.
        elif kind == "RunStarted" and ended is not None:
            out.append(ts - ended)
            ended = None
    return out


def summary(values: list[float]) -> dict[str, float]:
    """Count, median, p90 and mean of the values (zeros when empty)."""
    if not values:
        return {"n": 0, "median": 0.0, "p90": 0.0, "mean": 0.0}
    p90 = statistics.quantiles(values, n=10)[8] if len(values) >= 2 else values[0]
    return {"n": len(values), "median": statistics.median(values), "p90": p90,
            "mean": statistics.fmean(values)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("worker", help="worker name, e.g. Tiramisu64_82")
    parser.add_argument("--hours", type=float, default=24.0, help="window length (default 24)")
    args = parser.parse_args(argv)
    path = WORKERS_DIR / args.worker / "tower_bot.db"
    if not path.exists():
        print(f"no database at {path}")
        return 1
    since = time.time() - args.hours * 3600
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT ts, type FROM events WHERE ts >= ? AND type IN "
                          "('RunStarted', 'RunEnded', 'LabResearchStarted') ORDER BY ts, seq",
                          (since,)).fetchall()
    lab_starts = sum(1 for _, kind in rows if kind == "LabResearchStarted")
    stats = summary(gaps([row for row in rows if row[1] != "LabResearchStarted"]))
    print(f"{args.worker}, last {args.hours:g} h")
    print(f"between games: n={stats['n']:g} median={stats['median']:.1f}s "
          f"p90={stats['p90']:.1f}s mean={stats['mean']:.1f}s")
    print(f"lab starts: {lab_starts} ({lab_starts / args.hours:.2f}/h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
