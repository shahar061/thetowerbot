"""Battle purchase rate before and after wave 20, per worker DB (read-only).

The measurement behind docs/superpowers/specs/2026-10-05-fast-battle-buying-design.md.
Cash spent after wave 20 is approximated as spent / (spent + growth of the
wallet seen at taps after wave 20); the DB records no wallet at death.

    uv run python tools/battle_purchase_rate.py --since 1791331200 \
        ~/.local/share/thetowerbot/fleet/workers/Tiramisu64_82/tower_bot.db \
        ~/.local/share/thetowerbot/fleet/workers/Tiramisu64_83/tower_bot.db
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from pathlib import Path


def report(path: str, since: float) -> None:
    connection = sqlite3.connect(f"file:{Path(path).expanduser()}?mode=ro", uri=True)
    runs = connection.execute(
        "SELECT id, started_at, ended_at, wave FROM runs WHERE started_at >= ? "
        "AND abandoned = 0 AND ended_at IS NOT NULL", (since,)).fetchall()
    known = [(ended - started) / wave for _, started, ended, wave in runs if wave]
    if not known:
        print(f"{path}: no completed runs since {since}")
        return
    per_wave_default = statistics.median(known)
    gaps: dict[str, list[float]] = {"<20": [], "20+": []}
    levels = {"<20": 0, "20+": 0}
    span = {"<20": 0.0, "20+": 0.0}
    ratio: dict[str, list[float]] = {"<20": [], "20+": []}
    shares: list[float] = []
    counted = 0
    for run_id, started, ended, wave in runs:
        per_wave = (ended - started) / wave if wave else per_wave_default
        wave_20 = started + 20 * per_wave
        rows = connection.execute(
            "SELECT ts, type, price, wallet FROM events WHERE run_id = ? "
            "AND type IN ('BattlePurchased', 'Tapped') ORDER BY seq", (run_id,)).fetchall()
        bought = [ts for ts, kind, _, _ in rows if kind == "BattlePurchased"]
        if len(bought) < 5:
            continue
        counted += 1
        span["<20"] += max(0.0, min(wave_20, ended) - started)
        span["20+"] += max(0.0, ended - wave_20)
        previous = started
        for ts in bought:
            key = "<20" if ts < wave_20 else "20+"
            gaps[key].append(ts - previous)
            levels[key] += 1
            previous = ts
        for ts, kind, price, wallet in rows:
            if kind == "Tapped" and price and wallet is not None:
                ratio["<20" if ts < wave_20 else "20+"].append(wallet / price)
        late = [(kind, price, wallet) for ts, kind, price, wallet in rows if ts >= wave_20]
        spent = sum(price or 0 for kind, price, _ in late if kind == "BattlePurchased")
        wallets = [wallet for kind, _, wallet in late if kind == "Tapped" and wallet is not None]
        growth = max(0, wallets[-1] - wallets[0]) if len(wallets) > 1 else 0
        if spent + growth:
            shares.append(spent / (spent + growth))
    print(f"\n### {path}: {counted} runs, sec/wave median {per_wave_default:.1f}")
    for key in ("<20", "20+"):
        if not levels[key]:
            print(f"{key}: no purchases")
            continue
        waves = span[key] / per_wave_default
        cash = statistics.median(ratio[key]) if ratio[key] else float("nan")
        print(f"{key}: levels={levels[key]} levels/wave={levels[key] / waves:.2f} "
              f"mean gap={statistics.mean(gaps[key]):.1f}s cash/price at tap median={cash:.1f}x")
    share = statistics.median(shares) if shares else float("nan")
    print(f"post-wave-20 cash spent share (approx.) median={share:.0%}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=float, required=True, help="unix seconds")
    parser.add_argument("dbs", nargs="+")
    args = parser.parse_args()
    for path in args.dbs:
        report(path, args.since)


if __name__ == "__main__":
    main()
