"""
Tests for Git Pre-Push Interceptor and Push Scenarios.
Covers:
- Scenario 1: Normal human-initiated push
- Scenario 2: Simulated AI CLI push
- Scenario 19: Force push
- Scenario 20: Multiple commits in a single push
- Scenario 21: Uncommitted changes that are not part of the push
- Scenario 24: Existing Git hooks that must be preserved
- Scenario 25: Push using --no-verify
- Scenario 30: Blocked push does not update remote refs
"""

import sys
from pathlib import Path

# Ensure paths
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush.git_interceptor import GitInterceptor
from proofpush.utils import run_command


class TestGitAndHooks(GitRepoTestCase):
    def test_scenario_24_preserve_existing_git_hooks(self):
        """Scenario 24: Pre-existing git hooks must be preserved as pre-push.legacy and chained."""
        hooks_dir = self.local_dir / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        legacy_hook = hooks_dir / "pre-push"
        legacy_content = "#!/bin/sh\necho 'Running existing test hook'\nexit 0\n"
        legacy_hook.write_text(legacy_content, encoding="utf-8")
        legacy_hook.chmod(0o755)

        interceptor = GitInterceptor(self.local_dir)
        res = interceptor.install_hook()
        self.assertTrue(res["success"])
        self.assertTrue(res["preserved_legacy"])

        # Check pre-push.legacy exists with original content
        backup = hooks_dir / "pre-push.legacy"
        self.assertTrue(backup.exists())
        self.assertIn("Running existing test hook", backup.read_text(encoding="utf-8"))

        # Verify new hook is proofpush
        new_hook_content = (hooks_dir / "pre-push").read_text(encoding="utf-8")
        self.assertIn("proofpush", new_hook_content)

        # Test uninstall restores legacy
        unres = interceptor.uninstall_hook()
        self.assertTrue(unres["success"])
        self.assertTrue(unres["restored_legacy"])
        self.assertEqual(legacy_hook.read_text(encoding="utf-8"), legacy_content)

    def test_scenario_20_multiple_commits_in_single_push(self):
        """Scenario 20: Stdin parsing and commit resolution with multiple commits in one push."""
        c1 = self.commit_file("file1.txt", "content1", "Commit 1")
        c2 = self.commit_file("file2.txt", "content2", "Commit 2")
        c3 = self.commit_file("file3.txt", "content3", "Commit 3")

        interceptor = GitInterceptor(self.local_dir)
        stdin_input = f"refs/heads/main {c3} refs/heads/main {c1}\n"
        updates = interceptor.parse_pre_push_stdin(stdin_input)

        self.assertEqual(len(updates), 1)
        up = updates[0]
        self.assertEqual(up.local_sha, c3)
        self.assertEqual(up.remote_sha, c1)
        # Should identify commits between c1 and c3 (c2 and c3)
        self.assertIn(c2, up.commits)
        self.assertIn(c3, up.commits)
        self.assertNotIn(c1, up.commits)

    def test_scenario_19_force_push_detection(self):
        """Scenario 19: Identify force push when remote ref is not an ancestor."""
        self.commit_file("base.txt", "base", "Initial base")
        # Push initial to remote
        run_command(["git", "push", "origin", "main"], cwd=self.local_dir, timeout=300)

        # Create branch and commit on remote
        run_command(["git", "checkout", "-b", "feature"], cwd=self.local_dir)
        c_feat = self.commit_file("feat.txt", "feat", "Feature commit")
        run_command(["git", "push", "origin", "feature"], cwd=self.local_dir, timeout=300)

        # Now reset local feature and diverge (simulate force push)
        run_command(["git", "reset", "--hard", "HEAD~1"], cwd=self.local_dir)
        c_diverged = self.commit_file("diverged.txt", "diverged", "Diverged commit")

        interceptor = GitInterceptor(self.local_dir)
        stdin_input = f"refs/heads/feature {c_diverged} refs/heads/feature {c_feat}\n"
        updates = interceptor.parse_pre_push_stdin(stdin_input)

        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0].is_force_push)

    def test_scenario_21_uncommitted_changes_ignored(self):
        """Scenario 21: Uncommitted dirty changes in working tree are ignored during commit resolution."""
        c1 = self.commit_file("committed.txt", "v1", "Committed change")
        # Add dirty uncommitted file and modified file
        dirty_file = self.local_dir / "dirty.txt"
        dirty_file.write_text("uncommitted content", encoding="utf-8")
        (self.local_dir / "committed.txt").write_text("v2 uncommitted", encoding="utf-8")

        interceptor = GitInterceptor(self.local_dir)
        head_sha = interceptor.get_current_head_sha()
        self.assertEqual(head_sha, c1)

        # Diff should only reflect committed changes between refs
        changed = interceptor.get_changed_files("HEAD~0", head_sha)
        changed_paths = [f["path"] for f in changed]
        self.assertNotIn("dirty.txt", changed_paths)

    def test_scenario_1_normal_human_initiated_push(self):
        """Scenario 1: Normal human-initiated push with pre-push hook execution."""
        self.create_mock_site(version="baseline")
        run_command(["git", "push", "origin", "main"], cwd=self.local_dir, timeout=300)

        interceptor = GitInterceptor(self.local_dir)
        interceptor.install_hook()

        self.commit_file("index.html", "<h1>Test Store Updated</h1>", "feat: Update store greeting")
        code, out, err = run_command(["git", "push", "origin", "main"], cwd=self.local_dir, timeout=300)
        self.assertEqual(code, 0)

    def test_scenario_2_simulated_ai_cli_push(self):
        """Scenario 2: Push initiated by an autonomous AI agent CLI without TTY interaction."""
        self.create_mock_site(version="baseline")
        run_command(["git", "push", "origin", "main"], cwd=self.local_dir, timeout=300)

        interceptor = GitInterceptor(self.local_dir)
        interceptor.install_hook()

        # Simulate AI agent generating a new feature cleanly
        self.commit_file("index.html", "<h1>Store AI Enhanced</h1>", "feat: Agent automated update")
        env_vars = {"CI": "1", "NONINTERACTIVE": "1", "AGENT_CLI": "claude-code"}
        code, out, err = run_command(["git", "push", "origin", "main"], cwd=self.local_dir, env=env_vars, timeout=300)
        self.assertEqual(code, 0)

    def test_scenario_25_push_using_no_verify(self):
        """Scenario 25: Pushing with --no-verify cleanly bypasses pre-push hook per Git design."""
        self.create_mock_site(version="baseline")
        run_command(["git", "push", "origin", "main"], cwd=self.local_dir, timeout=300)

        interceptor = GitInterceptor(self.local_dir)
        interceptor.install_hook()

        # Add a file with a deliberate secret that would normally fail audit
        self.commit_file("secret.txt", "OPENAI_API_KEY=sk-abcdef1234567890abcdef1234567890", "Add secret")

        # Push with --no-verify must succeed per Git standard behavior
        code, out, _ = run_command(["git", "push", "--no-verify", "origin", "main"], cwd=self.local_dir, timeout=300)
        self.assertEqual(code, 0)

    def test_scenario_30_blocked_push_does_not_update_remote(self):
        """Scenario 30: When push is blocked by pre-push hook, remote refs remain unchanged."""
        c_initial = self.commit_file("init.txt", "v1", "Initial commit")
        run_command(["git", "push", "origin", "main"], cwd=self.local_dir, timeout=300)

        # Get initial remote sha
        code, out, _ = run_command(["git", "rev-parse", "main"], cwd=self.remote_dir)
        remote_sha_initial = out.strip()
        self.assertEqual(remote_sha_initial, c_initial)

        # Install hook that deliberately rejects push
        hooks_dir = self.local_dir / ".git" / "hooks"
        hook_file = hooks_dir / "pre-push"
        hook_file.write_text("#!/bin/sh\necho 'Push rejected by test auditor'\nexit 1\n", encoding="utf-8")
        hook_file.chmod(0o755)

        # Commit new change
        c_new = self.commit_file("new.txt", "v2", "New commit")

        # Attempt push (should fail with exit code != 0)
        code, out, err = run_command(["git", "push", "origin", "main"], cwd=self.local_dir, timeout=300)
        self.assertNotEqual(code, 0)

        # Verify remote ref was NOT updated
        code, out, _ = run_command(["git", "rev-parse", "main"], cwd=self.remote_dir)
        remote_sha_after = out.strip()
        self.assertEqual(remote_sha_after, remote_sha_initial)
        self.assertNotEqual(remote_sha_after, c_new)
