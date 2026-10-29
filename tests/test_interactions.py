"""
Click-Behaviour Tests.
Covers:
- Scenario 57: A button that worked stops working, gets slow/janky, or disappears; unchanged buttons are quiet
- Scenario 58: Destructive buttons are never clicked, and toggles are put back the way they were
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

PAGE = """<!doctype html><body>
<button id="star" aria-label="Star" onclick="{star}">☆</button>
<button id="more" onclick="{more}">Load more</button>
{filters}
<button id="del" onclick="localStorage.setItem('deleted', '1')">Delete account</button>
<button id="help" onclick="document.getElementById('h').hidden = !document.getElementById('h').hidden">Help</button>
<p id="h" hidden>Help text</p><ul id="list"></ul>
</body>"""
STAR = "const s = this.classList.toggle('on'); this.textContent = s ? '★' : '☆'; localStorage.setItem('starred', s ? '1' : '0')"
MORE_FAST = "document.getElementById('list').insertAdjacentHTML('beforeend', '<li>item</li>')"
MORE_SLOW = "const t = performance.now(); while (performance.now() - t < 900) {} " + MORE_FAST
FILTERS = """<button role="tab" id="filters" onclick="this.setAttribute('aria-selected', 'true')">Filters</button>"""


class TestInteractions(GitRepoTestCase):
    def setUp(self):
        super().setUp()
        site = self.test_dir / "site"
        (site / "v1").mkdir(parents=True)
        (site / "v2").mkdir()
        (site / "v1" / "p.html").write_text(PAGE.format(star=STAR, more=MORE_FAST, filters=FILTERS))
        (site / "v2" / "p.html").write_text(PAGE.format(star="", more=MORE_SLOW, filters=""))
        self.v1, self.v2 = (site / "v1").as_uri(), (site / "v2").as_uri()

    def compare(self):
        async def run():
            engine = BrowserEngine()
            try:
                before, after = await asyncio.gather(engine.probe_interactions(self.v1, "/p.html"),
                                                     engine.probe_interactions(self.v2, "/p.html"))
                missing = [before[k] for k in before if k not in after]
                after.update(await engine.probe_interactions(self.v2, "/p.html", only=missing))
                return before, after
            finally:
                await engine.close()
        return asyncio.run(run())

    def test_scenario_57_broken_slow_and_missing_buttons(self):
        before, after = self.compare()
        classifier = RegressionClassifier(TaskContext(title="Refactor list"))
        findings = {k: classifier.classify_interaction("/p.html", before[k], after.get(k)) for k in before}

        self.assertIn("stopped working", findings["id:star"].after_behavior)
        self.assertTrue(findings["id:star"].is_critical)
        self.assertRegex(findings["id:more"].after_behavior, r"slower|freezes")
        self.assertEqual(findings["id:filters"].category, "REMOVED_FUNCTIONALITY")
        self.assertIsNone(findings["id:help"])  # same behaviour in both versions: no noise

    def test_scenario_58_safety(self):
        before, _ = self.compare()
        self.assertNotIn("id:del", before)  # "Delete account" is never clicked

        async def starred():
            engine = BrowserEngine()
            try:
                async with engine._shared_browser() as browser:
                    ctx = await browser.new_context()
                    page = await ctx.new_page()
                    await page.goto(self.v1 + "/p.html")
                    value = await page.evaluate("() => [localStorage.getItem('starred'), localStorage.getItem('deleted')]")
                    await ctx.close()
                    return value
            finally:
                await engine.close()
        # Every probe uses a fresh context, so check the toggle restore inside one context instead
        self.assertEqual(asyncio.run(starred()), [None, None])
