"""Reviewed person-identity merging; API and sync use temporary data exclusively."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import server
from tests.test_project_merge import EARLY, LATER, LATEST, SCRIPT, edited, merge


class PeopleMergeApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.file = self.root / "data" / "todos.json"
        self.file.parent.mkdir()
        self.target = edited({"id": "target", "name": "佳禾", "created": EARLY}, EARLY,
                             employee_id="", english_name="Existing English", aliases=["target-old"])
        self.source = {"id": "source", "name": "余佳禾", "employee_id": "00007", "chinese_name": "余佳禾",
                       "english_name": "Alice Yu", "aliases": ["小禾"], "dingtalk_user_id": "u00007"}
        self.other_source = {"id": "source-2", "name": "Alice Old", "employee_id": "00007",
                             "chinese_name": "余佳禾", "english_name": "Alice Yu",
                             "aliases": ["英文别名", "小禾"], "dingtalk_user_id": "u00007"}
        self.unrelated = {"id": "unrelated", "name": "Other", "employee_id": "00999", "custom": "keep"}
        self.original = {"revision": 7, "tasks": [
            {"id": "task", "text": "Keep history", "owner": "余佳禾、佳禾", "done": False}],
            "projects": [{"id": "project", "name": "Keep project", "owner": "余佳禾"}],
            "milestones": [{"id": "milestone", "project_id": "project", "name": "Keep milestone", "owner": "Alice Old"}],
            "people": [self.target, self.source, self.other_source, self.unrelated]}
        self.file.write_text(json.dumps(self.original))
        for key, value in {"DATA_DIR": str(self.file.parent), "DATA_FILE": str(self.file),
                           "APP_SUPPORT_DIR": str(self.root), "STATE_FILE": str(self.root / "state.json"),
                           "BACKUP_DIR": str(self.root / "backups")}.items():
            mock = patch.object(server, key, value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(server, "schedule_sync")
        self.sync = mock.start()
        self.addCleanup(mock.stop)
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_http)
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def stop_http(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)

    def request(self, path, payload, expected=200):
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            result = json.loads(response.read())
            self.assertEqual(response.status, expected, result)
            return result

    def payload(self, sources=None, target="target"):
        sources = ["source", "source-2"] if sources is None else sources
        selected = {target, *sources}
        return {"target_id": target, "source_ids": sources,
                "base_people": [person for person in server.read_data()["people"] if person["id"] in selected]}

    def people(self):
        return {person["id"]: person for person in server.read_data()["people"]}

    def test_multiple_sources_merge_atomically_preserving_history_and_target_identity(self):
        result = self.request("/people/merge", self.payload())
        self.assertFalse(result["already_merged"])
        self.assertEqual(result["target_id"], "target")
        self.assertEqual(result["merged_ids"], ["source", "source-2"])
        people = self.people()
        target = people["target"]
        self.assertEqual((target["id"], target["name"], target["employee_id"], target["chinese_name"],
                          target["english_name"], target["dingtalk_user_id"]),
                         ("target", "佳禾", "00007", "余佳禾", "Existing English", "u00007"))
        self.assertEqual(target["aliases"], ["target-old", "余佳禾", "Alice Yu", "小禾", "Alice Old", "英文别名"])
        self.assertEqual(target["_field_updated"]["english_name"], EARLY)
        self.assertEqual(target["created"], EARLY)
        self.assertNotIn("name", {key for key, stamp in target["_field_updated"].items() if stamp != EARLY})
        for identity, old in (("source", self.source), ("source-2", self.other_source)):
            source = people[identity]
            self.assertEqual({key: source[key] for key in old}, old)
            self.assertEqual(source["merged_into"], "target")
            self.assertTrue(source["deleted_at"])
            self.assertEqual(len(source["_merge_fingerprint"]), 64)
            self.assertEqual(source["_field_updated"]["merged_into"], source["deleted_at"])
        self.assertEqual(people["unrelated"], self.unrelated)
        current = server.read_data()
        for collection in ("tasks", "projects", "milestones"):
            self.assertEqual(current[collection], self.original[collection])
        self.sync.assert_called_once()

    def test_original_request_retry_is_noop_and_does_not_overwrite_later_target_edit(self):
        payload = self.payload()
        self.request("/people/merge", payload)
        self.request("/people/upsert", {"people": [{"name": "佳禾", "employee_id": "00007", "base_employee_id": "00007",
                                                   "english_name": "Corrected Later", "base_english_name": "Existing English"}]})
        before, modified = self.file.read_bytes(), self.file.stat().st_mtime_ns
        result = self.request("/people/merge", payload)
        self.assertTrue(result["already_merged"])
        self.assertEqual(self.people()["target"]["english_name"], "Corrected Later")
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.file.stat().st_mtime_ns, modified)
        self.assertEqual(self.sync.call_count, 2)

    def test_changed_target_or_source_snapshot_conflicts_without_partial_merge(self):
        for identity in ("target", "source", "source-2"):
            with self.subTest(identity=identity):
                self.file.write_text(json.dumps(self.original))
                payload = self.payload()
                person = next(person for person in self.original["people"] if person["id"] == identity)
                self.request("/people/upsert", {"people": [{"name": person["name"],
                    "employee_id": person["employee_id"], "base_employee_id": person["employee_id"],
                    "english_name": "Remote correction", "base_english_name": person.get("english_name", "")}]})
                before = self.file.read_bytes()
                self.request("/people/merge", payload, expected=409)
                self.assertEqual(self.file.read_bytes(), before)
                self.assertFalse(self.people()["source"].get("deleted_at"))
                self.assertFalse(self.people()["source-2"].get("deleted_at"))

    def test_missing_deleted_or_already_merged_people_are_rejected(self):
        for identity, mode in (("target", "deleted"), ("source", "deleted"), ("source-2", "merged"),
                               ("source", "missing")):
            with self.subTest(identity=identity, mode=mode):
                data = copy.deepcopy(self.original)
                payload = self.payload()
                if mode == "missing":
                    data["people"] = [person for person in data["people"] if person["id"] != identity]
                else:
                    person = next(person for person in data["people"] if person["id"] == identity)
                    person["deleted_at"] = EARLY
                    if mode == "merged":
                        person["merged_into"] = "unrelated"
                self.file.write_text(json.dumps(data))
                before = self.file.read_bytes()
                self.request("/people/merge", payload, expected=409)
                self.assertEqual(self.file.read_bytes(), before)

    def test_differing_nonempty_employee_or_dingtalk_ids_are_not_combined(self):
        for identity, field, value in (("target", "employee_id", "00008"),
                                        ("target", "dingtalk_user_id", "other-user"),
                                        ("source-2", "employee_id", "00008"),
                                        ("source-2", "dingtalk_user_id", "other-user")):
            with self.subTest(identity=identity, field=field):
                data = copy.deepcopy(self.original)
                next(person for person in data["people"] if person["id"] == identity)[field] = value
                self.file.write_text(json.dumps(data))
                before = self.file.read_bytes()
                self.request("/people/merge", self.payload(), expected=409)
                self.assertEqual(self.file.read_bytes(), before)

    def test_invalid_typed_merge_requests_preserve_every_record(self):
        valid = self.payload()
        malformed = [
            {}, {**valid, "target_id": None}, {**valid, "target_id": 7},
            {**valid, "source_ids": None}, {**valid, "source_ids": []},
            {**valid, "source_ids": [None]}, {**valid, "source_ids": ["source", "source"]},
            {**valid, "source_ids": ["target"]}, {**valid, "base_people": None},
            {**valid, "base_people": {}}, {**valid, "base_people": [None]},
            {**valid, "base_people": [{"id": "target"}]},
            {**valid, "base_people": [self.target, self.source]},
            {**valid, "base_people": valid["base_people"] + [self.source]},
            {**valid, "base_people": valid["base_people"] + [self.unrelated]},
        ]
        before = self.file.read_bytes()
        for payload in malformed:
            with self.subTest(payload=payload):
                self.request("/people/merge", payload, expected=400)
                self.assertEqual(self.file.read_bytes(), before)
        self.sync.assert_not_called()

    def test_alias_limit_rejects_whole_merge_instead_of_dropping_names(self):
        data = copy.deepcopy(self.original)
        data["people"][0]["aliases"] = [f"existing-{index}" for index in range(30)]
        self.file.write_text(json.dumps(data))
        before = self.file.read_bytes()
        self.request("/people/merge", self.payload(), expected=400)
        self.assertEqual(self.file.read_bytes(), before)
        self.sync.assert_not_called()

    def test_unchanged_target_fields_keep_their_clocks(self):
        data = copy.deepcopy(self.original)
        target = data["people"][0]
        target.update(employee_id="00007", chinese_name="余佳禾", dingtalk_user_id="u00007",
                      aliases=["target-old", "余佳禾", "Alice Yu", "小禾", "Alice Old", "英文别名"])
        self.file.write_text(json.dumps(data))
        before = copy.deepcopy(target)
        self.request("/people/merge", self.payload())
        self.assertEqual(self.people()["target"], before)

    def test_merged_source_upsert_is_blocked_even_for_unchanged_or_batched_old_clients(self):
        self.request("/people/merge", self.payload())
        before = self.file.read_bytes()
        for source_name in ("余佳禾", "Alice Old", "ALICE OLD"):
            self.request("/people/upsert", {"people": [
                {"name": "New", "employee_id": "00002", "base_employee_id": ""},
                {"name": source_name, "employee_id": "00007", "base_employee_id": "00007"},
            ]}, expected=409)
            self.assertEqual(self.file.read_bytes(), before)

    def test_old_payload_cannot_claim_idempotence_for_a_different_merge_request(self):
        payload = self.payload()
        self.request("/people/merge", payload)
        different = copy.deepcopy(payload)
        different["source_ids"].reverse()
        before = self.file.read_bytes()
        self.request("/people/merge", different, expected=409)
        self.assertEqual(self.file.read_bytes(), before)

    def test_competing_requests_cannot_merge_one_source_into_two_active_targets(self):
        data = copy.deepcopy(self.original)
        data["people"].append({"id": "second-target", "name": "备用目标", "employee_id": ""})
        self.file.write_text(json.dumps(data))
        payloads = [self.payload(sources=["source"], target=identity)
                    for identity in ("target", "second-target")]

        def attempt(payload):
            try:
                server.merge_people(payload)
                return "ok"
            except server.ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(attempt, payloads)), ["ok", "conflict"])
        people = self.people()
        self.assertIn(people["source"]["merged_into"], ("target", "second-target"))
        untouched = "second-target" if people["source"]["merged_into"] == "target" else "target"
        original = next(person for person in data["people"] if person["id"] == untouched)
        self.assertEqual(people[untouched], original)


class PeopleMergeSyncTests(unittest.TestCase):
    def setUp(self):
        self.target = edited({"id": "target", "name": "佳禾", "employee_id": "00007"}, EARLY,
                             chinese_name="余佳禾", aliases=[])
        self.source = edited({"id": "source", "name": "余佳禾", "employee_id": "00007"}, EARLY,
                             english_name="Alice Yu", aliases=["小禾"])
        self.base = {"revision": 1, "tasks": [], "people": [self.target, self.source]}

    def test_merge_tombstone_survives_stale_source_edits_and_independent_target_enrichment(self):
        ours, theirs = copy.deepcopy(self.base), copy.deepcopy(self.base)
        ours["people"][0] = edited(self.target, LATER, aliases=["余佳禾", "Alice Yu", "小禾"])
        ours["people"][1] = edited(self.source, LATER, deleted_at=LATER, merged_into="target", _merge_fingerprint="a" * 64)
        theirs["people"][0] = edited(self.target, LATEST, english_name="Latest target name")
        theirs["people"][1] = edited(self.source, LATEST, english_name="Stale device edit")
        for first, second in ((ours, theirs), (theirs, ours)):
            result = merge.union(first, second, self.base)
            target, source = result["people"]
            self.assertEqual(target["english_name"], "Latest target name")
            self.assertEqual(target["aliases"], ["余佳禾", "Alice Yu", "小禾"])
            self.assertEqual(source["merged_into"], "target")
            self.assertEqual(source["deleted_at"], LATER)
            self.assertEqual(source["_merge_fingerprint"], "a" * 64)
            self.assertEqual(source["english_name"], "Stale device edit")

    def test_competing_merge_destinations_and_receipts_are_an_atomic_group(self):
        ours = edited(self.source, LATER, deleted_at=LATER, merged_into="target-a", _merge_fingerprint="a" * 64)
        theirs = edited(self.source, LATEST, deleted_at=LATEST, merged_into="target-b", _merge_fingerprint="b" * 64)
        for first, second in ((ours, theirs), (theirs, ours)):
            person = merge.merge_record(self.source, first, second, collection="people")
            self.assertEqual((person["deleted_at"], person["merged_into"], person["_merge_fingerprint"]),
                             (LATEST, "target-b", "b" * 64))

    def test_merge_destination_and_receipt_have_typed_validation(self):
        for field in ("merged_into", "_merge_fingerprint"):
            for invalid in (None, 12, True, [], {}, "a b", "a\nb", "a\u200bb"):
                with self.subTest(field=field, invalid=invalid):
                    data = copy.deepcopy(self.base)
                    data["people"][1][field] = invalid
                    with self.assertRaises(ValueError):
                        merge.validate(data)

    def test_tombstone_is_persisted_by_three_way_merge_cli(self):
        ours, theirs = copy.deepcopy(self.base), copy.deepcopy(self.base)
        ours["people"][0] = edited(self.target, LATER, aliases=["余佳禾", "小禾"])
        ours["people"][1] = edited(self.source, LATER, deleted_at=LATER, merged_into="target", _merge_fingerprint="a" * 64)
        theirs["people"][1] = edited(self.source, LATEST, english_name="Other device")
        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary) / name for name in ("base.json", "ours.json", "theirs.json")]
            for path, data in zip(paths, (self.base, ours, theirs)):
                path.write_text(json.dumps(data))
            result = subprocess.run([sys.executable, str(SCRIPT), *map(str, paths)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            persisted = json.loads(paths[1].read_text())
        source = next(person for person in persisted["people"] if person["id"] == "source")
        self.assertEqual(source["merged_into"], "target")
        self.assertEqual(source["deleted_at"], LATER)
        self.assertEqual(source["english_name"], "Other device")


if __name__ == "__main__":
    unittest.main()
