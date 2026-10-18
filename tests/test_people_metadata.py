"""Directory metadata API/merge acceptance using disposable data exclusively."""
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
from unittest.mock import patch

import server
from tests.test_project_merge import EARLY, LATER, LATEST, SCRIPT, edited, merge


class PeopleMetadataApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.file = self.root / "data" / "todos.json"
        self.file.parent.mkdir()
        self.legacy_person = {"id": "person-ocean", "name": "Ocean", "employee_id": "00123",
                              "created": EARLY, "_field_updated": {"employee_id": EARLY}}
        self.legacy_task = {"id": "task-1", "text": "Existing task", "owner": "Ocean", "done": False}
        self.file.write_text(json.dumps({"revision": 3, "tasks": [self.legacy_task],
                                         "people": [self.legacy_person]}))
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

    def request(self, method, path, payload=None, expected=200):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base + path, data=body, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            result = json.loads(response.read())
            self.assertEqual(response.status, expected, result)
            return result

    def person(self, **fields):
        return {"name": "Ocean", "employee_id": "00123", "base_employee_id": "00123", **fields}

    def upsert(self, *rows, expected=200):
        return self.request("POST", "/people/upsert", {"people": list(rows)}, expected)

    def test_legacy_read_and_omitted_metadata_do_not_migrate_or_write(self):
        before, modified = self.file.read_bytes(), self.file.stat().st_mtime_ns
        self.assertEqual(self.request("GET", "/todos")["people"], [self.legacy_person])
        self.upsert(self.person())
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.file.stat().st_mtime_ns, modified)
        self.sync.assert_not_called()

    def test_new_person_defaults_and_normalized_directory_details(self):
        result = self.upsert(self.person(name="  Lee  ", employee_id=" 00009 ", base_employee_id=""))
        new = next(person for person in result["people"] if person["name"] == "Lee")
        self.assertEqual({field: new[field] for field in server.PERSON_METADATA_FIELDS},
                         {"chinese_name": "", "english_name": "", "aliases": [], "dingtalk_user_id": ""})
        result = self.upsert(self.person(name="Lee", employee_id="00009", base_employee_id="",
                                         chinese_name=" 李 华 ", base_chinese_name="",
                                         english_name=" Lee Hua ", base_english_name="",
                                         dingtalk_user_id=" 000567 ", base_dingtalk_user_id="",
                                         aliases=["  小李  ", "Lee Hua", "小李"], base_aliases=[]))
        changed = next(person for person in result["people"] if person["name"] == "Lee")
        self.assertEqual(changed["id"], new["id"])
        self.assertEqual(changed["employee_id"], "00009")
        self.assertEqual(changed["chinese_name"], "李 华")
        self.assertEqual(changed["english_name"], "Lee Hua")
        self.assertEqual(changed["dingtalk_user_id"], "000567")
        self.assertEqual(changed["aliases"], ["小李", "Lee Hua"])

    def test_incremental_enrichment_changes_only_supplied_fields_and_keeps_owners(self):
        first = self.upsert(self.person(chinese_name="吴振川", base_chinese_name=""))["people"][0]
        self.assertNotIn("english_name", first)
        self.assertNotIn("aliases", first)
        first_clock = copy.deepcopy(first["_field_updated"])
        second = self.upsert(self.person(english_name="Ocean Wu", base_english_name="",
                                          aliases=["Ocean", "振川"], base_aliases=[]))["people"][0]
        self.assertEqual((second["name"], second["chinese_name"], second["english_name"]),
                         ("Ocean", "吴振川", "Ocean Wu"))
        self.assertEqual(second["_field_updated"]["chinese_name"], first_clock["chinese_name"])
        self.assertEqual(second["_field_updated"]["employee_id"], EARLY)
        self.assertEqual(self.request("GET", "/todos")["tasks"], [self.legacy_task])

    def test_metadata_can_be_cleared_independently(self):
        original = self.upsert(self.person(chinese_name="吴振川", base_chinese_name="",
                                            english_name="Ocean Wu", base_english_name="",
                                            aliases=["振川"], base_aliases=[],
                                            dingtalk_user_id="00021", base_dingtalk_user_id=""))["people"][0]
        cleared = self.upsert(self.person(chinese_name="", base_chinese_name="吴振川",
                                           english_name="", base_english_name="Ocean Wu",
                                           aliases=[], base_aliases=["振川"],
                                           dingtalk_user_id="", base_dingtalk_user_id="00021"))["people"][0]
        for field in server.PERSON_METADATA_FIELDS:
            self.assertEqual(cleared[field], [] if field == "aliases" else "")
            self.assertGreater(cleared["_field_updated"][field], original["_field_updated"][field])
        self.assertEqual(cleared["_field_updated"]["employee_id"], EARLY)

    def test_old_client_employee_edit_preserves_metadata_without_renaming(self):
        original = self.upsert(self.person(chinese_name="吴振川", base_chinese_name="",
                                            english_name="Ocean Wu", base_english_name="",
                                            aliases=["振川"], base_aliases=[]))["people"][0]
        updated = self.upsert(self.person(name="oCEAN", employee_id="00044"))["people"][0]
        self.assertEqual(updated["id"], original["id"])
        self.assertEqual(updated["name"], "Ocean")
        self.assertEqual(updated["employee_id"], "00044")
        for field in ("chinese_name", "english_name", "aliases"):
            self.assertEqual(updated[field], original[field])
            self.assertEqual(updated["_field_updated"][field], original["_field_updated"][field])

    def test_repeated_metadata_retry_is_noop_even_with_original_base(self):
        row = self.person(english_name="Ocean Wu", base_english_name="", aliases=["振川"], base_aliases=[])
        first = self.upsert(row)
        before, modified = self.file.read_bytes(), self.file.stat().st_mtime_ns
        retry = self.upsert(row)
        self.assertEqual(retry, first)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(self.file.stat().st_mtime_ns, modified)
        self.assertEqual(self.sync.call_count, 1)

    def test_mixed_batch_metadata_conflict_is_atomic(self):
        self.upsert(self.person(english_name="Current Name", base_english_name=""))
        before = self.file.read_bytes()
        self.upsert(self.person(name="New person", employee_id="00003", base_employee_id=""),
                    self.person(name="Ocean", chinese_name="吴振川", base_chinese_name="",
                                english_name="Stale Name", base_english_name=""), expected=409)
        self.assertEqual(self.file.read_bytes(), before)

    def test_metadata_requires_typed_bases_before_any_write(self):
        before = self.file.read_bytes()
        for field, target in (("chinese_name", "吴振川"), ("english_name", "Ocean Wu"),
                              ("aliases", ["振川"]), ("dingtalk_user_id", "00012")):
            with self.subTest(field=field):
                self.upsert(self.person(name="Other", employee_id="004", base_employee_id=""),
                            self.person(**{field: target}), expected=400)
                self.upsert(self.person(**{field: target, "base_" + field: None}), expected=400)
                self.assertEqual(self.file.read_bytes(), before)

    def test_invalid_metadata_types_controls_and_limits_are_rejected(self):
        before = self.file.read_bytes()
        examples = {
            "chinese_name": [None, 12, {}, [], "x\ny", "x\u200by", "x" * 257],
            "english_name": [True, [], "A\tB", "A\x00B"],
            "dingtalk_user_id": [12, None, "001 23", "a\nb", "a" * 257],
            "aliases": [None, "Alias", {}, [None], [12], [""], ["  "], ["A\nB"],
                        ["A\u200bB"], ["A\x7fB"], ["a" * 257], [str(n) for n in range(31)]],
        }
        for field, invalids in examples.items():
            for invalid in invalids:
                with self.subTest(field=field, invalid=invalid):
                    self.upsert(self.person(**{field: invalid,
                                              "base_" + field: [] if field == "aliases" else ""}), expected=400)
                    self.assertEqual(self.file.read_bytes(), before)


class PeopleMetadataMergeTests(unittest.TestCase):
    def setUp(self):
        self.person = edited({"id": "person-ocean", "name": "Ocean", "employee_id": "00123"},
                             EARLY, chinese_name="吴振川", english_name="", aliases=[], dingtalk_user_id="")
        self.data = {"tasks": [], "people": [self.person], "revision": 1}

    def test_independent_directory_fields_merge_without_recipient_changes(self):
        ours, theirs = copy.deepcopy(self.data), copy.deepcopy(self.data)
        ours["people"][0] = edited(self.person, LATER, english_name="Ocean Wu", aliases=["振川"])
        theirs["people"][0] = edited(self.person, LATEST, employee_id="00004", dingtalk_user_id="00011")
        for first, second in ((ours, theirs), (theirs, ours)):
            person = merge.union(first, second, self.data)["people"][0]
            self.assertEqual((person["name"], person["employee_id"], person["english_name"],
                              person["aliases"], person["dingtalk_user_id"]),
                             ("Ocean", "00004", "Ocean Wu", ["振川"], "00011"))
            self.assertEqual(person["_field_updated"]["english_name"], LATER)
            self.assertEqual(person["_field_updated"]["dingtalk_user_id"], LATEST)

    def test_same_field_conflict_and_explicit_clear_use_individual_clocks(self):
        old = edited(self.person, EARLY, aliases=["Old"], english_name="Old Name")
        ours = edited(old, LATEST, aliases=[], english_name="New Name")
        theirs = edited(old, LATER, aliases=["Stale"], english_name="Stale Name")
        theirs["updated_at"] = "2027-01-01T00:00:00Z"
        for first, second in ((ours, theirs), (theirs, ours)):
            person = merge.merge_record(old, first, second, collection="people")
            self.assertEqual(person["aliases"], [])
            self.assertEqual(person["english_name"], "New Name")
            self.assertEqual(person["employee_id"], "00123")

    def test_validation_accepts_legacy_and_rejects_invalid_directory_shapes(self):
        merge.validate({"tasks": [], "people": [{"id": "legacy", "name": "Old", "employee_id": "01"}]})
        merge.validate(self.data)
        for field, invalid in (("chinese_name", None), ("english_name", []),
                                ("dingtalk_user_id", 123), ("dingtalk_user_id", "00 1"),
                                ("chinese_name", "x\ny"), ("aliases", None), ("aliases", "abc"),
                                ("aliases", [None]), ("aliases", [1]), ("aliases", ["A\u200bB"]),
                                ("aliases", [" "]), ("aliases", [str(n) for n in range(31)])):
            with self.subTest(field=field, invalid=invalid):
                invalid_data = copy.deepcopy(self.data)
                invalid_data["people"][0][field] = invalid
                with self.assertRaises(ValueError):
                    merge.validate(invalid_data)

    def test_merge_cli_persists_enrichment_and_keeps_untouched_legacy_record(self):
        legacy = {"id": "legacy", "name": "Legacy", "employee_id": "00006"}
        base = {**self.data, "people": [self.person, legacy]}
        ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
        ours["people"][0] = edited(self.person, LATER, english_name="Ocean Wu")
        theirs["people"][0] = edited(self.person, LATEST, aliases=["振川"])
        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary) / name for name in ("base.json", "ours.json", "theirs.json")]
            for path, data in zip(paths, (base, ours, theirs)):
                path.write_text(json.dumps(data))
            result = subprocess.run([sys.executable, str(SCRIPT), *map(str, paths)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            merged = json.loads(paths[1].read_text())
        by_id = {person["id"]: person for person in merged["people"]}
        self.assertEqual(by_id["person-ocean"]["english_name"], "Ocean Wu")
        self.assertEqual(by_id["person-ocean"]["aliases"], ["振川"])
        self.assertEqual(by_id["legacy"], legacy)


if __name__ == "__main__":
    unittest.main()
