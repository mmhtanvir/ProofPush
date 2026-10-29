"""
Configuration management for ProofPush.
Loads .proofpush.yml (or legacy .change-auditor.yml) or falls back to sane defaults.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Any, List, Optional
import yaml


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


@dataclass
class BaselineConfig:
    strategy: str = "merge-base"  # merge-base, captured, commit, latest-passed
    reference_ref: Optional[str] = None  # e.g. origin/main


@dataclass
class RoutesConfig:
    auto_discover: bool = True
    base_routes: List[str] = field(default_factory=lambda: ["/"])
    excluded_routes: List[str] = field(default_factory=list)


@dataclass
class SecurityConfig:
    sensitive_paths: List[str] = field(default_factory=lambda: [
        ".env*", "**/*.pem", "**/*.key", "**/secrets.*", "credentials.json"
    ])
    fail_on_secret: bool = True
    fail_on_unrelated: bool = False


@dataclass
class AIConfig:
    provider: str = "local"  # local, openai, anthropic
    model: Optional[str] = None
    redact_before_send: bool = True


@dataclass
class PoliciesConfig:
    fail_open: bool = False
    interactive_fallback: str = "block"  # block or allow


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
    report_retention: int = 30


def find_config_file(repo_path: Path) -> Optional[Path]:
    """Return the first config file present (.proofpush.yml preferred, legacy .change-auditor.yml accepted)."""
    for name in (".proofpush.yml", ".proofpush.yaml", ".change-auditor.yml", ".change-auditor.yaml"):
        if (repo_path / name).is_file():
            return repo_path / name
    return None


def load_config(repo_path: Optional[Path] = None) -> AuditorConfig:
    """Load configuration from .proofpush.yml or .change-auditor.yml in repo_path, or return default config."""
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

    if "workflows" in raw and isinstance(raw["workflows"], list):
        parsed_workflows = []
        for w in raw["workflows"]:
            if not isinstance(w, dict) or "name" not in w:
                continue
            steps = []
            for s in w.get("steps", []):
                if isinstance(s, dict) and "action" in s:
                    steps.append(WorkflowStep(
                        action=s["action"],
                        selector=s.get("selector"),
                        value=s.get("value"),
                        expected=s.get("expected"),
                        optional=s.get("optional", False),
                    ))
            parsed_workflows.append(WorkflowConfig(
                name=w["name"],
                path=w.get("path", "/"),
                steps=steps,
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
        config.ai.redact_before_send = ai.get("redact_before_send", config.ai.redact_before_send)

    if "policies" in raw and isinstance(raw["policies"], dict):
        p = raw["policies"]
        config.policies.fail_open = p.get("fail_open", config.policies.fail_open)
        config.policies.interactive_fallback = p.get("interactive_fallback", config.policies.interactive_fallback)

    if "report_retention" in raw:
        config.report_retention = int(raw["report_retention"])

    return config
