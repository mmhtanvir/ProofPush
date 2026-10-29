"""
Git Pre-Push Interceptor and Hook Manager.
Handles pre-push hook installation, ref parsing from stdin, outgoing commit discovery,
hook chaining, worktree awareness, and recursion prevention.
"""

import os
import sys
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from change_auditor.utils import run_command

ZERO_SHA = "0000000000000000000000000000000000000000"

HOOK_TEMPLATE = """#!/usr/bin/env bash
# ProofPush Pre-Push Hook
# Automatically installed by `proofpush install`

# Prevent recursive invocation
if [ "${PROOFPUSH_RUNNING}" = "1" ] || [ "${CHANGE_AUDITOR_RUNNING}" = "1" ]; then
    exit 0
fi

# Run legacy / pre-existing hook first if present
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEGACY_HOOK="${HOOK_DIR}/pre-push.legacy"

# Detect proofpush or change-auditor executable
BIN_CMD="proofpush"
if ! command -v proofpush >/dev/null 2>&1; then
    BIN_CMD="change-auditor"
fi

if [ -f "${LEGACY_HOOK}" ] && [ -x "${LEGACY_HOOK}" ]; then
    STDIN_TEMP=$(mktemp)
    cat > "${STDIN_TEMP}"
    "${LEGACY_HOOK}" "$@" < "${STDIN_TEMP}"
    LEGACY_EXIT=$?
    if [ ${LEGACY_EXIT} -ne 0 ]; then
        rm -f "${STDIN_TEMP}"
        exit ${LEGACY_EXIT}
    fi
    exec ${BIN_CMD} review --hook < "${STDIN_TEMP}"
    EXIT_CODE=$?
    rm -f "${STDIN_TEMP}"
    exit ${EXIT_CODE}
else
    exec ${BIN_CMD} review --hook
fi
"""


@dataclass
class PushRefUpdate:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str
    is_delete: bool = False
    is_new_branch: bool = False
    is_force_push: bool = False
    commits: List[str] = None

    def __post_init__(self):
        if self.commits is None:
            self.commits = []
        self.is_delete = (self.local_sha == ZERO_SHA)
        self.is_new_branch = (self.remote_sha == ZERO_SHA)


class GitInterceptor:
    def __init__(self, repo_path: Optional[Path] = None):
        self.repo_path = (repo_path or Path.cwd()).resolve()

    def get_git_dirs(self) -> Tuple[Optional[Path], Optional[Path]]:
        """Return (git_dir, common_git_dir) accounting for worktrees."""
        code, git_dir_str, _ = run_command(["git", "rev-parse", "--git-dir"], cwd=self.repo_path)
        if code != 0:
            return None, None
        
        git_dir = Path(git_dir_str.strip())
        if not git_dir.is_absolute():
            git_dir = (self.repo_path / git_dir).resolve()

        code, common_str, _ = run_command(["git", "rev-parse", "--git-common-dir"], cwd=self.repo_path)
        if code == 0 and common_str.strip():
            common_dir = Path(common_str.strip())
            if not common_dir.is_absolute():
                common_dir = (self.repo_path / common_dir).resolve()
        else:
            common_dir = git_dir

        return git_dir, common_dir

    def get_hooks_dir(self) -> Optional[Path]:
        """Return the effective git hooks directory."""
        _, common_dir = self.get_git_dirs()
        if not common_dir:
            return None
        return common_dir / "hooks"

    def install_hook(self, global_mode: bool = False) -> Dict[str, Any]:
        """Install pre-push hook. Preserves existing hooks by renaming to pre-push.legacy."""
        if global_mode:
            code, stdout, _ = run_command(["git", "config", "--global", "core.hooksPath"])
            hooks_dir = Path(stdout.strip()) if (code == 0 and stdout.strip()) else Path.home() / ".git_template" / "hooks"
            hooks_dir.mkdir(parents=True, exist_ok=True)
            run_command(["git", "config", "--global", "core.hooksPath", str(hooks_dir)])
        else:
            hooks_dir = self.get_hooks_dir()
            if not hooks_dir:
                return {"success": False, "error": f"Not a git repository: {self.repo_path}"}
            hooks_dir.mkdir(parents=True, exist_ok=True)

        hook_file = hooks_dir / "pre-push"
        legacy_hook = hooks_dir / "pre-push.legacy"
        preserved = False

        if hook_file.exists():
            content = hook_file.read_text(encoding="utf-8", errors="ignore")
            if "proofpush" in content or "change-auditor" in content:
                # Update existing hook
                hook_file.write_text(HOOK_TEMPLATE, encoding="utf-8")
                hook_file.chmod(hook_file.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                return {
                    "success": True,
                    "action": "updated",
                    "path": str(hook_file),
                    "preserved_legacy": legacy_hook.exists(),
                }
            else:
                # Preserve third-party hook
                if not legacy_hook.exists():
                    hook_file.rename(legacy_hook)
                    preserved = True

        hook_file.write_text(HOOK_TEMPLATE, encoding="utf-8")
        hook_file.chmod(hook_file.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        return {
            "success": True,
            "action": "installed",
            "path": str(hook_file),
            "preserved_legacy": preserved,
        }

    def uninstall_hook(self, global_mode: bool = False) -> Dict[str, Any]:
        """Uninstall pre-push hook and restore legacy hook if present."""
        if global_mode:
            code, stdout, _ = run_command(["git", "config", "--global", "core.hooksPath"])
            if code != 0 or not stdout.strip():
                return {"success": False, "error": "No global git hooksPath configured"}
            hooks_dir = Path(stdout.strip())
        else:
            hooks_dir = self.get_hooks_dir()
            if not hooks_dir or not hooks_dir.exists():
                return {"success": False, "error": "No hooks directory found"}

        hook_file = hooks_dir / "pre-push"
        legacy_hook = hooks_dir / "pre-push.legacy"

        if not hook_file.exists():
            return {"success": True, "action": "not_installed"}

        content = hook_file.read_text(encoding="utf-8", errors="ignore")
        if "proofpush" not in content and "change-auditor" not in content:
            return {"success": False, "error": "pre-push hook is not managed by proofpush or change-auditor"}

        hook_file.unlink()
        restored = False
        if legacy_hook.exists():
            legacy_hook.rename(hook_file)
            restored = True

        return {
            "success": True,
            "action": "uninstalled",
            "restored_legacy": restored,
        }

    def inspect_hook(self) -> Dict[str, Any]:
        """Diagnose and inspect the status of the pre-push hook."""
        hooks_dir = self.get_hooks_dir()
        if not hooks_dir:
            return {"installed": False, "reason": "Not a git repository"}

        hook_file = hooks_dir / "pre-push"
        legacy_hook = hooks_dir / "pre-push.legacy"

        if not hook_file.exists():
            return {"installed": False, "path": str(hook_file)}

        is_executable = os.access(hook_file, os.X_OK)
        content = hook_file.read_text(encoding="utf-8", errors="ignore")
        is_managed = "proofpush" in content or "change-auditor" in content

        return {
            "installed": is_managed,
            "executable": is_executable,
            "path": str(hook_file),
            "custom_or_other": not is_managed,
            "has_legacy": legacy_hook.exists(),
        }

    def parse_pre_push_stdin(self, stdin_text: str) -> List[PushRefUpdate]:
        """Parse standard Git pre-push stdin format:
        <local ref> <local sha> <remote ref> <remote sha>
        """
        updates = []
        for line in stdin_text.strip().splitlines():
            parts = line.strip().split()
            if len(parts) >= 4:
                local_ref, local_sha, remote_ref, remote_sha = parts[:4]
                update = PushRefUpdate(
                    local_ref=local_ref,
                    local_sha=local_sha,
                    remote_ref=remote_ref,
                    remote_sha=remote_sha,
                )
                self._populate_update_details(update)
                updates.append(update)
        return updates

    def _populate_update_details(self, update: PushRefUpdate) -> None:
        """Analyze commits and force-push status for an update."""
        if update.is_delete:
            return

        if not update.is_new_branch:
            # Check if force-push
            code, _, _ = run_command(
                ["git", "merge-base", "--is-ancestor", update.remote_sha, update.local_sha],
                cwd=self.repo_path
            )
            update.is_force_push = (code != 0)

        # Retrieve commit list
        if update.is_new_branch:
            # For new branch, outgoing commits are local_sha not reachable from any remote
            code, out, _ = run_command(
                ["git", "rev-list", update.local_sha, "--not", "--remotes"],
                cwd=self.repo_path
            )
            if code != 0 or not out.strip():
                # If no remotes exist, compare against HEAD~1 or initial
                code, out, _ = run_command(["git", "rev-list", "-n", "10", update.local_sha], cwd=self.repo_path)
            commits = [c.strip() for c in out.strip().splitlines() if c.strip()]
        else:
            if update.is_force_push:
                code, base_out, _ = run_command(
                    ["git", "merge-base", update.remote_sha, update.local_sha],
                    cwd=self.repo_path
                )
                base = base_out.strip() if code == 0 and base_out.strip() else update.remote_sha
                code, out, _ = run_command(
                    ["git", "rev-list", f"{base}..{update.local_sha}"],
                    cwd=self.repo_path
                )
            else:
                code, out, _ = run_command(
                    ["git", "rev-list", f"{update.remote_sha}..{update.local_sha}"],
                    cwd=self.repo_path
                )
            commits = [c.strip() for c in out.strip().splitlines() if c.strip()]

        update.commits = commits

    def get_current_head_sha(self) -> str:
        """Get the current HEAD commit SHA."""
        code, out, _ = run_command(["git", "rev-parse", "HEAD"], cwd=self.repo_path)
        if code != 0:
            raise RuntimeError(f"Failed to get HEAD commit: {out}")
        return out.strip()

    def get_commit_messages(self, commits: List[str]) -> List[str]:
        """Fetch commit subjects and bodies for a list of commit SHAs."""
        messages = []
        for sha in commits:
            code, out, _ = run_command(["git", "log", "-1", "--pretty=format:%B", sha], cwd=self.repo_path)
            if code == 0 and out.strip():
                messages.append(out.strip())
        return messages

    def get_changed_files(self, base_sha: str, proposed_sha: str) -> List[Dict[str, str]]:
        """Get list of changed files with their status (A, M, D, R)."""
        code, out, _ = run_command(
            ["git", "diff", "--name-status", f"{base_sha}..{proposed_sha}"],
            cwd=self.repo_path
        )
        if code != 0:
            return []

        files = []
        for line in out.strip().splitlines():
            parts = line.strip().split("\t")
            if len(parts) >= 2:
                status = parts[0]
                path = parts[1]
                old_path = parts[1] if len(parts) == 2 else parts[1]
                new_path = parts[2] if len(parts) > 2 else parts[1]
                files.append({
                    "status": status,
                    "path": new_path,
                    "old_path": old_path,
                })
        return files
