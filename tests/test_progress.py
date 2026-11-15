import unittest

from proofpush.cli import progress_bar


class ProgressBarTest(unittest.TestCase):
    def test_bar_and_time_left_from_the_estimate(self):
        # 60 s into a check expected to take 240 s -> a quarter done, 3:00 left
        self.assertEqual(progress_bar(60, 240, width=8), "[██░░░░░░] 25% · ~3:00 left")

    def test_never_full_until_finished_and_no_bar_without_an_estimate(self):
        self.assertEqual(progress_bar(500, 240, width=4), "[████] 99% · almost done")
        self.assertEqual(progress_bar(500, 240, finished=True, width=4), "[████] 100%")
        self.assertEqual(progress_bar(10, None), "")


if __name__ == "__main__":
    unittest.main()
