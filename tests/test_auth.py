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
