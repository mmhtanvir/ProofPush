import unittest
from datetime import datetime, timedelta

from proofpush.cli import progress_bar


class ProgressBarTest(unittest.TestCase):
    def test_bar_counts_and_time_left(self):
        # 2 of 4 done in 60 s -> 30 s each -> 2 left take ~1:00
        bar = progress_bar(2, 4, datetime.now() - timedelta(seconds=60), width=10)
        self.assertTrue(bar.startswith("[█████░░░░░] 2/4"))
        self.assertIn("~1:00 left", bar)

    def test_no_estimate_before_first_item_or_when_done(self):
        self.assertNotIn("left", progress_bar(0, 4, datetime.now()))
        self.assertEqual(progress_bar(4, 4, datetime.now(), width=4), "[████] 4/4")


if __name__ == "__main__":
    unittest.main()
