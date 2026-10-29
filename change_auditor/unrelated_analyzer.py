"""
Unrelated Changes and Security Analyzer.
Inspects outgoing Git diffs, classifies modified files according to the task context,
detects hardcoded secrets, deleted test cases, altered auth logic, and unexpected changes.
"""

import re
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from change_auditor.task_resolver import TaskContext
from change_auditor.utils import run_command, SECRET_PATTERNS


@dataclass
class SecretFinding:
    file_path: str
    line_number: int
    pattern_type: str
    snippet: str


@dataclass
class FileChangeClassification:
    file_path: str
    status: str  # A, M, D, R
    classification: str  # Relevant, Supporting, Possibly related, Unrelated, Suspicious, Sensitive
    reason: str
    lines_added: int = 0
    lines_deleted: int = 0


@dataclass
class SecurityAuditResult:
    has_critical_issue: bool
    secrets_found: List[SecretFinding] = field(default_factory=list)
    deleted_tests: List[str] = field(default_factory=list)
    auth_changes: List[str] = field(default_factory=list)
    unrelated_files: List[FileChangeClassification] = field(default_factory=list)
    classified_files: List[FileChangeClassification] = field(default_factory=list)
    summary: str = ""


class UnrelatedAnalyzer:
    def __init__(self, repo_path: Path, sensitive_globs: Optional[List[str]] = None):
        self.repo_path = repo_path.resolve()
        self.sensitive_globs = sensitive_globs or [
            ".env*", "**/*.pem", "**/*.key", "**/secrets.*", "credentials.json", "id_rsa*"
        ]

    def analyze_changes(
        self,
        base_sha: str,
        proposed_sha: str,
        task_context: TaskContext,
    ) -> SecurityAuditResult:
        """Analyze the git diff between base and proposed commits for task alignment and security issues."""
        code, numstat_out, _ = run_command(
            ["git", "diff", "--numstat", f"{base_sha}..{proposed_sha}"],
            cwd=self.repo_path
        )
        stats_map = {}
        if code == 0:
            for line in numstat_out.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    added = int(parts[0]) if parts[0] != "-" else 0
                    deleted = int(parts[1]) if parts[1] != "-" else 0
                    path = parts[2]
                    stats_map[path] = (added, deleted)

        code, name_status_out, _ = run_command(
            ["git", "diff", "--name-status", f"{base_sha}..{proposed_sha}"],
            cwd=self.repo_path
        )
        if code != 0:
            return SecurityAuditResult(has_critical_issue=False, summary="No diff available")

        # Get full diff text for deep inspection
        code, diff_text, _ = run_command(
            ["git", "diff", f"{base_sha}..{proposed_sha}"],
            cwd=self.repo_path
        )

        secrets = self._scan_diff_for_secrets(diff_text)
        classified_files = []
        deleted_tests = []
        auth_changes = []
        unrelated_files = []

        # Parse task keywords
        task_keywords = set()
        for word in re.findall(r'\b[a-zA-Z]{3,}\b', task_context.title.lower()):
            task_keywords.add(word)
        for area in task_context.expected_areas:
            for word in re.findall(r'\b[a-zA-Z]{3,}\b', area.lower()):
                task_keywords.add(word)

        unrelated_keywords = set()
        for area in task_context.unrelated_areas:
            for word in re.findall(r'\b[a-zA-Z]{3,}\b', area.lower()):
                unrelated_keywords.add(word)

        for line in name_status_out.strip().splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 2:
                continue

            status = parts[0]
            file_path = parts[-1]
            added, deleted = stats_map.get(file_path, (0, 0))

            classification, reason = self._classify_file(
                file_path=file_path,
                status=status,
                task_keywords=task_keywords,
                unrelated_keywords=unrelated_keywords,
                task_context=task_context,
            )

            fc = FileChangeClassification(
                file_path=file_path,
                status=status,
                classification=classification,
                reason=reason,
                lines_added=added,
                lines_deleted=deleted,
            )
            classified_files.append(fc)

            if classification == "Sensitive":
                pass
            elif classification == "Suspicious":
                if "test" in file_path.lower() and status == "D":
                    deleted_tests.append(file_path)
            elif classification == "Unrelated":
                unrelated_files.append(fc)

            # Check for auth changes specifically
            if any(term in file_path.lower() for term in ["auth", "session", "security", "token", "password", "oauth"]):
                if not any(k in file_path.lower() for k in task_keywords):
                    auth_changes.append(file_path)

        has_critical = len(secrets) > 0 or len(deleted_tests) > 0

        summary = (
            f"Analyzed {len(classified_files)} changed files. "
            f"Found {len(secrets)} secrets, {len(deleted_tests)} deleted test suites, "
            f"{len(auth_changes)} unexpected auth changes, and {len(unrelated_files)} unrelated changes."
        )

        return SecurityAuditResult(
            has_critical_issue=has_critical,
            secrets_found=secrets,
            deleted_tests=deleted_tests,
            auth_changes=auth_changes,
            unrelated_files=unrelated_files,
            classified_files=classified_files,
            summary=summary,
        )

    def _classify_file(
        self,
        file_path: str,
        status: str,
        task_keywords: Set[str],
        unrelated_keywords: Set[str],
        task_context: TaskContext,
    ) -> Tuple[str, str]:
        """Classify a changed file into one of the 6 categories."""
        p_lower = file_path.lower()

        # 1. Check for sensitive files (e.g. .env, credentials, pem keys)
        for glob_pat in self.sensitive_globs:
            if fnmatch.fnmatch(file_path, glob_pat) or fnmatch.fnmatch(Path(file_path).name, glob_pat):
                return "Sensitive", f"Matches sensitive path pattern: {glob_pat}"

        # 2. Check for deleted tests (Suspicious)
        if status == "D" and ("test" in p_lower or "spec" in p_lower):
            return "Suspicious", "Test file was deleted"

        # 3. Check for explicitly listed unrelated areas
        if any(uk in p_lower for uk in unrelated_keywords):
            return "Unrelated", "File belongs to an explicitly declared unrelated area in task context"

        # 4. Check for direct match with task keywords
        if any(tk in p_lower for tk in task_keywords):
            return "Relevant", "File name or directory matches task terms"

        # 5. Supporting files (tests, package dependencies, configs)
        if any(s in p_lower for s in ["test", "spec", "package.json", "requirements", "docker", "tsconfig", "vite.config"]):
            return "Supporting", "Supporting test, build, or configuration artifact"

        # 6. Shared components / utils (Possibly related)
        if any(s in p_lower for s in ["utils", "helpers", "common", "components/ui", "lib"]):
            return "Possibly related", "Shared utility or UI component"

        # 7. Unrelated
        return "Unrelated", "File is not mentioned in task and does not appear to directly support the stated objective"

    def _scan_diff_for_secrets(self, diff_text: str) -> List[SecretFinding]:
        """Scan added lines in Git diff for hardcoded credentials or tokens."""
        findings = []
        current_file = "unknown"
        line_num = 0
        seen_line_secrets = set()

        for line in diff_text.splitlines():
            if line.startswith("+++ b/"):
                current_file = line[6:].strip()
                line_num = 0
                continue
            elif line.startswith("@@ "):
                m = re.search(r'\+(\d+)', line)
                if m:
                    line_num = int(m.group(1)) - 1
                continue

            if line.startswith("+") and not line.startswith("+++"):
                line_num += 1
                added_content = line[1:]
                key = (current_file, line_num)
                if key in seen_line_secrets:
                    continue

                for pattern, placeholder in SECRET_PATTERNS:
                    match = pattern.search(added_content)
                    if match:
                        matched_str = match.group(0)
                        # Filter out test mock tokens or placeholders
                        if any(mock in matched_str.lower() for mock in ["example", "test", "fake", "placeholder", "xxx"]):
                            continue
                        findings.append(SecretFinding(
                            file_path=current_file,
                            line_number=line_num,
                            pattern_type=placeholder.strip("[]"),
                            snippet=added_content[:100].strip(),
                        ))
                        seen_line_secrets.add(key)
                        break
            elif not line.startswith("-"):
                line_num += 1

        return findings
