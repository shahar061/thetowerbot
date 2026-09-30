"""List lab prices and durations the bot observed that differ from catalog/labs.v2.json.

Read-only. The bot never edits the catalog; the owner updates it by PR from
this list. Usage: uv run python tools/lab_observed_prices.py [--fleet-root PATH]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lab_catalog  # noqa: E402
from lab_starter_rollout import LabStarterRollout, StarterState, price_matches  # noqa: E402

DEFAULT_ROOT = Path.home() / ".local/share/thetowerbot/fleet"


def differences(state: StarterState) -> list[dict[str, Any]]:
    rows = []
    for lab_id, lab in sorted(state.labs.items()):
        if lab.level is None or lab.price is None:
            continue
        level = lab_catalog.level(lab_id, lab.level)
        # An abbreviated read ("1.34K") within its rounding is not a difference.
        if level is None or not price_matches(lab.price, level.coins):
            rows.append({"lab_id": lab_id, "level": lab.level, "field": "coins", "observed": lab.price,
                         "catalog": level.coins if level is not None else None})
        if level is not None and lab.seconds is not None and abs(lab.seconds - level.seconds) > 1:
            rows.append({"lab_id": lab_id, "level": lab.level, "field": "seconds",
                         "observed": lab.seconds, "catalog": level.seconds})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fleet-root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    rows = differences(LabStarterRollout(args.fleet_root).state(quarantine=False))
    if not rows:
        print("Every observed lab price and duration matches the catalog.")
    for row in rows:
        print(f"{row['lab_id']} Lv.{row['level']} {row['field']}: observed {row['observed']}, "
              f"catalog {row['catalog']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
