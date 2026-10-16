"""Project template and link acceptance against an isolated HTTP data store."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest

import server
from tests import test_project_api as api


class ProjectTemplateTests(unittest.TestCase):
    # Reuse only the isolated server helpers, without rerunning the base suite.
    setUp = api.ProjectApiTests.setUp
    stop_http = api.ProjectApiTests.stop_http
    request = api.ProjectApiTests.request
    project = api.ProjectApiTests.project
    milestone = api.ProjectApiTests.milestone
    task = api.ProjectApiTests.task

    def source(self):
        project = self.project("CRM模板", owner="原负责人", goal="原目标", core=True,
                               dingtalk_group_id="old-group", prd_url="https://example.invalid/original")
        milestones = [self.milestone(project, name, due="2026-10-24")
                      for name in ("准备", "开发上线", "效果运营")]
        tasks = []
        for count, milestone in zip((5, 4, 3), milestones):
            for index in range(count):
                task = self.task(text=f"{milestone['name']}-{index}", owner="已分配人员",
                                 project_id=project["id"], milestone_id=milestone["id"],
                                 priority="P0" if index == 0 else "P1", tag="work")
                task = self.request("PATCH", "/todos/" + task["id"], {
                    "done": True, "due": "2026-10-22", "sort_order": 30 + index})["task"]
                tasks.append(task)
            self.request("POST", "/todos/reorder", {
                "scope": "project", "project_id": project["id"], "milestone_id": milestone["id"],
                "ids": [t["id"] for t in reversed(tasks[-count:])]})
        self.request("POST", "/milestones/reorder", {
            "project_id": project["id"], "ids": [m["id"] for m in reversed(milestones)]})
        return project, milestones, tasks

    def snapshot(self, project):
        return self.request("POST", "/projects/" + project["id"] + "/template", {})["project"]["template_snapshot"]

    def clone(self, project, request_id="clone-one", **fields):
        return self.project("新项目", template_id=project["id"], request_id=request_id, **fields)

    def test_snapshot_freezes_three_milestones_twelve_tasks_and_clones_new_identities(self):
        source, milestones, tasks = self.source()
        before = self.request("GET", "/todos")
        snapshot = self.snapshot(source)
        self.assertEqual((len(snapshot["milestones"]), len(snapshot["tasks"])), (3, 12))
        self.assertEqual([m["id"] for m in snapshot["milestones"]], [m["id"] for m in reversed(milestones)])
        for milestone in milestones:
            source_ids = [t["id"] for t in tasks if t["milestone_id"] == milestone["id"]]
            self.assertEqual([t["id"] for t in snapshot["tasks"] if t["milestone_id"] == milestone["id"]],
                             list(reversed(source_ids)))
        after_save = self.request("GET", "/todos")
        self.assertEqual(after_save["tasks"], before["tasks"])
        self.assertEqual(after_save["milestones"], before["milestones"])

        # Editing the source after saving does not silently alter the reusable template.
        self.request("PATCH", "/todos/" + tasks[0]["id"], {"text": "模板保存后的修改"})
        self.request("DELETE", "/milestones/" + milestones[0]["id"])
        before_clone = self.request("GET", "/todos")
        project = self.clone(source)
        state = self.request("GET", "/todos")
        children = [t for t in state["tasks"] if t.get("project_id") == project["id"]]
        copied_milestones = [m for m in state["milestones"] if m["project_id"] == project["id"]]
        self.assertEqual((len(copied_milestones), len(children)), (3, 12))
        self.assertEqual(project["owner"], "Ocean")
        self.assertFalse(project["core"])
        self.assertFalse(project["archived"])
        self.assertEqual(project["template_source_id"], source["id"])
        self.assertEqual(project["template_saved_at"], snapshot["saved_at"])
        self.assertNotIn("template_snapshot", project)
        self.assertNotIn("dingtalk_group_id", project)
        self.assertNotIn("prd_url", project)
        self.assertEqual(project["goal"], "")
        old_ids = {record["id"] for name in ("tasks", "milestones", "projects") for record in before_clone[name]}
        new_records = [project, *copied_milestones, *children]
        self.assertEqual(len({r["id"] for r in new_records}), 16)
        self.assertTrue(old_ids.isdisjoint(r["id"] for r in new_records))
        copied_by_source = {m["template_source_milestone_id"]: m for m in copied_milestones}
        task_by_source = {t["template_source_task_id"]: t for t in children}
        for entry in snapshot["milestones"]:
            copied = copied_by_source[entry["id"]]
            self.assertEqual((copied["name"], copied["sort_order"]), (entry["name"], entry["sort_order"]))
            self.assertEqual(copied["owner"], "Ocean")
            self.assertIsNone(copied["due"])
        for entry in snapshot["tasks"]:
            copied = task_by_source[entry["id"]]
            for field in ("text", "priority", "tag", "sort_order", "project_sort_order"):
                self.assertEqual(copied.get(field), entry.get(field))
            self.assertEqual(copied["milestone_id"], copied_by_source[entry["milestone_id"]]["id"])
            self.assertEqual(copied["owner"], "Ocean")
            self.assertFalse(copied["done"])
            for field in ("due", "deadline", "done_at"):
                self.assertIsNone(copied[field])
        for collection in ("tasks", "milestones", "projects"):
            self.assertEqual([r for r in state[collection] if r["id"] in old_ids], before_clone[collection])

    def test_snapshot_excludes_deleted_and_recurring_but_preserves_unscheduled_tasks(self):
        source = self.project()
        milestone = self.milestone(source)
        deleted_milestone = self.milestone(source, "已删除节点")
        kept = self.task(text="正常事项", project_id=source["id"], milestone_id=milestone["id"])
        unscheduled = self.task(text="待安排", project_id=source["id"])
        orphan = self.task(text="已删除节点的事项", project_id=source["id"], milestone_id=deleted_milestone["id"])
        self.request("DELETE", "/milestones/" + deleted_milestone["id"])
        data = server.read_data()
        for index, fields in enumerate(({"deleted_at": "2026-10-15T01:00:00Z"}, {"recurring": "daily"},
                                        {"recur_source": "seed"}, {"auto_skipped": True}, {"source": "claude-suggest"})):
            data["tasks"].append({**kept, "id": f"exclude-{index}", **fields})
        server.write_data(data)
        snapshot = self.snapshot(source)
        self.assertEqual([m["id"] for m in snapshot["milestones"]], [milestone["id"]])
        self.assertEqual({t["id"] for t in snapshot["tasks"]}, {kept["id"], unscheduled["id"], orphan["id"]})
        self.assertTrue(all(t["milestone_id"] is None for t in snapshot["tasks"] if t["id"] != kept["id"]))
        project = self.clone(source)
        copied = [t for t in self.request("GET", "/todos")["tasks"] if t.get("project_id") == project["id"]]
        self.assertEqual(sum(t["milestone_id"] is None for t in copied), 2)

    def test_template_create_is_idempotent_for_concurrent_retries(self):
        source = self.project()
        milestone = self.milestone(source)
        self.task(project_id=source["id"], milestone_id=milestone["id"])
        self.snapshot(source)
        with ThreadPoolExecutor(max_workers=6) as pool:
            projects = list(pool.map(lambda _: self.clone(source), range(12)))
        self.assertEqual(len({p["id"] for p in projects}), 1)
        state = self.request("GET", "/todos")
        self.assertEqual((len(state["projects"]), len(state["milestones"]), len(state["tasks"])), (2, 2, 3))
        before = Path(self.file).read_bytes()
        self.clone(source)
        self.assertEqual(Path(self.file).read_bytes(), before)
        self.request("POST", "/projects/add", {"name": "不同内容", "template_id": source["id"],
                                                 "request_id": "clone-one"}, expected=409)
        self.assertEqual(Path(self.file).read_bytes(), before)

    def test_missing_invalid_or_deleted_template_never_partially_creates(self):
        source = self.project()
        before = Path(self.file).read_bytes()
        cases = [({"name": "无幂等号", "template_id": source["id"]}, 400),
                 ({"name": "未保存模板", "template_id": source["id"], "request_id": "first"}, 400),
                 ({"name": "不存在", "template_id": "missing", "request_id": "second"}, 404),
                 ({"name": "错误类型", "template_id": {}, "request_id": "third"}, 400)]
        for payload, status in cases:
            self.request("POST", "/projects/add", payload, expected=status)
            self.assertEqual(Path(self.file).read_bytes(), before)
        self.snapshot(source)
        data = server.read_data()
        data["projects"][0]["template_snapshot"]["tasks"].append({"id": "bad", "text": "坏关联", "milestone_id": "missing"})
        server.write_data(data)
        before = Path(self.file).read_bytes()
        self.request("POST", "/projects/add", {"name": "坏模板", "template_id": source["id"], "request_id": "bad"}, expected=400)
        self.assertEqual(Path(self.file).read_bytes(), before)
        self.request("DELETE", "/projects/" + source["id"])
        before = Path(self.file).read_bytes()
        self.request("POST", "/projects/" + source["id"] + "/template", {}, expected=404)
        self.assertEqual(Path(self.file).read_bytes(), before)

    def test_project_links_create_edit_and_clear_without_changing_children(self):
        project = self.project(prd_url=" https://example.invalid/prd?q=1#design ", materials_url="http://localhost:1234/materials")
        self.assertEqual(project["prd_url"], "https://example.invalid/prd?q=1#design")
        milestone = self.milestone(project)
        self.task(project_id=project["id"], milestone_id=milestone["id"])
        before = self.request("GET", "/todos")
        updated = self.request("PATCH", "/projects/" + project["id"], {"prd_url": "", "materials_url": "https://example.invalid/new"})["project"]
        self.assertEqual((updated["prd_url"], updated["materials_url"]), ("", "https://example.invalid/new"))
        self.assertGreater(updated["_field_updated"]["prd_url"], project["_field_updated"]["prd_url"])
        after = self.request("GET", "/todos")
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["milestones"], before["milestones"])

    def test_invalid_project_links_reject_entire_create_or_patch(self):
        project = self.project()
        before = Path(self.file).read_bytes()
        invalid = ["javascript:alert(1)", "data:text/html,x", "//example.invalid/doc", "file:///tmp/prd",
                   "https://user:password@example.invalid/doc", "https://@example.invalid/doc",
                   "https://example.invalid:bad/doc", "https://example.invalid:99999/doc",
                   "https://example.invalid\\@other.invalid", "https://example.invalid/has space",
                   "https://example.invalid/\nline", "https://example.invalid/\u200bhidden", "https://", None, 123, {}]
        for field in ("prd_url", "materials_url"):
            for value in invalid:
                with self.subTest(field=field, value=value):
                    self.request("POST", "/projects/add", {"name": "不能创建", field: value}, expected=400)
                    self.request("PATCH", "/projects/" + project["id"], {"name": "不能更名", field: value}, expected=400)
                    self.assertEqual(Path(self.file).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
