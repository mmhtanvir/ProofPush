"""
Responsive Layout and Load Speed Tests.
Covers:
- Scenario 59: On mobile, a release that adds sideways scrolling, pushes a control off-screen, hides the
  menu button, overlaps controls or freezes while loading is reported; an unchanged page is not
- Scenario 63: a release where an image or video no longer loads, data never finishes loading, or the page
  shows "Failed to load" is reported; lazy images further down and signed-URL query changes are not
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

    def test_scenario_63_broken_media_and_data_not_loading(self):
        tiny = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="
        (self.test_dir / "logo.gif").write_bytes(__import__("base64").b64decode(tiny.split(",")[1]))
        lazy = '<div style="height:5000px"></div><img loading=lazy src="nowhere-lazy.png">'
        good = f'<!doctype html><body><img alt=Logo src="logo.gif?sig=1"><p>Patients: 3</p>{lazy}</body>'
        bad = f'''<!doctype html><body><img alt=Logo src="missing-logo.gif?sig=2"><video src="missing.mp4"></video>
<div role=progressbar style="width:40px;height:40px">.</div><p>Failed to load patients</p>{lazy}</body>'''
        good_l, bad_l = self.layout(good), self.layout(bad)
        self.assertEqual(good_l["broken_media"], [])  # lazy image below the fold isn't reached, so not broken
        self.assertFalse(good_l["still_loading"])
        finding = RegressionClassifier(TaskContext()).classify_layout("/patients", good_l, bad_l)
        text = finding.after_behavior
        self.assertIn("no longer shows: image Logo", text)
        self.assertIn("video", text)
        self.assertIn("data never finishes loading", text)
        self.assertIn("Failed to load patients", text)
        self.assertEqual(finding.category, "REGRESSION")
        # same page again with a different signed-URL query: nothing to report
        self.assertIsNone(RegressionClassifier(TaskContext()).classify_layout(
            "/patients", good_l, self.layout(good.replace("sig=1", "sig=9"))))


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
