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
            ours[name].append({"id": name + "-ours"})
            theirs[name].append({"id": name + "-theirs"})
        result = merge.union(ours, theirs, self.base)
        for name in merge.COLLECTIONS:
            self.assertEqual(len(result[name]), 3)
        self.assertEqual(result["revision"], 2)

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
