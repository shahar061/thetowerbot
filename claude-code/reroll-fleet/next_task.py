from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parent
BASE_MANIFEST = ROOT.parent / "tower-autonomy" / "manifest.json"


def ready_tasks(manifest: dict[str, Any], completed_base: set[str]) -> list[dict[str, Any]]:
    completed_local = {task["id"] for task in manifest["tasks"] if task["status"] == "completed"}
    return [
        task for task in manifest["tasks"]
        if task["status"] == "not_started"
        and set(task["depends_on"]) <= completed_local
        and set(task["requires_tower_tasks"]) <= completed_base
    ]


def main() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    base = json.loads(BASE_MANIFEST.read_text())
    completed_base = set(base["completed_task_ids"])
    for task in ready_tasks(manifest, completed_base):
        print(f"{task['id']}\t{task['title']}\t{task['task_file']}")


if __name__ == "__main__":
    main()
