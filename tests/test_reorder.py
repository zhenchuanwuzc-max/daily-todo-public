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


class ProjectTaskReorderTest(unittest.TestCase):
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
        task = {"project_id": "p", "milestone_id": "m", "owner": "Ocean", "done": False,
                "due": "2999-01-01", "deadline": "2026-10-01", "created": "2026-10-01T00:00:00Z"}
        self.tasks = [
            {**task, "id": "a", "text": "First", "priority": "P0", "sort_order": 20},
            {**task, "id": "b", "text": "Second", "priority": "P2", "sort_order": 10},
            {**task, "id": "done", "text": "Completed", "priority": "P1", "done": True,
             "done_at": "2026-10-02T00:00:00Z"},
            {**task, "id": "concurrent", "text": "Concurrent addition", "priority": "P1"},
            {**task, "id": "other-milestone", "milestone_id": "m2"},
            {**task, "id": "other-project", "project_id": "other", "milestone_id": "other-m"},
            {**task, "id": "deleted", "deleted_at": "2026-10-01T00:00:00Z"},
            {**task, "id": "seed", "recurring": "daily"},
            {**task, "id": "instance", "recur_source": "seed"},
            {**task, "id": "suggest", "source": "claude-suggest"},
            {**task, "id": "skipped", "auto_skipped": True},
            {**task, "id": "unscheduled", "milestone_id": None},
            {**task, "id": "deleted-milestone", "milestone_id": "removed-m"},
            {**task, "id": "missing-milestone", "milestone_id": "missing-m"},
        ]
        server.write_data({"tasks": copy.deepcopy(self.tasks), "projects": [{"id": "p"}, {"id": "other"}],
                           "milestones": [{"id": "m", "project_id": "p"}, {"id": "m2", "project_id": "p"},
                                          {"id": "other-m", "project_id": "other"},
                                          {"id": "removed-m", "project_id": "p", "deleted_at": "2026-10-01T00:00:00Z"}]})

    def reorder(self, ids, **extra):
        return server.reorder_tasks({"ids": ids, "scope": "project", "project_id": "p", "milestone_id": "m", **extra})

    def test_project_reorder_crosses_priorities_and_includes_completed_without_other_changes(self):
        self.reorder(["done", "b", "a"])
        tasks = {task["id"]: task for task in server.read_data()["tasks"]}
        for position, identity in enumerate(["done", "b", "a"]):
            self.assertEqual(tasks[identity]["project_sort_order"], position)
            self.assertEqual(set(tasks[identity]["_field_updated"]), {"project_sort_order"})
        for expected in self.tasks:
            actual = tasks[expected["id"]]
            self.assertEqual({key: value for key, value in actual.items()
                              if key not in {"project_sort_order", "updated_at", "_field_updated"}}, expected)
        self.assertEqual(tasks["a"]["sort_order"], 20)
        self.assertEqual(tasks["b"]["sort_order"], 10)
        self.assertNotIn("project_sort_order", tasks["concurrent"])

    def test_project_reorder_unscheduled_keeps_deleted_links_for_restore(self):
        self.reorder(["missing-milestone", "deleted-milestone", "unscheduled"], milestone_id=None)
        tasks = {task["id"]: task for task in server.read_data()["tasks"]}
        self.assertEqual([tasks[key]["project_sort_order"] for key in
                          ("missing-milestone", "deleted-milestone", "unscheduled")], [0, 1, 2])
        self.assertEqual(tasks["deleted-milestone"]["milestone_id"], "removed-m")
        self.assertEqual(tasks["missing-milestone"]["milestone_id"], "missing-m")
        self.reorder(["unscheduled", "deleted-milestone"], milestone_id="")
        self.assertEqual(next(t for t in server.read_data()["tasks"] if t["id"] == "unscheduled")["project_sort_order"], 0)

    def test_project_reorder_rejects_stale_or_cross_scope_rows_atomically(self):
        before = Path(server.DATA_FILE).read_bytes()
        for identity in ("missing", "other-milestone", "other-project", "deleted", "seed", "instance",
                         "suggest", "skipped", "unscheduled", "deleted-milestone", "missing-milestone"):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.reorder(["b", identity, "a"])
            self.assertEqual(Path(server.DATA_FILE).read_bytes(), before)

    def test_project_reorder_requires_current_project_and_milestone(self):
        before = Path(server.DATA_FILE).read_bytes()
        for extra in ({"project_id": None}, {"project_id": ""}, {"project_id": []},
                      {"project_id": "missing"}, {"project_id": "other"}, {"milestone_id": 1},
                      {"milestone_id": False}, {"milestone_id": []}, {"milestone_id": "missing"},
                      {"milestone_id": "removed-m"}, {"milestone_id": "other-m"}, {"scope": "unknown"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.reorder(["b", "a"], **extra)
            self.assertEqual(Path(server.DATA_FILE).read_bytes(), before)
        data = server.read_data()
        data["projects"][0]["deleted_at"] = "2026-10-01T00:00:00Z"
        server.write_data(data)
        before = Path(server.DATA_FILE).read_bytes()
        with self.assertRaises(ValueError):
            self.reorder(["b", "a"])
        self.assertEqual(Path(server.DATA_FILE).read_bytes(), before)

    def test_project_order_field_survives_legacy_task_patch(self):
        self.reorder(["b", "a"])
        server.patch_task("a", {"owner": "Lin"})
        task = next(t for t in server.read_data()["tasks"] if t["id"] == "a")
        self.assertEqual((task["owner"], task["project_sort_order"], task["sort_order"]), ("Lin", 1, 20))


if __name__ == "__main__":
    unittest.main()
