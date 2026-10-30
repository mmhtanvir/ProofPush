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
<script>const t = performance.now(); while (performance.now() - t < 1300) {}</script>
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
        # Long-task timing isn't reported by every headless browser build; a 1.3 s block also shows as slower loading
        self.assertTrue("freezes while loading" in text or "loads slower" in text or "shows its main content slower" in text, text)
        self.assertEqual(finding.category, "REGRESSION")
        self.assertTrue(finding.is_critical)
        self.assertIsNone(classifier.classify_layout("/ (mobile)", good, self.layout(GOOD)))


class TestStillLoading(GitRepoTestCase):
    def test_scenario_62_tailwind_skeletons_count_as_loading(self):
        """Scenario 62: empty .animate-pulse placeholder cards mean data is still coming (screenshots wait);
        a small pulsing status dot or a pulsing label does not."""
        from playwright.async_api import async_playwright
        from proofpush.browser_engine import _STILL_LOADING_JS

        async def loading(html):
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()
                await page.set_content(html)
                result = await page.evaluate(_STILL_LOADING_JS)
                await browser.close()
                return result

        card = '<div class="animate-pulse" style="height:180px;width:300px;background:#e2e8f0"></div>'
        dot = '<span class="animate-pulse" style="display:inline-block;height:8px;width:8px"></span> Live'
        label = '<p class="animate-pulse" style="height:20px">Updated just now</p>'
        self.assertTrue(asyncio.run(loading(card)))
        self.assertFalse(asyncio.run(loading(dot)))
        self.assertFalse(asyncio.run(loading(label)))
