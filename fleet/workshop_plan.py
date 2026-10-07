"""Read a worker's saved Workshop decision for the Plan graph tab."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fleet.build_route_runtime import WORKSHOP_PLAN_FILE, read_applied_revision


def load_workshop_plan(worker_dir: Path, account_id: str, now: float) -> dict[str, Any]:
    try:
        plan = json.loads((worker_dir / WORKSHOP_PLAN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"plan": None}
    # Another account's record (a replaced account) is never shown.
    if (not isinstance(plan, dict) or plan.get("account_id") != account_id
            or not all(isinstance(plan.get(key), dict)
                       for key in ("evaluation", "budget", "strategy", "upgrade_names"))):
        return {"plan": None}
    current = read_applied_revision(worker_dir / "build-route-applied.json", account_id)
    written = plan.get("written_at")
    age = max(0.0, now - written) if isinstance(written, (int, float)) else None
    return {"plan": plan, "age_seconds": age, "current_revision": current,
            "stale": current is None or plan.get("revision") != current}
