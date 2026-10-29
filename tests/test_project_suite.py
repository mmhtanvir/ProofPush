"""
Project Test Suite Tests.
Covers:
- Scenario 61: The project's own tests run on both versions; a test that passed before and fails now is a
  regression (named), already-failing tests are only a notice, and a clean change reports nothing
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush import test_suite

TESTS = """import unittest
from cart import total, discount

class TestCart(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total([(10, 2), (5, 1)]), 25)

    def test_discount(self):
        self.assertEqual(discount(100), 90)

    def test_legacy_rounding(self):
        self.assertEqual(round(2.675, 2), 2.67)
"""
CART_OK = "def total(items):\n    return sum(p * q for p, q in items)\n\ndef discount(x):\n    return x * 0.9\n"
CART_BROKEN = "def total(items):\n    return sum(p for p, q in items)\n\ndef discount(x):\n    return x * 0.9\n"


class TestProjectSuite(GitRepoTestCase):
    def suite(self, cart):
        root = Path(tempfile.mkdtemp(dir=self.test_dir))
        (root / "cart.py").write_text(cart)
        (root / "test_cart.py").write_text(TESTS)
        return test_suite.run_suite(root, "{python} -m unittest -v", timeout=120)

    def test_scenario_61_newly_failing_tests(self):
        before, after = self.suite(CART_OK), self.suite(CART_BROKEN)
        self.assertEqual(before.failing, set())  # round(2.675, 2) is 2.67 in Python: passes
        findings = test_suite.compare_suites(before, after)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "REGRESSION")
        self.assertIn("test_total", findings[0].after_behavior)
        self.assertNotIn("test_discount", findings[0].after_behavior)
        self.assertEqual(test_suite.compare_suites(before, self.suite(CART_OK)), [])
        self.assertEqual([f.category for f in test_suite.compare_suites(after, self.suite(CART_BROKEN))], ["NOTICE"])

    def test_scenario_61b_detects_runner(self):
        root = self.test_dir / "proj"
        (root / "tests").mkdir(parents=True)
        (root / "tests" / "test_x.py").write_text("")
        self.assertIn("pytest", test_suite.detect_test_command(root))
        (root / "manage.py").write_text("")
        self.assertIn("manage.py test", test_suite.detect_test_command(root))
        (root / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
        self.assertEqual(test_suite.detect_test_command(root), "npm test --silent")
