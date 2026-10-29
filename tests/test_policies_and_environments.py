"""
Tests for Policies, Environments, Baseline Fallback, and Error Handling.
Covers:
- Scenario 12: Failed application startup
- Scenario 13: Unavailable test database
- Scenario 14: Baseline that cannot be established
- Scenario 15: Missing AI provider (local mode fallback)
- Scenario 32: Force-push from a stale checkout uses the live remote tip as baseline
- Scenario 22: Failed critical test under strict mode (exit code 1)
- Scenario 23: Inconclusive test under advisory mode (exit code 0)
- Scenario 26: Unavailable browser handled cleanly
- Scenario 29: Cleanup after failed comparison
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush.config import PoliciesConfig, ApplicationConfig, AIConfig
from proofpush.policy_enforcer import PolicyEnforcer
from proofpush.classifier import Finding
from proofpush.baseline_manager import BaselineManager
from proofpush.ai_engine import get_ai_adapter
from proofpush.environment_manager import EnvironmentManager
from proofpush.utils import run_command


class TestPoliciesAndEnvironments(GitRepoTestCase):
    def test_scenario_22_failed_critical_test_under_strict_mode(self):
        """Scenario 22: In strict mode, any critical regression blocks push with exit code 1."""
        policies = PoliciesConfig(fail_open=False)
        enforcer = PolicyEnforcer(mode="strict", policies_config=policies)

        findings = [
            Finding(
                category="REGRESSION",
                target="/checkout",
                before_behavior="200 OK",
                after_behavior="500 Internal Server Error",
                expected_behavior="200 OK",
                actual_result="Crash",
                confidence=1.0,
                recommended_action="Fix route crash",
                is_critical=True,
            )
        ]

        decision = enforcer.evaluate(findings=findings)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.exit_code, 1)
        self.assertIn("Critical functional or route regressions detected", decision.reason)

    def test_scenario_23_inconclusive_test_under_advisory_mode(self):
        """Scenario 23: In advisory mode, findings are reported but push is allowed with exit code 0."""
        policies = PoliciesConfig(fail_open=False)
        enforcer = PolicyEnforcer(mode="advisory", policies_config=policies)

        findings = [
            Finding(
                category="POSSIBLE_REGRESSION",
                target="/profile",
                before_behavior="Loaded in 100ms",
                after_behavior="Loaded in 800ms",
                expected_behavior="Fast response",
                actual_result="Performance difference",
                confidence=0.5,
                recommended_action="Review performance",
                is_critical=False,
            )
        ]

        decision = enforcer.evaluate(findings=findings)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.exit_code, 0)
        self.assertIn("Advisory mode", decision.reason)

    def test_scenario_14_baseline_cannot_be_established(self):
        """Scenario 14: Clear error when no remote tracking ref, commit, or baseline snapshot exists."""
        bm = BaselineManager(self.local_dir)
        # Empty repo without remote commits
        res = bm.resolve_baseline_sha(strategy="merge-base", proposed_sha="nonexistent_sha")
        self.assertIsNone(res)

    def test_scenario_32_stale_force_push_baseline_is_live_remote(self):
        """Scenario 32: A fix pushed from another clone is the baseline, not the stale merge-base."""
        old_sha = self.commit_file("app.js", "buggy", "initial")
        run_command(["git", "push", "origin", "main"], cwd=self.local_dir)

        other = self.test_dir / "other"
        run_command(["git", "clone", "-q", str(self.remote_dir), str(other)], cwd=self.test_dir)
        run_command(["git", "config", "user.name", "Other"], cwd=other)
        run_command(["git", "config", "user.email", "other@example.com"], cwd=other)
        (other / "app.js").write_text("fixed", encoding="utf-8")
        run_command(["git", "commit", "-qam", "fix bug"], cwd=other)
        run_command(["git", "push", "-q", "origin", "main"], cwd=other)
        _, fix_sha, _ = run_command(["git", "rev-parse", "HEAD"], cwd=other)
        fix_sha = fix_sha.strip()

        # Local clone never fetched the fix and builds on the old commit
        stale_sha = self.commit_file("other.js", "x", "unrelated change")
        bm = BaselineManager(self.local_dir)
        self.assertEqual(bm.resolve_baseline_sha("merge-base", stale_sha, remote_sha=fix_sha), fix_sha)
        self.assertNotEqual(fix_sha, old_sha)

    def test_scenario_15_missing_ai_provider_local_fallback(self):
        """Scenario 15: No external AI keys configured; seamlessly falls back to LocalRuleEngine."""
        ai_cfg = AIConfig(provider="openai", model="gpt-4o-mini")
        # Ensure OPENAI_API_KEY is unset
        import os
        old_val = os.environ.pop("OPENAI_API_KEY", None)
        try:
            adapter = get_ai_adapter(ai_cfg)
            # Must return LocalRuleEngine or fallback safely
            findings = [{"category": "UNCHANGED", "target": "/"}]
            summary = adapter.synthesize_summary("Test Task", findings)
            self.assertIn("Deterministic Local Analysis", summary)
            self.assertIn("No functional regressions observed", summary)
        finally:
            if old_val:
                os.environ["OPENAI_API_KEY"] = old_val

    def test_scenario_12_failed_application_startup(self):
        """Scenario 12: Application startup failure (bad command) captured cleanly with error message."""
        app_cfg = ApplicationConfig(
            type="custom",
            start_command="exit 1",  # Instantly crashes
            health_check="/",
            health_timeout=2,
        )
        c1 = self.commit_file("test.txt", "content", "initial")
        env_mgr = EnvironmentManager(self.local_dir, app_cfg)
        try:
            env = env_mgr.launch_environment(c1, "proposed")
            self.assertFalse(env.is_ready)
            self.assertIsNotNone(env.startup_error)
            self.assertIn("terminated prematurely", env.startup_error)
        finally:
            env_mgr.cleanup()

    def test_scenario_13_unavailable_test_database(self):
        """Scenario 13: Database failure or missing file provides isolated path and doesn't pollute host."""
        app_cfg = ApplicationConfig(start_command="", port=3450)
        c1 = self.commit_file("index.html", "<h1>Hello</h1>", "initial")
        env_mgr = EnvironmentManager(self.local_dir, app_cfg)
        try:
            env = env_mgr.launch_environment(c1, "proposed")
            self.assertTrue(env.worktree_path.exists())
            self.assertIn(".auditor_test.db", str(env.db_path))
        finally:
            env_mgr.cleanup()

    def test_scenario_26_unavailable_browser(self):
        """Scenario 26: Gracefully reports error if browser binary is missing or cannot launch."""
        from proofpush.browser_engine import BrowserEngine
        import asyncio
        # Point to an unreachable port
        be = BrowserEngine(timeout=1000)
        res = asyncio.run(be.visit_page("http://127.0.0.1:19999", "/test"))
        self.assertFalse(res.passed)
        self.assertIsNotNone(res.error_message)

    def test_scenario_29_cleanup_after_failed_comparison(self):
        """Scenario 29: Temporary worktrees and background child processes are cleaned up."""
        app_cfg = ApplicationConfig(start_command="python3 -m http.server $PORT", health_timeout=2)
        c1 = self.commit_file("index.html", "<h1>Test</h1>", "initial")
        env_mgr = EnvironmentManager(self.local_dir, app_cfg)
        env = env_mgr.launch_environment(c1, "baseline")
        worktree_path = env.worktree_path

        self.assertTrue(worktree_path.exists())
        env_mgr.cleanup()
        # After cleanup, worktree must be removed
        self.assertFalse(worktree_path.exists())
