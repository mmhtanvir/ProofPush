"""
Suspicious Push Rules.
Evaluates Pushguard's rule catalog (MIT, (c) 2026 Saad Qayyum, https://github.com/saadiqayyum/pushguard)
locally against one pushed ref: force pushes, install hooks, CI workflow tampering, hidden/obfuscated code,
agent instruction tampering, committed credentials and binaries. push_rules.json holds the catalog unchanged;
this module reimplements its matching semantics (picomatch globs, JS regexes, `when`, `any_of`, `unicode_risk`).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from proofpush.classifier import Finding
from proofpush.utils import redact_secrets, run_command

CATALOG_PATH = Path(__file__).with_name("push_rules.json")

# Facts only the Git host knows (PR review state, pushing account, deletions); rules needing them are skipped locally
REMOTE_ONLY_WHEN = {"unreviewed", "author_mismatch", "sender_first_push", "pr_draft", "pr_opened", "branch_deleted"}

# Same normalization Pushguard applies before matching added lines and commit subjects
INVISIBLE = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")
BIDI_CONTROL = re.compile("[\u202a-\u202e\u2066-\u2069\u061c]")

# Limit: diffs past this many added lines are truncated for content rules; path rules still see every file
MAX_ADDED_LINES = 200_000


def normalize(line: str) -> str:
    return " ".join(BIDI_CONTROL.sub("", INVISIBLE.sub("", line)).split())


# --- picomatch-compatible globs (dot: true) ---------------------------------------------------------------

def _expand_braces(glob: str) -> List[str]:
    m = re.search(r"\{([^{}]*)\}", glob)
    if not m:
        return [glob]
    return [g for alt in m.group(1).split(",") for g in _expand_braces(glob[:m.start()] + alt + glob[m.end():])]


def _glob_to_regex(glob: str) -> str:
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("/**", i) and i + 3 == len(glob):
            out, i = out + "(?:/.*)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        elif glob[i] == "[" and "]" in glob[i:]:
            j = glob.index("]", i)
            cls = glob[i + 1:j]
            out, i = out + "[" + ("^" + cls[1:] if cls.startswith("!") else cls) + "]", j + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return out


@lru_cache(maxsize=None)
def _glob_set(globs: Tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(f"(?:{_glob_to_regex(g)})" for glob in globs for g in _expand_braces(glob)))


def glob_match(globs: List[str], value: str) -> bool:
    return _glob_set(tuple(globs)).fullmatch(value) is not None


# --- JavaScript regexes -------------------------------------------------------------------------------------

def _code_point(high: str, low: str) -> int:
    return 0x10000 + ((int(high, 16) - 0xD800) << 10) + (int(low, 16) - 0xDC00)


# The catalog compiles without JS's `u` flag, so astral ranges are UTF-16 surrogate pairs; Python strings hold code points
_SURROGATE_RANGE = re.compile(r"\[\\u(D[89AB][0-9A-F]{2})-\\u(D[89AB][0-9A-F]{2})\]\[\\u(D[C-F][0-9A-F]{2})-\\u(D[C-F][0-9A-F]{2})\]", re.I)
_SURROGATE_FIXED = re.compile(r"\\u(D[89AB][0-9A-F]{2})\[\\u(D[C-F][0-9A-F]{2})-\\u(D[C-F][0-9A-F]{2})\]", re.I)


@lru_cache(maxsize=None)
def js_regex(source: str) -> re.Pattern:
    py = _SURROGATE_RANGE.sub(lambda m: f"[\\U{_code_point(m[1], m[3]):08X}-\\U{_code_point(m[2], m[4]):08X}]", source)
    py = _SURROGATE_FIXED.sub(lambda m: f"[\\U{_code_point(m[1], m[2]):08X}-\\U{_code_point(m[1], m[3]):08X}]", py)
    if re.search(r"\\u[dD][89a-fA-F]", py):
        raise ValueError(f"Unconverted surrogate escape in rule pattern: {source}")
    return re.compile(py)


def _patterns(value) -> List[re.Pattern]:
    return [js_regex(v) for v in ([value] if isinstance(value, str) else value or [])]


# --- unicode_risk -------------------------------------------------------------------------------------------

_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | {0x200E, 0x200F, 0x061C}
_INVISIBLE = set(range(0x200B, 0x200E)) | set(range(0x2060, 0x2065)) | {0xFEFF} | set(range(0xE0000, 0xE0080))


def _unicode_control(line: str) -> Optional[str]:
    """First bidi/invisible control in the line, skipping legitimate uses (emoji ZWJ/tag sequences, ZWNJ in scripts)."""
    for i, ch in enumerate(line):
        o = ord(ch)
        if o not in _BIDI and o not in _INVISIBLE:
            continue
        prev = ord(line[i - 1]) if i else 0
        if o in (0x200C, 0x200D) and prev > 0x7F:  # emoji ZWJ sequences, Persian/Indic joiners
            continue
        if 0xE0000 <= o <= 0xE007F and (prev >= 0x1F000 or 0xE0000 <= prev <= 0xE007F):  # subdivision flags
            continue
        if o == 0xFEFF and i == 0:  # byte order mark
            continue
        return f"U+{o:04X} {unicodedata.name(ch, 'FORMAT CONTROL')} at column {i + 1}"
    return None


_IDENTIFIER = re.compile(r"[^\W\d_]\w*")


def _confusable(line: str) -> Optional[str]:
    """An identifier mixing Latin with Cyrillic/Greek/Armenian letters that look alike."""
    for m in _IDENTIFIER.finditer(line):
        scripts = {unicodedata.name(c, "").split(" ")[0] for c in m.group() if c.isalpha()}
        if "LATIN" in scripts and scripts & {"CYRILLIC", "GREEK", "ARMENIAN"}:
            return f"mixed-script identifier '{m.group()}' at column {m.start() + 1}"
    return None


# --- evaluation ---------------------------------------------------------------------------------------------

@dataclass
class PushContext:
    files: Dict[str, str]  # path -> added | modified | removed
    added_lines: Dict[str, List[str]]
    commit_messages: List[str]
    branch: str
    repo: str
    forced: bool
    branch_created: bool
    hour_utc: int


def _hour_in_range(hour: int, span: List[int]) -> bool:
    start, end = span
    return start <= hour <= end if start <= end else hour >= start or hour <= end


def _variants(rule: dict) -> List[dict]:
    """One flat rule per condition group (top-level conditions plus each `any_of` group), sharing scope and severity."""
    keys = ("paths", "all_of", "exclude_paths", "change_type", "when", "added_lines", "commit_message", "unicode_risk")
    shared = {k: v for k, v in rule.items() if k not in keys and k != "any_of"}
    top = {k: rule[k] for k in keys if k in rule}
    groups = ([top] if any(k in top for k in keys if k not in ("exclude_paths", "change_type")) else []) + rule.get("any_of", [])
    return [{**shared, **group} for group in groups]


def _match(rule: dict, ctx: PushContext) -> Optional[Tuple[List[str], List[str], List[str]]]:
    """(files, commit subjects, evidence lines) when the variant fires, else None."""
    if "push" not in rule.get("on", ["push"]):
        return None
    if rule.get("branches") and not glob_match(rule["branches"], ctx.branch):
        return None
    if rule.get("repos") and not glob_match(rule["repos"], ctx.repo):
        return None

    when = rule.get("when") or {}
    if set(when) & REMOTE_ONLY_WHEN:
        return None
    if "forced" in when and when["forced"] != ctx.forced:
        return None
    if "branch_created" in when and when["branch_created"] != ctx.branch_created:
        return None
    if "hour_utc" in when:
        span = when["hour_utc"].get("between") or when["hour_utc"].get("not_between")
        if _hour_in_range(ctx.hour_utc, span) != ("between" in when["hour_utc"]):
            return None

    messages: List[str] = []
    if rule.get("commit_message"):
        pats = _patterns(rule["commit_message"])
        subjects = [m.split("\n")[0] for m in ctx.commit_messages]
        messages = [s for s in subjects if any(p.search(s) or p.search(normalize(s)) for p in pats)]
        if not messages:
            return None

    files: Optional[List[str]] = None
    if rule.get("paths") or rule.get("all_of"):
        types = set(rule.get("change_type", ["added", "modified", "removed"]))
        exclude = rule.get("exclude_paths")
        eligible = [f for f, t in ctx.files.items() if t in types and not (exclude and glob_match(exclude, f))]
        files = []
        if rule.get("paths"):
            files = [f for f in eligible if glob_match(rule["paths"], f)]
            if not files:
                return None
        for group in rule.get("all_of", []):
            hit = [f for f in eligible if glob_match(group, f)]
            if not hit:
                return None
            files += [f for f in hit if f not in files]

    evidence: List[str] = []
    if rule.get("added_lines") or rule.get("unicode_risk"):
        pats = _patterns(rule.get("added_lines"))
        check = {"controls": _unicode_control, "confusables": _confusable}.get(rule.get("unicode_risk"))
        hit_files = []
        for path in (files if files is not None else ctx.added_lines):
            hits = [l for l in ctx.added_lines.get(path, []) if not pats or any(p.search(l) or p.search(normalize(l)) for p in pats)]
            if check:
                hits = [f"{d}: {l.strip()}" for l in hits for d in [check(l)] if d]
            if hits:
                hit_files.append(path)
                evidence += hits
        if not evidence:
            return None
        files = hit_files

    return files or [], messages, evidence


def load_catalog() -> List[dict]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["rules"]


def evaluate(rules: List[dict], ctx: PushContext, enable: List[str] = (), disable: List[str] = ()) -> List[Finding]:
    findings = []
    for rule in rules:
        if rule["id"] in disable or (not rule.get("enabled", True) and rule["id"] not in enable):
            continue
        for variant in _variants(rule):
            hit = _match(variant, ctx)
            if hit is None:
                continue
            files, messages, evidence = hit
            sev = rule["severity"]
            findings.append(Finding(
                # critical blocks (strict) / needs approval; high needs approval; medium/low are notices
                category="SUSPICIOUS_PUSH" if sev in ("critical", "high") else "NOTICE",
                target=f"{rule['id']} ({sev}, {rule['pack']} pack)",
                before_behavior=rule.get("description", ""),
                after_behavior=("Files: " + ", ".join(files[:5]) + (f" (+{len(files) - 5} more)" if len(files) > 5 else ""))
                if files else ("Commits: " + "; ".join(messages[:3]) if messages else "Push metadata matched"),
                expected_behavior="No suspicious change in this push",
                actual_result=rule.get("description", rule["id"]),
                confidence=0.9,
                recommended_action=(f"Review this change before pushing. If it is expected here, add '{rule['id']}' "
                                    "to push_rules.disable in .proofpush.yml."),
                reproduction_steps=[redact_secrets(e.strip())[:160] for e in evidence[:3]],
                is_critical=sev == "critical",
            ))
            break  # one finding per rule
    return findings


def _repo_name(repo_path: Path) -> str:
    code, url, _ = run_command(["git", "remote", "get-url", "origin"], cwd=repo_path)
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url.strip()) if code == 0 else None
    return m.group(1) if m else repo_path.name


def build_context(repo_path: Path, base_sha: str, proposed_sha: str, remote_ref: str,
                  commit_messages: List[str], forced: bool, branch_created: bool) -> PushContext:
    files: Dict[str, str] = {}
    code, out, _ = run_command(["git", "diff", "--name-status", "--no-renames", base_sha, proposed_sha], cwd=repo_path)
    for line in out.splitlines() if code == 0 else []:
        status, _, path = line.partition("\t")
        files[path] = {"A": "added", "D": "removed"}.get(status[:1], "modified")

    added: Dict[str, List[str]] = {}
    code, diff, _ = run_command(["git", "diff", "-U0", "--no-renames", "--no-color", base_sha, proposed_sha],
                                cwd=repo_path, timeout=120)
    path, total = None, 0
    for raw in diff.splitlines() if code == 0 else []:
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("+") and path and total < MAX_ADDED_LINES:
            added.setdefault(path, []).append(raw[1:])
            total += 1

    branch = remote_ref[len("refs/heads/"):] if remote_ref.startswith("refs/heads/") else remote_ref.split("/")[-1]
    return PushContext(
        files=files, added_lines=added, commit_messages=commit_messages, branch=branch,
        repo=_repo_name(repo_path), forced=forced, branch_created=branch_created,
        hour_utc=datetime.now(timezone.utc).hour,
    )
