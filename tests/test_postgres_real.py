"""
Disposable PostgreSQL against a real Docker/Podman daemon.
Runs only with PROOFPUSH_DOCKER_TESTS=1, a working container runtime, and pg8000 importable (the test app's driver).
Covers:
- Scenario 74: Baseline and candidate get separate databases; the app's migration runs in its own one; no data leaks
- Scenario 75: Missing image, app startup failure and health-check timeout are clear errors and leave no containers
- Scenario 76: An audit stopped with SIGTERM cleans up; one killed with SIGKILL is reaped by the next run
- Scenario 77: Two runs at once get different containers and ports
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from tests.test_helpers import GitRepoTestCase
except ImportError:
    from test_helpers import GitRepoTestCase
from proofpush.config import ApplicationConfig
from proofpush.environment_manager import EnvironmentManager
from proofpush.utils import run_command

DOCKER = shutil.which("docker") or shutil.which("podman")
# The test sandbox swaps HOME; the docker CLI finds its daemon/context through the real ~/.docker
DOCKER_CONFIG = os.environ.get("DOCKER_CONFIG") or str(Path.home() / ".docker")
ENABLED = (os.environ.get("PROOFPUSH_DOCKER_TESTS") == "1" and DOCKER
           and run_command([DOCKER, "info"], timeout=20)[0] == 0)
import importlib.util
HAVE_DRIVER = importlib.util.find_spec("pg8000") is not None

APP = '''import http.server, json, os, urllib.parse
import pg8000.native
u = urllib.parse.urlparse(os.environ["DATABASE_URL"])
db = pg8000.native.Connection(user=u.username, password=u.password, host=u.hostname, port=u.port, database=u.path[1:])
db.run("CREATE TABLE IF NOT EXISTS notes (id serial PRIMARY KEY, body text NOT NULL)")  # the app's migration

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path == "/api/health":
            return self.send(200, {"ok": True})
        return self.send(200, [r[0] for r in db.run("SELECT body FROM notes ORDER BY id")])
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["body"]
        db.run("INSERT INTO notes (body) VALUES (:b)", b=body)
        self.send(201, {"ok": True})

from proofpush.static_server import LoopbackServer
LoopbackServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''


def ours(pid: int = None):
    """Names of disposable ProofPush containers (optionally only those owned by one process)."""
    flt = ["--filter", "label=proofpush.disposable=1"] + (["--filter", f"label=proofpush.owner_pid={pid}"] if pid else [])
    return run_command([DOCKER, "ps", "-a", *flt, "--format", "{{.Names}}"], timeout=30)[1].split()


@unittest.skipUnless(ENABLED and HAVE_DRIVER, "set PROOFPUSH_DOCKER_TESTS=1 with Docker/Podman running and pg8000 installed")
class TestPostgresReal(GitRepoTestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"DOCKER_CONFIG": DOCKER_CONFIG})
        patch.start()
        self.addCleanup(patch.stop)
        self.assertEqual(run_command([DOCKER, "info"], timeout=20)[0], 0, "docker unreachable inside the test sandbox")
        self.commit_file("app.py", APP, "app")
        self.cfg = ApplicationConfig(start_command=f'"{sys.executable}" app.py', health_check="/api/health",
                                     database="postgres", health_timeout=60)

    def get(self, url):
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())

    def post(self, url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status

    def test_scenario_74_separate_databases_no_leak(self):
        em = EnvironmentManager(self.local_dir, self.cfg)
        try:
            base = em.launch_environment("HEAD", "baseline")
            cand = em.launch_environment("HEAD", "proposed")
            self.assertTrue(base.is_ready, base.startup_error)
            self.assertTrue(cand.is_ready, cand.startup_error)
            self.assertNotEqual(base.database_url, cand.database_url)
            self.assertEqual(self.post(base.frontend.base_url + "/api/notes", {"body": "baseline-only"}), 201)
            self.assertEqual(self.get(base.frontend.base_url + "/api/notes"), ["baseline-only"])
            self.assertEqual(self.get(cand.frontend.base_url + "/api/notes"), [])  # nothing leaked
            # The candidate's migration ran in the candidate's own database
            names = {label: n for _, n in em._containers for label in ("baseline", "proposed") if f"-{label}-" in n}
            count = run_command([DOCKER, "exec", names["proposed"], "psql", "-U", "proofpush", "-d", "proofpush", "-tAc",
                                 "SELECT count(*) FROM notes"], timeout=30)
            self.assertEqual((count[0], count[1].strip()), (0, "0"))
            created = [n for _, n in em._containers]
        finally:
            em.cleanup()
        self.assertEqual([n for n in created if n in ours()], [])

    def test_scenario_75_failures_are_clear_and_clean(self):
        for cfg, expect in (
            (ApplicationConfig(start_command="true", database="postgres", database_image="proofpush/does-not-exist:never"),
             "image available"),
            (ApplicationConfig(start_command=f'"{sys.executable}" -c "raise SystemExit(3)"', database="postgres"),
             "terminated prematurely with code 3"),
            (ApplicationConfig(start_command=f'"{sys.executable}" -c "import time; time.sleep(60)"', database="postgres",
                               health_timeout=5), "Health check timed out"),
        ):
            em = EnvironmentManager(self.local_dir, cfg)
            try:
                env = em.launch_environment("HEAD", "proposed")
                problem = env.startup_error or (env.frontend.error_message if env.frontend else "")
                self.assertIn(expect, problem)
            finally:
                em.cleanup()
            self.assertEqual(ours(os.getpid()), [], expect)

    def audit_process(self):
        """`proofpush review` on this repo in its own process; returns (process, its container names once started)."""
        self.commit_file(".proofpush.yml", "mode: advisory\nnotify: {desktop: false}\napplication:\n"
                         f"  start_command: '\"{sys.executable}\" app.py'\n  health_check: /api/health\n  database: postgres\n", "cfg")
        proc = subprocess.Popen([sys.executable, "-m", "proofpush", "review", "--base", "HEAD~1"], cwd=self.local_dir,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        for _ in range(120):
            names = ours(proc.pid)
            if names:
                return proc, names
            time.sleep(0.5)
        proc.kill()
        self.fail("audit never started a disposable database")

    @unittest.skipIf(sys.platform == "win32", "POSIX signals")
    def test_scenario_76_interrupted_audits_clean_up(self):
        proc, _ = self.audit_process()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=120)
        self.assertEqual(ours(proc.pid), [])

        proc, names = self.audit_process()
        proc.kill()  # SIGKILL: no chance to clean up
        proc.wait(timeout=30)
        self.assertTrue(set(names) & set(ours(proc.pid)))  # left behind...
        EnvironmentManager._reap_orphans(DOCKER)  # ...until the next run reaps it
        self.assertEqual(ours(proc.pid), [])

    def test_scenario_77_concurrent_runs_dont_collide(self):
        managers = [EnvironmentManager(self.local_dir, self.cfg) for _ in range(2)]
        envs = [None, None]

        def launch(i):
            envs[i] = managers[i].launch_environment("HEAD", "proposed")

        threads = [threading.Thread(target=launch, args=(i,)) for i in range(2)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=180)
            for env in envs:
                self.assertTrue(env and env.is_ready, env and env.startup_error)
            self.assertNotEqual(envs[0].database_url, envs[1].database_url)
        finally:
            for m in managers:
                m.cleanup()
