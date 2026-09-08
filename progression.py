"""Farming comparisons from completed runs, using actual elapsed time."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any


def _usable(run: dict[str, Any]) -> bool:
    """The guard chain a run must pass to count toward farming measurements.

    Shared by compare_tiers and rates so the two never disagree about which
    runs are real farming data: a farm run (purpose defaults to "farm"),
    not abandoned, with a non-None tier/started_at/ended_at/coins, an
    elapsed time that is actually positive, and non-negative coins.
    """
    if run.get("purpose", "farm") != "farm":
        return False
    tier, end, coins = run.get("tier"), run.get("ended_at"), run.get("coins")
    start = run.get("started_at")
    return not (tier is None or start is None or end is None or coins is None
                or run.get("abandoned") or end <= start or coins < 0)


def compare_tiers(runs: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[int, list[tuple[float, float, int | None]]] = {}
    for run in runs:
        if not _usable(run):
            continue
        tier = run.get("tier")
        end, start, coins = run.get("ended_at"), run.get("started_at"), run.get("coins")
        grouped.setdefault(tier, []).append((coins, end-start, run.get("wave")))
    tiers = []
    for tier, samples in sorted(grouped.items()):
        waves = [r[2] for r in samples if r[2] is not None]
        tiers.append(dict(tier=tier, runs=len(samples),
                          coins_per_hour=sum(r[0] for r in samples) * 3600 / sum(r[1] for r in samples),
                          median_wave=median(waves) if waves else None))
    eligible = [t for t in tiers if t["runs"] >= 3]
    best = max(eligible, key=lambda t: t["coins_per_hour"], default=None)
    return dict(tiers=tiers, recommended_tier=best["tier"] if best else None,
                reason=("Best observed coin rate among tiers with at least three completed runs. "
                        "Milestone pushes are a separate goal; this does not change the game tier."
                        if best else "Complete at least three runs on a tier to establish a farming baseline."))


@dataclass(frozen=True)
class CurrencyRates:
    """Measured income, or the reason there is not a measurement yet.

    coins_per_hour is None when there is no baseline (fewer than
    MIN_SAMPLES usable runs on the tier) - we have not measured, so the
    horizon is unknown. It is 0.0, never None, when MIN_SAMPLES or more
    farm runs were measured and every one of them earned nothing - that is
    a fact, not a missing observation, and the resulting infinite horizon
    ("this tier will never pay for it at this rate") is the correct,
    actionable answer rather than "unknown".
    """

    coins_per_hour: float | None
    tier: int | None
    samples: int
    reason: str


MIN_SAMPLES = 3


def rates(runs: list[dict[str, Any]], *, tier: int | None = None) -> CurrencyRates:
    """The coin rate to plan against, for one tier.

    Pooled like compare_tiers - total coins over total elapsed - rather than a
    mean of per-run rates, so a 20-second run cannot outvote a 20-minute one.

    tier defaults to the most recently run tier (runs[0], since db.list_runs
    orders newest first) when not given.
    """
    usable = [r for r in runs if _usable(r)]
    if tier is None:
        tier = usable[0]["tier"] if usable else None
    samples = [r for r in usable if r["tier"] == tier]
    if len(samples) < MIN_SAMPLES:
        return CurrencyRates(None, tier, len(samples),
                             "Complete at least three farm runs on a tier "
                             "to establish an income baseline.")
    coins = sum(r["coins"] for r in samples)
    elapsed = sum(r["ended_at"] - r["started_at"] for r in samples)
    if elapsed <= 0:
        # Defensive only: _usable already requires end > start per row, so a
        # sum of positive elapsed times cannot reach here. Guards the
        # division itself rather than trusting that invariant blindly.
        return CurrencyRates(None, tier, len(samples),
                             "The measured runs had no elapsed time, so no rate "
                             "can be established.")
    if coins <= 0:
        # _usable requires coins >= 0 per row, so pooled coins <= 0 means
        # exactly 0: samples were measured and every one earned nothing.
        # That is a fact, not a missing observation - 0.0, never None.
        return CurrencyRates(0.0, tier, len(samples),
                             f"Measured across {len(samples)} farm runs on tier "
                             f"{tier}: zero coins earned. The horizon at this "
                             "rate is infinite.")
    return CurrencyRates(coins * 3600 / elapsed, tier, len(samples),
                         f"Pooled across {len(samples)} farm runs on tier {tier}.")
