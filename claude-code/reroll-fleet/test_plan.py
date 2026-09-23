from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from next_task import ready_tasks


class PlanIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest: dict[str, Any] = json.loads((ROOT / "manifest.json").read_text())
        self.base: dict[str, Any] = json.loads((ROOT.parent / "tower-autonomy" / "manifest.json").read_text())

    def test_task_references_resolve(self) -> None:
        tasks = self.manifest["tasks"]
        ids = {task["id"] for task in tasks}
        base_ids = {task["id"] for task in self.base["tasks"]}
        self.assertEqual(13, len(tasks))
        self.assertEqual(13, len(ids))
        for task in tasks:
            self.assertLessEqual(set(task["depends_on"]), ids)
            self.assertLessEqual(set(task["requires_tower_tasks"]), base_ids)
            self.assertTrue((ROOT / task["task_file"]).is_file())

    def test_initial_ready_task_is_identity_isolation(self) -> None:
        ready = ready_tasks(self.manifest, set(self.base["completed_task_ids"]))
        self.assertEqual(["M01"], [task["id"] for task in ready])


if __name__ == "__main__":
    unittest.main()
