"""
Notification Tests.
Covers:
- Scenario 45: Webhook payloads for Slack, Discord and generic endpoints; URL read from secrets.env
- Scenario 46: Desktop notification only when nobody is at a terminal (GUI / AI agent), never in CI
- Scenario 47: A dead webhook never breaks or stalls a push
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush import notifier
from proofpush.config import NotifyConfig
from proofpush.utils import AGENT_ENV_VARS


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in AGENT_ENV_VARS and k not in ("CI", "PROOFPUSH_NONINTERACTIVE")}
    return mock.patch.dict(os.environ, {**env, **extra}, clear=True)


class TestNotify(GitRepoTestCase):
    def test_scenario_45_webhook_payloads(self):
        self.assertEqual(notifier.webhook_payload("https://hooks.slack.com/services/T/B/x", "T", "m", {"a": 1}), {"text": "*T*\nm"})
        self.assertEqual(notifier.webhook_payload("https://discord.com/api/webhooks/1/x", "T", "m", {}), {"content": "**T**\nm"})
        self.assertEqual(notifier.webhook_payload("https://example.com/hook", "T", "m", {"event": "push_blocked"}),
                         {"text": "*T*\nm", "event": "push_blocked"})

        received = []

        class Hook(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        (self.local_dir / ".proofpush").mkdir()
        (self.local_dir / ".proofpush" / "secrets.env").write_text(f"TEAM_HOOK=http://127.0.0.1:{server.server_port}/hook\n")
        with clean_env():
            notifier.notify(NotifyConfig(desktop=False, webhook_env="TEAM_HOOK"), self.local_dir,
                            "ProofPush blocked a push to main", "shop: secret AKIAIOSFODNN7ABCDEF1 found", {"event": "push_blocked"})
        server.shutdown()
        server.server_close()
        self.assertEqual(received[0]["event"], "push_blocked")
        self.assertNotIn("AKIAIOSFODNN7ABCDEF1", received[0]["text"])  # redacted before leaving the machine

    def test_scenario_46_desktop_only_when_unwatched(self):
        cfg = NotifyConfig(desktop=True)
        with mock.patch.object(notifier.subprocess, "Popen") as popen, mock.patch.object(notifier.sys, "platform", "darwin"):
            with clean_env(CLAUDECODE="1"):
                notifier.notify(cfg, self.local_dir, 'Push "blocked"', "reason", {})
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args[0][0][:2], ["osascript", "-e"])
            self.assertIn('with title "Push \\"blocked\\""', popen.call_args[0][0][2])

            with clean_env(CI="1"):
                notifier.notify(cfg, self.local_dir, "t", "m", {})
            with clean_env(CLAUDECODE="1"):
                notifier.notify(NotifyConfig(desktop=False), self.local_dir, "t", "m", {})
            self.assertEqual(popen.call_count, 1)

    def test_scenario_47_dead_webhook_does_not_stall(self):
        start = time.time()
        with clean_env(DEAD_HOOK="http://127.0.0.1:9/nothing-listens"):
            notifier.notify(NotifyConfig(desktop=False, webhook_env="DEAD_HOOK"), self.local_dir, "t", "m", {})
        self.assertLess(time.time() - start, 6)
