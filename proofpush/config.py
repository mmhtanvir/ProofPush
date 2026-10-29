"""
Configuration management for ProofPush.
Loads .proofpush.yml / .proofpush.yaml or falls back to sane defaults.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import yaml

from proofpush.utils import exclude_from_git


@dataclass
class ViewportConfig:
    width: int = 1280
    height: int = 800
    name: str = "desktop"


@dataclass
class VisualConfig:
    enabled: bool = True
    viewports: List[ViewportConfig] = field(default_factory=lambda: [
        ViewportConfig(1280, 800, "desktop"),
        ViewportConfig(375, 667, "mobile"),
    ])
    diff_threshold: float = 0.02
    excluded_selectors: List[str] = field(default_factory=lambda: [
        ".timestamp", ".dynamic-id", ".ad-banner", "iframe", "[data-dynamic]"
    ])


@dataclass
class WorkflowStep:
    action: str  # navigate, click, fill, select, wait_for, assert_text, assert_url
    selector: Optional[str] = None
    value: Optional[str] = None
    expected: Optional[str] = None
    optional: bool = False


@dataclass
class WorkflowConfig:
    name: str
    path: str = "/"
    steps: List[WorkflowStep] = field(default_factory=list)
    guard: bool = False  # reproduces a previously fixed bug: any failure blocks, whatever the baseline did
    prod: bool = False  # safe to run against a live environment (`proofpush check`); off by default


@dataclass
class ServiceConfig:
    start_command: str = ""
    port: Optional[int] = None
    health_check: str = "/"
    health_timeout: int = 30
    env: Dict[str, str] = field(default_factory=dict)


@dataclass
class ApplicationConfig:
    type: str = "custom"  # custom, static, nextjs, django, fastapi, node
    start_command: str = ""
    port: Optional[int] = None
    health_check: str = "/"
    health_timeout: int = 30
    env: Dict[str, str] = field(default_factory=dict)
    backend: Optional[ServiceConfig] = None
    # "postgres": a disposable PostgreSQL container per version (needs Docker/Podman); its URL becomes DATABASE_URL
    # "none": don't set DATABASE_URL, the app uses its own setting (e.g. an async driver URL from .env)
    # default "": DATABASE_URL=sqlite:///<file> per version (isolates apps that read DATABASE_URL)
    database: str = ""
    database_image: str = "postgres:16-alpine"


@dataclass
class BaselineConfig:
    strategy: str = "merge-base"  # merge-base, captured, commit, latest-passed
    reference_ref: Optional[str] = None  # e.g. origin/main


@dataclass
class RoutesConfig:
    auto_discover: bool = True
    base_routes: List[str] = field(default_factory=lambda: ["/"])
    excluded_routes: List[str] = field(default_factory=list)
    crawl: bool = False  # follow links to check every reachable page, not just base/source routes
    max_pages: int = 50


@dataclass
class AccountConfig:
    # Names of environment variables (or keys in .proofpush/secrets.env) holding the credentials, never the values
    username_env: str = ""
    password_env: str = ""
    otp_secret_env: str = ""  # authenticator-app (TOTP) secret, for logins that ask for a one-time code


@dataclass
class Credentials:
    username: str
    password: str
    otp_secret: Optional[str] = None


@dataclass
class AuthConfig:
    login_path: str = "/login"
    # Optional; {username} and {password} are substituted. Without steps, the first email/username and password fields are filled
    steps: List[WorkflowStep] = field(default_factory=list)
    accounts: Dict[str, AccountConfig] = field(default_factory=dict)  # per environment: local, prod, ...


@dataclass
class SecurityConfig:
    sensitive_paths: List[str] = field(default_factory=lambda: [
        ".env*", "**/*.pem", "**/*.key", "**/secrets.*", "credentials.json"
    ])
    fail_on_secret: bool = True
    fail_on_unrelated: bool = False


@dataclass
class AIConfig:
    provider: str = "local"  # local, openai
    model: Optional[str] = None


@dataclass
class PoliciesConfig:
    fail_open: bool = False
    interactive_fallback: str = "block"  # block or allow


@dataclass
class PushRulesConfig:
    enabled: bool = True
    enable: List[str] = field(default_factory=list)  # turn on rules that are off by default
    disable: List[str] = field(default_factory=list)


@dataclass
class InteractionsConfig:
    # Click every safe button/toggle/tab on each checked page in both versions and compare what happens.
    # Opt-in: a local app may talk to a real backend, and a click can change data there.
    auto_click: bool = False
    max_per_page: int = 8
    max_pages: int = 10  # pages whose buttons get clicked per audit (each click takes ~0.5-1 s per version)
    allow_on_live: bool = False  # also click on live sites (`check`, `gate --base-url`)
    skip: List[str] = field(default_factory=list)  # extra words that mark a button as unsafe to click


@dataclass
class TestsConfig:
    # Run the project's own tests on both versions; tests that passed before and fail now are regressions.
    # Opt-in: some test runners (e.g. Django) create databases on whatever server the app is configured for.
    enabled: bool = False
    command: str = ""  # default: detected (npm test, pytest, manage.py test, go test, cargo test)
    timeout: int = 600
    allow_remote_database: bool = False  # run even if the app's env points at a non-local database


@dataclass
class ApiTestsConfig:
    """Endpoint discovery + generated/explicit requests sent to baseline and candidate (see api_testing.py)."""
    mode: str = "off"  # off | smoke | affected | full
    openapi: str = ""  # file in the repo, URL path on the app (/openapi.json) or full URL; default: auto
    sources: List[str] = field(default_factory=lambda: ["openapi", "framework", "traffic"])
    include: List[str] = field(default_factory=list)  # path prefixes to test (empty: all)
    exclude: List[str] = field(default_factory=list)  # path prefixes never to test
    cases: List[Dict[str, Any]] = field(default_factory=list)  # explicit requests + expectations
    allow_writes: bool = False  # POST/PUT/PATCH/DELETE on local/staging (production: see environments)
    normalize_fields: List[str] = field(default_factory=list)  # field names whose values may differ (ids, tokens)
    max_requests: int = 200
    rate_per_second: float = 10.0
    timeout: float = 10.0
    max_body_bytes: int = 1_000_000
    max_invalid_per_endpoint: int = 4
    auth: Dict[str, Dict[str, str]] = field(default_factory=dict)  # environment -> headers ({NAME} from secrets)
    permissions: List[Dict[str, Any]] = field(default_factory=list)  # [{path, GET: [owner, admin], DELETE: [owner]}]


@dataclass
class NotifyConfig:
    desktop: bool = True  # OS notification when nobody is watching a terminal (GUI/agent pushes, scheduled checks)
    webhook_env: str = ""  # name of the env var / secrets.env key holding a Slack, Discord or generic webhook URL


@dataclass
class AuditorConfig:
    mode: str = "interactive"  # advisory, interactive, strict
    audit_level: str = "quick"  # quick, full
    baseline: BaselineConfig = field(default_factory=BaselineConfig)
    application: ApplicationConfig = field(default_factory=ApplicationConfig)
    routes: RoutesConfig = field(default_factory=RoutesConfig)
    workflows: List[WorkflowConfig] = field(default_factory=list)
    visual: VisualConfig = field(default_factory=VisualConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    policies: PoliciesConfig = field(default_factory=PoliciesConfig)
    push_rules: PushRulesConfig = field(default_factory=PushRulesConfig)
    auth: Optional[AuthConfig] = None
    environments: Dict[str, str] = field(default_factory=dict)  # name -> live base URL, e.g. prod
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    interactions: InteractionsConfig = field(default_factory=InteractionsConfig)
    # Headers sent with every request, e.g. {"Authorization": "Bearer {API_TOKEN}"}; {NAME} is read like other secrets
    api_headers: Dict[str, str] = field(default_factory=dict)
    tests: TestsConfig = field(default_factory=TestsConfig)
    api_tests: ApiTestsConfig = field(default_factory=ApiTestsConfig)
    # Per environment: local | staging | production (default production: most conservative) and writes allowed
    environment_types: Dict[str, str] = field(default_factory=dict)
    environment_writes: Dict[str, bool] = field(default_factory=dict)
    report_retention: int = 30


def _parse_steps(raw_steps) -> List[WorkflowStep]:
    return [
        WorkflowStep(
            action=s["action"],
            selector=s.get("selector"),
            value=s.get("value"),
            expected=s.get("expected"),
            optional=s.get("optional", False),
        )
        for s in raw_steps or [] if isinstance(s, dict) and "action" in s
    ]


def secret_value(repo_path: Path, name: str) -> Optional[str]:
    """A secret by name: environment variable first, then .proofpush/secrets.env (kept out of git)."""
    if not name:
        return None
    if os.environ.get(name):
        return os.environ[name]
    secrets_file = repo_path / ".proofpush" / "secrets.env"
    if not secrets_file.is_file():
        return None
    exclude_from_git(repo_path)
    if sys.platform != "win32" and secrets_file.stat().st_mode & 0o077:
        secrets_file.chmod(0o600)  # holds passwords and webhook URLs: owner-only
    for line in secrets_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == name:
            return value.strip().strip("'\"") or None
    return None


def resolve_headers(repo_path: Path, headers: Dict[str, str]) -> Dict[str, str]:
    """api_headers with {NAME} placeholders filled from the environment / .proofpush/secrets.env."""
    import re
    return {k: re.sub(r"\{([A-Z0-9_]+)\}", lambda m: secret_value(repo_path, m.group(1)) or "", v) for k, v in headers.items()}


def session_file(repo_path: Path, env_name: str) -> Path:
    """Browser session saved by `proofpush login` (for SMS/email codes, SSO, captchas)."""
    return repo_path / ".proofpush" / f"session-{env_name}.json"


def resolve_account(repo_path: Path, auth: AuthConfig, env_name: str) -> Tuple[Optional[Credentials], str]:
    """(credentials, "") for the environment's test account, or (None, what to set)."""
    acc = auth.accounts.get(env_name)
    if not acc or not acc.username_env or not acc.password_env:
        return None, f"auth.accounts.{env_name} needs username_env and password_env in .proofpush.yml"

    def lookup(name: str) -> Optional[str]:
        return secret_value(repo_path, name)

    user, password = lookup(acc.username_env), lookup(acc.password_env)
    if not user or not password:
        return None, (f"Set {acc.username_env} and {acc.password_env} in your environment or in "
                      f".proofpush/secrets.env (kept out of git) for the '{env_name}' test account, "
                      f"or run `proofpush login --env {env_name}` to save a session")
    return Credentials(user, password, lookup(acc.otp_secret_env)), ""


def find_config_file(repo_path: Path) -> Optional[Path]:
    """Return the first config file present (.proofpush.yml or .proofpush.yaml)."""
    for name in (".proofpush.yml", ".proofpush.yaml"):
        if (repo_path / name).is_file():
            return repo_path / name
    return None


def load_config(repo_path: Optional[Path] = None) -> AuditorConfig:
    """Load configuration from .proofpush.yml in repo_path, or return default config."""
    config = AuditorConfig()
    if not repo_path:
        return config

    found_file = find_config_file(repo_path)

    if not found_file:
        return config

    try:
        with open(found_file, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as exc:
        print(f"Warning: Failed to parse configuration file {found_file}: {exc}")
        return config

    # Parse raw dictionary into AuditorConfig
    if "mode" in raw:
        config.mode = str(raw["mode"]).lower()
    if "audit_level" in raw:
        config.audit_level = str(raw["audit_level"]).lower()

    if "baseline" in raw and isinstance(raw["baseline"], dict):
        b = raw["baseline"]
        config.baseline.strategy = b.get("strategy", config.baseline.strategy)
        config.baseline.reference_ref = b.get("reference_ref", config.baseline.reference_ref)

    if "application" in raw and isinstance(raw["application"], dict):
        a = raw["application"]
        config.application.type = a.get("type", config.application.type)
        config.application.start_command = a.get("start_command", config.application.start_command)
        config.application.port = a.get("port", config.application.port)
        config.application.health_check = a.get("health_check", config.application.health_check)
        config.application.health_timeout = int(a.get("health_timeout", config.application.health_timeout))
        config.application.env = a.get("env", {})
        db = a.get("database")
        if isinstance(db, dict):
            config.application.database = str(db.get("engine", "")).lower()
            config.application.database_image = str(db.get("image", config.application.database_image))
        elif db:
            config.application.database = str(db).lower()
        if "backend" in a and isinstance(a["backend"], dict):
            bk = a["backend"]
            config.application.backend = ServiceConfig(
                start_command=bk.get("start_command", ""),
                port=bk.get("port"),
                health_check=bk.get("health_check", "/"),
                health_timeout=int(bk.get("health_timeout", 30)),
                env=bk.get("env", {}),
            )

    if "routes" in raw and isinstance(raw["routes"], dict):
        r = raw["routes"]
        config.routes.auto_discover = r.get("auto_discover", config.routes.auto_discover)
        config.routes.base_routes = r.get("base_routes", config.routes.base_routes)
        config.routes.excluded_routes = r.get("excluded_routes", config.routes.excluded_routes)
        config.routes.crawl = bool(r.get("crawl", config.routes.crawl))
        config.routes.max_pages = int(r.get("max_pages", config.routes.max_pages))

    if "workflows" in raw and isinstance(raw["workflows"], list):
        parsed_workflows = []
        for w in raw["workflows"]:
            if not isinstance(w, dict) or "name" not in w:
                continue
            steps = _parse_steps(w.get("steps", []))
            parsed_workflows.append(WorkflowConfig(
                name=w["name"],
                path=w.get("path", "/"),
                steps=steps,
                guard=bool(w.get("guard", False)),
                prod=bool(w.get("prod", False)),
            ))
        config.workflows = parsed_workflows

    if "visual" in raw and isinstance(raw["visual"], dict):
        v = raw["visual"]
        config.visual.enabled = v.get("enabled", config.visual.enabled)
        config.visual.diff_threshold = float(v.get("diff_threshold", config.visual.diff_threshold))
        config.visual.excluded_selectors = v.get("excluded_selectors", config.visual.excluded_selectors)
        if "viewports" in v and isinstance(v["viewports"], list):
            vps = []
            for vp in v["viewports"]:
                if isinstance(vp, dict) and "width" in vp and "height" in vp:
                    vps.append(ViewportConfig(
                        width=int(vp["width"]),
                        height=int(vp["height"]),
                        name=vp.get("name", f"{vp['width']}x{vp['height']}")
                    ))
            if vps:
                config.visual.viewports = vps

    if "security" in raw and isinstance(raw["security"], dict):
        s = raw["security"]
        config.security.sensitive_paths = s.get("sensitive_paths", config.security.sensitive_paths)
        config.security.fail_on_secret = s.get("fail_on_secret", config.security.fail_on_secret)
        config.security.fail_on_unrelated = s.get("fail_on_unrelated", config.security.fail_on_unrelated)

    if "ai" in raw and isinstance(raw["ai"], dict):
        ai = raw["ai"]
        config.ai.provider = ai.get("provider", config.ai.provider)
        config.ai.model = ai.get("model", config.ai.model)

    if "policies" in raw and isinstance(raw["policies"], dict):
        p = raw["policies"]
        config.policies.fail_open = p.get("fail_open", config.policies.fail_open)
        config.policies.interactive_fallback = p.get("interactive_fallback", config.policies.interactive_fallback)

    if isinstance(raw.get("auth"), dict):
        a = raw["auth"]
        config.auth = AuthConfig(
            login_path=a.get("login_path", "/login"),
            steps=_parse_steps(a.get("steps", [])),
            accounts={
                name: AccountConfig(
                    username_env=acc.get("username_env", ""), password_env=acc.get("password_env", ""),
                    otp_secret_env=acc.get("otp_secret_env", ""),
                )
                for name, acc in (a.get("accounts") or {}).items() if isinstance(acc, dict)
            },
        )

    if isinstance(raw.get("environments"), dict):
        config.environments = {
            name: (env.get("url", "") if isinstance(env, dict) else str(env))
            for name, env in raw["environments"].items()
        }
        for name, env in raw["environments"].items():
            env_type = str(env.get("type", "production")).lower() if isinstance(env, dict) else "production"
            config.environment_types[name] = env_type if env_type in ("local", "staging", "production") else "production"
            # allow_writes on a live environment = an isolated test tenant whose data may change
            config.environment_writes[name] = bool(env.get("allow_writes", False)) if isinstance(env, dict) else False

    if isinstance(raw.get("api_tests"), dict):
        a = raw["api_tests"]
        d = ApiTestsConfig()
        config.api_tests = ApiTestsConfig(
            mode=str(a.get("mode", d.mode)).lower(), openapi=str(a.get("openapi") or ""),
            sources=list(a.get("sources") or d.sources), include=list(a.get("include") or []),
            exclude=list(a.get("exclude") or []), cases=[c for c in a.get("cases") or [] if isinstance(c, dict)],
            allow_writes=bool(a.get("allow_writes", False)), normalize_fields=list(a.get("normalize_fields") or []),
            max_requests=int(a.get("max_requests", d.max_requests)), rate_per_second=float(a.get("rate_per_second", d.rate_per_second)),
            timeout=float(a.get("timeout", d.timeout)), max_body_bytes=int(a.get("max_body_bytes", d.max_body_bytes)),
            max_invalid_per_endpoint=int(a.get("max_invalid_per_endpoint", d.max_invalid_per_endpoint)),
            auth={str(k): {str(h): str(v) for h, v in (hv or {}).items()} for k, hv in (a.get("auth") or {}).items()},
            permissions=[r for r in a.get("permissions") or [] if isinstance(r, dict)],
        )

    if isinstance(raw.get("tests"), dict):
        t = raw["tests"]
        config.tests = TestsConfig(enabled=bool(t.get("enabled", False)), command=str(t.get("command") or ""),
                                   timeout=int(t.get("timeout", 600)),
                                   allow_remote_database=bool(t.get("allow_remote_database", False)))

    if isinstance(raw.get("api_headers"), dict):
        config.api_headers = {str(k): str(v) for k, v in raw["api_headers"].items()}

    if isinstance(raw.get("interactions"), dict):
        i = raw["interactions"]
        config.interactions = InteractionsConfig(
            auto_click=bool(i.get("auto_click", False)), max_per_page=int(i.get("max_per_page", 8)),
            max_pages=int(i.get("max_pages", 10)),
            allow_on_live=bool(i.get("allow_on_live", False)), skip=[str(w) for w in i.get("skip") or []],
        )

    if isinstance(raw.get("notify"), dict):
        n = raw["notify"]
        config.notify = NotifyConfig(desktop=bool(n.get("desktop", True)), webhook_env=n.get("webhook_env", "") or "")

    if "push_rules" in raw and isinstance(raw["push_rules"], dict):
        pr = raw["push_rules"]
        config.push_rules.enabled = bool(pr.get("enabled", True))
        config.push_rules.enable = list(pr.get("enable") or [])
        config.push_rules.disable = list(pr.get("disable") or [])

    if "report_retention" in raw:
        config.report_retention = int(raw["report_retention"])

    return config
