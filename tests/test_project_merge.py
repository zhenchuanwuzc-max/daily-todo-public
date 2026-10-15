import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "sync" / "json-merge.py"
SPEC = importlib.util.spec_from_file_location("project_merge", SCRIPT)
merge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge)
EARLY = "2026-10-15T01:00:00.000001+00:00"
LATER = "2026-10-15T01:00:00.000002+00:00"
LATEST = "2026-10-15T01:00:00.000003+00:00"


def edited(record, timestamp, **fields):
    result = copy.deepcopy(record)
    result.update(fields)
    result["updated_at"] = timestamp
    result.setdefault("_field_updated", {}).update({key: timestamp for key in fields})
    return result


class ProjectMergeTest(unittest.TestCase):
    def setUp(self):
        self.base = {"revision": 1, "tasks": [{"id": "t1", "text": "Original", "owner": "Ocean", "done": False,
                                               "done_at": None, "created": "2026-10-14T12:00:00"}],
                     "projects": [{"id": "p1", "name": "Project", "owner": "Ocean", "deleted_at": None}],
                     "milestones": [{"id": "m1", "project_id": "p1", "name": "First", "deleted_at": None}]}

    def task_merge(self, ours, theirs, old=None):
        return merge.merge_record(self.base["tasks"][0] if old is None else old, ours, theirs)

    def test_independent_fields_survive(self):
        old = self.base["tasks"][0]
        result = self.task_merge(edited(old, LATER, owner="Lin"), edited(old, LATEST, text="Other text", deadline="2026-10-20"))
        self.assertEqual((result["owner"], result["text"], result["deadline"]), ("Lin", "Other text", "2026-10-20"))
        self.assertEqual(result["_field_updated"]["owner"], LATER)

    def test_project_task_order_is_independent_from_thing_order_and_content(self):
        old = {**self.base["tasks"][0], "sort_order": 4, "project_sort_order": 3}
        ours = edited(old, LATER, project_sort_order=0)
        theirs = edited(old, LATEST, sort_order=2, text="Latest content")
        for first, second in ((ours, theirs), (theirs, ours)):
            result = self.task_merge(first, second, old)
            self.assertEqual((result["project_sort_order"], result["sort_order"], result["text"]),
                             (0, 2, "Latest content"))
            self.assertEqual(result["_field_updated"]["project_sort_order"], LATER)
        newer = edited(old, LATEST, project_sort_order=3)
        result = self.task_merge(newer, ours, old)
        self.assertEqual(result["project_sort_order"], 3)

    def test_undo_against_stale_completed_record(self):
        old = edited(self.base["tasks"][0], EARLY, done=True, done_at=EARLY)
        result = self.task_merge(edited(old, LATER, done=False, done_at=None), edited(old, LATEST, text="Edited later"), old)
        self.assertFalse(result["done"])
        self.assertIsNone(result["done_at"])
        self.assertEqual(result["text"], "Edited later")

    def test_undo_returns_to_ancestor_value_with_new_clock(self):
        old = edited(self.base["tasks"][0], EARLY, done=False, done_at=None)
        undone = edited(old, LATEST, done=False, done_at=None)
        stale = edited(old, LATER, done=True, done_at=LATER)
        for first, second in ((undone, stale), (stale, undone)):
            result = self.task_merge(first, second, old)
            self.assertFalse(result["done"])
            self.assertIsNone(result["done_at"])
            self.assertEqual(result["_field_updated"]["done"], LATEST)

    def test_actual_same_field_conflict_uses_field_clock(self):
        old = self.base["tasks"][0]
        ours = edited(old, LATEST, owner="Lin")
        theirs = edited(old, LATER, owner="Wei")
        theirs["updated_at"] = "2026-10-16T00:00:00Z"
        self.assertEqual(self.task_merge(ours, theirs)["owner"], "Lin")

    def test_timestamp_tie_is_argument_order_independent(self):
        old = self.base["tasks"][0]
        a, b = edited(old, LATER, owner="A"), edited(old, LATER, owner="B")
        self.assertEqual(self.task_merge(a, b), self.task_merge(b, a))

    def test_timestamp_offsets_are_compared_as_instants(self):
        old = self.base["tasks"][0]
        a = edited(old, "2026-10-15T10:00:00+08:00", owner="New")
        b = edited(old, "2026-10-15T03:00:00+02:00", owner="Old")
        self.assertEqual(self.task_merge(a, b)["owner"], "New")

    def test_relation_is_an_atomic_pair(self):
        old = {**self.base["tasks"][0], "project_id": "p1", "milestone_id": "m1"}
        a = edited(old, LATER, project_id="p2", milestone_id="m2")
        b = edited(old, LATEST, milestone_id="m3")
        result = self.task_merge(a, b, old)
        self.assertEqual((result["project_id"], result["milestone_id"]), ("p1", "m3"))

    def test_reassignment_back_to_ancestor_is_explicit_edit(self):
        old = edited({**self.base["tasks"][0], "project_id": "p1", "milestone_id": "m1"}, EARLY, project_id="p1", milestone_id="m1")
        a = edited(old, LATEST, project_id="p1", milestone_id="m1")
        b = edited(old, LATER, project_id="p2", milestone_id="m2")
        result = self.task_merge(a, b, old)
        self.assertEqual((result["project_id"], result["milestone_id"]), ("p1", "m1"))

    def test_concurrent_additions_all_collections(self):
        ours, theirs = copy.deepcopy(self.base), copy.deepcopy(self.base)
        for name in merge.COLLECTIONS:
            ours.setdefault(name, []).append({"id": name + "-ours"})
            theirs.setdefault(name, []).append({"id": name + "-theirs"})
        result = merge.union(ours, theirs, self.base)
        for name in merge.COLLECTIONS:
            self.assertEqual(len(result[name]), len(self.base.get(name, [])) + 2)
        self.assertEqual(result["revision"], 2)

    def test_concurrent_people_and_report_group_additions_keep_both_sides(self):
        ours, theirs = copy.deepcopy(self.base), copy.deepcopy(self.base)
        ours["people"] = [edited({"id": "person-ocean", "created": EARLY}, LATER,
                                 name="Ocean", employee_id="000123")]
        theirs["people"] = [edited({"id": "person-lin", "created": EARLY}, LATEST,
                                   name="Lin", employee_id="000456")]
        ours["weekly_report_groups"] = [edited({"id": "group-one", "created": EARLY}, LATER,
                                               group_id="chat-1", deleted_at=None)]
        theirs["weekly_report_groups"] = [edited({"id": "group-two", "created": EARLY}, LATEST,
                                                 group_id="chat-2", deleted_at=None)]
        for first, second in ((ours, theirs), (theirs, ours)):
            result = merge.union(first, second, self.base)
            self.assertEqual({p["name"]: p["employee_id"] for p in result["people"]},
                             {"Ocean": "000123", "Lin": "000456"})
            self.assertEqual({g["group_id"] for g in result["weekly_report_groups"]}, {"chat-1", "chat-2"})

    def test_people_name_and_employee_id_are_atomic(self):
        base = copy.deepcopy(self.base)
        old = edited({"id": "person-ocean", "created": EARLY}, EARLY, name="Ocean", employee_id="000001")
        base["people"] = [old]
        ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
        ours["people"][0] = edited(old, LATER, name="OCEAN")
        theirs["people"][0] = edited(old, LATEST, employee_id="000002")
        for first, second in ((ours, theirs), (theirs, ours)):
            person = merge.union(first, second, base)["people"][0]
            self.assertEqual((person["name"], person["employee_id"]), ("Ocean", "000002"))
            self.assertEqual(person["_field_updated"]["employee_id"], LATEST)
            self.assertEqual(person["_field_updated"]["name"], EARLY)

    def test_employee_id_correction_beats_stale_edit_even_back_to_ancestor(self):
        old = edited({"id": "person-ocean", "created": EARLY}, EARLY, name="Ocean", employee_id="000001")
        corrected = edited(old, LATEST, employee_id="000001")
        stale = edited(old, LATER, employee_id="000002")
        for first, second in ((corrected, stale), (stale, corrected)):
            person = merge.merge_record(old, first, second, collection="people")
            self.assertEqual((person["name"], person["employee_id"]), ("Ocean", "000001"))
            self.assertEqual(person["_field_updated"]["employee_id"], LATEST)

    def test_same_person_concurrent_creation_is_one_complete_identity(self):
        ours, theirs = copy.deepcopy(self.base), copy.deepcopy(self.base)
        ours["people"] = [edited({"id": "person-ocean", "created": EARLY}, LATER,
                                 name="Ocean", employee_id="000001")]
        theirs["people"] = [edited({"id": "person-ocean", "created": EARLY}, LATEST,
                                   name="OCEAN", employee_id="000002")]
        for first, second in ((ours, theirs), (theirs, ours)):
            people = merge.union(first, second, self.base)["people"]
            self.assertEqual(len(people), 1)
            self.assertEqual((people[0]["name"], people[0]["employee_id"]), ("OCEAN", "000002"))

    def test_project_group_merges_independently_of_project_details(self):
        ours, theirs = copy.deepcopy(self.base), copy.deepcopy(self.base)
        ours["projects"][0] = edited(ours["projects"][0], LATER, dingtalk_group_id="chat-project")
        theirs["projects"][0] = edited(theirs["projects"][0], LATEST, name="Renamed", owner="Lin")
        for first, second in ((ours, theirs), (theirs, ours)):
            project = merge.union(first, second, self.base)["projects"][0]
            self.assertEqual((project["dingtalk_group_id"], project["name"], project["owner"]),
                             ("chat-project", "Renamed", "Lin"))

    def test_report_group_delete_and_restore_keep_other_groups(self):
        base = copy.deepcopy(self.base)
        group = edited({"id": "group-one", "created": EARLY}, EARLY, group_id="chat-1", deleted_at=None)
        base["weekly_report_groups"] = [group]
        ours, theirs = copy.deepcopy(base), copy.deepcopy(base)
        ours["weekly_report_groups"][0] = edited(group, LATER, deleted_at=LATER)
        theirs["weekly_report_groups"].append(edited({"id": "group-two", "created": EARLY}, LATEST,
                                                   group_id="chat-2", deleted_at=None))
        result = merge.union(ours, theirs, base)
        groups = {g["id"]: g for g in result["weekly_report_groups"]}
        self.assertEqual(set(groups), {"group-one", "group-two"})
        self.assertEqual(groups["group-one"]["deleted_at"], LATER)
        restored, stale = copy.deepcopy(result), copy.deepcopy(result)
        restored["weekly_report_groups"] = [edited(g, LATEST, deleted_at=None) if g["id"] == "group-one" else g
                                              for g in restored["weekly_report_groups"]]
        for first, second in ((restored, stale), (stale, restored)):
            final = merge.union(first, second, result)
            self.assertTrue(all(g["deleted_at"] is None for g in final["weekly_report_groups"]))

    def test_report_group_restore_back_to_ancestor_beats_stale_delete(self):
        old = edited({"id": "group-one", "created": EARLY}, EARLY, group_id="chat-1", deleted_at=None)
        restored, stale = edited(old, LATEST, deleted_at=None), edited(old, LATER, deleted_at=LATER)
        for first, second in ((restored, stale), (stale, restored)):
            group = merge.merge_record(old, first, second, collection="weekly_report_groups")
            self.assertIsNone(group["deleted_at"])
            self.assertEqual(group["_field_updated"]["deleted_at"], LATEST)

    def test_legacy_snapshots_missing_configuration_do_not_remove_it(self):
        base = copy.deepcopy(self.base)
        base["people"] = [edited({"id": "person-ocean", "created": EARLY}, EARLY,
                                 name="Ocean", employee_id="000123")]
        base["weekly_report_groups"] = [edited({"id": "group-one", "created": EARLY}, EARLY,
                                               group_id="chat-1", deleted_at=None)]
        legacy = copy.deepcopy(self.base)
        legacy["tasks"][0] = edited(legacy["tasks"][0], LATEST, text="Legacy task edit")
        for first, second in ((legacy, base), (base, legacy), (legacy, legacy)):
            result = merge.union(first, second, base)
            self.assertEqual(result["people"], base["people"])
            self.assertEqual(result["weekly_report_groups"], base["weekly_report_groups"])
            self.assertEqual(result["tasks"][0]["text"], "Legacy task edit")
        ours = copy.deepcopy(base)
        ours["people"][0] = edited(ours["people"][0], LATER, employee_id="000999")
        ours["weekly_report_groups"][0] = edited(ours["weekly_report_groups"][0], LATER, deleted_at=LATER)
        result = merge.union(ours, legacy, base)
        self.assertEqual(result["people"][0]["employee_id"], "000999")
        self.assertEqual(result["weekly_report_groups"][0]["deleted_at"], LATER)

    def test_legacy_documents_are_not_migrated_by_validation_or_merge(self):
        old = copy.deepcopy(self.base)
        before = copy.deepcopy(old)
        merge.validate(old)
        result = merge.union(old, old, old)
        self.assertEqual(old, before)
        for name in merge.CONFIG_COLLECTIONS:
            self.assertNotIn(name, old)
            self.assertNotIn(name, result)

    def test_configuration_identifiers_must_remain_strings(self):
        for collection, field in (("projects", "dingtalk_group_id"), ("people", "employee_id"),
                                  ("people", "name"), ("weekly_report_groups", "group_id")):
            for invalid in (123, None, True, [], {}):
                with self.subTest(collection=collection, field=field, invalid=invalid):
                    data = copy.deepcopy(self.base)
                    data[collection] = [{"id": "record-one", field: invalid}]
                    with self.assertRaises(ValueError):
                        merge.validate(data)

    def test_project_and_milestone_independent_edits(self):
        ours, theirs = copy.deepcopy(self.base), copy.deepcopy(self.base)
        ours["projects"][0] = edited(ours["projects"][0], LATER, name="Renamed")
        theirs["projects"][0] = edited(theirs["projects"][0], LATEST, owner="Li")
        ours["milestones"][0] = edited(ours["milestones"][0], LATER, target_date="2026-10-20")
        theirs["milestones"][0] = edited(theirs["milestones"][0], LATEST, sort_order=2)
        result = merge.union(ours, theirs, self.base)
        self.assertEqual((result["projects"][0]["name"], result["projects"][0]["owner"]), ("Renamed", "Li"))
        self.assertEqual((result["milestones"][0]["target_date"], result["milestones"][0]["sort_order"]), ("2026-10-20", 2))

    def test_project_soft_delete_preserves_children_and_other_edits(self):
        ours, theirs = copy.deepcopy(self.base), copy.deepcopy(self.base)
        ours["projects"][0] = edited(ours["projects"][0], LATER, deleted_at=LATER)
        theirs["projects"][0] = edited(theirs["projects"][0], LATEST, name="Edited while deleted")
        result = merge.union(ours, theirs, self.base)
        self.assertEqual(result["projects"][0]["deleted_at"], LATER)
        self.assertEqual(result["projects"][0]["name"], "Edited while deleted")
        self.assertEqual(result["tasks"], self.base["tasks"])
        self.assertEqual(result["milestones"], self.base["milestones"])

    def test_restore_survives_stale_tombstone(self):
        old = edited(self.base["projects"][0], EARLY, deleted_at=EARLY)
        ours = edited(old, LATEST, deleted_at=None)
        theirs = edited(old, LATER, name="A change")
        result = merge.merge_record(old, ours, theirs)
        self.assertIsNone(result["deleted_at"])
        self.assertEqual(result["name"], "A change")

    def test_delete_restore_back_to_ancestor_beats_older_delete(self):
        old = edited(self.base["projects"][0], EARLY, deleted_at=None)
        result = merge.merge_record(old, edited(old, LATEST, deleted_at=None), edited(old, LATER, deleted_at=LATER))
        self.assertIsNone(result["deleted_at"])

    def test_legacy_physical_delete_propagates_unless_edited(self):
        ours, theirs = copy.deepcopy(self.base), copy.deepcopy(self.base)
        theirs["tasks"] = []
        self.assertEqual(merge.union(ours, theirs, self.base)["tasks"], [])
        ours["tasks"][0]["text"] = "Keep edit"
        self.assertEqual(merge.union(ours, theirs, self.base)["tasks"][0]["text"], "Keep edit")

    def test_legacy_same_field_conflict_falls_back_to_record_clock(self):
        old = self.base["tasks"][0]
        a = {**old, "text": "A", "updated_at": LATER}
        b = {**old, "text": "B", "updated_at": LATEST}
        self.assertEqual(self.task_merge(a, b)["text"], "B")

    def test_unknown_fields_and_schema_metadata_survive(self):
        ours, theirs = copy.deepcopy(self.base), copy.deepcopy(self.base)
        ours.update({"theme": "dark", "schema_version": 2, "revision": 4})
        theirs.update({"language": "zh", "schema_version": 3, "revision": 9})
        result = merge.union(ours, theirs, self.base)
        self.assertEqual((result["theme"], result["language"], result["schema_version"], result["revision"]), ("dark", "zh", 3, 10))

    def test_field_removal_keeps_other_edits(self):
        old = {**self.base["tasks"][0], "note": "Old"}
        ours = dict(old)
        del ours["note"]
        result = self.task_merge(ours, edited(old, LATEST, owner="Li"), old)
        self.assertNotIn("note", result)
        self.assertEqual(result["owner"], "Li")

    def test_cli_bad_input_does_not_overwrite_ours(self):
        with tempfile.TemporaryDirectory() as directory:
            base, ours, theirs = [Path(directory) / name for name in ("base", "ours", "theirs")]
            for path in (base, ours, theirs):
                path.write_text(json.dumps(self.base), encoding="utf-8")
            before = ours.read_bytes()
            for invalid in ('<<<<<<< ours\n{}\n=======\n{}\n>>>>>>> theirs', '{"tasks":NaN}', '{"tasks":[],"tasks":[]}', '{"tasks":[{"id":"x"},{"id":"x"}]}', '{"tasks":{}}', '{}', 'null'):
                with self.subTest(invalid=invalid):
                    theirs.write_text(invalid, encoding="utf-8")
                    result = subprocess.run([sys.executable, str(SCRIPT), str(base), str(ours), str(theirs)], capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(ours.read_bytes(), before)
            theirs.write_bytes(before)
            base.write_text("broken", encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPT), str(base), str(ours), str(theirs)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(ours.read_bytes(), before)

    def test_cli_empty_ancestor_supports_add_add(self):
        with tempfile.TemporaryDirectory() as directory:
            base, ours, theirs = [Path(directory) / name for name in ("base", "ours", "theirs")]
            base.write_text("", encoding="utf-8")
            ours.write_text('{"tasks":[{"id":"a"}]}', encoding="utf-8")
            theirs.write_text('{"tasks":[{"id":"b"}]}', encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPT), str(base), str(ours), str(theirs)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({task["id"] for task in json.loads(ours.read_text())["tasks"]}, {"a", "b"})


if __name__ == "__main__":
    unittest.main()
