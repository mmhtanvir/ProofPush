"""
Responsive Layout and Load Speed Tests.
Covers:
- Scenario 59: On mobile, a release that adds sideways scrolling, pushes a control off-screen, hides the
  menu button, overlaps controls or freezes while loading is reported; an unchanged page is not
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush.browser_engine import BrowserEngine
from proofpush.classifier import RegressionClassifier
from proofpush.task_resolver import TaskContext

GOOD = """<!doctype html><meta name=viewport content="width=device-width"><body style="margin:0;font:16px sans-serif">
<nav><button id=menu style="width:44px;height:44px">Menu</button> <a href="/cart" style="display:inline-block;padding:12px">Cart</a></nav>
<p>Welcome</p></body>"""
BAD = """<!doctype html><meta name=viewport content="width=device-width"><body style="margin:0;font:16px sans-serif">
<script>const t = performance.now(); while (performance.now() - t < 700) {}</script>
<nav><a href="/cart" style="position:absolute;left:420px;padding:12px">Cart</a>
<button id=search style="position:absolute;left:0;top:0;width:44px;height:44px">Search</button>
<button id=filter style="position:absolute;left:10px;top:5px;width:44px;height:44px">Filter</button></nav>
<div style="width:600px">Welcome</div></body>"""


class TestResponsive(GitRepoTestCase):
    def layout(self, html):
        page = self.test_dir / f"p{abs(hash(html))}.html"
        page.write_text(html)

        async def run():
            engine = BrowserEngine()
            try:
                v = await engine.visit_page(page.parent.as_uri(), "/" + page.name, viewport={"width": 375, "height": 667},
                                            capture_screenshot=False, discover_elements=False)
                return v.layout
            finally:
                await engine.close()
        return asyncio.run(run())

    def test_scenario_59_mobile_layout_regressions(self):
        good, bad = self.layout(GOOD), self.layout(BAD)
        classifier = RegressionClassifier(TaskContext(title="Header tweak"))
        finding = classifier.classify_layout("/ (mobile)", good, bad)
        text = finding.after_behavior
        self.assertIn("scrolls sideways", text)
        self.assertIn("pushed off the screen: Cart", text)
        self.assertIn("no longer shown: Menu", text)
        self.assertIn("controls now overlap", text)
        self.assertIn("freezes while loading", text)
        self.assertEqual(finding.category, "REGRESSION")
        self.assertTrue(finding.is_critical)
        self.assertIsNone(classifier.classify_layout("/ (mobile)", good, self.layout(GOOD)))
