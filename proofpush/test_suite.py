"""
Project Test Suite.
Runs the repository's own tests on the baseline and on the proposed version, and reports tests that passed
before and fail now. Covers whatever the project's tests cover (backend logic, calculations, jobs, emails),
not just what a browser can see.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set

from proofpush.classifier import Finding

# Lines that name a failing test, per runner (pytest -rfE, Django/unittest, Jest/Vitest, Go, Cargo)
FAIL_PATTERNS = [
    re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)"),
    re.compile(r"^(?:FAIL|ERROR): (\S+ \([\w.]+\))"),
    re.compile(r"^\s*[✕×] (.+?)(?: \(\d+ ?m?s\))?$"),
    re.compile(r"^\s*FAIL\s+(\S+ > .+)$"),
    re.compile(r"^\s*--- FAIL: (\S+)"),
    re.compile(r"^test (\S+) \.\.\. FAILED$"),
]


@dataclass
class SuiteRun:
    command: str
    exit_code: int
    failing: Set[str] = field(default_factory=set)
    output_tail: str = ""
    timed_out: bool = False


def _python(root: Path) -> str:
    """The project's own virtualenv if there is one (its dependencies), else this interpreter (`python` may not exist)."""
    for venv in (".venv", "venv", "env"):
        for exe in ("bin/python", "Scripts/python.exe"):
            if (root / venv / exe).exists():
                return f'"{root / venv / exe}"'
    return f'"{sys.executable}"'


def detect_test_command(root: Path) -> Optional[str]:
    """The obvious way to run this project's tests, or None."""
    package = root / "package.json"
    if package.is_file():
        try:
            script = json.loads(package.read_text(encoding="utf-8")).get("scripts", {}).get("test", "")
        except ValueError:
            script = ""
        if script and "no test specified" not in script:
            return "npm test --silent"
    if (root / "go.mod").is_file():
        return "go test ./..."
    if (root / "Cargo.toml").is_file():
        return "cargo test --quiet"
    if (root / "manage.py").is_file():
        return "{python} manage.py test"
    pyproject = root / "pyproject.toml"
    if ((root / "pytest.ini").is_file() or (root / "conftest.py").is_file()
            or (pyproject.is_file() and "[tool.pytest" in pyproject.read_text(encoding="utf-8", errors="ignore"))
            or any(root.glob("tests/test_*.py")) or any(root.glob("test/test_*.py"))):
        return "{python} -m pytest -q -rfE -p no:cacheprovider"
    return None


def run_suite(root: Path, command: str, timeout: int) -> SuiteRun:
    cmd = command.replace("{python}", _python(root))
    env = {**os.environ, "CI": "1", "PROOFPUSH_RUNNING": "1"}
    try:
        proc = subprocess.run(cmd, cwd=root, shell=True, capture_output=True, text=True, timeout=timeout, env=env,
                              encoding="utf-8", errors="replace")
        output, code, timed_out = (proc.stdout or "") + (proc.stderr or ""), proc.returncode, False
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        code, timed_out = 124, True
    failing = {m.group(1).strip() for line in output.splitlines() for p in FAIL_PATTERNS for m in [p.match(line)] if m}
    return SuiteRun(cmd, code, failing, "\n".join(output.strip().splitlines()[-15:]), timed_out)


def compare_suites(before: SuiteRun, after: SuiteRun) -> List[Finding]:
    """Tests that passed before and fail now. A suite that was already failing doesn't hide new failures."""
    if after.timed_out and not before.timed_out:
        return [_finding("Test suite now times out", "The suite finished on the baseline but hangs now", after, critical=True)]
    newly = sorted(after.failing - before.failing)
    if newly:
        return [_finding(f"{len(newly)} test(s) passed before and fail now: " + ", ".join(newly[:5])
                         + (f" (+{len(newly) - 5} more)" if len(newly) > 5 else ""),
                         "Previously passing tests fail", after, critical=True, names=newly)]
    if before.exit_code == 0 and after.exit_code != 0:
        # Runner whose failures we couldn't name: the suite as a whole went from green to red
        return [_finding("The test suite passed before and fails now", "Suite exit code 0 -> "
                         f"{after.exit_code}", after, critical=True)]
    if before.failing and after.failing and not newly:
        return [Finding(category="NOTICE", target="Test suite", before_behavior=f"{len(before.failing)} failing",
                        after_behavior=f"{len(after.failing)} failing, none new", expected_behavior="All tests pass",
                        actual_result="Already-failing tests, not caused by this change", confidence=0.9,
                        recommended_action="Fix the existing failures separately.")]
    return []


def _finding(summary: str, actual: str, run: SuiteRun, critical: bool, names: Optional[List[str]] = None) -> Finding:
    return Finding(
        category="REGRESSION",
        target=f"Test suite ({run.command})",
        before_behavior="Passing on the baseline",
        after_behavior=summary,
        expected_behavior="Tests that passed keep passing",
        actual_result=actual,
        confidence=0.95,
        recommended_action="Run the failing tests locally; the change broke behaviour they cover (often backend logic).",
        is_critical=critical,
        reproduction_steps=[run.command] + (names or [])[:5] + [run.output_tail[-600:]],
    )
