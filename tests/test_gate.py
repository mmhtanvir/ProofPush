"""
Deployment Gate Tests.
Covers:
- Scenario 48: A release that reverts a past fix is rejected; a clean one deploys (backend / deploy.sh exit codes)
- Scenario 49: Vercel Ignored Build Step: inverted exit codes, shallow clone, previews and first deploys build
- Scenario 50: A page already broken on the live side is a notice, not a regression of the new release
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush.browser_engine import PageVisitResult
from proofpush.classifier import RegressionClassifier
from proofpush.task_resolver import TaskContext
from proofpush.utils import run_command


class TestGate(GitRepoTestCase):
    def gate(self, *args, cwd=None, **env):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("VERCEL_")}
        clean.update(env)
        return subprocess.run([sys.executable, "-m", "proofpush", "gate", *args], cwd=cwd or self.local_dir,
                              env=clean, capture_output=True, text=True, timeout=300)

    def make_history(self):
        self.commit_file(".proofpush.yml", "notify: {desktop: false}\n", "config")
        self.commit_file("cart.py", "def total(c):\n    count = len(c.items)\n    return count * c.price\n", "init")
        live = self.commit_file("cart.py", "def total(c):\n    return sum(i.price * i.qty for i in c.items)\n", "Fix total ignoring quantity")
        clean = self.commit_file("README.md", "# shop\n", "Add docs")
        run_command(["git", "revert", "--no-edit", "HEAD~1"], cwd=self.local_dir)
        bad = run_command(["git", "rev-parse", "HEAD"], cwd=self.local_dir)[1].strip()
        return live, clean, bad

    def test_scenario_48_backend_gate(self):
        live, clean, bad = self.make_history()
        self.assertEqual(self.gate("--base", live, "--candidate", clean).returncode, 0)
        rejected = self.gate("--base", live, "--candidate", bad)
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("Deployment REJECTED", rejected.stdout)
        self.assertIn("Fix total ignoring quantity", rejected.stdout)

    def test_scenario_49_vercel_ignored_build_step(self):
        live, _, bad = self.make_history()
        shallow = self.test_dir / "vercel-build"
        run_command(["git", "clone", "-q", "--depth", "1", f"file://{self.local_dir}", str(shallow)], cwd=self.test_dir)
        canceled = self.gate("--vercel", cwd=shallow, VERCEL_ENV="production", VERCEL_GIT_PREVIOUS_SHA=live, VERCEL_GIT_COMMIT_SHA=bad)
        self.assertEqual(canceled.returncode, 0, canceled.stdout + canceled.stderr)  # 0 = Vercel cancels the deployment
        self.assertEqual(self.gate("--vercel", cwd=shallow, VERCEL_ENV="preview", VERCEL_GIT_PREVIOUS_SHA=live,
                                   VERCEL_GIT_COMMIT_SHA=bad).returncode, 1)
        self.assertEqual(self.gate("--vercel", cwd=shallow, VERCEL_ENV="production", VERCEL_GIT_COMMIT_SHA=bad).returncode, 1)

    def test_scenario_50_already_broken_page_is_a_notice(self):
        classifier = RegressionClassifier(TaskContext(title="Copy tweak"))
        broken = PageVisitResult(route="/reports", url="u", status=500, title="", passed=False)
        fine = PageVisitResult(route="/reports", url="u", status=200, title="Reports")
        finding = classifier.classify_route_visit("/reports", broken, broken)
        self.assertEqual(finding.category, "NOTICE")
        self.assertFalse(finding.is_critical)
        self.assertTrue(classifier.classify_route_visit("/reports", fine, broken).is_critical)
