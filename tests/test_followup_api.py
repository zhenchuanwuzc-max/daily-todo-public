"""Follow-up configuration acceptance; every request uses a disposable data store."""
import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import server


class FollowupApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "data" / "todos.json"
        self.file.parent.mkdir()
        self.legacy_project = {"id": "old-project", "name": "存量项目", "owner": "Ocean"}
        self.legacy_task = {"id": "old-task", "text": "存量事项", "done": False, "custom": "keep"}
        self.file.write_text(json.dumps({"tasks": [self.legacy_task],
                                        "projects": [self.legacy_project], "custom": "keep"}))
        for name, value in {
            "DATA_DIR": str(self.file.parent), "DATA_FILE": str(self.file),
            "APP_SUPPORT_DIR": self.tmp.name, "STATE_FILE": str(Path(self.tmp.name) / "state.json"),
            "BACKUP_DIR": str(Path(self.tmp.name) / "backups"),
        }.items():
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(server, "schedule_sync")
        self.sync = patcher.start()
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
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            result = json.loads(response.read())
            self.assertEqual(response.status, expected, result)
            return result

    def people(self, *rows, expected=200):
        return self.request("POST", "/people/upsert", {"people": list(rows)}, expected)

    def person(self, name="Ocean", employee_id="00123", base_employee_id=""):
        return {"name": name, "employee_id": employee_id, "base_employee_id": base_employee_id}

    def groups(self, desired, base, expected=200):
        return self.request("POST", "/weekly-report/groups", {
            "group_ids": desired, "base_group_ids": base}, expected)

    def snapshot(self):
        return self.request("GET", "/todos")

    def test_reads_and_noop_configs_do_not_migrate_or_write_legacy_data(self):
        before, mtime = self.file.read_bytes(), self.file.stat().st_mtime_ns
        data = self.snapshot()
        self.assertEqual(data["people"], [])
        self.assertEqual(data["weekly_report_groups"], [])
        self.assertEqual(data["projects"], [self.legacy_project])
        self.assertEqual(data["tasks"], [self.legacy_task])
        self.people()
        self.groups([], [])
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.file.stat().st_mtime_ns, mtime)
        self.sync.assert_not_called()

    def test_project_group_create_edit_clear_keeps_other_projects_untouched(self):
        added = self.request("POST", "/projects/add", {
            "name": "新项目", "dingtalk_group_id": "  0012345  "})["project"]
        self.assertEqual(added["dingtalk_group_id"], "0012345")
        changed = self.request("PATCH", "/projects/" + added["id"], {
            "dingtalk_group_id": "cidExample=="})["project"]
        self.assertEqual(changed["dingtalk_group_id"], "cidExample==")
        self.assertIn("dingtalk_group_id", changed["_field_updated"])
        cleared = self.request("PATCH", "/projects/" + added["id"], {
            "dingtalk_group_id": "  "})["project"]
        self.assertEqual(cleared["dingtalk_group_id"], "")
        self.assertEqual(self.snapshot()["projects"][0], self.legacy_project)
        self.assertEqual(self.snapshot()["tasks"], [self.legacy_task])

    def test_invalid_project_group_rejects_entire_change(self):
        before = self.file.read_bytes()
        for invalid in (None, 123, True, [], {}, "a b", "a\tb", "\ngroup", "a\x00b", "a\x7fb", "a\u200bb", "a" * 257):
            with self.subTest(invalid=invalid):
                self.request("PATCH", "/projects/old-project", {
                    "name": "不能写入", "dingtalk_group_id": invalid}, expected=400)
                self.request("POST", "/projects/add", {
                    "name": "不能创建", "dingtalk_group_id": invalid}, expected=400)
                self.assertEqual(self.file.read_bytes(), before)

    def test_people_identity_leading_zero_case_preservation_and_partial_updates(self):
        result = self.people(self.person(" Ocean ", " 00123 "), self.person("李四", "00999"))
        original = result["people"][0]
        self.assertEqual(original["name"], "Ocean")
        self.assertEqual(original["employee_id"], "00123")
        result = self.people(self.person("oCEAN", "00456", "00123"))
        self.assertEqual(result["people"][0]["id"], original["id"])
        self.assertEqual(result["people"][0]["name"], "Ocean")
        self.assertEqual(result["people"][0]["employee_id"], "00456")
        self.assertEqual(result["people"][1]["employee_id"], "00999")
        self.assertEqual(self.snapshot()["projects"], [self.legacy_project])
        other_store = str(Path(self.tmp.name) / "another-device" / "todos.json")
        with patch.object(server, "DATA_FILE", other_store):
            on_other_device = server.upsert_people({"people": [self.person("ocean")]})
        self.assertEqual(on_other_device["people"][0]["id"], original["id"])

    def test_people_stale_conflict_is_batch_atomic_and_same_value_retry_is_idempotent(self):
        self.people(self.person())
        before, mtime = self.file.read_bytes(), self.file.stat().st_mtime_ns
        result = self.people(self.person())  # Client retry still carries its original empty base.
        self.assertEqual(result["people"][0]["employee_id"], "00123")
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.file.stat().st_mtime_ns, mtime)
        self.people(self.person("新同事", "00999"), self.person("Ocean", "00666"), expected=409)
        self.assertEqual(self.file.read_bytes(), before)
        cleared = self.people(self.person("Ocean", "", "00123"))["people"][0]
        self.assertEqual(cleared["employee_id"], "")
        self.assertGreater(cleared["_field_updated"]["employee_id"], result["people"][0]["_field_updated"]["employee_id"])

    def test_people_validation_rejects_missing_base_duplicates_and_invalid_rows(self):
        invalid_rows = [
            [{"name": "Ocean", "employee_id": "00123"}],
            [self.person(), self.person(" OCEAN ", "00999")],
            [self.person(" ")], [self.person(employee_id=123)],
            [self.person(employee_id="00 123")], [self.person(base_employee_id=None)],
            [self.person("Ocean\nWu")], [self.person(employee_id="001\x001")],
            [self.person(), "invalid"], [self.person(), {"name": "李四"}],
        ]
        before = self.file.read_bytes()
        for rows in invalid_rows:
            with self.subTest(rows=rows):
                self.people(*rows, expected=400)
                self.assertEqual(self.file.read_bytes(), before)
        self.request("POST", "/people/upsert", {"people": {}}, expected=400)
        self.assertEqual(self.file.read_bytes(), before)

    def test_concurrent_people_additions_are_preserved_and_same_person_conflicts(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: self.people(self.person(f"同事{n}", f"00{n}")), range(12)))
        self.assertEqual(len(self.snapshot()["people"]), 12)

        def competing_update(number):
            try:
                server.upsert_people({"people": [self.person("同一人", number)]})
                return "ok"
            except server.ConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(competing_update, ["001", "002"]))
        self.assertCountEqual(results, ["ok", "conflict"])
        self.assertEqual(len(self.snapshot()["people"]), 13)

    def test_weekly_groups_normalize_soft_delete_restore_and_idempotence(self):
        first = self.groups([" 001 ", "001", "cidExample=="], [])
        self.assertEqual(first["group_ids"], ["001", "cidExample=="])
        before = self.file.read_bytes()
        retry = self.groups(["001", "cidExample=="], [])
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(retry["revision"], first["revision"])
        removed = self.groups(["cidExample=="], ["001", "cidExample=="])
        removed_record = next(g for g in removed["weekly_report_groups"] if g["group_id"] == "001")
        self.assertIsNotNone(removed_record["deleted_at"])
        restored = self.groups(["001", "cidExample=="], ["cidExample=="])
        restored_record = next(g for g in restored["weekly_report_groups"] if g["group_id"] == "001")
        self.assertEqual(restored_record["id"], removed_record["id"])
        self.assertIsNone(restored_record["deleted_at"])
        self.assertGreater(restored_record["_field_updated"]["deleted_at"], removed_record["_field_updated"]["deleted_at"])
        other_store = str(Path(self.tmp.name) / "another-device" / "todos.json")
        with patch.object(server, "DATA_FILE", other_store):
            other = server.update_weekly_report_groups({"group_ids": ["001"], "base_group_ids": []})
        self.assertEqual(other["weekly_report_groups"][0]["id"], restored_record["id"])

    def test_weekly_group_delta_preserves_concurrent_additions_and_deletions(self):
        self.groups(["A"], [])
        self.groups(["A", "B"], ["A"])
        result = self.groups(["C"], ["A"])
        self.assertCountEqual(result["group_ids"], ["B", "C"])
        # A stale editor only added D; retaining A in its form must not restore A.
        result = self.groups(["A", "D"], ["A"])
        self.assertCountEqual(result["group_ids"], ["B", "C", "D"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: self.groups([f"g{n}"], []), range(12)))
        self.assertEqual(len(self.snapshot()["weekly_report_groups"]), 16)
        active = [g for g in self.snapshot()["weekly_report_groups"] if not g["deleted_at"]]
        self.assertEqual(len(active), 15)

    def test_weekly_group_validation_and_limit_preserve_data(self):
        before = self.file.read_bytes()
        for desired, base in [(None, []), ([], None), ([""], []), ([None], []),
                              ([123], []), (["a b"], []), (["a\nb"], []),
                              (["a" * 257], []), ([], [" "]),
                              ([str(n) for n in range(101)], [])]:
            with self.subTest(desired=desired, base=base):
                self.groups(desired, base, expected=400)
                self.assertEqual(self.file.read_bytes(), before)
        hundred = [str(n) for n in range(100)]
        self.groups(hundred + ["0"], [])
        before = self.file.read_bytes()
        self.groups(["new"], [], expected=400)
        self.assertEqual(self.file.read_bytes(), before)
        result = self.groups(hundred[1:] + ["new"], hundred)
        self.assertEqual(len(result["group_ids"]), 100)

    def test_snapshot_cannot_change_followup_config_and_legacy_task_save_retains_it(self):
        self.people(self.person())
        self.groups(["001"], [])
        current = self.snapshot()
        before = self.file.read_bytes()
        for field in ("people", "weekly_report_groups"):
            modified = copy.deepcopy(current)
            modified[field] = []
            modified["tasks"][0]["done"] = True
            self.request("POST", "/todos", modified, expected=400)
            self.assertEqual(self.file.read_bytes(), before)
        self.request("POST", "/todos", {
            "revision": current["revision"], "tasks": [{**self.legacy_task, "done": True}]})
        updated = self.snapshot()
        self.assertTrue(updated["tasks"][0]["done"])
        self.assertEqual(updated["people"], current["people"])
        self.assertEqual(updated["weekly_report_groups"], current["weekly_report_groups"])

    def test_configuration_calls_do_not_start_external_processes(self):
        with patch.object(server.subprocess, "Popen") as popen:
            self.people(self.person())
            self.groups(["001"], [])
            self.request("PATCH", "/projects/old-project", {"dingtalk_group_id": "001"})
        popen.assert_not_called()
        self.assertEqual(self.sync.call_count, 3)


if __name__ == "__main__":
    unittest.main()
