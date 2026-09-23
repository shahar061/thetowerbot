"""Which opening variant each fresh reroll account plays.

A variant is assigned once, when an emulator joins a run through "Start new
reroll", and stored beside the account's data in
`workers/<name>/reroll-variant.json` so it outlives retirement. See
`docs/superpowers/specs/2026-09-23-reroll-variants-design.md`.
"""

from __future__ import annotations

import json
import os
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

import builds
import db as bot_db
import upgrades
from fleet.reroll_metrics import read_play

VARIANT_FILE = "reroll-variant.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def opening_variants() -> tuple[builds.Variant, ...]:
    opening = builds.by_id("opening")
    return opening.variants if opening is not None else ()


def read_variant(worker_root: Path) -> str | None:
    try:
        variant = json.loads((Path(worker_root) / VARIANT_FILE).read_text(encoding="utf-8"))["variant"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return variant if isinstance(variant, str) and variant else None


def assign_variant(worker_root: Path, *, workers_dir: Path,
                   variants: Sequence[builds.Variant],
                   now: Callable[[], str] = _now) -> str:
    """The worker's variant, choosing and storing one if it has none.

    The choice is the variant with the fewest accounts so far, ties in pack
    order, so the groups stay even as emulators come and go.
    """
    worker_root = Path(worker_root)
    existing = read_variant(worker_root)
    if existing is not None:
        return existing
    if not variants:
        raise ValueError("no_variants")
    known = {variant.id for variant in variants}
    counts = Counter(variant for folder in _folders(workers_dir)
                     if (variant := read_variant(folder)) in known)
    chosen = min(variants, key=lambda variant: counts[variant.id]).id
    worker_root.mkdir(parents=True, exist_ok=True)
    _write(worker_root / VARIANT_FILE, {"variant": chosen, "assigned_at": now()})
    return chosen


def _folders(workers_dir: Path) -> list[Path]:
    try:
        return sorted(path for path in Path(workers_dir).iterdir() if path.is_dir())
    except OSError:
        return []


def _write(path: Path, payload: dict[str, str]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def variant_name(variant_id: str, variants: Sequence[builds.Variant]) -> str:
    return next((v.name for v in variants if v.id == variant_id), f"Unknown ({variant_id})")


def caps_summary(caps: Mapping[str, builds.LevelCap]) -> str:
    """`Damage, Attack Speed ≤ 1 + 1 per Coins / Wave · Coins / Wave ≤ 5`."""
    groups: dict[builds.LevelCap, list[str]] = {}
    for row, cap in caps.items():
        groups.setdefault(cap, []).append(_upgrade_name(row))
    parts = []
    for cap, names in groups.items():
        rule = f"{cap.base:g}"
        if cap.ratio:
            rule += f" + {cap.ratio:g} per " + " + ".join(_upgrade_name(r) for r in cap.per)
        parts.append(f"{', '.join(names)} ≤ {rule}")
    return " · ".join(parts)


def compare(workers_dir: Path, variants: Sequence[builds.Variant]) -> list[dict[str, Any]]:
    """One row per variant, then one per unknown id, over every assigned worker.

    Retired workers count: their folders stay on disk. Median and fastest
    cover only the accounts that reached W20, so `reached` sits beside them.
    """
    times: dict[str, list[float | None]] = {v.id: [] for v in variants}
    for folder in _folders(workers_dir):
        variant = read_variant(folder)
        if variant is None:
            continue
        play = _own_play(folder)
        times.setdefault(variant, []).append(play[0] if play is not None else None)
    caps = {v.id: caps_summary(v.level_caps) for v in variants}
    rows = []
    for variant_id, seconds in times.items():
        reached = sorted(s for s in seconds if s is not None)
        rows.append({
            "id": variant_id, "name": variant_name(variant_id, variants),
            "caps": caps.get(variant_id, ""), "accounts": len(seconds),
            "reached": len(reached),
            "median_seconds": statistics.median(reached) if reached else None,
            "fastest_seconds": reached[0] if reached else None,
        })
    return rows


def _own_play(folder: Path) -> tuple[float | None, float] | None:
    """`read_play` for the account registered in this folder, else None.

    A recreated emulator can reuse an old name, and with it a database bound
    to the previous account; that history is not this account's play. The
    same guard `observed_metrics` applies.
    """
    db_path = folder / "tower_bot.db"
    try:
        account = json.loads((folder / "fleet-registration.json").read_text(encoding="utf-8"))["account_id"]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not db_path.is_file() or bot_db.bound_account(db_path) != account:
        return None
    return read_play(db_path)


def _upgrade_name(upgrade_id: str) -> str:
    upgrade = upgrades.by_id(upgrade_id)
    return upgrade.name if upgrade is not None else upgrade_id
