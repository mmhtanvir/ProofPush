"""
Backend-Through-Frontend Tests.
Covers:
- Scenario 60: The API calls a page makes are compared between versions: a call that now errors, loses fields,
  comes back empty, or (for a button) fails when clicked is reported as a backend problem; unchanged calls are quiet
"""

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush.browser_engine import BrowserEngine, api_key
from proofpush.static_server import LoopbackServer
from proofpush.classifier import RegressionClassifier
from proofpush.task_resolver import TaskContext

PAGE = b"""<!doctype html><body><ul id=p></ul><span id=o></span><button id=star aria-label=Star>Star</button>
<script>
fetch('/api/patients/17').then(r => r.json()).then(d => document.getElementById('p').textContent = JSON.stringify(d)).catch(() => {});
fetch('/api/orders?page=1').then(r => r.json()).then(d => document.getElementById('o').textContent = d.items.length);
fetch('/api/profile').then(r => r.json());
fetch('/api/settings').then(r => r.json());
document.getElementById('star').onclick = () => fetch('/api/star', {method: 'POST'}).then(r => { document.getElementById('star').textContent = r.ok ? 'Starred' : 'Error'; });
</script></body>"""

GOOD = {"/api/patients/17": (200, {"id": 17, "name": "Ann"}), "/api/orders": (200, {"items": [1, 2]}),
        "/api/profile": (200, {"name": "Dr X", "email": "x@clinic.test"}), "/api/settings": (200, {"theme": "dark"}),
        "/api/star": (200, {"ok": True})}
BAD = {**GOOD, "/api/patients/17": (500, {"error": "boom"}), "/api/orders": (200, {"items": []}),
       "/api/profile": (200, {"name": "Dr X"}), "/api/star": (500, {"error": "db"})}


def serve(routes):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self):
            path = self.path.split("?")[0]
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(PAGE)
                return
            status, body = routes.get(path, (404, {}))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        do_GET = do_POST = reply

    server = LoopbackServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TestBackendCalls(GitRepoTestCase):
    def test_scenario_60_backend_breaks_seen_through_the_page(self):
        self.assertEqual(api_key("GET", "http://x/api/patients/17?page=2&q=a"), "GET /api/patients/:id?page=&q=")
        old, new = serve(GOOD), serve(BAD)
        for s in (old, new):
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)
        urls = [f"http://127.0.0.1:{s.server_port}" for s in (old, new)]

        async def run():
            engine = BrowserEngine()
            try:
                visits = [await engine.visit_page(u, "/", capture_screenshot=False, discover_elements=False) for u in urls]
                clicks = [await engine.probe_interactions(u, "/") for u in urls]
                return visits, clicks
            finally:
                await engine.close()

        (before, after), (clicks_before, clicks_after) = asyncio.run(run())
        classifier = RegressionClassifier(TaskContext(title="Refactor API"))
        finding = classifier.classify_api("/", before.api_calls, after.api_calls)
        text = finding.after_behavior
        self.assertIn("GET /api/patients/:id now returns 500 (was 200)", text)
        self.assertIn("GET /api/profile no longer returns fields: email", text)
        self.assertIn("now returns an empty 'items' (was 2 items)", text)
        self.assertNotIn("/api/settings", text)
        self.assertEqual(finding.category, "REGRESSION")
        self.assertTrue(finding.target.startswith("Backend"))

        star = classifier.classify_interaction("/", clicks_before["id:star"], clicks_after["id:star"])
        self.assertIn("fails on the backend: POST /api/star -> 500", star.after_behavior)
        self.assertTrue(star.is_critical)

    def test_scenario_61_unreadable_body_is_not_lost_fields(self):
        """Scenario 61: a response whose body was gone before it could be read (the page moved on) says
        nothing about its fields; only its speed is compared."""
        classifier = RegressionClassifier(TaskContext(title="Refactor page"))
        before = {"GET /api/settings": {"status": 200, "ms": 100, "keys": ["language", "date_format"]}}
        after = {"GET /api/settings": {"status": 200, "ms": 110, "no_body": True}}
        self.assertIsNone(classifier.classify_api("/", before, after))
        after["GET /api/settings"]["ms"] = 900
        self.assertIn("got slower", classifier.classify_api("/", before, after).after_behavior)
        # ...but a live (non-local) API's speed changes on its own, not with the push
        after["GET /api/settings"]["remote"] = True
        self.assertIsNone(classifier.classify_api("/", before, after))
