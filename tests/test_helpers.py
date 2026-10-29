"""
Test Helpers and Fixtures for ProofPush test suite.
Sets up temporary git repositories, bare remotes, mock websites, and pre-push hooks.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from proofpush.utils import run_command


class GitRepoTestCase(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="auditor_test_"))

        # Sandbox HOME so baselines (~/.proofpush) and global git config stay out of the real home,
        # including for hook subprocesses; keep Playwright pointed at the real browser cache.
        if sys.platform == "win32":
            default_cache = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ms-playwright"
        elif sys.platform == "darwin":
            default_cache = Path.home() / "Library" / "Caches" / "ms-playwright"
        else:
            default_cache = Path.home() / ".cache" / "ms-playwright"
        browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(default_cache)
        sandbox = str(self.test_dir / "home")
        env_patch = mock.patch.dict(os.environ, {"HOME": sandbox, "USERPROFILE": sandbox, "PLAYWRIGHT_BROWSERS_PATH": browsers})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.remote_dir = self.test_dir / "remote.git"
        self.local_dir = self.test_dir / "repo"

        # 1. Initialize bare remote
        self.remote_dir.mkdir(parents=True)
        run_command(["git", "init", "--bare", "-b", "main"], cwd=self.remote_dir)

        # 2. Initialize local repository
        self.local_dir.mkdir(parents=True)
        run_command(["git", "init", "-b", "main"], cwd=self.local_dir)
        run_command(["git", "config", "user.name", "Auditor Tester"], cwd=self.local_dir)
        run_command(["git", "config", "user.email", "tester@example.com"], cwd=self.local_dir)
        run_command(["git", "remote", "add", "origin", str(self.remote_dir)], cwd=self.local_dir)

    def tearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def commit_file(self, filename: str, content: str, message: str = "commit") -> str:
        """Create or update a file and commit it."""
        file_path = self.local_dir / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        run_command(["git", "add", filename], cwd=self.local_dir)
        run_command(["git", "commit", "-m", message], cwd=self.local_dir)
        code, out, _ = run_command(["git", "rev-parse", "HEAD"], cwd=self.local_dir)
        return out.strip()

    def create_mock_site(self, version: str = "baseline", has_regression: bool = False, has_new_feature: bool = False):
        """Create static HTML files for mock site."""
        index_html = """<!DOCTYPE html>
<html>
<head><title>Test Store</title></head>
<body>
    <h1>Welcome to Test Store</h1>
    <nav><a href="/checkout.html">Checkout</a></nav>
    <div id="content">Standard store content</div>
</body>
</html>"""

        coupon_section = """
        <div id="coupon-area">
            <input id="coupon" placeholder="Coupon code" />
            <button id="apply-btn" onclick="applyCoupon()">Apply</button>
            <div id="discount-msg"></div>
        </div>
        <script>
            function applyCoupon() {
                const code = document.getElementById('coupon').value;
                if (code === 'SAVE10') {
                    document.getElementById('discount-msg').innerText = '10% discount applied!';
                    document.getElementById('total').innerText = '$90.00';
                } else {
                    document.getElementById('discount-msg').innerText = 'Invalid coupon code';
                }
            }
        </script>
        """ if has_new_feature else ""

        broken_script = """
        <script>
            console.error("Uncaught TypeError: Cannot read properties of undefined (reading 'calculateTotal')");
        </script>
        """ if has_regression else ""

        checkout_html = f"""<!DOCTYPE html>
<html>
<head><title>Checkout Page</title></head>
<body>
    <h1>Checkout</h1>
    <p>Order Total: <span id="total">$100.00</span></p>
    {coupon_section}
    {broken_script}
</body>
</html>"""

        self.commit_file("index.html", index_html, "Add index.html")
        self.commit_file("checkout.html", checkout_html, "Add checkout.html")

        # Configuration file
        config_yaml = """mode: strict
baseline:
  strategy: merge-base
application:
  type: custom
  start_command: "python3 -m http.server $PORT"
  port: 3200
  health_check: "/"
routes:
  base_routes:
    - "/"
    - "/checkout.html"
visual:
  enabled: true
"""
        self.commit_file(".proofpush.yml", config_yaml, "Add .proofpush.yml")
