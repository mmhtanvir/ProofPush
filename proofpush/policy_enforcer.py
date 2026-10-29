"""
Policy Enforcer.
Evaluates findings against the configured mode (advisory, interactive, strict)
and determines whether to allow (exit 0) or block (exit 1) the Git push.
"""

from __future__ import annotations

import sys
import webbrowser
from dataclasses import dataclass
from typing import Tuple, List, Optional
from proofpush.config import PoliciesConfig
from proofpush.classifier import Finding
from proofpush.unrelated_analyzer import SecurityAuditResult


@dataclass
class EnforcementDecision:
    allowed: bool
    decision_label: str  # ALLOWED or BLOCKED
    reason: str
    exit_code: int


class PolicyEnforcer:
    def __init__(self, mode: str, policies_config: PoliciesConfig):
        self.mode = mode.lower()  # advisory, interactive, strict
        self.policies = policies_config

    def evaluate(
        self,
        findings: List[Finding],
        security_result: Optional[SecurityAuditResult] = None,
        is_tty: Optional[bool] = None,
        report_html_path: Optional[str] = None,
    ) -> EnforcementDecision:
        """Evaluate audit findings against the enforcement policy."""
        has_critical_regression = any(f.is_critical or f.category == "REGRESSION" for f in findings)
        has_warning = any(f.category in ("REGRESSION", "POSSIBLE_REGRESSION", "REMOVED_FUNCTIONALITY", "BLOCKED") for f in findings)
        has_secret = bool(security_result and security_result.secrets_found)
        has_deleted_tests = bool(security_result and security_result.deleted_tests)

        needs_review = has_warning or has_secret or has_deleted_tests

        # 1. Advisory Mode: Always allows push
        if self.mode == "advisory":
            if needs_review:
                return EnforcementDecision(
                    allowed=True,
                    decision_label="ALLOWED (ADVISORY)",
                    reason="Advisory mode: Findings logged for review without blocking push.",
                    exit_code=0,
                )
            return EnforcementDecision(
                allowed=True,
                decision_label="ALLOWED",
                reason="All checks passed.",
                exit_code=0,
            )

        # 2. Strict Mode: Blocks push on any critical regression or secret
        if self.mode == "strict":
            if has_secret:
                return EnforcementDecision(
                    allowed=False,
                    decision_label="BLOCKED",
                    reason="Hardcoded credentials/secrets detected in outgoing commits.",
                    exit_code=1,
                )
            if has_deleted_tests:
                return EnforcementDecision(
                    allowed=False,
                    decision_label="BLOCKED",
                    reason="Existing automated test suites were deleted.",
                    exit_code=1,
                )
            if has_critical_regression:
                return EnforcementDecision(
                    allowed=False,
                    decision_label="BLOCKED",
                    reason="Critical functional or route regressions detected between baseline and proposed version.",
                    exit_code=1,
                )
            return EnforcementDecision(
                allowed=True,
                decision_label="ALLOWED",
                reason="Audit passed in strict mode.",
                exit_code=0,
            )

        # 3. Interactive Mode: Prompts user if issues found, or applies fallback if non-interactive
        if not needs_review:
            return EnforcementDecision(
                allowed=True,
                decision_label="ALLOWED",
                reason="All functional, visual, and security checks passed.",
                exit_code=0,
            )

        tty_available = sys.stdin.isatty() if is_tty is None else is_tty

        if not tty_available:
            # Non-interactive environment (CI, unattended AI CLI, etc.)
            if self.policies.interactive_fallback == "allow":
                return EnforcementDecision(
                    allowed=True,
                    decision_label="ALLOWED (FALLBACK)",
                    reason="Non-interactive session: Allowed under interactive fallback policy.",
                    exit_code=0,
                )
            else:
                return EnforcementDecision(
                    allowed=False,
                    decision_label="BLOCKED",
                    reason="Non-interactive session: Blocked due to detected regressions under default policy.",
                    exit_code=1,
                )

        # Interactive user prompt
        print("\n[proofpush] Attention: Issues detected during pre-push audit.")
        if has_secret:
            print("  - [CRITICAL] Secret or credential detected in commits!")
        if has_critical_regression:
            print("  - [REGRESSION] Functional regressions detected between baseline and proposed.")

        while True:
            choice = input("\nApprove push anyway? [y/N/inspect/open report]: ").strip().lower()
            if choice in ("y", "yes"):
                return EnforcementDecision(
                    allowed=True,
                    decision_label="ALLOWED (USER OVERRIDE)",
                    reason="Approved manually by developer.",
                    exit_code=0,
                )
            elif choice in ("n", "no", ""):
                return EnforcementDecision(
                    allowed=False,
                    decision_label="BLOCKED",
                    reason="Rejected by developer during pre-push audit.",
                    exit_code=1,
                )
            elif choice in ("open", "o", "open report") and report_html_path:
                print(f"Opening report: {report_html_path}")
                webbrowser.open(f"file://{report_html_path}")
            elif choice in ("i", "inspect"):
                for idx, f in enumerate(findings):
                    if f.category == "REGRESSION":
                        print(f"\n--- Finding #{idx + 1} ---")
                        print(f"Target: {f.target}")
                        print(f"Error:  {f.after_behavior}")
                        print(f"Action: {f.recommended_action}")
            else:
                print("Invalid option. Please enter 'y', 'n', 'inspect', or 'open'.")
