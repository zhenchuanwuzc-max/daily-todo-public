import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server


class ReorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in {
            "DATA_DIR": self.tmp.name,
            "DATA_FILE": os.path.join(self.tmp.name, "todos.json"),
            "BACKUP_DIR": os.path.join(self.tmp.name, "backups"),
            "schedule_sync": lambda: None,
        }.items():
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tasks = [
            {"id": "a", "text": "Latest edited text", "priority": "P0", "done": False},
            {"id": "b", "text": "Second", "priority": "P0", "note": "Keep this"},
            {"id": "new", "text": "Concurrent addition", "priority": "P0"},
            {"id": "p1", "priority": "P1"},
            {"id": "done", "priority": "P0", "done": True},
            {"id": "future", "priority": "P0", "due": "2999-01-01", "deadline": "2026-10-01"},
            {"id": "seed", "priority": "P0", "recurring": "daily"},
            {"id": "suggest", "priority": "P0", "source": "claude-suggest"},
        ]
        server.write_data({"tasks": copy.deepcopy(self.tasks), "custom": "preserve"})

    def test_reorder_preserves_current_content_and_concurrent_additions(self):
        server.reorder_tasks({"ids": ["b", "a"]})
        data = server.read_data()
        self.assertEqual(data["custom"], "preserve")
        expected = copy.deepcopy(self.tasks)
        expected[0]["sort_order"] = 1
        expected[1]["sort_order"] = 0
        # Ordering now also records field timestamps for multi-device merging.
        for actual, original in zip(data["tasks"], expected):
            self.assertEqual({k: v for k, v in actual.items()
                              if k not in {"updated_at", "_field_updated"}}, original)
        for task in data["tasks"][:2]:
            self.assertTrue(task["_field_updated"]["sort_order"])
        server.reorder_tasks({"ids": ["a", "b"]})
        self.assertEqual(server.read_data()["tasks"][0]["sort_order"], 0)

    def test_legacy_p3_can_reorder_with_p2_without_rewriting_priorities(self):
        data = server.read_data()
        data["tasks"].extend([
            {"id": "p2", "text": "Current P2", "priority": "P2"},
            {"id": "legacy-p3", "text": "Old P3", "priority": "P3"},
        ])
        server.write_data(data)
        server.reorder_tasks({"ids": ["legacy-p3", "p2"]})
        tasks = {task["id"]: task for task in server.read_data()["tasks"]}
        self.assertEqual(tasks["legacy-p3"]["sort_order"], 0)
        self.assertEqual(tasks["p2"]["sort_order"], 1)
        self.assertEqual(tasks["legacy-p3"]["priority"], "P3")
        self.assertEqual(tasks["p2"]["priority"], "P2")
        self.assertNotIn("priority", tasks["legacy-p3"]["_field_updated"])
        server.reorder_tasks({"ids": ["p2", "legacy-p3"]})
        self.assertEqual(next(task for task in server.read_data()["tasks"]
                              if task["id"] == "legacy-p3")["sort_order"], 1)
        before = Path(server.DATA_FILE).read_bytes()
        for ids in (["p1", "legacy-p3"], ["a", "p2"]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                server.reorder_tasks({"ids": ids})
            self.assertEqual(Path(server.DATA_FILE).read_bytes(), before)

    def test_legacy_future_planned_date_allows_reordering_without_changing_dates(self):
        original = next(task for task in self.tasks if task["id"] == "future")
        server.reorder_tasks({"ids": ["future", "a"]})
        tasks = {task["id"]: task for task in server.read_data()["tasks"]}
        self.assertEqual(tasks["future"]["sort_order"], 0)
        self.assertEqual(tasks["a"]["sort_order"], 1)
        self.assertEqual(tasks["future"]["due"], original["due"])
        self.assertEqual(tasks["future"]["deadline"], original["deadline"])
        self.assertEqual(set(tasks["future"]["_field_updated"]), {"sort_order"})

    def test_invalid_or_stale_moves_do_not_write(self):
        with open(server.DATA_FILE) as handle:
            before = handle.read()
        for ids in [[], ["a"], ["a", "a"], ["a", {}], ["a", "missing"],
                    ["a", "p1"], ["a", "done"],
                    ["a", "seed"], ["a", "suggest"]]:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                server.reorder_tasks({"ids": ids})
            with open(server.DATA_FILE) as handle:
                self.assertEqual(handle.read(), before)


if __name__ == "__main__":
    unittest.main()
