"""Project sidebar order persistence and merge acceptance with disposable data."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import server


SPEC = importlib.util.spec_from_file_location(
    "sidebar_merge", Path(__file__).resolve().parents[1] / "sync" / "json-merge.py")
merge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge)
EARLY = "2026-10-16T01:00:00.000001+00:00"
LATER = "2026-10-16T01:00:00.000002+00:00"
LATEST = "2026-10-16T01:00:00.000003+00:00"


class ProjectListOrderTests(unittest.TestCase):
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
        self.projects = [
            {"id": "a", "name": "Latest project name", "owner": "Ocean", "core": True,
             "prd_url": "https://example.invalid/keep", "created": EARLY},
            {"id": "b", "name": "Second project", "owner": "Lin", "created": LATER},
            {"id": "concurrent", "name": "New since drag began", "created": LATEST},
            {"id": "archived", "name": "Archived", "archived": True, "sort_order": 10},
            {"id": "deleted", "name": "Deleted", "deleted_at": LATER, "sort_order": 11},
            {"id": "complete", "name": "Automatically hidden", "sort_order": 12},
        ]
        self.tasks = [{"id": "t", "project_id": "complete", "milestone_id": "m", "done": True}]
        self.milestones = [{"id": "m", "project_id": "complete", "name": "Done"}]
        server.write_data({"projects": copy.deepcopy(self.projects), "tasks": copy.deepcopy(self.tasks),
                           "milestones": copy.deepcopy(self.milestones), "custom": {"keep": True}})

    def test_reorder_changes_only_selected_order_fields(self):
        result = server.reorder_projects({"ids": ["b", "a"]})
        self.assertTrue(result["ok"])
        data = server.read_data()
        self.assertEqual(data["tasks"], self.tasks)
        self.assertEqual(data["milestones"], self.milestones)
        self.assertEqual(data["custom"], {"keep": True})
        self.assertEqual(data["revision"], result["revision"])
        for actual, original in zip(data["projects"], self.projects):
            if original["id"] in ("a", "b"):
                self.assertEqual(actual["sort_order"], {"b": 0, "a": 1}[actual["id"]])
                self.assertEqual(set(actual["_field_updated"]), {"sort_order"})
                self.assertEqual({key: value for key, value in actual.items()
                                  if key not in {"sort_order", "updated_at", "_field_updated"}}, original)
            else:
                self.assertEqual(actual, original)
        self.assertEqual(data["projects"][0]["_field_updated"], data["projects"][1]["_field_updated"])

    def test_invalid_stale_requests_do_not_write(self):
        before = Path(server.DATA_FILE).read_bytes()
        for payload in (None, [], {}, {"ids": []}, {"ids": ["a"]}, {"ids": ["a", "a"]},
                        {"ids": ["a", 4]}, {"ids": ["a", ""]}, {"ids": ["a", "missing"]},
                        {"ids": ["b", "a", "archived"]}, {"ids": ["b", "a", "deleted"]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                server.reorder_projects(payload)
            self.assertEqual(Path(server.DATA_FILE).read_bytes(), before)

    def test_reorder_reads_current_content_and_preserves_field_clocks(self):
        server.patch_entity("projects", "a", {"name": "Edited while dragging", "owner": "Qin"})
        before = next(p for p in server.read_data()["projects"] if p["id"] == "a")
        server.reorder_projects({"ids": ["b", "a"]})
        actual = next(p for p in server.read_data()["projects"] if p["id"] == "a")
        self.assertEqual((actual["name"], actual["owner"]), ("Edited while dragging", "Qin"))
        for key in ("name", "owner"):
            self.assertEqual(actual["_field_updated"][key], before["_field_updated"][key])
        server.reorder_projects({"ids": ["a", "b"]})
        self.assertEqual({p["id"]: p["sort_order"] for p in server.read_data()["projects"]
                          if p["id"] in ("a", "b")}, {"a": 0, "b": 1})

    def test_new_project_appends_after_existing_explicit_orders_without_legacy_migration(self):
        before = server.read_data()["projects"]
        new = server.add_project({"name": "New core project", "core": True})
        self.assertEqual(new["sort_order"], 13)
        self.assertIn("sort_order", new["_field_updated"])
        self.assertEqual(server.read_data()["projects"][:-1], before)

    def test_without_custom_order_creation_retains_legacy_default_sort(self):
        data = server.read_data()
        for project in data["projects"]:
            project.pop("sort_order", None)
        server.write_data(data)
        new = server.add_project({"name": "Legacy default"})
        self.assertNotIn("sort_order", new)
        self.assertFalse(any("sort_order" in p for p in server.read_data()["projects"]))

    def test_http_route_and_restart_readback(self):
        http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{http.server_port}/projects/reorder"
            for ids, expected in ((["b", "a"], 200), (["a", "deleted"], 400)):
                request = urllib.request.Request(url, data=json.dumps({"ids": ids}).encode(),
                                                 headers={"Content-Type": "application/json"}, method="POST")
                try:
                    response = urllib.request.urlopen(request, timeout=5)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    self.assertEqual(response.status, expected, response.read().decode())
            # Read the persisted JSON independently of server memory.
            saved = json.loads(Path(server.DATA_FILE).read_text())
            self.assertEqual({p["id"]: p["sort_order"] for p in saved["projects"]
                              if p["id"] in ("a", "b")}, {"a": 1, "b": 0})
        finally:
            http.shutdown()
            http.server_close()
            thread.join(timeout=2)

    def test_merge_keeps_order_and_independent_edits_additions(self):
        base = server.read_data()
        ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
        for index, project in enumerate(ours["projects"][:2]):
            project["sort_order"] = 1 - index
            server.stamp_fields(project, ["sort_order"], LATER)
        theirs["projects"][0].update({"name": "Other device rename", "owner": "Qin"})
        server.stamp_fields(theirs["projects"][0], ["name", "owner"], LATEST)
        theirs["projects"].append({"id": "new-device", "name": "Concurrent addition", "created": LATEST})
        for first, second in ((ours, theirs), (theirs, ours)):
            result = merge.union(first, second, base)
            projects = {p["id"]: p for p in result["projects"]}
            self.assertEqual((projects["a"]["sort_order"], projects["b"]["sort_order"]), (1, 0))
            self.assertEqual((projects["a"]["name"], projects["a"]["owner"]), ("Other device rename", "Qin"))
            self.assertIn("new-device", projects)
            self.assertEqual(result["tasks"], self.tasks)
            self.assertEqual(result["milestones"], self.milestones)

    def test_merge_same_field_uses_order_clock_even_when_reverted(self):
        old = {**self.projects[0], "sort_order": 0}
        server.stamp_fields(old, ["sort_order"], EARLY)
        newer, stale = copy.deepcopy(old), copy.deepcopy(old)
        server.stamp_fields(newer, ["sort_order"], LATEST)
        stale["sort_order"] = 3
        server.stamp_fields(stale, ["sort_order"], LATER)
        stale["updated_at"] = "2027-01-01T00:00:00Z"
        for first, second in ((newer, stale), (stale, newer)):
            result = merge.merge_record(old, first, second, collection="projects")
            self.assertEqual(result["sort_order"], 0)
            self.assertEqual(result["_field_updated"]["sort_order"], LATEST)


if __name__ == "__main__":
    unittest.main()
