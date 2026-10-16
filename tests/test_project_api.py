"""API-level acceptance with a fresh isolated store; no production data is opened."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import server


class ProjectApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = os.path.join(self.tmp.name, "data", "todos.json")
        os.makedirs(os.path.dirname(self.file))
        self.legacy = {"id": "legacy", "text": "原有事项", "done": False,
                       "priority": "P1", "due": "2026-10-17", "custom": "keep"}
        Path(self.file).write_text(json.dumps({"tasks": [self.legacy], "custom": "keep"}))
        for name, value in {
            "DATA_DIR": os.path.dirname(self.file), "DATA_FILE": self.file,
            "APP_SUPPORT_DIR": self.tmp.name, "STATE_FILE": os.path.join(self.tmp.name, "state.json"),
            "BACKUP_DIR": os.path.join(self.tmp.name, "backups"), "schedule_sync": lambda: None,
        }.items():
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {"TODO_DISABLE_SYNC": "1", "TODO_PREVIEW": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.addCleanup(self.stop_http)

    def stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, payload=None, expected=200):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = json.loads(response.read())
            self.assertEqual(response.status, expected, body)
            return body

    def project(self, name="项目甲", **fields):
        return self.request("POST", "/projects/add", {"name": name, **fields})["project"]

    def milestone(self, project, name="方案确认", **fields):
        return self.request("POST", "/milestones/add", {
            "name": name, "project_id": project["id"], **fields})["milestone"]

    def task(self, **fields):
        return self.request("POST", "/todos/add", {
            "text": "检查验收", "deadline": "2026-11-01", **fields})["task"]

    def test_read_legacy_has_no_migration_write(self):
        before = Path(self.file).read_bytes()
        mtime = os.stat(self.file).st_mtime_ns
        data = self.request("GET", "/todos")
        self.assertEqual(data["tasks"], [self.legacy])
        self.assertEqual(data["projects"], [])
        self.assertEqual(data["milestones"], [])
        self.assertEqual(data["revision"], 0)
        self.assertEqual(Path(self.file).read_bytes(), before)
        self.assertEqual(os.stat(self.file).st_mtime_ns, mtime)
        self.assertNotIn("owner", data["tasks"][0])
        self.assertNotIn("deadline", data["tasks"][0])

    def test_project_crud_archive_and_milestone_order_persist(self):
        project = self.project(owner=" 张三 ", goal="首版", core=True)
        self.assertEqual(project["owner"], "张三")
        first = self.milestone(project)
        second = self.milestone(project, "交付", due="2026-11-01")
        self.assertEqual(first["owner"], "张三")
        self.request("POST", "/milestones/reorder", {
            "project_id": project["id"], "ids": [second["id"], first["id"]]})
        self.request("PATCH", "/projects/" + project["id"], {
            "name": "更名项目", "owner": "Ocean", "archived": True})
        self.request("PATCH", "/milestones/" + first["id"], {"name": "确认范围", "owner": "李四"})
        data = self.request("GET", "/todos")
        self.assertEqual(data["projects"][0]["name"], "更名项目")
        self.assertTrue(data["projects"][0]["archived"])
        self.assertEqual(data["milestones"][0]["sort_order"], 1)
        self.assertEqual(data["milestones"][1]["sort_order"], 0)
        self.assertEqual(data["milestones"][0]["owner"], "李四")
        # A separate Python interpreter sees the same records after restart.
        env = {**os.environ, "TODO_DATA_DIR": os.path.dirname(self.file)}
        output = subprocess.check_output([sys.executable, "-c",
                                          "import json,server;print(json.dumps(server.read_data()))"],
                                         cwd=Path(server.__file__).parent, env=env, text=True)
        self.assertEqual(json.loads(output)["projects"], data["projects"])
        self.assertEqual(json.loads(output)["milestones"], data["milestones"])

    def test_task_links_owner_deadline_completion_and_undo(self):
        project = self.project(owner="项目负责人")
        milestone = self.milestone(project)
        task = self.task(project_id=project["id"], milestone_id=milestone["id"])
        self.assertEqual(task["owner"], "Ocean")
        self.assertIsNone(task["due"])
        done = self.request("PATCH", "/todos/" + task["id"], {
            "done": True, "owner": " 李四 ", "deadline": "2026-12-01"})["task"]
        self.assertTrue(done["done"])
        self.assertIsNotNone(done["done_at"])
        self.assertEqual(done["owner"], "李四")
        undone = self.request("PATCH", "/todos/" + task["id"], {"done": False})["task"]
        self.assertFalse(undone["done"])
        self.assertIsNone(undone["done_at"])
        self.assertGreater(undone["_field_updated"]["done"], done["_field_updated"]["done"])
        self.assertEqual(undone["_field_updated"]["done"], undone["_field_updated"]["done_at"])
        self.assertEqual(self.request("GET", "/todos")["tasks"][0], self.legacy)

    def test_soft_delete_and_restore_preserve_reassignment(self):
        first_project, second_project = self.project(), self.project("项目乙")
        milestone = self.milestone(first_project)
        retained = self.task(project_id=first_project["id"], milestone_id=milestone["id"])
        moved = self.task(project_id=first_project["id"], milestone_id=milestone["id"])
        self.request("DELETE", "/milestones/" + milestone["id"])
        self.request("PATCH", "/todos/" + retained["id"], {"done": True})
        self.request("PATCH", "/todos/" + moved["id"], {
            "project_id": second_project["id"], "milestone_id": None})
        self.request("POST", "/milestones/" + milestone["id"] + "/restore")
        self.request("DELETE", "/projects/" + first_project["id"])
        self.request("PATCH", "/todos/" + retained["id"], {"owner": "王五"})
        self.request("POST", "/projects/" + first_project["id"] + "/restore")
        records = {task["id"]: task for task in self.request("GET", "/todos")["tasks"]}
        self.assertEqual(records[retained["id"]]["milestone_id"], milestone["id"])
        self.assertEqual(records[retained["id"]]["owner"], "王五")
        self.assertEqual(records[moved["id"]]["project_id"], second_project["id"])
        self.assertIsNone(records[moved["id"]]["milestone_id"])
        self.assertEqual(len(records), 3)

    def test_validation_errors_leave_file_unchanged(self):
        project = self.project()
        other = self.project("其他项目")
        milestone = self.milestone(project)
        cases = [
            ("/todos/add", {"text": "missing deadline"}),
            ("/todos/add", {"text": "x", "deadline": "2026-02-30"}),
            ("/todos/add", {"text": "x", "deadline": "2026-01-01", "owner": "  "}),
            ("/todos/add", {"text": "x", "deadline": "2026-01-01", "milestone_id": milestone["id"]}),
            ("/todos/add", {"text": "x", "deadline": "2026-01-01", "project_id": other["id"], "milestone_id": milestone["id"]}),
            ("/projects/add", {"name": ""}),
            ("/projects/add", {"name": "x", "core": "yes"}),
            ("/milestones/add", {"name": "x", "project_id": project["id"], "due": "2026-2-01"}),
            ("/todos/add", []),
        ]
        before = Path(self.file).read_bytes()
        for path, payload in cases:
            with self.subTest(path=path, payload=payload):
                self.request("POST", path, payload, expected=400)
                self.assertEqual(Path(self.file).read_bytes(), before)
        self.request("DELETE", "/projects/" + project["id"])
        self.request("POST", "/todos/add", {"text": "x", "deadline": "2026-01-01",
                                            "project_id": project["id"]}, expected=400)

    def test_concurrent_adds_identical_text_survive_and_revision_advances(self):
        def add(i):
            return self.task(text="同名事项", owner=f"负责人{i}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks = list(pool.map(add, range(20)))
        data = self.request("GET", "/todos")
        self.assertEqual(len(data["tasks"]), 21)
        self.assertEqual(len({t["id"] for t in tasks}), 20)
        self.assertEqual(data["revision"], 20)

    def test_concurrent_process_writes_use_file_lock(self):
        script = """import server
for n in range(8):
    server.add_single_task({'text':'from process','deadline':'2026-12-01'})
"""
        env = {**os.environ, "TODO_DATA_DIR": os.path.dirname(self.file),
               "TODO_APP_SUPPORT_DIR": self.tmp.name, "TODO_DISABLE_SYNC": "1"}
        processes = [subprocess.Popen([sys.executable, "-c", script], env=env,
                                      cwd=Path(server.__file__).parent) for _ in range(3)]
        for process in processes:
            self.assertEqual(process.wait(timeout=20), 0)
        data = self.request("GET", "/todos")
        self.assertEqual(len(data["tasks"]), 25)
        self.assertEqual(data["revision"], 24)

    def test_stale_snapshot_and_metadata_cannot_overwrite(self):
        old = self.request("GET", "/todos")
        project = self.project()
        self.request("POST", "/todos", old, expected=409)
        self.request("POST", "/todos", {"tasks": []}, expected=409)
        current = self.request("GET", "/todos")
        mutated = copy.deepcopy(current)
        mutated["projects"] = []
        self.request("POST", "/todos", mutated, expected=400)
        current["tasks"][0]["done"] = True
        result = self.request("POST", "/todos", current)
        data = self.request("GET", "/todos")
        self.assertEqual(result["revision"], data["revision"])
        self.assertEqual(data["projects"], [project])
        self.assertEqual(data["tasks"][0]["custom"], "keep")
        self.assertTrue(data["tasks"][0]["done"])

    def test_noop_snapshot_and_repeated_done_preserve_completion_history(self):
        # An old automatically skipped instance must not become a genuine
        # completion today merely because a versioned client saved its snapshot.
        old = {"id": "old-skipped", "text": "历史循环", "done": True,
               "done_at": "2026-01-01T12:00:00+08:00", "auto_skipped": True,
               "recur_source": "old-seed", "updated_at": "2026-01-01T04:00:00+00:00",
               "_field_updated": {"done": "2026-01-01T04:00:00+00:00"}}
        data = server.read_data()
        data["tasks"].append(old)
        server.write_data(data)
        snapshot = self.request("GET", "/todos")
        # Client metadata is never trusted to replace the server's field clocks.
        snapshot["tasks"][-1]["updated_at"] = "2999-01-01T00:00:00+00:00"
        snapshot["tasks"][-1]["_field_updated"] = {"done": "2999-01-01T00:00:00+00:00"}
        self.request("POST", "/todos", snapshot)
        after = self.request("GET", "/todos")["tasks"][-1]
        self.assertEqual(after, old)
        after = self.request("PATCH", "/todos/old-skipped", {"done": True})["task"]
        self.assertEqual(after, old)
        undone = self.request("PATCH", "/todos/old-skipped", {"done": False})["task"]
        self.assertIsNone(undone["done_at"])
        self.assertFalse(undone["auto_skipped"])

    def test_write_requests_require_local_origin_and_json_body(self):
        before = Path(self.file).read_bytes()
        def raw(headers, expected, method="POST", path="/projects/add", body=b'{"name":"blocked"}'):
            req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
            try:
                response = urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                result = response.read()
                self.assertEqual(response.status, expected, result)
                self.assertEqual(int(response.headers['Content-Length']), len(result))
                if expected >= 400:
                    self.assertTrue(json.loads(result)['error'])
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
        raw({"Origin": "https://unrelated.example", "Content-Type": "text/plain"}, 403)
        raw({"Origin": "https://unrelated.example", "Content-Type": "application/json"}, 403)
        raw({"Origin": "null", "Content-Type": "application/json"}, 403)
        raw({"Origin": "http://localhost:1", "Content-Type": "application/json"}, 403)
        raw({"Content-Type": "text/plain"}, 400)
        raw({"Host": "attacker.example:" + str(self.http.server_port), "Content-Type": "application/json"}, 403)
        raw({"Origin": "https://unrelated.example"}, 403, method="DELETE", path="/todos/legacy", body=None)
        self.assertEqual(Path(self.file).read_bytes(), before)
        raw({"Origin": self.base, "Content-Type": "application/json; charset=utf-8"}, 200)
        raw({"Origin": "http://localhost:" + str(self.http.server_port), "Content-Type": "application/json"}, 200)
        self.assertEqual(len(self.request("GET", "/todos")["projects"]), 2)

    def test_deleted_tasks_are_tombstones_and_excluded_from_reorder(self):
        task = self.task()
        result = self.request("DELETE", "/todos/" + task["id"])["task"]
        self.assertTrue(result["deleted_at"])
        self.request("PATCH", "/todos/" + task["id"], {"done": True}, expected=404)
        self.request("POST", "/todos/reorder", {"ids": ["legacy", task["id"]]}, expected=400)
        self.assertEqual(len(self.request("GET", "/todos")["tasks"]), 2)

    def test_deleted_recurring_seed_and_instance_stay_deleted(self):
        seed = self.request("POST", "/recurring/add", {"text": "循环任务", "recurring": "daily"})["task"]
        self.request("POST", "/ensure-recurring")
        instance = next(t for t in self.request("GET", "/todos")["tasks"] if t.get("recur_source"))
        self.request("DELETE", "/todos/" + instance["id"])
        self.request("POST", "/ensure-recurring")
        self.assertEqual(len(self.request("GET", "/todos")["tasks"]), 3)
        self.request("DELETE", "/todos/" + seed["id"])
        with patch.object(server, "today_str", return_value="2026-12-15"):
            self.request("POST", "/ensure-recurring")
        self.assertEqual(len(self.request("GET", "/todos")["tasks"]), 3)

    def test_health_and_preview_cannot_sync_or_public_update(self):
        health = self.request("GET", "/health")
        self.assertEqual(health["data_file"], self.file)
        self.assertTrue(health["preview"])
        self.assertFalse(health["sync_enabled"])
        self.assertEqual(health["channel"], "project-preview")
        self.assertFalse(self.request("POST", "/sync-now")["ok"])
        self.request("POST", "/install-update", expected=400)
        self.assertIn("error", self.request("GET", "/check-update"))


if __name__ == "__main__":
    unittest.main()
