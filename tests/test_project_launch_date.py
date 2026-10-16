"""Optional project launch dates do not change milestone or task scheduling."""
from pathlib import Path
import unittest

import server
from tests import test_project_api as api
from tests.test_project_merge import EARLY, LATER, LATEST, edited, merge


class ProjectLaunchDateTests(unittest.TestCase):
    setUp = api.ProjectApiTests.setUp
    stop_http = api.ProjectApiTests.stop_http
    request = api.ProjectApiTests.request
    project = api.ProjectApiTests.project
    milestone = api.ProjectApiTests.milestone
    task = api.ProjectApiTests.task

    def test_create_edit_clear_readback_and_child_dates_are_independent(self):
        project = self.project(launch_date="2026-11-06")
        self.assertEqual(project["launch_date"], "2026-11-06")
        self.assertIn("launch_date", project["_field_updated"])
        milestone = self.milestone(project, due="2026-11-04")
        task = self.task(project_id=project["id"], milestone_id=milestone["id"],
                         deadline="2026-11-03", due="2026-11-01")
        before = self.request("GET", "/todos")
        project = self.request("PATCH", "/projects/" + project["id"],
                               {"launch_date": "2026-11-10"})["project"]
        self.assertEqual(project["launch_date"], "2026-11-10")
        changed_at = project["_field_updated"]["launch_date"]
        after = self.request("GET", "/todos")
        for collection in ("tasks", "milestones"):
            self.assertEqual(after[collection], before[collection])
        for value in ("", "2026-11-11", None):
            project = self.request("PATCH", "/projects/" + project["id"], {"launch_date": value})["project"]
            self.assertEqual(project["launch_date"], value or None)
            self.assertGreater(project["_field_updated"]["launch_date"], changed_at)
            changed_at = project["_field_updated"]["launch_date"]
        self.assertIsNone(self.request("GET", "/projects/" + project["id"])["project"]["launch_date"])
        self.assertEqual(self.request("GET", "/todos")["milestones"][0]["due"], "2026-11-04")
        self.assertEqual(next(t for t in server.read_data()["tasks"] if t["id"] == task["id"])["deadline"], "2026-11-03")

    def test_invalid_date_rejects_entire_add_or_patch(self):
        project = self.project(launch_date="2026-12-01")
        before = Path(self.file).read_bytes()
        for value in ("2026-02-30", "2026-13-01", "2026-1-02", "2026/01/02", " tomorrow ",
                      "2026-11-02T00:00:00Z", 20261102, False, [], {}):
            with self.subTest(value=value):
                self.request("POST", "/projects/add", {"name": "Invalid date", "launch_date": value}, expected=400)
                self.assertEqual(Path(self.file).read_bytes(), before)
                self.request("PATCH", "/projects/" + project["id"],
                             {"name": "Should not rename", "launch_date": value}, expected=400)
                self.assertEqual(Path(self.file).read_bytes(), before)

    def test_legacy_read_and_unrelated_edit_do_not_add_launch_date(self):
        project = self.project()
        self.assertNotIn("launch_date", project)
        before = Path(self.file).read_bytes()
        self.request("GET", "/todos")
        self.request("GET", "/projects/" + project["id"])
        self.assertEqual(Path(self.file).read_bytes(), before)
        project = self.request("PATCH", "/projects/" + project["id"], {"owner": "Lin"})["project"]
        self.assertNotIn("launch_date", project)
        for value in (None, ""):
            self.assertIsNone(self.project(launch_date=value)["launch_date"])

    def test_creation_fingerprint_includes_launch_date_and_normalizes_blanks(self):
        payload = {"name": "Retry", "request_id": "launch-date-request", "launch_date": "2026-11-05"}
        first = self.request("POST", "/projects/add", payload)["project"]
        before = Path(self.file).read_bytes()
        self.assertEqual(self.request("POST", "/projects/add", payload)["project"], first)
        for value in ("2026-11-06", None, ""):
            self.request("POST", "/projects/add", {**payload, "launch_date": value}, expected=409)
            self.assertEqual(Path(self.file).read_bytes(), before)
        legacy_request = {"name": "Previously supported request", "request_id": "no-launch-date"}
        first = self.request("POST", "/projects/add", legacy_request)["project"]
        before = Path(self.file).read_bytes()
        for value in (None, ""):
            self.assertEqual(self.request("POST", "/projects/add", {**legacy_request, "launch_date": value})["project"], first)
            self.assertEqual(Path(self.file).read_bytes(), before)

    def test_template_clones_do_not_inherit_source_launch_date(self):
        source = self.project("Template", launch_date="2026-12-20")
        milestone = self.milestone(source, due="2026-12-19")
        self.task(project_id=source["id"], milestone_id=milestone["id"], deadline="2026-12-18")
        self.request("POST", "/projects/" + source["id"] + "/template", {})
        before = self.request("GET", "/todos")
        for index, extra in enumerate(({}, {"launch_date": ""}, {"launch_date": "2027-01-20"})):
            clone = self.project("Clone", template_id=source["id"], request_id=f"launch-clone-{index}", **extra)
            self.assertEqual(clone.get("launch_date"), extra.get("launch_date") or None)
            state = self.request("GET", "/todos")
            for m in state["milestones"]:
                if m["project_id"] == clone["id"]:
                    self.assertIsNone(m["due"])
            for t in state["tasks"]:
                if t.get("project_id") == clone["id"]:
                    self.assertIsNone(t["deadline"])
                    self.assertIsNone(t["due"])
        for collection in ("projects", "milestones", "tasks"):
            original_ids = {record["id"] for record in before[collection]}
            self.assertEqual([record for record in state[collection] if record["id"] in original_ids], before[collection])

    def test_merge_launch_date_clear_and_independent_fields(self):
        project = edited({"id": "p", "name": "Project", "launch_date": "2026-11-01"}, EARLY,
                         launch_date="2026-11-01")
        cleared = edited(project, LATER, launch_date=None)
        renamed = edited(project, LATEST, name="Renamed", owner="Lin")
        for first, second in ((cleared, renamed), (renamed, cleared)):
            result = merge.merge_record(project, first, second, collection="projects")
            self.assertIsNone(result["launch_date"])
            self.assertEqual((result["name"], result["owner"]), ("Renamed", "Lin"))
            self.assertEqual(result["_field_updated"]["launch_date"], LATER)
        stale = edited(project, LATER, launch_date="2026-11-09")
        reverted = edited(project, LATEST, launch_date="2026-11-01")
        for first, second in ((stale, reverted), (reverted, stale)):
            result = merge.merge_record(project, first, second, collection="projects")
            self.assertEqual(result["launch_date"], "2026-11-01")
            self.assertEqual(result["_field_updated"]["launch_date"], LATEST)


if __name__ == "__main__":
    unittest.main()
