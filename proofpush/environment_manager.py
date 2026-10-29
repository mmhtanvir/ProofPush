"""
Environment Manager.
Creates isolated Git worktrees, starts baseline and proposed application instances on separate ports,
monitors readiness/health, manages process lifecycles, and cleans up temporary resources.
"""

from __future__ import annotations

import os
import re
import sys
import time
import signal
import shutil
import tempfile
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, List
import subprocess

from proofpush.config import ApplicationConfig, ServiceConfig
from proofpush.utils import find_free_port, run_command


@dataclass
class ServiceInstance:
    name: str
    port: int
    process: Optional[subprocess.Popen] = None
    log_file: Optional[Path] = None
    log_handle: Optional[Any] = None
    base_url: str = ""
    is_ready: bool = False
    error_message: Optional[str] = None


@dataclass
class AppEnvironment:
    label: str  # "baseline" or "proposed"
    revision: str
    worktree_path: Path
    frontend: Optional[ServiceInstance] = None
    backend: Optional[ServiceInstance] = None
    db_path: Optional[Path] = None
    is_ready: bool = False
    startup_error: Optional[str] = None


MIGRATION_COMMAND = re.compile(r"\bmigrate\b|alembic\s+upgrade|prisma\s+(migrate|db\s+push)|knex\s+migrate|db:migrate|sequelize\s+db:migrate|flyway\s+migrate|goose\s+up", re.I)
DEPENDENCY_DIRS = ("node_modules", ".venv", "venv", "env", "vendor", ".bundle")


class EnvironmentManager:
    def __init__(self, repo_path: Path, app_config: ApplicationConfig):
        self.repo_path = repo_path.resolve()
        self.app_config = app_config
        self.active_environments: List[AppEnvironment] = []
        self._temp_base_dir: Optional[Path] = None

    def _get_temp_dir(self) -> Path:
        if not self._temp_base_dir:
            self._temp_base_dir = Path(tempfile.mkdtemp(prefix="auditor_env_"))
        return self._temp_base_dir

    def create_worktree(self, revision: str, label: str) -> Path:
        """Create an isolated Git worktree for the given revision."""
        temp_dir = self._get_temp_dir() / f"{label}_{revision[:10]}_{int(time.time() * 1000) % 100000}"
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Run git worktree add --detach
        code, out, err = run_command(
            ["git", "worktree", "add", "--detach", "--force", str(temp_dir), revision],
            cwd=self.repo_path
        )
        if code != 0:
            # Fallback for bare repos or shallow clones: try checkout or clone
            raise RuntimeError(f"Failed to create Git worktree for revision {revision}: {err or out}")

        self._link_local_files(temp_dir)
        return temp_dir

    def _link_local_files(self, worktree: Path) -> None:
        """Give the checkout what git doesn't track but apps need to start: installed dependencies and .env files.

        Limit: both versions share the dependencies installed in your working copy; if a push changes them,
        run your install command first.
        """
        names = list(DEPENDENCY_DIRS) + [f"{p.parent.name}/{p.name}" for p in self.repo_path.glob("*/node_modules")]
        names += [p.name for p in self.repo_path.glob(".env*") if p.is_file() and not p.name.endswith((".example", ".sample", ".template"))]
        for name in names:
            src, dst = self.repo_path / name, worktree / name
            if not src.exists() or dst.exists() or not dst.parent.is_dir():
                continue
            try:
                os.symlink(src, dst, target_is_directory=src.is_dir())
            except OSError:
                if src.is_dir() and sys.platform == "win32":
                    subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], capture_output=True)  # junction: no admin needed
                elif src.is_file():
                    shutil.copy2(src, dst)

    def launch_environment(self, revision: str, label: str) -> AppEnvironment:
        """Set up and start an isolated application environment."""
        worktree = self.create_worktree(revision, label)
        env = AppEnvironment(label=label, revision=revision, worktree_path=worktree)
        self.active_environments.append(env)

        # Setup isolated database path if sqlite is used
        db_file = worktree / ".auditor_test.db"
        env.db_path = db_file

        # If no start command configured, check if it's static HTML or return ready
        start_cmd = self.app_config.start_command
        if not start_cmd:
            # Check if there is an index.html in the worktree
            if (worktree / "index.html").is_file():
                # This interpreter (Windows has no `python3`), loopback only (not reachable from the network)
                start_cmd = f'"{sys.executable}" -m http.server $PORT --bind 127.0.0.1'
            else:
                # No server needed or no start command provided
                env.is_ready = True
                return env

        # Start backend first if configured
        if self.app_config.backend and self.app_config.backend.start_command:
            backend_port = find_free_port(start=4100 + (100 if label == "proposed" else 0))
            backend_instance = self._start_service(
                name=f"{label}-backend",
                service_cfg=self.app_config.backend,
                port=backend_port,
                cwd=worktree,
                db_path=db_file,
            )
            env.backend = backend_instance
            if not backend_instance.is_ready:
                env.startup_error = f"Backend failed to become ready: {backend_instance.error_message}"
                return env

        # Start primary (frontend/full-stack) service
        fe_cfg = ServiceConfig(
            start_command=start_cmd,
            port=self.app_config.port,
            health_check=self.app_config.health_check,
            health_timeout=self.app_config.health_timeout,
            env=self.app_config.env,
        )
        frontend_port = find_free_port(start=3100 + (100 if label == "proposed" else 0))
        fe_instance = self._start_service(
            name=f"{label}-frontend",
            service_cfg=fe_cfg,
            port=frontend_port,
            cwd=worktree,
            db_path=db_file,
            backend_port=env.backend.port if env.backend else None,
        )
        env.frontend = fe_instance
        if not fe_instance.is_ready:
            env.startup_error = f"Application failed to become ready: {fe_instance.error_message}"
            return env

        env.is_ready = True
        return env

    def _start_service(
        self,
        name: str,
        service_cfg: ServiceConfig,
        port: int,
        cwd: Path,
        db_path: Path,
        backend_port: Optional[int] = None,
    ) -> ServiceInstance:
        """Launch a service process and wait for readiness."""
        base_url = f"http://127.0.0.1:{port}"
        instance = ServiceInstance(name=name, port=port, base_url=base_url)

        # Environment variables for process
        proc_env = os.environ.copy()
        proc_env["PORT"] = str(port)
        proc_env["AUDITOR_PORT"] = str(port)
        proc_env["BASE_URL"] = base_url
        proc_env["DATABASE_URL"] = f"sqlite:///{db_path}"
        proc_env["PROOFPUSH_RUNNING"] = "1"
        if backend_port:
            proc_env["BACKEND_URL"] = f"http://127.0.0.1:{backend_port}"
            proc_env["API_URL"] = f"http://127.0.0.1:{backend_port}"

        for k, v in service_cfg.env.items():
            proc_env[k] = str(v).replace("$PORT", str(port))

        # Substitute $PORT in start command
        cmd_str = service_cfg.start_command.replace("$PORT", str(port))

        # Never run a candidate's migrations against a shared/production database
        if MIGRATION_COMMAND.search(cmd_str):
            from proofpush.api_testing import read_env_files, remote_database
            db = remote_database({**read_env_files(cwd), **proc_env})
            if db:
                instance.error_message = (f"Refused to start: the start command runs migrations and the app's database is "
                                          f"not local ({db}). Use a local/disposable database for audits.")
                return instance

        log_path = cwd / f"{name}.log"
        instance.log_file = log_path
        log_f = open(log_path, "w", encoding="utf-8")
        instance.log_handle = log_f

        try:
            # We use process group so children can be cleanly terminated
            proc = subprocess.Popen(
                cmd_str,
                cwd=str(cwd),
                env=proc_env,
                shell=True,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid if sys.platform != "win32" else None,
            )
            instance.process = proc
        except Exception as exc:
            instance.error_message = f"Process execution failed: {exc}"
            return instance

        # Poll readiness endpoint
        health_path = service_cfg.health_check
        if not health_path.startswith("/"):
            health_path = "/" + health_path
        health_url = f"{base_url}{health_path}"

        deadline = time.time() + service_cfg.health_timeout
        ready = False
        while time.time() < deadline:
            # Check if process died
            ret = proc.poll()
            if ret is not None:
                log_content = log_path.read_text(encoding="utf-8", errors="ignore")[-2000:]
                instance.error_message = f"Process terminated prematurely with code {ret}. Log:\n{log_content}"
                return instance

            try:
                req = urllib.request.Request(health_url, headers={"User-Agent": "proofpush-healthcheck"})
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    if resp.status in (200, 204, 301, 302, 304, 401, 403):
                        ready = True
                        break
            except (urllib.error.HTTPError) as http_err:
                # 401 or 403 indicates server is alive and responding
                if http_err.code in (200, 204, 301, 302, 304, 401, 403):
                    ready = True
                    break
            except Exception:
                time.sleep(0.3)

        if ready:
            instance.is_ready = True
        else:
            log_content = log_path.read_text(encoding="utf-8", errors="ignore")[-2000:]
            instance.error_message = f"Health check timed out after {service_cfg.health_timeout}s on {health_url}. Log:\n{log_content}"

        return instance

    def cleanup(self) -> None:
        """Terminate all active child processes and prune temporary worktrees."""
        for env in self.active_environments:
            for s in [env.frontend, env.backend]:
                if s:
                    if s.process and s.process.poll() is None:
                        try:
                            if sys.platform != "win32":
                                os.killpg(os.getpgid(s.process.pid), signal.SIGTERM)
                            else:
                                # shell=True: the tracked pid is cmd.exe; /T also ends the server it started
                                subprocess.run(["taskkill", "/F", "/T", "/PID", str(s.process.pid)], capture_output=True)
                            s.process.wait(timeout=2)
                        except Exception:
                            try:
                                if sys.platform != "win32":
                                    os.killpg(os.getpgid(s.process.pid), signal.SIGKILL)
                                else:
                                    s.process.kill()
                            except Exception:
                                pass
                    if s.log_handle:
                        try:
                            s.log_handle.close()
                        except Exception:
                            pass

            # Prune and remove worktree
            if env.worktree_path.exists():
                run_command(
                    ["git", "worktree", "remove", "--force", str(env.worktree_path)],
                    cwd=self.repo_path
                )
                if env.worktree_path.exists():
                    shutil.rmtree(env.worktree_path, ignore_errors=True)

        run_command(["git", "worktree", "prune"], cwd=self.repo_path)

        if self._temp_base_dir and self._temp_base_dir.exists():
            shutil.rmtree(self._temp_base_dir, ignore_errors=True)

        self.active_environments.clear()
