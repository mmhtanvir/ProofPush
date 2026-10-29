"""
System Health and Diagnostic Doctor.
Checks environment, dependencies, Git hooks, Playwright browsers, and configuration.
"""

from __future__ import annotations

import sys
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
from proofpush.utils import run_command
from proofpush.git_interceptor import GitInterceptor
from datetime import datetime

from proofpush.config import find_config_file, load_config, resolve_account, secret_value, session_file
from proofpush.baseline_manager import BaselineManager


class Doctor:
    def __init__(self, repo_path: Optional[Path] = None):
        self.repo_path = (repo_path or Path.cwd()).resolve()

    def run_all_checks(self) -> List[Dict[str, Any]]:
        """Run all diagnostic checks and return structured results."""
        checks = []

        # 1. Python environment
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        checks.append({
            "name": "Python Environment",
            "passed": sys.version_info >= (3, 10),
            "detail": f"Python {py_ver} ({sys.executable})",
        })

        # 2. Git installation
        git_path = shutil.which("git")
        git_detail = "Git not found in PATH"
        git_passed = bool(git_path)
        if git_passed:
            code, out, _ = run_command(["git", "--version"])
            git_detail = out.strip() if code == 0 else "Git found but returned error"
        checks.append({
            "name": "Git CLI",
            "passed": git_passed,
            "detail": git_detail,
        })

        # 3. Repository check
        git_interceptor = GitInterceptor(self.repo_path)
        git_dir, _ = git_interceptor.get_git_dirs()
        in_repo = bool(git_dir)
        checks.append({
            "name": "Git Repository",
            "passed": in_repo,
            "detail": f"Repository root: {self.repo_path}" if in_repo else "Current directory is not a Git repository",
        })

        # 4. Pre-push hook check
        if in_repo:
            hook_status = git_interceptor.inspect_hook()
            hook_installed = hook_status.get("installed", False)
            checks.append({
                "name": "Pre-Push Hook",
                "passed": hook_installed,
                "detail": (
                    f"Installed at {hook_status.get('path')} (legacy preserved: {hook_status.get('has_legacy')})"
                    if hook_installed else "Not installed. Run `proofpush install` to enable."
                ),
            })

        # 5. Playwright & Chromium browser check
        browser_passed = False
        browser_detail = ""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                b.close()
            browser_passed = True
            browser_detail = "Playwright Chromium headless browser verified and ready"
        except ImportError:
            browser_detail = "Not installed: git checks work; for page checks run pip install 'proofpush[browser]'"
        except Exception as exc:
            browser_detail = f"Chromium launch check failed: {exc}. Run `playwright install chromium`"

        checks.append({
            "name": "Browser Automation Engine",
            "passed": browser_passed,
            "detail": browser_detail,
        })

        # 6. Configuration file check
        cfg = load_config(self.repo_path)
        cfg_file = find_config_file(self.repo_path)
        has_cfg = cfg_file is not None
        checks.append({
            "name": "Configuration File",
            "passed": True,  # Optional since defaults work
            "detail": f"Using {cfg_file.name} (mode: {cfg.mode}, baseline: {cfg.baseline.strategy})" if has_cfg else "Using default configuration",
        })

        # 7. Test accounts: can each one be resolved? (values are never printed)
        if cfg.auth:
            for env_name in cfg.auth.accounts:
                creds, problem = resolve_account(self.repo_path, cfg.auth, env_name)
                saved = session_file(self.repo_path, env_name)
                detail = "credentials found" + (" + authenticator secret" if creds and creds.otp_secret else "") if creds else problem
                if saved.is_file():
                    detail += f"; saved session from {datetime.fromtimestamp(saved.stat().st_mtime):%Y-%m-%d}"
                checks.append({"name": f"Test Account ({env_name})", "passed": bool(creds) or saved.is_file(), "detail": detail})

        # 8. Live environments and alerts
        for env_name, url in cfg.environments.items():
            checks.append({"name": f"Environment ({env_name})", "passed": url.startswith(("http://", "https://")),
                           "detail": f"{url}  (`proofpush check --env {env_name}`)"})
        if cfg.notify.webhook_env:
            has_hook = bool(secret_value(self.repo_path, cfg.notify.webhook_env))
            checks.append({"name": "Team Webhook", "passed": has_hook,
                           "detail": "configured" if has_hook else f"set {cfg.notify.webhook_env} in your environment or .proofpush/secrets.env"})

        # 9. secrets.env must not be readable by other users
        secrets_env = self.repo_path / ".proofpush" / "secrets.env"
        if secrets_env.is_file() and sys.platform != "win32":  # Windows has no Unix permission bits
            loose = secrets_env.stat().st_mode & 0o077
            checks.append({"name": "Secrets File", "passed": not loose,
                           "detail": "readable only by you" if not loose else f"readable by others: run `chmod 600 {secrets_env}`"})

        # 10. Project test suite (run on both versions when enabled)
        if in_repo:
            from proofpush.test_suite import detect_test_command
            detected = cfg.tests.command or detect_test_command(self.repo_path)
            if detected:
                checks.append({"name": "Project Tests", "passed": True,
                               "detail": f"{'enabled' if cfg.tests.enabled else 'found, off'}: {detected}"
                                         + ("" if cfg.tests.enabled else "  (turn on: tests: {enabled: true})")})

        # 11. Backend API description (needed to test write endpoints)
        if cfg.api_tests.mode != "off" and "openapi" in cfg.api_tests.sources:
            from proofpush.api_testing import load_openapi
            spec, where = load_openapi(self.repo_path, cfg.api_tests.openapi if not cfg.api_tests.openapi.startswith(("/", "http")) else "")
            served = cfg.api_tests.openapi.startswith(("/", "http"))
            checks.append({"name": "Backend API Description", "passed": bool(spec) or served,
                           "detail": f"OpenAPI: {where}" if spec else (f"read from the running app at {cfg.api_tests.openapi}" if served else
                                     "No OpenAPI file in the repo. If the app serves one while running (FastAPI: /openapi.json) "
                                     "audits read it; if not, backend write endpoints (POST/PUT/PATCH) won't be tested unless "
                                     "you add api_tests.cases (DRF: add drf-spectacular).")})

        # 12. Baseline store check
        if in_repo:
            bm = BaselineManager(self.repo_path)
            baselines = bm.list_baselines()
            checks.append({
                "name": "Baseline Availability",
                "passed": True,
                "detail": f"{len(baselines)} baseline snapshot(s) stored locally",
            })

        return checks
