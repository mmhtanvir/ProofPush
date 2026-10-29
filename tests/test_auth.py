"""
Test-Account Login Tests.
Covers:
- Scenario 40: Authenticator-app codes match RFC 6238
- Scenario 41: Credentials come from env vars or .proofpush/secrets.env (kept out of git), never from .proofpush.yml
- Scenario 42: The crawler never follows logout/delete links or downloads
"""

import base64
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush.browser_engine import NON_PAGE, UNSAFE_LINK
from proofpush.config import load_config, resolve_account
from proofpush.utils import totp

CONFIG = """
routes:
  crawl: true
  max_pages: 20
auth:
  login_path: /signin
  accounts:
    local: {username_env: SHOP_USER, password_env: SHOP_PASSWORD}
    prod: {username_env: SHOP_PROD_USER, password_env: SHOP_PROD_PASSWORD, otp_secret_env: SHOP_PROD_OTP}
environments:
  prod: https://shop.example.com
  staging: {url: https://staging.shop.example.com}
"""


class TestAuth(GitRepoTestCase):
    def test_scenario_40_totp_rfc6238(self):
        key = base64.b32encode(b"12345678901234567890").decode()
        for at, code in [(59, "94287082"), (1111111109, "07081804"), (1234567890, "89005924"), (2000000000, "69279037")]:
            self.assertEqual(totp(key, at=at, digits=8), code)
        self.assertEqual(len(totp("JBSWY3DPEHPK3PXP")), 6)

    def test_scenario_41_credentials_sources(self):
        (self.local_dir / ".proofpush.yml").write_text(CONFIG)
        cfg = load_config(self.local_dir)
        self.assertEqual(cfg.auth.login_path, "/signin")
        self.assertEqual(cfg.environments, {"prod": "https://shop.example.com", "staging": "https://staging.shop.example.com"})
        self.assertTrue(cfg.routes.crawl)

        clean = {k: v for k, v in os.environ.items() if not k.startswith("SHOP_")}
        with mock.patch.dict(os.environ, clean, clear=True):
            creds, problem = resolve_account(self.local_dir, cfg.auth, "local")
            self.assertIsNone(creds)
            self.assertIn("SHOP_USER", problem)

            secrets = self.local_dir / ".proofpush" / "secrets.env"
            secrets.parent.mkdir()
            secrets.write_text('SHOP_PROD_USER=qa@shop.example.com\nSHOP_PROD_PASSWORD="p@ss word"\nSHOP_PROD_OTP=JBSWY3DPEHPK3PXP\n')
            creds, _ = resolve_account(self.local_dir, cfg.auth, "prod")
            self.assertEqual((creds.username, creds.password, creds.otp_secret), ("qa@shop.example.com", "p@ss word", "JBSWY3DPEHPK3PXP"))
            # Environment variables win over the file
            with mock.patch.dict(os.environ, {"SHOP_PROD_PASSWORD": "from-env"}):
                self.assertEqual(resolve_account(self.local_dir, cfg.auth, "prod")[0].password, "from-env")

        exclude = (self.local_dir / ".git" / "info" / "exclude").read_text()
        self.assertIn("/.proofpush/", exclude.splitlines())

    def test_scenario_42_crawler_skips_unsafe_links(self):
        for route in ["/logout", "/account/sign-out", "/items/4/delete", "/newsletter/unsubscribe", "/subscription/cancel"]:
            self.assertTrue(UNSAFE_LINK.search(route), route)
        for route in ["/invoice.pdf", "/export.csv"]:
            self.assertTrue(NON_PAGE.search(route), route)
        for route in ["/dashboard", "/settings/profile", "/reports/2026"]:
            self.assertFalse(UNSAFE_LINK.search(route) or NON_PAGE.search(route), route)

    def test_scenario_43_saved_session_flow_and_loopback_retarget(self):
        """Scenario 43: `proofpush login` saves a private session file; local sessions follow the audit's host:port."""
        import asyncio
        import json
        import stat
        from proofpush.browser_engine import BrowserEngine, retarget_session

        out = self.local_dir / ".proofpush" / "session-local.json"
        page = self.test_dir / "login.html"
        page.write_text("<form><input type=password></form>")
        typed = []
        engine = BrowserEngine()
        asyncio.run(engine.save_session_interactively(page.as_uri(), out, lambda: typed.append("enter"), headless=True))
        self.assertEqual(typed, ["enter"])
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        self.assertIn("cookies", json.loads(out.read_text()))

        out.write_text(json.dumps({
            "cookies": [{"name": "session", "value": "ok", "domain": "localhost", "path": "/"},
                        {"name": "sso", "value": "x", "domain": ".idp.example.com", "path": "/"}],
            "origins": [{"origin": "http://localhost:3000", "localStorage": [{"name": "token", "value": "t"}]}],
        }))
        local = retarget_session(out, "http://127.0.0.1:41234")
        self.assertEqual([c["domain"] for c in local["cookies"]], ["127.0.0.1", ".idp.example.com"])
        self.assertEqual(local["origins"][0]["origin"], "http://127.0.0.1:41234")
        prod = retarget_session(out, "https://shop.example.com")
        self.assertEqual(prod["cookies"][0]["domain"], "localhost")

    def test_scenario_44_report_retention_and_hook_quoting(self):
        """Scenario 44: Old reports are pruned to report_retention; the hook survives interpreter paths with spaces."""
        import subprocess
        from proofpush.reporter import AuditReportData, ReportGenerator
        import proofpush.git_interceptor as gi

        reporter = ReportGenerator(self.local_dir, retention=2)
        names = []
        for i in range(4):
            data = AuditReportData(timestamp="t", repo_path=str(self.local_dir), baseline_sha="a" * 10, proposed_sha="b" * 10,
                                   task_title=f"run {i}", task_source="explicit", enforcement_mode="advisory",
                                   decision="ALLOWED", decision_reason="")
            names.append(Path(reporter.save_reports(data)["json"]).name)
        self.assertEqual(len(set(names)), 4)  # same-second runs don't overwrite each other
        left = sorted(f.name for f in reporter.reports_dir.glob("report_*.json"))
        self.assertEqual(left, sorted(names[-2:]))

        with mock.patch("shutil.which", return_value=None), \
                mock.patch.object(gi.sys, "executable", "/opt/My Python/bin/python3"):
            hook = gi.render_hook()
        self.assertIn("BIN_CMD=('/opt/My Python/bin/python3' -m proofpush)", hook)
        script = self.test_dir / "hook.sh"
        script.write_text(hook)
        self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)

    def test_scenario_45b_captcha_is_explained_not_bypassed(self):
        """A login page with Turnstile/reCAPTCHA/hCaptcha yields advice (test keys locally, `proofpush login` in prod)."""
        import asyncio
        from proofpush.browser_engine import BrowserEngine

        async def advice_for(html):
            engine = BrowserEngine()
            page_file = self.test_dir / "login.html"
            page_file.write_text(html)
            async with engine._shared_browser() as browser:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(page_file.as_uri())
                result = await engine._captcha_advice(page)
                await context.close()
            await engine.close()
            return result

        self.assertIn("Cloudflare Turnstile", asyncio.run(advice_for('<form><div class="cf-turnstile" data-sitekey="0x4AAA"></div></form>')))
        self.assertIn("1x00000000000000000000AA", asyncio.run(advice_for('<div class="cf-turnstile"></div>')))
        self.assertIn("hCaptcha", asyncio.run(advice_for('<div class="h-captcha"></div>')))
        self.assertIsNone(asyncio.run(advice_for("<form><input type=password></form>")))
