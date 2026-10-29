"""
Fix History Guard.
Zero-config, language-agnostic detection of previously fixed bugs coming back:
finds past fix commits (by message) touching the files in this push, and flags the push
if it brings back most of the code a fix removed (critical when it also drops the fix's code).
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

from proofpush.classifier import Finding
from proofpush.utils import redact_secrets, run_command

FIX_PATTERNS = ["fix", "bug", "hotfix", "regression", "patch", "crash", "broken"]
# Limit: scans the last 300 fix commits touching the pushed files; raise if old fixes get missed
MAX_FIX_COMMITS = 300
MIN_LINE_LEN = 6
COMMIT_MARK = "\x00PROOFPUSH_COMMIT\x01"  # emitted by git via %x00/%x01; NUL cannot go in argv

# Lockfiles and build output churn by tooling, not by hand
GENERATED = re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|poetry\.lock|Pipfile\.lock|uv\.lock|Cargo\.lock|go\.sum|composer\.lock|Gemfile\.lock)$|\.min\.(js|css)$|\.map$")

Changes = Dict[str, Tuple[Set[str], Set[str]]]  # file -> (added lines, removed lines)


def _meaningful(line: str) -> str:
    line = " ".join(line.split())
    # Skip blank / punctuation-only / tiny lines that match by coincidence (braces, `end`, `});`)
    if len(line) < MIN_LINE_LEN or not re.search(r"[A-Za-z0-9]", line):
        return ""
    return line


def _parse_diff(diff: str) -> Changes:
    changes: Changes = defaultdict(lambda: (set(), set()))
    path = None
    for raw in diff.splitlines():
        if raw.startswith("--- "):
            path = raw[6:] if raw.startswith("--- a/") and not GENERATED.search(raw[6:]) else None
        elif raw.startswith("+++ "):
            if raw.startswith("+++ b/") and not GENERATED.search(raw[6:]):
                path = raw[6:]  # deleted files keep the "--- a/" path
        elif path and raw[:1] in ("+", "-"):
            line = _meaningful(raw[1:])
            if line:
                changes[path][0 if raw[0] == "+" else 1].add(line)
    return changes


class FixHistoryGuard:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def analyze(self, base_sha: str, proposed_sha: str) -> List[Finding]:
        code, push_diff, _ = run_command(["git", "diff", "-U0", "--no-renames", base_sha, proposed_sha], cwd=self.repo_path)
        if code != 0:
            return []
        push = _parse_diff(push_diff)
        if not push:
            return []

        grep_args = [f"--grep={p}" for p in FIX_PATTERNS]
        code, log, _ = run_command(
            ["git", "log", f"-n{MAX_FIX_COMMITS}", "--no-merges", "-i", *grep_args, "-p", "-U0", "--no-renames",
             "--format=%x00PROOFPUSH_COMMIT%x01%H%x09%s", base_sha, "--", *push.keys()],
            cwd=self.repo_path,
            timeout=120,
        )
        if code != 0:
            return []

        findings = []
        for chunk in log.split(COMMIT_MARK)[1:]:
            header, _, diff = chunk.partition("\n")
            sha, _, subject = header.partition("\t")
            fix = _parse_diff(diff)

            reintroduced: List[str] = []  # bad lines the fix removed, re-added by this push
            undone: List[str] = []  # lines the fix added, removed by this push
            fix_added_total = fix_removed_total = 0
            files = []
            for path, (fix_added, fix_removed) in fix.items():
                fix_added_total += len(fix_added)
                fix_removed_total += len(fix_removed)
                if path not in push:
                    continue
                push_added, push_removed = push[path]
                r = fix_removed & push_added
                u = fix_added & push_removed
                if r:
                    files.append(path)
                reintroduced += sorted(r)
                undone += sorted(u)

            # The bug is back only if the code the fix removed comes back; removing fix lines alone is a rewrite
            back = len(reintroduced) / max(fix_removed_total, 1)
            gone = len(undone) / max(fix_added_total, 1)
            if not reintroduced or back < 0.5:
                continue
            reverted = gone >= 0.5  # buggy code back and the fix's code gone = effectively a revert
            if not reverted and len(reintroduced) < 2:
                continue  # one common line coming back is usually coincidence

            detail = []
            if reintroduced:
                detail.append(f"re-adds {len(reintroduced)} line(s) the fix removed")
            if undone and reverted:
                detail.append(f"removes {len(undone)} line(s) the fix added")
            findings.append(Finding(
                category="REGRESSION" if reverted else "POSSIBLE_REGRESSION",
                target=f"Past fix {sha[:10]}: {subject}",
                before_behavior=f"Bug fixed in {sha[:10]} ('{subject}')",
                after_behavior=f"This push {' and '.join(detail)} in {', '.join(files)}",
                expected_behavior="Previously fixed bugs must stay fixed.",
                actual_result="Previously fixed bug likely reintroduced" if reverted else "Code a previous fix removed is back",
                confidence=round((back + gone) / 2, 2),
                recommended_action=f"Check whether this push undoes '{subject}'. Re-apply the fix if so.",
                reproduction_steps=[f"git show {sha[:10]}"] + [redact_secrets(f"+ {l}") for l in reintroduced[:3]] + [redact_secrets(f"- {l}") for l in undone[:3]],
                is_critical=reverted,
            ))
        return findings


VAGUE_MESSAGE = re.compile(r"^(update[sd]?|changes?|changed|edits?|edited|wip|minor|misc|stuff|tmp|temp|tests?|testing|"
                           r"commit|save[sd]?|asdf|done|final|tweaks?|cleanup|clean up|more|small changes?|[.\-_]+)[\s.!]*$", re.I)


def vague_commits(messages: List[str]) -> List[str]:
    """Commit subjects too vague to tell whether they fix a bug (so the fix-history guard can't protect them)."""
    vague = []
    for message in messages:
        subject = message.strip().split("\n")[0].strip()
        if not subject or any(re.search(p, subject, re.I) for p in FIX_PATTERNS):
            continue
        if VAGUE_MESSAGE.match(subject) or len(subject.split()) <= 2:
            vague.append(subject)
    return vague


def commit_message_advice(messages: List[str]) -> List[Finding]:
    vague = vague_commits(messages)
    if not vague:
        return []
    names = ", ".join(f"'{v[:40]}'" for v in vague[:3]) + (f" (+{len(vague) - 3} more)" if len(vague) > 3 else "")
    return [Finding(
        category="NOTICE",
        target="Commit message",
        before_behavior="",
        after_behavior=f"{names} doesn't say what changed",
        expected_behavior="Bug-fix commits say they are fixes",
        actual_result="ProofPush can't tell whether this commit fixes a bug",
        confidence=1.0,
        recommended_action=("If it fixes a bug, start the message with 'fix:' (e.g. 'fix: cart total ignores quantity') "
                            "so ProofPush can warn you if that bug ever comes back."),
    )]

