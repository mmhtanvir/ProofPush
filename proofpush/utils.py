"""
Utility helpers for ProofPush.
Includes port finding, process execution, secret redaction, and serialization.
"""

import os
import re
import socket
import json
import hashlib
import subprocess
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path


SECRET_PATTERNS = [
    # OpenAI / Claude / standard API keys
    (re.compile(r'(sk-[a-zA-Z0-9_\-]{20,})', re.IGNORECASE), '[REDACTED_API_KEY]'),
    (re.compile(r'(claude-[a-zA-Z0-9_\-]{20,})', re.IGNORECASE), '[REDACTED_API_KEY]'),
    (re.compile(r'(ghp_[a-zA-Z0-9]{36,})', re.IGNORECASE), '[REDACTED_GITHUB_TOKEN]'),
    (re.compile(r'(github_pat_[a-zA-Z0-9_]{50,})', re.IGNORECASE), '[REDACTED_GITHUB_PAT]'),
    (re.compile(r'(AKIA[0-9A-Z]{16})', re.IGNORECASE), '[REDACTED_AWS_KEY_ID]'),
    (re.compile(r'(bearer\s+[a-zA-Z0-9_\-\.]{20,})', re.IGNORECASE), 'Bearer [REDACTED_TOKEN]'),
    (re.compile(r'-----BEGIN [A-Z ]+ PRIVATE KEY-----[^-]+-----END [A-Z ]+ PRIVATE KEY-----', re.DOTALL), '[REDACTED_PRIVATE_KEY]'),
    (re.compile(r'(password|passwd|secret|api_key|access_token|private_key)\s*[:=]\s*["\']([^"\']{6,})["\']', re.IGNORECASE), r'\1: "[REDACTED_CREDENTIAL]"'),
]


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
