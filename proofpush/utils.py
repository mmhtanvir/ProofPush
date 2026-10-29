"""
Utility helpers for ProofPush.
Includes port finding, process execution, secret redaction, and serialization.
"""

from __future__ import annotations

import os
import re
import socket
import hashlib
import io
import subprocess
import sys
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path


SECRET_PATTERNS = [
    # OpenAI / Anthropic (sk-ant-...) / standard API keys: standalone token containing a digit,
    # so identifiers like `task-list-item-container` or `risk-assessment-...` don't match
    (re.compile(r'(?<![A-Za-z0-9_\-])(sk-(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,})'), '[REDACTED_API_KEY]'),
    (re.compile(r'(ghp_[a-zA-Z0-9]{36,})'), '[REDACTED_GITHUB_TOKEN]'),
    (re.compile(r'(?<![0-9A-Za-z_\-])(AIza[0-9A-Za-z_\-]{35})(?![0-9A-Za-z_\-])'), '[REDACTED_GOOGLE_API_KEY]'),
    (re.compile(r'(github_pat_[a-zA-Z0-9_]{50,})'), '[REDACTED_GITHUB_PAT]'),
    (re.compile(r'\b(AKIA[0-9A-Z]{16})\b'), '[REDACTED_AWS_KEY_ID]'),
    (re.compile(r'(bearer\s+(?=[a-zA-Z0-9_\-\.]*\d)[a-zA-Z0-9_\-\.]{20,})', re.IGNORECASE), 'Bearer [REDACTED_TOKEN]'),
    (re.compile(r'-----BEGIN [A-Z ]+ PRIVATE KEY-----[^-]+-----END [A-Z ]+ PRIVATE KEY-----', re.DOTALL), '[REDACTED_PRIVATE_KEY]'),
    # Quoted value without spaces, so UI copy like "Password must be 8 characters" isn't a credential
    (re.compile(r'(password|passwd|secret|api_key|access_token|private_key)\s*[:=]\s*["\']([^"\'\s]{6,})["\']', re.IGNORECASE), r'\1: "[REDACTED_CREDENTIAL]"'),
]


# Set by AI coding CLIs in the shells they run. Some (Gemini CLI, Cursor) use a real PTY,
# so isatty() alone can't tell an agent from a person.
AGENT_ENV_VARS = {
    "CLAUDECODE": "Claude Code",
    "GEMINI_CLI": "Gemini CLI",
    "CODEX_SANDBOX": "Codex CLI",
    "CODEX_SANDBOX_NETWORK_DISABLED": "Codex CLI",
    "CURSOR_AGENT": "Cursor",
}


def detect_unattended() -> Optional[str]:
    """Name of the AI agent / automation running this push, or None for a person at a terminal."""
    if os.environ.get("PROOFPUSH_NONINTERACTIVE"):
        return "PROOFPUSH_NONINTERACTIVE"
    for var, name in AGENT_ENV_VARS.items():
        if os.environ.get(var):
            return name
    if os.environ.get("CI"):
        return "CI"
    return None


def open_user_terminal():
    """Read/write handle to the person's terminal, or None when unattended.

    Inside a git hook stdin is the ref list, so prompts must go through /dev/tty.
    """
    if detect_unattended() or not (sys.stderr.isatty() or sys.stdout.isatty()):
        return None
    try:
        # Text "r+" mode needs a seekable file; a terminal isn't, so wrap the raw device
        raw = open("/dev/tty", "r+b", buffering=0)
        return io.TextIOWrapper(raw, encoding="utf-8", line_buffering=True, write_through=True)
    except OSError:
        return None


def redact_secrets(text: str) -> str:
    """Scrub known credentials and high-entropy secrets from text."""
    if not text:
        return text
    scrubbed = text
    for pattern, replacement in SECRET_PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)
    return scrubbed


def sanitize_dict_secrets(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively scrub secrets from dictionary values while preserving keys."""
    sensitive_keys = {"password", "secret", "token", "key", "auth", "credential", "private", "api_key"}
    result = {}
    for k, v in data.items():
        if any(sens in k.lower() for sens in sensitive_keys):
            result[k] = "[REDACTED]"
        elif isinstance(v, dict):
            result[k] = sanitize_dict_secrets(v)
        elif isinstance(v, str):
            result[k] = redact_secrets(v)
        elif isinstance(v, list):
            result[k] = [
                sanitize_dict_secrets(item) if isinstance(item, dict)
                else (redact_secrets(item) if isinstance(item, str) else item)
                for item in v
            ]
        else:
            result[k] = v
    return result


def find_free_port(start: int = 3100, max_tries: int = 100) -> int:
    """Find an available TCP port starting from start."""
    for port in range(start, start + max_tries):
        if is_port_open(port):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"Could not find an available port in range {start}-{start + max_tries}")


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Check if a port is actively accepting connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except (OSError, socket.timeout):
            return False


def get_repo_identifier(repo_path: Path) -> str:
    """Generate a stable, human-readable directory identifier for a repo."""
    try:
        resolved = repo_path.resolve()
        name = resolved.name
        h = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
        return f"{name}_{h}"
    except Exception:
        return "repo_unknown"


def run_command(
    cmd: List[str] | str,
    cwd: Optional[Path | str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 60,
    check: bool = False,
    shell: bool = False,
) -> Tuple[int, str, str]:
    """Execute a shell or subprocess command and return (exit_code, stdout, stderr)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    try:
        res = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            shell=shell,
        )
        if check and res.returncode != 0:
            raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", (exc.stderr or "") + f"\nCommand timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)
