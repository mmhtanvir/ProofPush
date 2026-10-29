"""
API Payload Testing.
Covers:
- Scenario 62: OpenAPI discovery ($ref, security, responses) and framework route discovery
- Scenario 63: Schema-valid and invalid/boundary payload generation
- Scenario 64: Baseline vs candidate: removed endpoint, validation removed, auth bypass, type change, guard case,
  multi-step business rule; unchanged endpoints and dynamic timestamps are quiet
- Scenario 65: New regressions vs existing failures; potential (unconfirmed) differences
- Scenario 66: Production is read-only and explicit-only; writes need allow_writes and an isolated database
- Scenario 67: Request budget, rate limit, timeouts and connection failures (environment issues, not regressions)
- Scenario 68: Secrets never reach findings (headers, tokens, sensitive fields and query values)
- Scenario 69: Database safety: remote/production databases block test suites and migrations
- Scenario 70: Backward compatibility: configs without api_tests behave as before
- Scenario 72: A second test identity reading the first one's resource (IDOR) is a regression when it's new
- Scenario 81: Permission matrix: owner allowed, anonymous/other denied; newly allowed access is privilege escalation
- Scenario 82: A response that starts exposing sensitive fields (names only in the report) is a regression
- Scenario 71: Pre-push hook end to end (AI agent, strict): a backend change that removes an endpoint and
  brings back a fixed bug is blocked without prompting
"""

import os
import sys
import threading
import time
from pathlib import Path
from unittest import mock

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
from proofpush.config import load_config
from proofpush.utils import run_command

AUTH = {"Authorization": "Bearer good-token"}
GUARD_CASE = {"name": "total uses quantity (bug #12)", "guard": True, "path": "/api/total", "query": {"qty": 2},
              "expect": {"status": 200, "body": {"$.total": 5.0}}}


class TestApiTesting(GitRepoTestCase):
    def start(self, variant):
        server = app.serve_variant(variant)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def endpoints(self):
        return at.endpoints_from_openapi(app.SPEC)

    def run_pair(self, cases, policy=None, limits=None):
        good, bad = self.start("good"), self.start("bad")
        policy = policy or at.Policy(env_type="local", allow_writes=True)
        return at.run_suite(cases, policy, limits or at.RunLimits(rate_per_second=1000), bad, AUTH, good, AUTH)

    def test_scenario_62_discovery(self):
        eps = {e.key: e for e in self.endpoints()}
        post = eps["POST /api/items"]
        self.assertTrue(post.requires_auth)
        self.assertEqual(post.body_schema["required"], ["name", "price"])  # $ref resolved
        self.assertIn("price", post.responses["201"]["properties"])
        self.assertFalse(eps["GET /api/health"].requires_auth)

        spec, origin = at.load_openapi(self.local_dir, "", self.start("good"))
        self.assertEqual(origin, "/openapi.json")
        self.assertEqual(len(at.endpoints_from_openapi(spec)), 7)

        (self.local_dir / "api").mkdir()
        (self.local_dir / "api" / "main.py").write_text('@router.put("/api/users/{uid}")\ndef u(): ...\n@app.route("/x", methods=["GET", "POST"])\ndef x(): ...\n')
        (self.local_dir / "server.go").write_text('r.Delete("/api/sessions/:id", h)\nhttp.HandleFunc("POST /api/login", h)\n')
        (self.local_dir / "app" / "api" / "orders" / "[id]").mkdir(parents=True)
        (self.local_dir / "app" / "api" / "orders" / "[id]" / "route.ts").write_text("export async function PATCH(req) {}")
        keys = {e.key for e in at.discover_framework_endpoints(self.local_dir)}
        for k in ("PUT /api/users/{uid}", "GET /x", "POST /x", "DELETE /api/sessions/{id}", "POST /api/login", "PATCH /api/orders/{id}"):
            self.assertIn(k, keys)

    def test_scenario_63_payloads(self):
        schema = app.SPEC["components"]["schemas"]["NewItem"]
        self.assertEqual(at.sample_value(schema), {"name": "proofpush-test", "price": 0.0})
        names = [n for n, _, _ in at.invalid_bodies(schema, 20)]
        for expected in ("missing required 'name'", "null 'price'", "wrong type for 'price'", "empty 'name'",
                         "'price' below minimum", "malformed JSON"):
            self.assertIn(expected, names)
        cases = at.generate_cases(self.endpoints(), max_invalid=3)
        post = [c for c in cases if c.method == "POST"]
        self.assertEqual([c.kind for c in post].count("invalid"), 3)
        self.assertIn("unauth", [c.kind for c in post])
        self.assertEqual([c for c in cases if c.path == "/api/items/1"][0].kind, "valid")  # path example used

    def test_scenario_64_baseline_vs_candidate(self):
        cases = at.generate_cases(self.endpoints(), max_invalid=4) + at.cases_from_config([GUARD_CASE, {
            "name": "create then read", "steps": [
                {"method": "POST", "path": "/api/items", "json": {"name": "Cup", "price": 3}, "save": {"new_id": "$.id"},
                 "expect": {"status": 201}},
                {"method": "GET", "path": "/api/items/{new_id}", "expect": {"fields": ["name", "price"]}}]}])
        by = {(o.case.method, o.case.path, o.case.name): o for o in self.run_pair(cases)}
        f = lambda m, p, n: by[(m, p, n)]  # noqa: E731

        self.assertEqual(f("GET", "/api/legacy", "valid request").classification, "removed")
        self.assertEqual(f("POST", "/api/items", "missing required 'name'").classification, "regression")
        # The contract documents 401/422 for this request, so that (stronger) expectation is what's cited
        self.assertIn("status 201, expected 401/422", " ".join(f("POST", "/api/items", "missing required 'name'").details))
        self.assertEqual(f("POST", "/api/items", "without credentials").classification, "regression")
        items = f("GET", "/api/items", "valid request")
        self.assertEqual(items.classification, "regression")  # price string vs documented number
        self.assertIn("price should be number", " ".join(items.details))
        guard = f("GET", "/api/total", "total uses quantity (bug #12)")
        self.assertEqual(guard.classification, "regression")
        self.assertIn("previously fixed bug is back", guard.details[0])
        self.assertEqual(f("GET", "/api/health", "valid request").classification, "passed")
        # The two instances started at different moments: created timestamps differ but are dynamic
        self.assertFalse(any("created" in d for o in by.values() for d in o.details))

        findings = at.to_findings(list(by.values()), 6, "local")
        self.assertTrue(any(x.category == "REMOVED_FUNCTIONALITY" and "/api/legacy" in x.target for x in findings))
        self.assertTrue(any(x.category == "NOTICE" and x.target == "API coverage" for x in findings))

    def test_scenario_65_existing_vs_new_and_potential(self):
        good = at.ApiResult(status=200, ms=10, body={"total": 5.0, "at": "2026-01-01T00:00:00Z"})
        moved = at.ApiResult(status=200, ms=12, body={"total": 4.0, "at": "2026-02-02T10:00:00Z"})
        case = at.ApiCase(name="v", method="GET", path="/t")
        out = at.classify(case, good, moved, [])
        self.assertEqual(out.classification, "potential_regression")  # value changed, no contract says it is wrong
        self.assertEqual(out.details, ["$.total changed 5.0 -> 4.0"])  # timestamp ignored, total not
        broken = at.ApiResult(status=500, ms=5, body={})
        self.assertEqual(at.classify(case, broken, broken, []).classification, "existing_failure")
        self.assertEqual(at.classify(case, good, broken, []).classification, "regression")
        self.assertEqual(at.classify(case, good, at.ApiResult(status=200, ms=9, body=dict(good.body)), []).classification, "passed")

    def test_scenario_66_production_and_write_policy(self):
        explicit_get = at.cases_from_config([{"name": "health", "path": "/api/health", "prod": True, "expect": {"status": 200}}])[0]
        unapproved = at.cases_from_config([{"name": "x", "path": "/api/items"}])[0]
        write = at.cases_from_config([{"name": "w", "method": "POST", "path": "/api/items", "prod": True}])[0]
        generated = at.generate_cases(self.endpoints())[0]
        prod = at.Policy(env_type="production")
        self.assertIsNone(prod.refusal(explicit_get))
        self.assertIn("approved", prod.refusal(unapproved))
        self.assertIn("approved", prod.refusal(generated))
        self.assertIn("never sent to production", prod.refusal(write))
        self.assertIsNone(at.Policy(env_type="production", allow_writes=True).refusal(write))  # explicit test tenant
        local = at.Policy(env_type="local")
        self.assertIn("allow_writes", local.refusal(write))
        self.assertIn("not isolated", at.Policy(env_type="local", allow_writes=True, remote_database="DB_HOST=rds").refusal(write))
        pay = at.ApiCase(name="valid request", method="POST", path="/api/payments/charge", kind="valid")
        self.assertIn("side effects", at.Policy(env_type="staging", allow_writes=True).refusal(pay))

        outcomes = at.run_suite([explicit_get, unapproved, write], prod, at.RunLimits(rate_per_second=1000),
                                self.start("good"), AUTH)
        self.assertEqual([o.classification for o in outcomes], ["passed", "not_tested", "not_tested"])

    def test_scenario_67_limits_timeouts_and_failures(self):
        url = self.start("good")
        cases = [at.ApiCase(name=f"c{i}", method="GET", path="/api/health") for i in range(5)]
        start = time.monotonic()
        outcomes = at.run_suite(cases, at.Policy("local"), at.RunLimits(max_requests=3, rate_per_second=10), url, {})
        self.assertGreaterEqual(time.monotonic() - start, 0.15)  # 3 requests at 10/s: two 0.1 s gaps (coarse clocks on Windows)
        self.assertEqual([o.classification for o in outcomes].count("not_tested"), 2)

        slow = at.ApiCase(name="slow", method="GET", path="/slow")
        out = at.run_suite([slow], at.Policy("local"), at.RunLimits(timeout=0.5, rate_per_second=1000), url, {})[0]
        self.assertEqual(out.classification, "environment")
        dead = "http://127.0.0.1:9"
        both_down = at.run_suite([cases[0]], at.Policy("local"), at.RunLimits(rate_per_second=1000), dead, {}, dead, {})[0]
        self.assertEqual(both_down.classification, "environment")
        cand_down = at.run_suite([cases[0]], at.Policy("local"), at.RunLimits(rate_per_second=1000), dead, {}, url, {})[0]
        self.assertEqual(cand_down.classification, "regression")

    def test_scenario_68_redaction(self):
        case = at.ApiCase(name="login", method="POST", path="/api/login?api_key=sk-live-abc123def456ghi789jkl0",
                          query={"token": "abc", "page": 2}, body={"email": "a@b.c", "password": "hunter2secret"},
                          headers={"Authorization": "Bearer super-secret-token-123456789"})
        outcome = at.CaseOutcome(case, at.ApiResult(status=200), at.ApiResult(status=500), "regression", ["server error 500"], "server error")
        text = repr(at.to_findings([outcome], 1, "local")[0])
        for secret in ("hunter2secret", "super-secret-token", "sk-live-abc123def456ghi789jkl0", "token=abc"):
            self.assertNotIn(secret, text)
        self.assertIn("page=2", text)

    def test_scenario_69_database_safety(self):
        self.assertIsNone(at.remote_database({"DATABASE_URL": "postgres://u:p@localhost:5432/app", "DB_HOST": "db"}))
        self.assertIsNone(at.remote_database({"DATABASE_URL": "sqlite:///tmp/x.db"}))
        self.assertIn("rds", at.remote_database({"DATABASE_URL": "postgres://u:p@prod.abc.rds.amazonaws.com/app"}))
        self.assertIn("ENV=Production", at.remote_database({"ENV": "Production"}))

        from proofpush.cli import _compare_test_suites
        cfg = load_config(self.local_dir)
        cfg.tests.enabled, cfg.tests.command = True, "echo should-not-run"
        (self.local_dir / ".env").write_text("DB_HOST=crm-prod.cluster.example.com\n")
        findings = _compare_test_suites(cfg, self.local_dir, self.local_dir)
        self.assertEqual(findings[0].category, "BLOCKED")
        self.assertIn("non-local database", findings[0].after_behavior)

        from proofpush.config import ServiceConfig
        from proofpush.environment_manager import EnvironmentManager
        em = EnvironmentManager(self.local_dir, cfg.application)
        inst = em._start_service("app", ServiceConfig(start_command="python manage.py migrate && echo up", health_timeout=2),
                                 9, self.local_dir, self.local_dir / "x.db")
        self.assertIn("Refused to start", inst.error_message)

    def test_scenario_70_backward_compatible_config(self):
        (self.local_dir / ".proofpush.yml").write_text("mode: strict\nenvironments:\n  prod: https://shop.example.com\n")
        cfg = load_config(self.local_dir)
        self.assertEqual(cfg.api_tests.mode, "off")
        self.assertEqual(cfg.environments["prod"], "https://shop.example.com")
        self.assertEqual(cfg.environment_types["prod"], "production")
        from proofpush.cli import _run_api_tests
        self.assertEqual(_run_api_tests(cfg, [self.local_dir], "local", "local", "http://127.0.0.1:9", {}), [])

    def test_scenario_71_prepush_backend_regression_blocked_for_agent(self):
        mock_src = Path(app.__file__).read_text()
        self.commit_file("api_mock_app.py", mock_src, "init api")
        self.commit_file(".proofpush.yml", (
            "mode: interactive\nnotify: {desktop: false}\nvisual: {enabled: false}\n"
            "application:\n  start_command: \"python3 api_mock_app.py\"\n  health_check: /api/health\n"
            "api_tests:\n  mode: full\n  rate_per_second: 200\n"
            "  cases:\n    - name: \"total uses quantity (bug #12)\"\n      guard: true\n      path: /api/total\n"
            "      query: {qty: 2}\n      expect: {status: 200, body: {\"$.total\": 5.0}}\n"), "config")
        run_command(["git", "push", "origin", "main"], cwd=self.local_dir, timeout=300)
        run_command([sys.executable, "-m", "proofpush", "install", "--skip-deps"], cwd=self.local_dir)
        self.commit_file("api_mock_app.py", mock_src.replace('\nVARIANT = "good"\n', '\nVARIANT = "bad"\n', 1), "refactor items api")
        env = {"CLAUDECODE": "1", "PATH": os.environ["PATH"]}
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(Path(__file__).resolve().parent.parent)}):
            code, out, err = run_command(["git", "push", "origin", "main"], cwd=self.local_dir, env=env, timeout=300)
        text = out + err
        self.assertNotEqual(code, 0, text[-3000:])
        self.assertIn("previously fixed bug is back", text)
        self.assertIn("/api/legacy", text)
        self.assertIn("Push blocked for Claude Code", text)
        self.assertNotIn("Approve push anyway", text)

    def test_scenario_72_other_user_cannot_read_your_records(self):
        other = {"other": {"Authorization": "Bearer other-token"}}
        cases = [c for c in at.generate_cases(self.endpoints(), other_identity="other") if c.kind == "other_user"]
        # /api/items/{id} is documented as public (no security), so it isn't an ownership check
        self.assertEqual([c.path for c in cases], ["/api/notes/1"])
        outcomes = {o.case.path: o for o in at.run_suite(cases, at.Policy("local"), at.RunLimits(rate_per_second=1000),
                                                          self.start("bad"), AUTH, self.start("good"), AUTH, identities=other)}
        notes = outcomes["/api/notes/1"]
        self.assertEqual(notes.classification, "regression")
        self.assertIn("another test user (other) can read this resource (200)", notes.details[0])
        missing = at.run_suite(cases[:1], at.Policy("local"), at.RunLimits(), self.start("good"), AUTH, identities={})
        self.assertIn("has no credentials", missing[0].refusal)


    def test_scenario_73_disposable_postgres_per_version(self):
        """application.database: postgres gives each version its own throwaway database (Docker stood in by a script)."""
        if sys.platform == "win32":
            self.skipTest("stand-in docker is a shell script")
        from proofpush.config import ApplicationConfig
        from proofpush.environment_manager import EnvironmentManager
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir()
        log = self.test_dir / "docker.log"
        fake = bin_dir / "docker"
        fake.write_text('#!/bin/sh\necho "$@" >> "$FAKE_DOCKER_LOG"\n'
                        'if [ "$1" = run ] && [ -n "$FAKE_DOCKER_DOWN" ]; then echo "Cannot connect to the Docker daemon" >&2; exit 1; fi\n'
                        'if [ "$1" = run ]; then echo cid; fi\n'
                        'if [ "$1" = port ]; then echo 127.0.0.1:55432; fi\nexit 0\n')
        fake.chmod(0o755)
        self.commit_file("app.py", "import http.server, os\nopen('db.txt', 'w').write(os.environ['DATABASE_URL'])\n"
                         "from proofpush.static_server import make_server\nmake_server(int(os.environ['PORT'])).serve_forever()\n", "app")
        cfg = ApplicationConfig(start_command=f'"{sys.executable}" app.py', database="postgres", health_timeout=15)
        path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        with mock.patch.dict(os.environ, {"PATH": path, "FAKE_DOCKER_LOG": str(log)}):
            em = EnvironmentManager(self.local_dir, cfg)
            try:
                env = em.launch_environment("HEAD", "proposed")
                self.assertTrue(env.is_ready, env.startup_error)
                self.assertEqual(env.database_url, "postgresql://proofpush:proofpush@127.0.0.1:55432/proofpush")
                self.assertEqual((env.worktree_path / "db.txt").read_text(), env.database_url)  # the app got it
            finally:
                em.cleanup()
            calls = log.read_text()
            self.assertIn("run -d --rm --name proofpush-proposed-", calls)
            self.assertIn("-p 127.0.0.1::5432", calls)  # Docker picks the port: no race between concurrent runs
            self.assertIn("--label proofpush.owner_pid=", calls)
            self.assertIn("postgres:16-alpine", calls)
            self.assertIn("pg_isready", calls)
            self.assertIn("rm -f proofpush-proposed-", calls)

            with mock.patch.dict(os.environ, {"FAKE_DOCKER_DOWN": "1"}):
                em = EnvironmentManager(self.local_dir, cfg)
                env = em.launch_environment("HEAD", "baseline")
                em.cleanup()
                self.assertIn("Is Docker running", env.startup_error)
        with mock.patch("shutil.which", return_value=None):
            em = EnvironmentManager(self.local_dir, cfg)
            env = em.launch_environment("HEAD", "baseline")
            em.cleanup()
            self.assertIn("needs Docker or Podman", env.startup_error)

    def test_scenario_81_permission_matrix(self):
        rules = [{"path": "/api/notes/{note_id}", "GET": ["owner"]}, {"path": "/api/orders/{id}", "GET": ["owner"]}]
        cases, problems = at.permission_cases(rules, self.endpoints(), ["other"])
        self.assertEqual([c.name for c in cases], ["owner may GET", "anonymous must not GET", "other must not GET"])
        self.assertIn("example_path", problems[0])  # /api/orders/{id} is unknown and has no example
        ids = {"other": {"Authorization": "Bearer other-token"}}
        out = {o.case.name: o for o in at.run_suite(cases, at.Policy("local"), at.RunLimits(rate_per_second=1000),
                                                     self.start("bad"), AUTH, self.start("good"), AUTH, identities=ids)}
        self.assertEqual(out["owner may GET"].classification, "passed")
        self.assertEqual(out["anonymous must not GET"].classification, "passed")
        other = out["other must not GET"]
        self.assertEqual(other.classification, "regression")
        self.assertIn("privilege escalation", other.details[0])
        self.assertEqual(other.source, "permission matrix")
        # Production never runs generated permission probes
        self.assertIsNotNone(at.Policy("production").refusal(cases[0]))

    def test_scenario_82_sensitive_fields_exposed(self):
        case = at.ApiCase(name="valid request", method="GET", path="/api/me")
        before = at.ApiResult(status=200, ms=5, body={"id": 1, "name": "Ann"})
        after = at.ApiResult(status=200, ms=5, body={"id": 1, "name": "Ann", "password_hash": "pbkdf2$abc$secret",
                                                     "profile": {"api_key": "sk-live-123456789012345678901"}})
        out = at.classify(case, before, after, [])
        self.assertEqual(out.classification, "regression")
        self.assertIn("response exposes sensitive field $.password_hash", out.details)
        self.assertIn("response exposes sensitive field $.profile.api_key", out.details)
        text = repr(at.to_findings([out], 1, "local"))
        self.assertNotIn("pbkdf2$abc$secret", text)
        self.assertNotIn("sk-live-123456789012345678901", text)
        self.assertEqual(at.classify(case, after, after, []).classification, "existing_failure")

