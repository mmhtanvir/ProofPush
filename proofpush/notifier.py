"""
Notifier.
Tells people when a push was blocked or a live check failed, wherever they are not looking at a terminal:
an OS desktop notification (GUI and AI-agent pushes, scheduled checks) and an optional team webhook
(Slack, Discord, or any URL that accepts JSON).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import List, Optional

from proofpush.config import NotifyConfig, secret_value
from proofpush.utils import detect_unattended, redact_secrets


def _desktop_command(title: str, message: str) -> Optional[List[str]]:
    """The OS's built-in notification command, or None where there is none."""
    if sys.platform == "darwin":
        def q(text: str) -> str:
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return ["osascript", "-e", f"display notification {q(message)} with title {q(title)}"]
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        return ["notify-send", title, message]
    if sys.platform == "win32":
        def ps(text: str) -> str:
            return "'" + text.replace("'", "''") + "'"
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; $n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Warning; $n.Visible = $true; "
            f"$n.ShowBalloonTip(10000, {ps(title)}, {ps(message)}, 'Warning'); Start-Sleep -Seconds 10; $n.Dispose()"
        )
        return ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script]
    return None


def someone_is_watching() -> bool:
    """A person at a terminal already sees the report; notify only GUI, agent and scheduled runs."""
    return not detect_unattended() and sys.stderr.isatty()


def webhook_payload(url: str, title: str, message: str, details: dict) -> dict:
    text = f"*{title}*\n{message}"
    if "hooks.slack.com" in url:
        return {"text": text}
    if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
        return {"content": text.replace("*", "**")[:2000]}
    return {"text": text, **details}  # generic: Zapier, n8n, Make, your own endpoint


def notify(cfg: NotifyConfig, repo_path: Path, title: str, message: str, details: dict) -> None:
    """Best effort: never raises, never delays a push for more than a few seconds."""
    title, message = redact_secrets(title), redact_secrets(message)

    if cfg.desktop and not someone_is_watching() and detect_unattended() != "CI":
        cmd = _desktop_command(title, message[:240])
        if cmd:
            try:
                # Windows keeps the balloon alive for a while, so don't wait for it
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    url = secret_value(repo_path, cfg.webhook_env)
    if url:
        body = json.dumps(webhook_payload(url, title, message, details)).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "proofpush"})
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as exc:
            print(f"Warning: could not send ProofPush notification to webhook: {redact_secrets(str(exc))[:120]}", file=sys.stderr)
