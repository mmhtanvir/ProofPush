"""
Route Discovery and Config Option Tests.
Covers:
- Scenario 53: Next.js app/pages routers: groups, slots, dynamic and private segments, api/, tests, CSS; GET-only backend routes
- Scenario 54: routes.auto_discover: false checks only the listed routes
- Scenario 55: security.fail_on_secret / fail_on_unrelated actually change the decision
- Scenario 56: Screenshots honour viewports and paint over visual.excluded_selectors
- Scenario 88: React Router routes (JSX and route objects) replace Next.js file routing for src/pages
- Scenario 89: Pages are checked after their data loads (spinners, aria-busy, "Loading..." text), within a cap
"""

import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush.config import PoliciesConfig, SecurityConfig
from proofpush.policy_enforcer import PolicyEnforcer
from proofpush.route_discoverer import RouteDiscoverer
from proofpush.unrelated_analyzer import FileChangeClassification, SecretFinding, SecurityAuditResult


class TestDiscoveryAndOptions(GitRepoTestCase):
    def touch(self, rel, text="export default function P() {}"):
        f = self.local_dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def test_scenario_53_framework_routes(self):
        for rel in ["app/page.tsx", "app/(marketing)/about/page.tsx", "app/patients/[id]/page.tsx", "app/@modal/login/page.tsx",
                    "app/_lib/page.tsx", "app/dashboard/page.module.css", "pages/apiDocs.tsx", "pages/api/users.ts",
                    "pages/settings/index.tsx", "pages/styles.module.css", "pages/index.test.tsx", "pages/blog/[slug].tsx"]:
            self.touch(rel)
        self.touch("server.py", '@app.get("/health")\ndef h(): ...\n@app.post("/submit")\ndef s(): ...\n')
        self.touch(".next/server/chunk.js", "app.get('/junk-from-build', h)")
        routes = RouteDiscoverer(self.local_dir).discover_source_routes()
        self.assertEqual(sorted(routes), ["/", "/about", "/apiDocs", "/health", "/login", "/settings"])

    def test_scenario_54_auto_discover_off(self):
        self.touch("app/about/page.tsx")
        rd = RouteDiscoverer(self.local_dir, base_routes=["/", "/pricing", "/admin/x"], excluded_routes=["/admin/*"], auto_discover=False)
        self.assertEqual(rd.discover_source_routes(), ["/", "/pricing"])

    def test_scenario_55_security_switches(self):
        secret = SecurityAuditResult(has_critical_issue=True, secrets_found=[SecretFinding("a.py", 1, "REDACTED_API_KEY", "k=[REDACTED]")])
        stray = SecurityAuditResult(has_critical_issue=False, unrelated_files=[FileChangeClassification("billing.py", "M", "Unrelated", "x")])
        strict = lambda sec: PolicyEnforcer("strict", PoliciesConfig(), sec)  # noqa: E731
        self.assertFalse(strict(SecurityConfig()).evaluate([], secret).allowed)
        self.assertTrue(strict(SecurityConfig(fail_on_secret=False)).evaluate([], secret).allowed)
        self.assertTrue(strict(SecurityConfig()).evaluate([], stray).allowed)
        self.assertFalse(strict(SecurityConfig(fail_on_unrelated=True)).evaluate([], stray).allowed)

    def test_scenario_56_viewport_and_masks(self):
        from PIL import Image
        from proofpush.browser_engine import BrowserEngine

        page = self.test_dir / "p.html"
        page.write_text('<body style="margin:0;background:#fff"><div class="timestamp" style="width:100px;height:40px;background:#f00"></div></body>')

        async def shot(**kw):
            engine = BrowserEngine()
            try:
                return await engine.visit_page(page.as_uri().rsplit("/", 1)[0], "/p.html", discover_elements=False, **kw)
            finally:
                await engine.close()

        mobile = asyncio.run(shot(viewport={"width": 375, "height": 667}))
        self.assertEqual(Image.open(io.BytesIO(mobile.screenshot_bytes)).width, 375)
        plain = Image.open(io.BytesIO(asyncio.run(shot()).screenshot_bytes)).convert("RGB").getpixel((10, 10))
        masked = Image.open(io.BytesIO(asyncio.run(shot(mask_selectors=[".timestamp"])).screenshot_bytes)).convert("RGB").getpixel((10, 10))
        self.assertEqual(plain, (255, 0, 0))
        self.assertNotEqual(masked, (255, 0, 0))

    def test_scenario_57b_static_server_skips_reverse_dns(self):
        """http.server's socket.getfqdn() delayed listening by 15-30 s on macOS CI; ProofPush's server must not call it."""
        import socket
        import threading
        import urllib.request
        from unittest import mock
        from proofpush.static_server import make_server
        (self.local_dir / "index.html").write_text("<h1>ok</h1>")
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("reverse DNS lookup")):
            server = make_server(0, str(self.local_dir))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/", timeout=5) as r:
            self.assertIn(b"ok", r.read())

    def test_scenario_88_react_router_routes(self):
        """Scenario 88: React Router routes are discovered (JSX in any attribute order, route objects); params,
        splats and relative children are skipped, and src/pages is no longer read as Next.js file routing."""
        self.touch("src/App.tsx", '''import { Routes, Route } from "react-router-dom";
<Routes>
  <Route path="/" element={<Home />} />
  <Route
    element={<Settings />}
    path="/settings/"
  />
  <Route element={<Layout />}>
    <Route path="/todo" element={<Todo />} />
    <Route path="archived" element={<A />} />
  </Route>
  <Route path="/share/:token" element={<S />} />
  <Route path="*" element={<NotFound />} />
</Routes>''')
        self.touch("src/router.ts", 'import { createBrowserRouter } from "react-router";\nexport default createBrowserRouter([{ path: "/billing" }]);')
        self.touch("src/config.ts", 'export const x = { path: "/not-a-route" };')
        self.touch("src/pages/Todo.tsx")
        routes = RouteDiscoverer(self.local_dir).discover_source_routes()
        self.assertEqual(sorted(routes), ["/", "/billing", "/settings", "/todo"])

    def test_scenario_89_pages_are_checked_after_their_data_loads(self):
        """Scenario 89: a page that shows "Loading..." while it fetches is checked once its content is there."""
        from proofpush.browser_engine import BrowserEngine
        page = self.test_dir / "slow.html"
        page.write_text('<body><p id="m">Loading content...</p><script>'
                        'setTimeout(() => { document.getElementById("m").textContent = "3 tasks due today"; }, 1500)'
                        '</script></body>')

        async def visit():
            engine = BrowserEngine()
            try:
                return await engine.visit_page(page.as_uri().rsplit("/", 1)[0], "/slow.html", discover_elements=False, capture_screenshot=False)
            finally:
                await engine.close()

        self.assertIn("3 tasks due today", asyncio.run(visit()).dom_text_snippet)
