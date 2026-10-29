"""
Developer Feedback Tests.
Covers:
- Scenario 83: Changes ProofPush can't judge are put to the developer ("on purpose or a mistake?"): on purpose
  becomes an intended change, a mistake blocks; AI agents are told to ask the user the same question
- Scenario 84: Vague commit messages ("update", "wip") get a tip to start bug fixes with 'fix:'
- Scenario 85: Without an OpenAPI description, the developer is warned which write endpoints won't be tested
"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush import policy_enforcer
from proofpush.classifier import Finding
from proofpush.config import PoliciesConfig, load_config
from proofpush.fix_history import commit_message_advice, vague_commits


class FakeTerminal:
    """A /dev/tty stand-in that answers prompts from a script and records what was shown."""

    def __init__(self, answers):
        self.answers, self.shown = list(answers), []

    def write(self, text):
        self.shown.append(text)

    def flush(self):
        pass

    def readline(self):
        return (self.answers.pop(0) + "\n") if self.answers else ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def finding(category, target, after, critical=False):
    return Finding(category=category, target=target, before_behavior="", after_behavior=after, expected_behavior="",
                   actual_result=after, confidence=0.7, recommended_action="", is_critical=critical)


class TestDeveloperFeedback(GitRepoTestCase):
    def decide(self, findings, answers):
        term = FakeTerminal(answers)
        with mock.patch.object(policy_enforcer, "open_user_terminal", return_value=term):
            decision = policy_enforcer.PolicyEnforcer("interactive", PoliciesConfig()).evaluate(findings=findings)
        return decision, "".join(term.shown)

    def test_scenario_83_on_purpose_or_mistake(self):
        total = finding("POSSIBLE_REGRESSION", "API GET /api/cart/total [valid request]", "$.total changed 100 -> 90")
        name = finding("POSSIBLE_REGRESSION", "API GET /api/me [valid request]", "$.name changed 'Ann' -> 'ann'")
        decision, shown = self.decide([total, name], ["y", "y"])
        self.assertTrue(decision.allowed)
        self.assertIn("Is this change on purpose?", shown)
        self.assertIn("$.total changed 100 -> 90", shown)
        self.assertEqual(total.category, "EXPECTED_CHANGE")

        total.category = "POSSIBLE_REGRESSION"
        name.category = "POSSIBLE_REGRESSION"
        decision, _ = self.decide([total, name], ["y", "n"])
        self.assertFalse(decision.allowed)
        self.assertIn("You marked this change as a mistake: API GET /api/me", decision.reason)

        # Other problems still need the usual approval after the unclear ones are settled
        total.category = "POSSIBLE_REGRESSION"
        broken = finding("REGRESSION", "/checkout", "page now returns 500", critical=True)
        decision, shown = self.decide([total, broken], ["y", "n"])
        self.assertFalse(decision.allowed)
        self.assertIn("Approve push anyway?", shown)

        # AI agents can't be asked: the summary tells them to ask the user
        from proofpush.cli import why_blocked
        total.category = "POSSIBLE_REGRESSION"
        text = why_blocked("WHY THIS PUSH WAS BLOCKED", "needs review", [total])
        self.assertIn("ask the user: is this change on purpose, or a mistake?", text)

    def test_scenario_84_vague_commit_messages(self):
        self.assertEqual(vague_commits(["update", "wip", "fix login redirect", "feat: add coupon field to checkout",
                                        "Add coupon support to checkout page", "header", "fixed it"]),
                         ["update", "wip", "header"])
        advice = commit_message_advice(["update"])
        self.assertEqual(advice[0].category, "NOTICE")  # a tip, never a block
        self.assertIn("start the message with 'fix:'", advice[0].recommended_action)
        self.assertEqual(commit_message_advice(["fix: cart total ignores quantity"]), [])

    def test_scenario_85_no_openapi_warning(self):
        from proofpush.cli import _run_api_tests
        (self.local_dir / "api.py").write_text('@app.post("/api/patients")\ndef c(): ...\n@app.get("/api/patients")\ndef l(): ...\n')
        (self.local_dir / ".proofpush.yml").write_text("api_tests:\n  mode: full\n  timeout: 1\n")
        cfg = load_config(self.local_dir)
        findings = _run_api_tests(cfg, [self.local_dir], "local", "local", "http://127.0.0.1:9", {})
        warning = [f for f in findings if f.target == "Backend API description"]
        self.assertEqual(len(warning), 1)
        self.assertIn("1 write endpoint(s) (POST /api/patients) won't be tested", warning[0].after_behavior)
        self.assertIn("FastAPI serves one at /openapi.json", warning[0].recommended_action)
