import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import server


class RecurringTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_file = os.path.join(self.tmp.name, "todos.json")
        self.state_file = os.path.join(self.tmp.name, "state.json")
        self.backup_dir = os.path.join(self.tmp.name, "backups")
        self.patches = [
            patch.object(server, "DATA_FILE", self.data_file),
            patch.object(server, "APP_SUPPORT_DIR", self.tmp.name),
            patch.object(server, "STATE_FILE", self.state_file),
            patch.object(server, "BACKUP_DIR", self.backup_dir),
            patch.object(server, "schedule_sync", lambda: None),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def write_tasks(self, tasks):
        with open(self.data_file, "w", encoding="utf-8") as handle:
            json.dump({"updated": None, "tasks": tasks}, handle, ensure_ascii=False)

    def read_tasks(self):
        with open(self.data_file, encoding="utf-8") as handle:
            return json.load(handle)["tasks"]

    def test_schedule_matches_daily_weekly_and_monthly(self):
        run_date = date(2026, 7, 13)
        self.assertTrue(server.schedule_matches({"recurring": "daily"}, run_date))
        self.assertTrue(server.schedule_matches(
            {"recurring": "weekly", "recur_weekday": 1}, run_date
        ))
        self.assertFalse(server.schedule_matches(
            {"recurring": "weekly", "recur_weekday": 2}, run_date
        ))
        self.assertTrue(server.schedule_matches(
            {"recurring": "monthly", "recur_monthday": 13}, run_date
        ))
        self.assertFalse(server.schedule_matches(
            {"recurring": "monthly", "recur_monthday": 12}, run_date
        ))
        self.assertFalse(server.schedule_matches({"recurring": "weekly"}, run_date))

    def test_ensure_is_selective_and_idempotent(self):
        self.write_tasks([
            {"id": "daily", "text": "daily", "recurring": "daily"},
            {"id": "mon", "text": "mon", "recurring": "weekly", "recur_weekday": 1},
            {"id": "tue", "text": "tue", "recurring": "weekly", "recur_weekday": 2},
            {"id": "m13", "text": "m13", "recurring": "monthly", "recur_monthday": 13},
        ])
        with patch.object(server, "today_str", return_value="2026-07-13"):
            self.assertTrue(server.ensure_recurring_today())
            self.assertFalse(server.ensure_recurring_today())
        ids = {item["id"] for item in self.read_tasks()}
        self.assertEqual(
            {item for item in ids if item.startswith("recur:")},
            {
                "recur:daily:2026-07-13",
                "recur:mon:2026-07-13",
                "recur:m13:2026-07-13",
            },
        )

    def test_ensure_auto_skips_an_older_unfinished_instance(self):
        self.write_tasks([
            {"id": "seed", "text": "weekly", "recurring": "weekly", "recur_weekday": 1},
            {
                "id": "recur:seed:2026-07-06",
                "text": "weekly",
                "done": False,
                "recur_source": "seed",
            },
        ])
        with patch.object(server, "today_str", return_value="2026-07-13"):
            server.ensure_recurring_today()
        old = next(
            item for item in self.read_tasks()
            if item["id"] == "recur:seed:2026-07-06"
        )
        self.assertTrue(old["done"])
        self.assertTrue(old["auto_skipped"])


if __name__ == "__main__":
    unittest.main()
