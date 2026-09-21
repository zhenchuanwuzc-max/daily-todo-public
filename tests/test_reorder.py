import copy
import json
import os
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
            {"id": "future", "priority": "P0", "due": "2999-01-01"},
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
        self.assertEqual(data["tasks"], expected)
        server.reorder_tasks({"ids": ["a", "b"]})
        self.assertEqual(server.read_data()["tasks"][0]["sort_order"], 0)

    def test_invalid_or_stale_moves_do_not_write(self):
        with open(server.DATA_FILE) as handle:
            before = handle.read()
        for ids in [[], ["a"], ["a", "a"], ["a", {}], ["a", "missing"],
                    ["a", "p1"], ["a", "done"], ["a", "future"],
                    ["a", "seed"], ["a", "suggest"]]:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                server.reorder_tasks({"ids": ids})
            with open(server.DATA_FILE) as handle:
                self.assertEqual(handle.read(), before)


if __name__ == "__main__":
    unittest.main()
