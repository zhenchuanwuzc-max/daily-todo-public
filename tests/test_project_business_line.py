"""Project labels and filtered ordering use disposable stores, never live todos."""
import copy
import json
from pathlib import Path
import unittest

import server
from tests import test_project_api as api
from tests.test_project_merge import EARLY, LATER, LATEST, edited, merge


class ProjectBusinessLineTests(unittest.TestCase):
    setUp = api.ProjectApiTests.setUp
    stop_http = api.ProjectApiTests.stop_http
    request = api.ProjectApiTests.request
    project = api.ProjectApiTests.project
    milestone = api.ProjectApiTests.milestone
    task = api.ProjectApiTests.task

    def test_create_edit_clear_readback_keeps_children_and_unrelated_fields(self):
        project = self.project(business_line="crm", launch_date="2026-12-01")
        self.assertEqual(project["business_line"], "crm")
        self.assertIn("business_line", project["_field_updated"])
        milestone = self.milestone(project, due="2026-11-20")
        self.task(project_id=project["id"], milestone_id=milestone["id"])
        before = self.request("GET", "/todos")
        for value in ("logistics", None, "crm"):
            project = self.request("PATCH", "/projects/" + project["id"], {"business_line": value})["project"]
            self.assertEqual(project["business_line"], value)
            saved = json.loads(Path(self.file).read_text())
            self.assertEqual(saved["projects"][0], project)
            self.assertEqual((project["owner"], project["launch_date"]), ("Ocean", "2026-12-01"))
            for collection in ("tasks", "milestones"):
                self.assertEqual(saved[collection], before[collection])
        project = self.request("PATCH", "/projects/" + project["id"], {"goal": "仅更新目标"})["project"]
        self.assertEqual(project["business_line"], "crm")

    def test_legacy_read_and_omitted_patch_do_not_migrate_projects(self):
        project = self.project()
        self.assertNotIn("business_line", project)
        before = Path(self.file).read_bytes()
        self.request("GET", "/todos")
        self.request("GET", "/projects/" + project["id"])
        self.assertEqual(Path(self.file).read_bytes(), before)
        result = self.request("PATCH", "/projects/" + project["id"], {"owner": "Lin"})["project"]
        self.assertNotIn("business_line", result)
        self.assertIsNone(self.project(business_line=None)["business_line"])

    def test_invalid_labels_reject_entire_request_without_write(self):
        project = self.project(business_line="crm")
        before = Path(self.file).read_bytes()
        for value in ("", "CRM", "other", " crm ", False, 1, [], {}):
            with self.subTest(value=value):
                self.request("POST", "/projects/add", {"name": "Invalid", "business_line": value}, expected=400)
                self.request("PATCH", "/projects/" + project["id"],
                             {"name": "Should not rename", "business_line": value}, expected=400)
                self.assertEqual(Path(self.file).read_bytes(), before)

    def test_template_uses_new_project_label_and_retries_include_it(self):
        source = self.project("Template", business_line="crm")
        milestone = self.milestone(source)
        self.task(project_id=source["id"], milestone_id=milestone["id"])
        self.request("POST", "/projects/" + source["id"] + "/template", {})
        for index, extra in enumerate(({}, {"business_line": None}, {"business_line": "logistics"})):
            payload = {"name": "Clone", "template_id": source["id"], "request_id": f"label-{index}", **extra}
            clone = self.request("POST", "/projects/add", payload)["project"]
            self.assertEqual(clone.get("business_line"), extra.get("business_line"))
            before = Path(self.file).read_bytes()
            self.assertEqual(self.request("POST", "/projects/add", payload)["project"], clone)
            self.request("POST", "/projects/add", {**payload, "business_line": "crm"}, expected=409)
            self.assertEqual(Path(self.file).read_bytes(), before)
        payload = {"name": "Old client", "request_id": "old-client"}
        old = self.request("POST", "/projects/add", payload)["project"]
        self.assertEqual(self.request("POST", "/projects/add", {**payload, "business_line": None})["project"], old)

    def test_scoped_order_replaces_slots_including_legacy_and_duplicate_orders(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                projects = [
                    {"id": identity, "name": identity, "business_line": label, "created": f"2026-10-{i+1:02d}"}
                    for i, (identity, label) in enumerate((("a", "crm"), ("b", "logistics"),
                        ("c", "crm"), ("u", None), ("d", "crm"), ("concurrent", "crm")))
                ]
                for index, project in enumerate(projects):
                    if not legacy:
                        project["sort_order"] = index
                    elif index < 2:
                        project["sort_order"] = 0
                hidden = [{"id": "archived", "archived": True, "sort_order": 2},
                          {"id": "deleted", "deleted_at": LATER, "sort_order": 4}]
                children = server.read_data()["tasks"]
                server.write_data({"projects": projects + hidden, "tasks": children, "milestones": []})
                # Only the caller's three known CRM projects move. A fourth CRM
                # project added during the drag remains at its existing position.
                server.patch_entity("projects", "b", {"owner": "Other device"})
                before = server.read_data()
                result = self.request("POST", "/projects/reorder", {
                    "ids": ["d", "a", "c"], "business_line": "crm"})
                self.assertEqual([p["id"] for p in result["orders"]], ["d", "b", "a", "u", "c", "concurrent"])
                saved = server.read_data()
                active = sorted(saved["projects"][:-2], key=lambda p: p["sort_order"])
                self.assertEqual([p["id"] for p in active], ["d", "b", "a", "u", "c", "concurrent"])
                self.assertEqual(saved["projects"][-2:], hidden)
                self.assertEqual(saved["tasks"], children)
                for actual, original in zip(saved["projects"][:-2], before["projects"][:-2]):
                    self.assertEqual({k: v for k, v in actual.items() if k not in ("sort_order", "updated_at", "_field_updated")},
                                     {k: v for k, v in original.items() if k not in ("sort_order", "updated_at", "_field_updated")})
                self.assertEqual(next(p for p in active if p["id"] == "b")["owner"], "Other device")

    def test_scoped_reorder_rejects_stale_labels_and_bad_scopes(self):
        a, b = self.project("A", business_line="crm"), self.project("B", business_line="crm")
        self.request("PATCH", "/projects/" + b["id"], {"business_line": "logistics"})
        before = Path(self.file).read_bytes()
        for scope, expected in (("crm", 409), (None, 400), ("", 400), ([], 400)):
            self.request("POST", "/projects/reorder", {"ids": [b["id"], a["id"]], "business_line": scope}, expected=expected)
            self.assertEqual(Path(self.file).read_bytes(), before)


class ProjectLabelMergeTests(unittest.TestCase):
    def test_merge_independent_label_and_content_updates_both_directions(self):
        base = {"tasks": [], "projects": [edited({"id": "p", "name": "Old"}, EARLY, business_line="crm")]}
        ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
        ours["projects"][0] = edited(ours["projects"][0], LATER, business_line="logistics")
        theirs["projects"][0] = edited(theirs["projects"][0], LATEST, name="Other device", owner="Lin")
        theirs["projects"].append({"id": "concurrent", "name": "New project"})
        for first, second in ((ours, theirs), (theirs, ours)):
            result = merge.union(first, second, base)
            project = next(p for p in result["projects"] if p["id"] == "p")
            self.assertEqual((project["business_line"], project["name"], project["owner"]), ("logistics", "Other device", "Lin"))
            self.assertEqual(len(result["projects"]), 2)

    def test_newer_clear_beats_stale_label_and_validation_accepts_legacy(self):
        base = edited({"id": "p"}, EARLY, business_line="crm")
        stale, cleared = edited(base, LATER, business_line="logistics"), edited(base, LATEST, business_line=None)
        for first, second in ((stale, cleared), (cleared, stale)):
            result = merge.merge_record(base, first, second, collection="projects")
            self.assertIsNone(result["business_line"])
            self.assertEqual(result["_field_updated"]["business_line"], LATEST)
        merge.validate({"tasks": [], "projects": [{"id": "old"}, cleared]})
        for value in ("CRM", "", [], False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                merge.validate({"tasks": [], "projects": [{"id": "p", "business_line": value}]})


if __name__ == "__main__":
    unittest.main()
