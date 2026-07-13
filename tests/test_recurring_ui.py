import pathlib
import unittest


class RecurringUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = pathlib.Path("index.html").read_text(encoding="utf-8")

    def test_frequency_controls_and_locked_endpoint_exist(self):
        self.assertIn('id="recurNewFrequency"', self.html)
        self.assertIn('<option value="daily">每日</option>', self.html)
        self.assertIn('<option value="weekly">每周</option>', self.html)
        self.assertIn('<option value="monthly">每月</option>', self.html)
        self.assertIn('id="recurNewWeekday"', self.html)
        self.assertIn('id="recurNewMonthday"', self.html)
        self.assertIn('fetch("/recurring/add"', self.html)
        self.assertIn("function scheduleLabel", self.html)

    def test_all_recurring_seeds_are_hidden_from_the_main_list(self):
        self.assertIn("function isRecurringSeed", self.html)
        self.assertIn("if (isRecurringSeed(t)) continue;", self.html)


if __name__ == "__main__":
    unittest.main()
