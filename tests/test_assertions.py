"""
Assertion Language Tests.
Covers:
- Scenario 78: Assertions evaluate business rules over responses (arithmetic relations, lengths, membership,
  saved variables) and failures name the observed values
- Scenario 79: The language can't run code: dunder access, imports, lambdas, comprehensions, arbitrary calls
  and unknown names are rejected before any request is sent
- Scenario 80: API cases with assertions: pass, fail with evidence, and invalid assertions are never sent
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
try:
    from tests import api_mock_app as app
except ImportError:
    import api_mock_app as app
from proofpush import api_testing as at
from proofpush import assertions as asr

ORDER = {"response": {"status": 201, "ms": 40, "body": {"subtotal": 100, "discount": 10, "total": 90,
                                                         "items": [{"sku": "PEN", "price": 2.5}], "owner": "a"}},
         "request": {"body": {"subtotal": 100}}, "vars": {"order_id": "7"}}


class TestAssertions(GitRepoTestCase):
    def test_scenario_78_business_rules(self):
        for ok in ["response.status == 201",
                   "response.body.total == response.body.subtotal - response.body.discount",
                   "len(response.body.items) > 0 and response.body.items[0].price >= 0",
                   "'email' not in response.body",
                   "response.body.owner in ['a', 'b']",
                   "200 <= response.status < 300",
                   "response.ms < 500",
                   "request.body.subtotal == 100",
                   "vars.order_id == '7'",
                   "response.body.missing == null or true",
                   "round(response.body.items[0].price * 2) == 5"]:
            self.assertIsNone(asr.check(ok, ORDER), ok)
        msg = asr.check("response.body.total == 95", ORDER)
        self.assertEqual(msg, "assertion failed: `response.body.total == 95` (response.body.total = 90)")
        msg = asr.check("response.body.total == response.body.subtotal", ORDER)
        self.assertIn("response.body.total = 90, response.body.subtotal = 100", msg)
        self.assertIn("<missing>", asr.check("response.body.tax == 0", ORDER))  # absent field never equals anything
        self.assertIsNotNone(asr.check("response.body.items[5].price > 0", ORDER))
        self.assertIn("could not be evaluated", asr.check("response.body.total / 0 == 1", ORDER))

    def test_scenario_79_cannot_run_code(self):
        for bad in ["response.__class__", "().__class__.__bases__[0].__subclasses__()", "__import__('os').system('id')",
                    "open('/etc/passwd')", "(lambda: 1)()", "[x for x in response.body]", "os.system('id')",
                    "response.body.items.pop()", "eval('1')", "f'{response}'", "1 if true else 2", "response.body[0:1]"]:
            self.assertRaises(asr.AssertionSyntaxError, asr.parse, bad)
            self.assertIn("invalid assertion", asr.check(bad, ORDER))
        self.assertRaises(asr.AssertionSyntaxError, asr.parse, "x" * 600)
        # Attribute syntax only ever reads JSON keys, never Python attributes
        self.assertIn("<missing>", asr.check("response.body.items.append == 1", ORDER))

    def test_scenario_80_api_cases_with_assertions(self):
        good, bad = app.serve_variant("good"), app.serve_variant("bad")
        for s in (good, bad):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)
        cases = at.cases_from_config([
            {"name": "total = price x qty", "path": "/api/total", "query": {"qty": 2},
             "assertions": ["response.status == 200", "response.body.total == 2.5 * request.query.qty"]},
            {"name": "typo in config", "path": "/api/health", "assertions": ["response.status = 200"]},
            {"name": "create then read", "steps": [
                {"method": "POST", "path": "/api/items", "payload": {"name": "Cup", "price": 3},
                 "save": {"new_id": "$.id"}, "assertions": ["response.status == 201"]},
                {"method": "GET", "path": "/api/items/{new_id}", "assertions": ["response.body.id == vars.new_id * 1 or true"]}]},
        ])
        self.assertEqual(cases[2].body, {"name": "Cup", "price": 3})  # `payload` alias
        out = {o.case.name: o for o in at.run_suite(
            cases, at.Policy("local", allow_writes=True), at.RunLimits(rate_per_second=1000),
            f"http://127.0.0.1:{bad.server_port}", {"Authorization": "Bearer good-token"},
            f"http://127.0.0.1:{good.server_port}", {"Authorization": "Bearer good-token"})}
        total = out["total = price x qty"]
        self.assertEqual(total.classification, "regression")
        self.assertIn("response.body.total = 2.5", total.details[0])
        self.assertEqual(total.source, "explicit assertion")
        typo = out["typo in config"]
        self.assertEqual(typo.classification, "not_tested")
        self.assertIn("invalid assertion", typo.refusal)
        self.assertNotEqual(out["create then read"].classification, "regression")
