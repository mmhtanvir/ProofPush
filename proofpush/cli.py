"""
Command Line Interface for ProofPush.
Orchestrates the pre-push workflow, baseline capture, diagnostics, and reports.
"""

from __future__ import annotations

import os
import signal
import sys
import asyncio
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List

import click
from rich.console import Console
from rich.table import Table

from proofpush import __version__
from proofpush.config import load_config, resolve_account, resolve_headers, session_file, AuditorConfig
from proofpush.git_interceptor import GitInterceptor, PushRefUpdate
from proofpush.task_resolver import TaskResolver
from proofpush.baseline_manager import BaselineManager, BaselineMetadata
from proofpush.environment_manager import EnvironmentManager
from proofpush.route_discoverer import RouteDiscoverer
from proofpush.fix_history import FixHistoryGuard
from proofpush import api_testing, notifier, push_rules, test_suite
from proofpush.browser_engine import BrowserEngine
from proofpush.visual_comparator import VisualComparator
from proofpush.unrelated_analyzer import UnrelatedAnalyzer
from proofpush.classifier import RegressionClassifier, Finding
from proofpush.ai_engine import get_ai_adapter
from proofpush.reporter import ReportGenerator, AuditReportData
from proofpush.policy_enforcer import PolicyEnforcer
from proofpush.doctor import Doctor
from proofpush.utils import detect_unattended, exclude_from_git, redact_secrets, run_command


console = Console(soft_wrap=True)


@click.group()
@click.version_option(version=__version__, prog_name="proofpush")
def main():
    """ProofPush: Audits website functionality and visual regressions before Git push."""


def ensure_dependencies():
    """Ensure required browser automation binaries and dependencies are downloaded and verified."""
    console.print("[dim]Verifying required browser dependencies...[/dim]")
    browser_ready = False
    import importlib.util
    if importlib.util.find_spec("playwright") is None:
        console.print("[yellow]Browser checks are not installed (git checks still work). For page checks run: "
                      "pip install 'proofpush[browser]'[/yellow]")
        return
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        browser_ready = True
    except Exception:
        browser_ready = False

    if not browser_ready:
        console.print("[cyan]Downloading necessary Playwright Chromium browser binaries...[/cyan]")
        try:
            import subprocess
            res = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            if res.returncode == 0:
                console.print("[bold green]✔ Chromium browser binaries downloaded successfully.[/bold green]")
            else:
                console.print("[yellow]Warning: Could not automatically download browser binaries.[/yellow]")
        except Exception as exc:
            console.print(f"[yellow]Warning: Browser download error: {exc}[/yellow]")
    else:
        console.print("[bold green]✔ Browser automation engine verified.[/bold green]")


@main.command()
@click.option("--global", "global_mode", is_flag=True, help="Install hook globally for all repositories")
@click.option("--skip-deps", is_flag=True, help="Skip downloading browser dependencies")
def install(global_mode: bool, skip_deps: bool):
    """Install the pre-push Git hook and ensure all necessary packages and browser binaries are ready."""
    if not skip_deps:
        ensure_dependencies()

    interceptor = GitInterceptor()
    res = interceptor.install_hook(global_mode=global_mode)
    if res.get("success"):
        console.print(f"[bold green]✔ Pre-push hook {res.get('action')} successfully![/bold green]")
        console.print(f"  Path: [dim]{res.get('path')}[/dim]")
        if res.get("preserved_legacy"):
            console.print("  [cyan]Notice:[/cyan] Existing pre-push hook preserved as pre-push.legacy and chained.")
    else:
        err = res.get("error", "Unknown error")
        console.print(f"[bold red]✘ Failed to install hook:[/bold red] {err}\n")
        if "Not a git repository" in err:
            console.print("[bold yellow]Tip:[/bold yellow] `proofpush install` installs a Git hook for a repository.")
            console.print("  1. Navigate to your project folder first:")
            console.print("     [cyan]cd /path/to/your/git-project[/cyan]")
            console.print("     [cyan]proofpush install[/cyan]\n")
            console.print("  2. Or enable ProofPush globally across all repositories on this machine:")
            console.print("     [cyan]proofpush install --global[/cyan]")
        sys.exit(1)


@main.command()
@click.option("--global", "global_mode", is_flag=True, help="Uninstall global hook")
def uninstall(global_mode: bool):
    """Uninstall the pre-push Git hook."""
    interceptor = GitInterceptor()
    res = interceptor.uninstall_hook(global_mode=global_mode)
    if res.get("success"):
        console.print("[bold green]✔ Pre-push hook uninstalled.[/bold green]")
        if res.get("restored_legacy"):
            console.print("  [cyan]Notice:[/cyan] Restored previous pre-push.legacy hook.")
    else:
        console.print(f"[bold red]✘ Failed to uninstall hook:[/bold red] {res.get('error')}")
        sys.exit(1)


@main.command()
def doctor():
    """Diagnose environment, dependencies, Git hooks, and configuration."""
    console.print("\n[bold]Running ProofPush diagnostics...[/bold]\n")
    doc = Doctor()
    checks = doc.run_all_checks()

    table = Table(title="ProofPush Diagnostic Results", show_lines=True)
    table.add_column("Component", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Details")

    all_passed = True
    for c in checks:
        passed = c["passed"]
        if not passed:
            all_passed = False
        status_str = "[green]✔ OK[/green]" if passed else "[red]✘ FAIL[/red]"
        table.add_row(c["name"], status_str, c["detail"])

    console.print(table)
    if all_passed:
        console.print("\n[bold green]All systems operational. ProofPush is ready to audit.[/bold green]\n")
    else:
        console.print("\n[bold yellow]Some diagnostic checks reported warnings or issues.[/bold yellow]\n")


@main.command()
@click.option("--hook", is_flag=True, help="Executed internally by Git pre-push hook via stdin")
@click.option("--full", is_flag=True, help="Run comprehensive full audit instead of quick audit")
@click.option("--task", "explicit_task", default=None, help="Explicit task description")
@click.option("--base", "explicit_base", default=None, help="Explicit baseline Git revision")
@click.option("--mode", type=click.Choice(["advisory", "interactive", "strict"]), help="Override audit policy mode")
@click.option("--history-only", is_flag=True, help="Only run git-based checks (past fixes, secrets); skip app and browser")
def review(hook: bool, full: bool, explicit_task: Optional[str], explicit_base: Optional[str], mode: Optional[str], history_only: bool):
    """Audit pending or proposed Git push changes."""
    # AI CLIs kill long-running commands on timeout; exit via SystemExit so `finally` blocks
    # still remove worktrees and stop the app servers instead of leaking them
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, lambda signum, _frame: sys.exit(128 + signum))
    repo_path = Path.cwd().resolve()
    cfg = load_config(repo_path)
    # Hooks can't take flags, so allow a one-off override: PROOFPUSH_MODE=advisory git push
    env_mode = os.environ.get("PROOFPUSH_MODE", "").lower()
    if mode or env_mode in ("advisory", "interactive", "strict"):
        cfg.mode = mode or env_mode
    if full:
        cfg.audit_level = "full"

    interceptor = GitInterceptor(repo_path)

    # 1. Parse incoming refs
    updates: List[PushRefUpdate] = []
    if hook:
        stdin_content = sys.stdin.read()
        updates = interceptor.parse_pre_push_stdin(stdin_content)
        # If no updates or all are branch deletions, allow immediately
        if not updates or all(u.is_delete for u in updates):
            sys.exit(0)
    else:
        # Direct CLI review of HEAD
        head_sha = interceptor.get_current_head_sha()
        # Find upstream or default branch
        updates = [
            PushRefUpdate(
                local_ref="HEAD",
                local_sha=head_sha,
                remote_ref="origin/main",
                remote_sha="0000000000000000000000000000000000000000",
                commits=[head_sha],
            )
        ]

    # Audit every pushed branch (tags point at commits already audited with their branch)
    to_audit = [u for u in updates if not u.is_delete and not u.remote_ref.startswith("refs/tags/")]
    if not to_audit:
        sys.exit(0)
    codes = [
        _audit_update(repo_path, cfg, interceptor, u, hook, explicit_task, explicit_base, history_only)
        for u in to_audit
    ]
    sys.exit(max(codes))


def _api_headers_for(cfg: AuditorConfig, repo_path: Path, env_name: str, cookies: Optional[list] = None) -> Dict[str, str]:
    """Credentials for API requests in one environment: api_tests.auth.<env>, else api_headers, plus login cookies."""
    headers = resolve_headers(repo_path, cfg.api_tests.auth.get(env_name) or cfg.api_headers)
    if cookies and not any(h.lower() == "cookie" for h in headers):
        headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    return headers


def _discover(cfg: AuditorConfig, roots: List[Path], app_url: Optional[str], headers: Dict[str, str],
              traffic: Optional[Dict[str, Dict[str, Any]]] = None):
    """Endpoints from the configured sources, highest confidence first; returns (endpoints, OpenAPI origin).

    Several roots (baseline + candidate code) are unioned, so an endpoint deleted by the change is still tested.
    """
    groups, origin = [], ""
    sources = cfg.api_tests.sources
    if "openapi" in sources:
        for i, root in enumerate(roots):
            spec, where = api_testing.load_openapi(root, cfg.api_tests.openapi, app_url if i == 0 else None, headers)
            if spec:
                groups.append(api_testing.endpoints_from_openapi(spec))
                origin = origin or where
    if "framework" in sources:
        for root in roots:
            groups.append(api_testing.discover_framework_endpoints(root))
    if "traffic" in sources and traffic:
        groups.append(api_testing.endpoints_from_traffic(traffic))
    endpoints = api_testing.merge_endpoints(*groups)
    inc, exc = cfg.api_tests.include, cfg.api_tests.exclude
    endpoints = [e for e in endpoints if (not inc or any(e.path.startswith(p) for p in inc))
                 and not any(e.path.startswith(p) for p in exc)]
    return endpoints, origin


def _run_api_tests(cfg: AuditorConfig, roots: List[Path], env_name: str, env_type: str, cand_url: str, cand_headers: Dict[str, str],
                   base_url: Optional[str] = None, base_headers: Optional[Dict[str, str]] = None,
                   changed_files: Optional[List[str]] = None, traffic=None, remote_db: Optional[str] = None,
                   allow_writes: Optional[bool] = None, repo_path: Optional[Path] = None) -> List[Finding]:
    """API payload tests for one environment (and a baseline to compare with, when given)."""
    opts = cfg.api_tests
    if opts.mode == "off":
        return []
    endpoints, origin = _discover(cfg, roots, base_url or cand_url, cand_headers, traffic)
    selected = api_testing.select_endpoints(endpoints, opts.mode, changed_files or [])
    cases = api_testing.cases_from_config(opts.cases)
    # Extra test identities: api_tests.auth keys "<env>:<name>" (e.g. local:other) for ownership checks
    secrets_root = repo_path or roots[-1]  # .proofpush/secrets.env lives in the working copy, not in audit checkouts
    identities = {k.split(":", 1)[1]: resolve_headers(secrets_root, v) for k, v in opts.auth.items() if k.startswith(f"{env_name}:")}
    if env_type != "production":  # generated (and invalid) requests never go to production
        cases += api_testing.generate_cases(selected, opts.max_invalid_per_endpoint, auth_configured=bool(cand_headers),
                                            other_identity=next(iter(identities), None))
    perm_cases, perm_problems = api_testing.permission_cases(opts.permissions, endpoints, list(identities))
    cases += perm_cases  # explicit rules: production still needs prod-approved cases, so these run only off production
    policy = api_testing.Policy(env_type=env_type, remote_database=remote_db,
                                allow_writes=opts.allow_writes if allow_writes is None else allow_writes)
    limits = api_testing.RunLimits(opts.max_requests, opts.rate_per_second, opts.timeout, opts.max_body_bytes)
    console.print(f"API tests ({opts.mode}, {env_type}): {len(endpoints)} endpoint(s) discovered"
                  + (f" incl. OpenAPI from {origin}" if origin else "") + f", {len(cases)} case(s)", markup=False)
    outcomes = api_testing.run_suite(cases, policy, limits, cand_url, cand_headers, base_url, base_headers,
                                     opts.normalize_fields, identities)
    findings = api_testing.to_findings(outcomes, len(endpoints), f"{env_name} ({env_type})")
    if perm_problems:
        findings.append(_blocked("Permission matrix", "; ".join(perm_problems[:5])))
    return findings


def _compare_test_suites(cfg: AuditorConfig, base_dir: Path, prop_dir: Path,
                         base_db: Optional[str] = None, prop_db: Optional[str] = None) -> List[Finding]:
    """The project's own tests on both versions (backend logic included): newly failing tests are regressions."""
    if not cfg.tests.enabled:
        return []
    command = cfg.tests.command or test_suite.detect_test_command(prop_dir)
    # Test runners create/flush databases: refuse when the app's settings point at a shared or production one
    db = api_testing.remote_database({**api_testing.read_env_files(prop_dir), **os.environ,
                                      **({"DATABASE_URL": prop_db} if prop_db else {})})
    if db and not cfg.tests.allow_remote_database:
        return [_blocked("Test suite", f"Not run: the app's environment points at a non-local database ({db}). "
                                       "Point tests at a local/disposable database, or set tests.allow_remote_database if that is safe.")]
    if not command:
        return [_blocked("Test suite", "tests.enabled is on but no test command was found; set tests.command")]
    console.print(f"Running the project's tests on both versions: {command}", markup=False)
    before = test_suite.run_suite(base_dir, command, cfg.tests.timeout, base_db)
    after = test_suite.run_suite(prop_dir, command, cfg.tests.timeout, prop_db)
    return test_suite.compare_suites(before, after)


async def _compare_clicks(engine, classifier, cfg, route, before_url, after_url, viewport, where=None) -> List[Finding]:
    """Click the same safe elements on both versions of a page; report ones that broke, vanished, slowed or got janky."""
    opts = cfg.interactions
    before, after = await asyncio.gather(
        engine.probe_interactions(before_url, route, opts.max_per_page, viewport, opts.skip),
        engine.probe_interactions(after_url, route, opts.max_per_page, viewport, opts.skip),
    )
    # Elements gone from the new version's candidate list may still exist (e.g. now disabled): probe them directly
    missing = [before[k] for k in before if k not in after]
    if missing:
        after.update(await engine.probe_interactions(after_url, route, viewport=viewport, only=missing))
    found = [classifier.classify_interaction(where or route, before[k], after.get(k)) for k in before]
    return [f for f in found if f]


def _have_commit(repo_path: Path, sha: str) -> bool:
    return run_command(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_path)[0] == 0


def _ensure_history(repo_path: Path, base: str, candidate: str) -> Optional[str]:
    """CI/CD checkouts are often shallow; fetch enough history to see the live commit and past fixes."""
    if run_command(["git", "rev-parse", "--is-shallow-repository"], cwd=repo_path)[1].strip() == "true":
        if run_command(["git", "fetch", "--quiet", "--unshallow"], cwd=repo_path, timeout=300)[0] != 0:
            run_command(["git", "fetch", "--quiet", "--deepen=1000"], cwd=repo_path, timeout=300)
    for sha in (base, candidate):
        if not _have_commit(repo_path, sha):
            run_command(["git", "fetch", "--quiet", "origin", sha], cwd=repo_path, timeout=120)
            if not _have_commit(repo_path, sha):
                return f"Commit {sha[:10]} is not available in this checkout"
    return None


@main.command()
@click.option("--base", "base_sha", default=None, help="Commit live now (default: $VERCEL_GIT_PREVIOUS_SHA)")
@click.option("--candidate", "candidate_sha", default=None, help="Commit about to deploy (default: $VERCEL_GIT_COMMIT_SHA or HEAD)")
@click.option("--base-url", default=None, help="Live site URL, to compare pages in the browser too")
@click.option("--candidate-url", default=None, help="Candidate deployment URL (e.g. a preview), to compare against --base-url")
@click.option("--env", "env_name", default="prod", help="Test account (auth.accounts.<env>) for the browser comparison")
@click.option("--vercel", is_flag=True, help="Run as Vercel's Ignored Build Step: exit 0 cancels the deployment, 1 builds it")
def gate(base_sha, candidate_sha, base_url, candidate_url, env_name, vercel):
    """Deployment gate: reject a release that brings back fixed bugs or carries a critical finding.

    Compares the commit live now with the one about to deploy (reverted past fixes, suspicious-push
    rules, secrets) and, given both URLs, the two sites page by page as the test account. Always strict
    and non-interactive. Exit 0 = deploy, 1 = reject (inverted with --vercel).
    """
    repo_path = Path.cwd().resolve()
    cfg = load_config(repo_path)
    deploy, reject = (1, 0) if vercel else (0, 1)
    if vercel and os.environ.get("VERCEL_ENV", "production") != "production":
        # Previews must build: they are what gets compared against prod before promoting
        console.print("[proofpush] Preview deployment: building (only production deployments are gated).", markup=False)
        sys.exit(deploy)

    base_sha = base_sha or os.environ.get("VERCEL_GIT_PREVIOUS_SHA")
    candidate_sha = candidate_sha or os.environ.get("VERCEL_GIT_COMMIT_SHA") or "HEAD"
    if not base_sha:
        console.print("[proofpush] No live commit to compare against (first deployment): deploying.", markup=False)
        sys.exit(deploy)
    problem = _ensure_history(repo_path, base_sha, candidate_sha)
    candidate_sha = run_command(["git", "rev-parse", candidate_sha], cwd=repo_path)[1].strip() or candidate_sha

    findings: List[Finding] = []
    security_res = None
    branch = os.environ.get("VERCEL_GIT_COMMIT_REF") or run_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)[1].strip() or "main"
    if problem:
        findings.append(Finding(category="BLOCKED", target="Release history", before_behavior="", after_behavior=problem,
                                expected_behavior="Live and candidate commits are both available", actual_result=problem,
                                confidence=1.0, recommended_action="Check out with full history (fetch-depth: 0)", is_critical=True))
        task_context = TaskResolver(repo_path).resolve(explicit_task="Deployment gate")
    else:
        commits = run_command(["git", "rev-list", f"{base_sha}..{candidate_sha}"], cwd=repo_path)[1].split()
        messages = GitInterceptor(repo_path).get_commit_messages(commits)
        task_context = TaskResolver(repo_path).resolve(commit_messages=messages)
        rewrites_history = run_command(["git", "merge-base", "--is-ancestor", base_sha, candidate_sha], cwd=repo_path)[0] != 0
        findings += FixHistoryGuard(repo_path).analyze(base_sha, candidate_sha)
        if cfg.push_rules.enabled:
            ctx = push_rules.build_context(repo_path, base_sha, candidate_sha, f"refs/heads/{branch}", messages,
                                           forced=rewrites_history, branch_created=False)
            findings += push_rules.evaluate(push_rules.load_catalog(), ctx, cfg.push_rules.enable, cfg.push_rules.disable)
        security_res = UnrelatedAnalyzer(repo_path, sensitive_globs=cfg.security.sensitive_paths).analyze_changes(
            base_sha=base_sha, proposed_sha=candidate_sha, task_context=task_context)

    if cfg.tests.enabled and not problem:
        gate_env = EnvironmentManager(repo_path, cfg.application)
        try:
            findings.extend(_compare_test_suites(cfg, gate_env.create_worktree(base_sha, "live"),
                                                 gate_env.create_worktree(candidate_sha, "candidate")))
        finally:
            gate_env.cleanup()

    pages = 0
    if base_url and candidate_url:
        engine = BrowserEngine(headless=True)
        engine.extra_headers = resolve_headers(repo_path, cfg.api_headers)
        excluded = RouteDiscoverer(repo_path, cfg.routes.base_routes, cfg.routes.excluded_routes)._is_excluded
        classifier = RegressionClassifier(task_context)

        async def compare():
            nonlocal pages
            if cfg.auth:
                creds, why = resolve_account(repo_path, cfg.auth, env_name)
                saved = session_file(repo_path, env_name)
                if not creds and not saved.is_file():
                    findings.append(_blocked(f"Test account ({env_name})", why))
                    return
                probe = (cfg.routes.base_routes or ["/"])[0]
                live_err = await engine.login(base_url, cfg.auth, creds, saved, probe)
                cand_err = await engine.login(candidate_url, cfg.auth, creds, saved, probe)
                if cand_err and not live_err:
                    findings.append(Finding(category="REGRESSION", target="Login (test account)",
                                            before_behavior="Logs in on the live site", after_behavior=cand_err,
                                            expected_behavior="Login keeps working", actual_result=cand_err,
                                            confidence=0.95, recommended_action="Fix login before deploying.", is_critical=True))
                    return
            live = {v.route: v for v in await engine.crawl(base_url, cfg.routes.base_routes, cfg.routes.max_pages, excluded)}
            cand = {v.route: v for v in await engine.crawl(candidate_url, cfg.routes.base_routes, cfg.routes.max_pages, excluded)}
            for route in list(dict.fromkeys([*live, *cand]))[: cfg.routes.max_pages]:
                pages += 1
                findings.append(classifier.classify_route_visit(route, live.get(route), cand.get(route)))
                if route in live and route in cand:
                    api_finding = classifier.classify_api(route, live[route].api_calls, cand[route].api_calls)
                    if api_finding:
                        findings.append(api_finding)
                    layout_finding = classifier.classify_layout(route, live[route].layout, cand[route].layout)
                    if layout_finding:
                        findings.append(layout_finding)
                    # Other screen sizes (mobile, tablet): layout and load speed
                    for vp in cfg.visual.viewports[1:]:
                        dims = {"width": vp.width, "height": vp.height}
                        before = await engine.visit_page(base_url, route, viewport=dims, capture_screenshot=False, discover_elements=False)
                        after = await engine.visit_page(candidate_url, route, viewport=dims, capture_screenshot=False, discover_elements=False)
                        layout_finding = classifier.classify_layout(f"{route} ({vp.name})", before.layout, after.layout)
                        if layout_finding:
                            findings.append(layout_finding)
                both_load = route in live and route in cand and live[route].status < 400 and cand[route].status < 400
                if cfg.interactions.auto_click and cfg.interactions.allow_on_live and both_load and pages <= cfg.interactions.max_pages:
                    findings.extend(await _compare_clicks(engine, classifier, cfg, route, base_url, candidate_url, None))
            for wf in cfg.workflows:
                if wf.prod:
                    findings.append(classifier.classify_workflow(await engine.execute_workflow(base_url, wf),
                                                                 await engine.execute_workflow(candidate_url, wf), guard=wf.guard))

        async def compare_and_close():
            try:
                await compare()
            finally:
                await engine.close()

        try:
            asyncio.run(compare_and_close())
        except Exception as exc:
            findings.append(_blocked("Browser Engine", str(exc).splitlines()[0]))

        if cfg.api_tests.mode != "off":
            # OpenAPI is read from the live deployment first, so endpoints the candidate dropped are still tested
            findings.extend(_run_api_tests(
                cfg, [repo_path], env_name, cfg.environment_types.get(env_name, "production"), candidate_url,
                _api_headers_for(cfg, repo_path, env_name, (engine.sessions.get(candidate_url) or {}).get("cookies")),
                base_url, _api_headers_for(cfg, repo_path, env_name, (engine.sessions.get(base_url) or {}).get("cookies")),
                changed_files=run_command(["git", "diff", "--name-only", base_sha, candidate_sha], cwd=repo_path)[1].split(),
                allow_writes=cfg.environment_writes.get(env_name, False),
            ))

    decision = PolicyEnforcer("strict", cfg.policies, cfg.security).evaluate(findings=findings, security_result=security_res, is_tty=False)
    report = AuditReportData(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), repo_path=str(repo_path),
        baseline_sha=base_sha, proposed_sha=candidate_sha, task_title=task_context.title, task_source=task_context.source,
        enforcement_mode="gate", decision="APPROVED" if decision.allowed else "REJECTED", decision_reason=decision.reason,
        routes_checked=pages, workflows_checked=sum(1 for w in cfg.workflows if w.prod and base_url and candidate_url),
        visual_checks=0, passed_count=sum(1 for f in findings if f.category == "UNCHANGED"), expected_count=0,
        regressions_count=sum(1 for f in findings if f.category == "REGRESSION"),
        not_tested_count=sum(1 for f in findings if f.category in ("NOT_TESTED", "BLOCKED")),
        findings=findings, security_result=security_res,
    )
    reporter = ReportGenerator(repo_path, cfg.report_retention)
    reporter.print_terminal_report(report)
    html = reporter.save_reports(report)["html"]
    if decision.allowed:
        console.print(f"[proofpush] Gate passed: deploying {candidate_sha[:10]}.", markup=False)
        sys.exit(deploy)
    console.print("\n" + why_blocked("WHY THIS DEPLOYMENT WAS REJECTED", decision.reason, findings, security_res,
                                     {"markdown": html.replace(".html", ".md"), "json": html.replace(".html", ".json")}), markup=False)
    console.print(f"[proofpush] Deployment REJECTED: {decision.reason} Live stays on {base_sha[:10]}.", markup=False)
    _notify(cfg, repo_path, f"ProofPush rejected a deployment of {branch}", decision.reason, findings, html,
            {"event": "deploy_rejected", "branch": branch, "live": base_sha, "candidate": candidate_sha})
    sys.exit(reject)


BLOCKING_ORDER = ["REGRESSION", "SUSPICIOUS_PUSH", "REMOVED_FUNCTIONALITY", "POSSIBLE_REGRESSION", "BLOCKED"]


def why_blocked(headline: str, reason: str, findings: List[Finding], security_res=None,
                report_paths: Optional[Dict[str, str]] = None, limit: int = 8) -> str:
    """Short plain-text list of what blocked a push/deploy: the last thing an AI agent (or a person) reads."""
    items = []
    for s in (security_res.secrets_found if security_res else []):
        items.append(("SECRET", f"{s.file_path}:{s.line_number}", f"{s.pattern_type} in outgoing code", ""))
    for t in (security_res.deleted_tests if security_res else []):
        items.append(("DELETED TEST", t, "test file removed in this push", ""))
    ranked = sorted((f for f in findings if f.category in BLOCKING_ORDER),
                    key=lambda f: (not f.is_critical, BLOCKING_ORDER.index(f.category)))
    for f in ranked:
        # BLOCKED findings keep the reason in actual_result ("Browser checks not run" alone says nothing)
        what = f.actual_result if f.category == "BLOCKED" else (f.after_behavior or f.actual_result)
        repro = next((r for r in f.reproduction_steps if r and not r.startswith(("body:", "+", "-"))), "")
        items.append((f.category.replace("_", " ") + (" (critical)" if f.is_critical and f.category != "REGRESSION" else ""),
                      f.target, what, repro))
    lines = [f"[proofpush] {headline}: {reason}"]
    for n, (kind, target, what, repro) in enumerate(items[:limit], 1):
        lines.append(f"  {n}. {kind}: {target}")
        lines.append(f"     what: {redact_secrets(' / '.join(str(what).split(chr(10))[:3]))[:300]}")
        if repro:
            lines.append(f"     reproduce: {redact_secrets(str(repro))[:200]}")
    if len(items) > limit:
        lines.append(f"  (+{len(items) - limit} more in the report)")
    if not items:
        lines.append("  (no individual finding: see the reason above)")
    if report_paths:
        lines.append(f"Full report: {report_paths.get('markdown')}  (JSON: {report_paths.get('json')})")
    return "\n".join(lines)


def _notify(cfg: AuditorConfig, repo_path: Path, title: str, reason: str, findings: List[Finding],
            report_path: str, details: dict) -> None:
    """Desktop + webhook alert for a blocked push or failed check (people may not be watching the terminal)."""
    problems = [f for f in findings if f.category not in ("UNCHANGED", "EXPECTED_CHANGE", "NEW_FUNCTIONALITY", "NOTICE")]
    repo = push_rules.repo_name(repo_path)
    lines = [f"{repo}: {reason}"] + [f"• {f.category.replace('_', ' ').lower()}: {f.target}" for f in problems[:3]]
    if len(problems) > 3:
        lines.append(f"• +{len(problems) - 3} more")
    lines.append(f"Report: {report_path}")
    notifier.notify(cfg.notify, repo_path, title, "\n".join(lines), {
        **details, "repo": repo, "reason": reason, "report": report_path,
        "findings": [{"category": f.category, "target": f.target, "critical": f.is_critical} for f in problems],
    })


def _blocked(target: str, problem: str) -> Finding:
    return Finding(
        category="BLOCKED", target=target, before_behavior="", after_behavior=problem,
        expected_behavior="Check can run", actual_result=problem, confidence=1.0, recommended_action=problem,
    )


@main.command()
@click.option("--env", "env_name", default="prod", help="Environment name from `environments:` in .proofpush.yml")
def check(env_name: str):
    """Check a live site (e.g. prod): log in with that environment's test account and visit every reachable page.

    Read-only: follows links with GET, never submits forms, skips logout/delete links, and runs only
    workflows marked `prod: true`. Exits 1 if anything is broken (usable after a deploy or on a schedule).
    """
    repo_path = Path.cwd().resolve()
    cfg = load_config(repo_path)
    base_url = cfg.environments.get(env_name)
    if not base_url:
        console.print(f"[red]No '{env_name}' environment in .proofpush.yml.[/red] Add:\n"
                      f"  environments:\n    {env_name}: https://your-site.example", markup=False)
        sys.exit(2)

    engine = BrowserEngine(headless=True)
    engine.extra_headers = resolve_headers(repo_path, cfg.api_headers)
    excluded = RouteDiscoverer(repo_path, cfg.routes.base_routes, cfg.routes.excluded_routes)._is_excluded
    findings: List[Finding] = []
    pages = 0

    async def run():
        nonlocal pages
        if cfg.auth:
            creds, problem = resolve_account(repo_path, cfg.auth, env_name)
            saved = session_file(repo_path, env_name)
            if not creds and not saved.is_file():
                findings.append(_blocked(f"Test account ({env_name})", problem))
                return
            err = await engine.login(base_url, cfg.auth, creds, saved, (cfg.routes.base_routes or ["/"])[0])
            if err:
                findings.append(Finding(
                    category="REGRESSION", target=f"Login ({env_name})", before_behavior="",
                    after_behavior=err, expected_behavior="Test account can log in", actual_result=err,
                    confidence=0.95, recommended_action="Check the live login page and the test account.", is_critical=True,
                ))
                return
        for v in await engine.crawl(base_url, cfg.routes.base_routes, cfg.routes.max_pages, excluded):
            pages += 1
            broken = v.status >= 400 or v.error_message
            noisy = v.console_errors or [n for n in v.network_failures if n.url.startswith(base_url)]
            if not broken and not noisy:
                findings.append(Finding(category="UNCHANGED", target=v.route, before_behavior="", after_behavior="OK",
                                        expected_behavior="Page loads", actual_result="OK", confidence=1.0,
                                        recommended_action="No action required."))
                continue
            detail = v.error_message or (f"HTTP {v.status}" if v.status >= 400 else "; ".join(
                (v.console_errors + [f"{n.method} {n.url} {n.error_text}" for n in v.network_failures])[:3]))
            findings.append(Finding(
                category="REGRESSION" if broken else "POSSIBLE_REGRESSION", target=v.route,
                before_behavior="", after_behavior=detail, expected_behavior="Page loads without errors",
                actual_result=detail, confidence=0.9, recommended_action=f"Open {v.url} and fix the error.",
                console_errors=v.console_errors, is_critical=bool(broken),
            ))
        for wf in cfg.workflows:
            if wf.prod:
                res = await engine.execute_workflow(base_url, wf)
                findings.append(RegressionClassifier(TaskResolver(repo_path).resolve(explicit_task=f"Live check {env_name}"))
                                .classify_workflow(res, res, guard=wf.guard))

    async def run_and_close():
        try:
            await run()
        finally:
            await engine.close()

    try:
        asyncio.run(run_and_close())
    except Exception as exc:
        findings.append(_blocked("Browser Engine", str(exc).splitlines()[0]))

    if cfg.api_tests.mode != "off":
        cookies = (engine.sessions.get(base_url) or {}).get("cookies")
        findings.extend(_run_api_tests(
            cfg, [repo_path], env_name, cfg.environment_types.get(env_name, "production"), base_url,
            _api_headers_for(cfg, repo_path, env_name, cookies), allow_writes=cfg.environment_writes.get(env_name, False),
        ))

    failed = [f for f in findings if f.category in ("REGRESSION", "POSSIBLE_REGRESSION", "REMOVED_FUNCTIONALITY", "BLOCKED")]
    report = AuditReportData(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), repo_path=str(repo_path),
        baseline_sha="live", proposed_sha=env_name, task_title=f"Live check: {env_name} ({base_url})",
        task_source="check", enforcement_mode="check", decision="FAILED" if failed else "PASSED",
        decision_reason=f"{len(failed)} problem(s) on {pages} page(s)" if failed else f"{pages} page(s) OK",
        routes_checked=pages, workflows_checked=sum(1 for w in cfg.workflows if w.prod), visual_checks=0,
        passed_count=len(findings) - len(failed), expected_count=0,
        regressions_count=sum(1 for f in failed if f.category == "REGRESSION"),
        not_tested_count=sum(1 for f in failed if f.category == "BLOCKED"), findings=findings,
    )
    reporter = ReportGenerator(repo_path, cfg.report_retention)
    reporter.print_terminal_report(report)
    html = reporter.save_reports(report)["html"]
    console.print(f"[dim]Evidence saved:[/dim] [cyan]{html}[/cyan]")
    if failed:
        _notify(cfg, repo_path, f"ProofPush: {env_name} check failed", report.decision_reason, findings, html,
                {"event": "check_failed", "environment": env_name, "url": base_url})
    sys.exit(1 if failed else 0)


@main.command()
@click.option("--env", "env_name", default="prod", help="Environment to save a session for")
@click.option("--url", default=None, help="Site URL (defaults to environments.<env> in .proofpush.yml)")
def login(env_name: str, url: Optional[str]):
    """Log in by hand once (for SMS/email codes, SSO, captchas) and save the session for checks to reuse."""
    repo_path = Path.cwd().resolve()
    cfg = load_config(repo_path)
    base = url or cfg.environments.get(env_name)
    if not base:
        console.print(f"[red]Pass --url or add environments.{env_name} to .proofpush.yml[/red]")
        sys.exit(2)
    out = session_file(repo_path, env_name)
    login_path = cfg.auth.login_path if cfg.auth else "/login"
    console.print(f"A browser window is opening at {base.rstrip('/')}{login_path}. Log in with the test account "
                  "(including any one-time code), then come back here.", markup=False)
    asyncio.run(BrowserEngine().save_session_interactively(
        f"{base.rstrip('/')}{login_path}", out, lambda: input("Press Enter once you are logged in... ")))
    exclude_from_git(repo_path)
    console.print(f"[green]✔ Session saved to {out} (kept out of git). Checks reuse it until it expires.[/green]")


def _audit_update(
    repo_path: Path,
    cfg: AuditorConfig,
    interceptor: GitInterceptor,
    active_update: PushRefUpdate,
    hook: bool,
    explicit_task: Optional[str],
    explicit_base: Optional[str],
    history_only: bool,
) -> int:
    """Audit one pushed ref and return the policy exit code."""
    proposed_sha = active_update.local_sha
    commit_messages = interceptor.get_commit_messages(active_update.commits)

    # 2. Resolve task context
    task_resolver = TaskResolver(repo_path)
    task_context = task_resolver.resolve(
        explicit_task=explicit_task,
        commit_messages=commit_messages,
        interactive=True,
    )

    # 3. Establish baseline revision
    baseline_mgr = BaselineManager(repo_path)
    baseline_sha = explicit_base or baseline_mgr.resolve_baseline_sha(
        strategy=cfg.baseline.strategy,
        proposed_sha=proposed_sha,
        remote_sha=active_update.remote_sha,
        reference_ref=cfg.baseline.reference_ref,
    )

    if not baseline_sha:
        console.print("[bold red]Error: Could not reliably establish a baseline Git revision.[/bold red]")
        console.print("Please capture a baseline first with `proofpush baseline capture` or specify `--base <commit>`.")
        if cfg.policies.fail_open:
            console.print("[yellow]Policy fail_open is true: Allowing push with warning.[/yellow]")
            return 0
        else:
            return 1

    console.print(f"[bold cyan]Auditing changes:[/bold cyan] {baseline_sha[:10]} (baseline) &rarr; {proposed_sha[:10]} (proposed)")
    console.print(f"[bold cyan]Task:[/bold cyan] {task_context.title}")

    # 4. Perform Git diff and unrelated change analysis
    unrelated_analyzer = UnrelatedAnalyzer(repo_path, sensitive_globs=cfg.security.sensitive_paths)
    security_res = unrelated_analyzer.analyze_changes(
        base_sha=baseline_sha,
        proposed_sha=proposed_sha,
        task_context=task_context,
    )

    # 5. Environment Management
    env_mgr = EnvironmentManager(repo_path, cfg.application)
    # Past fixes this push undoes (git history only, works for any project)
    findings: List[Finding] = FixHistoryGuard(repo_path).analyze(baseline_sha, proposed_sha)
    # Suspicious-push rules (Pushguard catalog): force pushes, install hooks, CI tampering, hidden code
    if cfg.push_rules.enabled:
        push_ctx = push_rules.build_context(
            repo_path, baseline_sha, proposed_sha, active_update.remote_ref, commit_messages,
            forced=active_update.is_force_push, branch_created=active_update.is_new_branch,
        )
        findings += push_rules.evaluate(push_rules.load_catalog(), push_ctx, cfg.push_rules.enable, cfg.push_rules.disable)
    visual_results = []
    routes_checked_count = 0
    workflows_checked_count = 0

    # Unconfigured repo under a global hook: git-only checks, no app or browser
    if not history_only:
        try:
            # Launch baseline environment
            console.print("Launching isolated baseline environment...")
            base_env = env_mgr.launch_environment(baseline_sha, label="baseline")

            # Launch proposed environment
            console.print("Launching isolated proposed environment...")
            prop_env = env_mgr.launch_environment(proposed_sha, label="proposed")
            findings.extend(_compare_test_suites(cfg, base_env.worktree_path, prop_env.worktree_path,
                                                 base_env.database_url, prop_env.database_url))

            if not base_env.is_ready or not prop_env.is_ready:
                err_msg = prop_env.startup_error or base_env.startup_error or "Application failed to become ready"
                console.print(f"[bold red]Environment startup error:[/bold red] {err_msg}")
                findings.append(Finding(
                    category="BLOCKED",
                    target="Application Server",
                    before_behavior="Baseline startup" if base_env.is_ready else base_env.startup_error,
                    after_behavior=prop_env.startup_error or "Failed readiness check",
                    expected_behavior="Application starts cleanly on isolated port",
                    actual_result=err_msg,
                    confidence=1.0,
                    recommended_action="Check application startup configuration in .proofpush.yml",
                    is_critical=True,
                ))
            elif not (base_env.frontend and prop_env.frontend):
                # No web app to run (no start_command, no index.html): git-only checks apply
                console.print("[dim]No web app detected; skipping browser checks.[/dim]")
            else:
                # 6. Route discovery
                route_discoverer = RouteDiscoverer(
                    repo_path=repo_path,
                    base_routes=cfg.routes.base_routes,
                    excluded_routes=cfg.routes.excluded_routes,
                    auto_discover=cfg.routes.auto_discover,
                )
                base_routes = route_discoverer.discover_source_routes(base_env.worktree_path)
                prop_routes = route_discoverer.discover_source_routes(prop_env.worktree_path)
                all_routes = sorted(list(set(base_routes) | set(prop_routes)))

                if cfg.audit_level == "quick" and not cfg.routes.crawl and len(all_routes) > 5:
                    # Prioritize root, task-related routes, and modified routes
                    quick_routes = [r for r in all_routes if r == "/" or any(w in r.lower() for w in task_context.title.lower().split())]
                    all_routes = (quick_routes + all_routes)[:5]

                # 7. Browser comparison execution
                base_url_b = base_env.frontend.base_url
                base_url_p = prop_env.frontend.base_url

                browser_engine = BrowserEngine(headless=True)
                browser_engine.extra_headers = resolve_headers(repo_path, cfg.api_headers)
                vis_comparator = VisualComparator(
                    diff_threshold=cfg.visual.diff_threshold,
                    reports_dir=repo_path / ".proofpush" / "reports"
                )
                classifier = RegressionClassifier(task_context)

                async def run_browser_audit():
                    nonlocal routes_checked_count, workflows_checked_count
                    routes = all_routes
                    # Log the local test account into both versions so pages behind login get checked too
                    if cfg.auth:
                        creds, problem = resolve_account(repo_path, cfg.auth, "local")
                        saved = session_file(repo_path, "local")
                        probe = (cfg.routes.base_routes or ["/"])[0]
                        if not creds and not saved.is_file():
                            findings.append(_blocked("Test account (local)", problem))
                        else:
                            base_err = await browser_engine.login(base_url_b, cfg.auth, creds, saved, probe)
                            prop_err = await browser_engine.login(base_url_p, cfg.auth, creds, saved, probe)
                            if prop_err and not base_err:
                                findings.append(Finding(
                                    category="REGRESSION", target="Login (test account)",
                                    before_behavior="Test account logs in on the baseline", after_behavior=prop_err,
                                    expected_behavior="Login keeps working", actual_result=prop_err, confidence=0.95,
                                    recommended_action="Fix login before pushing.", is_critical=True,
                                ))
                            elif prop_err:
                                findings.append(_blocked("Login (test account)", prop_err))
                    # Layout, speed and clicks are checked at every screen size; screenshots of the extra sizes only in full audits
                    viewports = cfg.visual.viewports or [None]
                    full_shots = cfg.audit_level != "quick"
                    shots, masks = cfg.visual.enabled, cfg.visual.excluded_selectors

                    def size(v):
                        return {"width": v.width, "height": v.height} if v else None

                    first = viewports[0] if viewports else None
                    crawled_b: dict = {}
                    crawled_p: dict = {}
                    if cfg.routes.crawl:
                        # The crawl's own visits (first viewport, with screenshots) are reused below, not repeated
                        for url, into in ((base_url_p, crawled_p), (base_url_b, crawled_b)):
                            for v in await browser_engine.crawl(url, all_routes, cfg.routes.max_pages, route_discoverer._is_excluded,
                                                                capture_screenshot=shots, viewport=size(first), mask_selectors=masks):
                                into[v.route] = v
                        routes = list(dict.fromkeys([*crawled_p, *crawled_b]))[: cfg.routes.max_pages]

                    async def visit(url, route, vp, screenshot=None):
                        return await browser_engine.visit_page(url, route, viewport=size(vp), mask_selectors=masks,
                                                               capture_screenshot=shots if screenshot is None else screenshot)

                    def at(route, vp):
                        return route if vp is first or not vp else f"{route} ({vp.name})"

                    def compare(route, before, after, vp):
                        if not (shots and before.screenshot_bytes and after.screenshot_bytes):
                            return None
                        result = vis_comparator.compare_images(
                            baseline_bytes=before.screenshot_bytes, proposed_bytes=after.screenshot_bytes,
                            route=route, viewport_name=vp.name if vp else "desktop",
                        )
                        visual_results.append(result)
                        return result

                    clicked_pages = 0
                    # Audit routes
                    for r in routes:
                        routes_checked_count += 1
                        base_visit = crawled_b.get(r) or await visit(base_url_b, r, first)
                        prop_visit = crawled_p.get(r) or await visit(base_url_p, r, first)
                        findings.append(classifier.classify_route_visit(
                            route=r, base_result=base_visit, prop_result=prop_visit,
                            visual_result=compare(r, base_visit, prop_visit, first),
                        ))
                        click_here = (cfg.interactions.auto_click and clicked_pages < cfg.interactions.max_pages
                                      and base_visit.status < 400 and prop_visit.status < 400)
                        clicked_pages += click_here
                        for vp in viewports:
                            if vp is first:
                                before, after = base_visit, prop_visit
                            else:
                                before = await visit(base_url_b, r, vp, screenshot=shots and full_shots)
                                after = await visit(base_url_p, r, vp, screenshot=shots and full_shots)
                                vis_res = compare(r, before, after, vp)
                                if vis_res and not vis_res.passed:
                                    findings.append(classifier.classify_route_visit(
                                        route=at(r, vp), base_result=before, prop_result=after, visual_result=vis_res))
                            # Backend calls behind the page (first screen size is enough: same calls at every size)
                            if vp is first:
                                observed_calls.update(before.api_calls)
                                api_finding = classifier.classify_api(r, before.api_calls, after.api_calls)
                                if api_finding:
                                    findings.append(api_finding)
                            # Responsive layout + load speed: sideways scroll, off-screen/overlapping controls, slower loads
                            layout_finding = classifier.classify_layout(at(r, vp), before.layout, after.layout)
                            if layout_finding:
                                findings.append(layout_finding)
                            # Buttons at this screen size (a mobile-only menu button can break on its own)
                            if click_here:
                                findings.extend(await _compare_clicks(browser_engine, classifier, cfg, r,
                                                                      base_url_b, base_url_p, size(vp), at(r, vp)))

                    # Audit workflows
                    for wf in cfg.workflows:
                        workflows_checked_count += 1
                        base_wf_res = await browser_engine.execute_workflow(base_url_b, wf)
                        prop_wf_res = await browser_engine.execute_workflow(base_url_p, wf)
                        wf_finding = classifier.classify_workflow(base_wf_res, prop_wf_res, guard=wf.guard)
                        findings.append(wf_finding)

                # Run async browser checks
                async def run_and_close():
                    try:
                        await run_browser_audit()
                    finally:
                        await browser_engine.close()

                observed_calls: Dict[str, Dict[str, Any]] = {}
                try:
                    asyncio.run(run_and_close())
                except Exception as exc:
                    no_extra = "browser extra" in str(exc) and cfg.api_tests.mode != "off"
                    findings.append(Finding(
                        category="NOTICE" if no_extra else "BLOCKED",
                        target="Browser Engine",
                        before_behavior="Browser checks not run",
                        after_behavior="Browser checks not run",
                        expected_behavior="Headless Chromium launches",
                        actual_result=str(exc).splitlines()[0],
                        confidence=1.0,
                        recommended_action="pip install 'proofpush[browser]' && python3 -m playwright install chromium (or `proofpush doctor`).",
                        is_critical=not no_extra,
                    ))

                # API payload tests against both running versions (independent of the browser stage)
                if cfg.api_tests.mode != "off":
                    def api_url(env):
                        return (env.backend or env.frontend).base_url
                    def cookies(url):
                        return (browser_engine.sessions.get(url) or {}).get("cookies")
                    app_env = {**api_testing.read_env_files(prop_env.worktree_path), **cfg.application.env,
                               **({"DATABASE_URL": prop_env.database_url} if prop_env.database_url else {})}
                    changed = run_command(["git", "diff", "--name-only", baseline_sha, proposed_sha], cwd=repo_path)[1].split()
                    findings.extend(_run_api_tests(
                        cfg, [base_env.worktree_path, prop_env.worktree_path], "local", "local",
                        api_url(prop_env), _api_headers_for(cfg, repo_path, "local", cookies(api_url(prop_env))),
                        api_url(base_env), _api_headers_for(cfg, repo_path, "local", cookies(api_url(base_env))),
                        changed_files=changed, traffic=observed_calls,
                        remote_db=api_testing.remote_database(app_env), repo_path=repo_path,
                    ))

        finally:
            # Ensure cleanup runs ALWAYS
            env_mgr.cleanup()

    # 8. AI synthesis (deterministic local by default)
    ai_adapter = get_ai_adapter(cfg.ai)
    ai_summary = ai_adapter.synthesize_summary(
        task_title=task_context.title,
        findings=[{"category": f.category, "target": f.target, "before_behavior": f.before_behavior, "after_behavior": f.after_behavior} for f in findings],
    )

    # 9. Compute summary metrics
    passed_cnt = sum(1 for f in findings if f.category == "UNCHANGED")
    expected_cnt = sum(1 for f in findings if f.category in ("EXPECTED_CHANGE", "NEW_FUNCTIONALITY"))
    regressions_cnt = sum(1 for f in findings if f.category == "REGRESSION")
    not_tested_cnt = sum(1 for f in findings if f.category in ("NOT_TESTED", "BLOCKED"))

    # 10. Policy Evaluation
    enforcer = PolicyEnforcer(cfg.mode, cfg.policies, cfg.security)
    decision = enforcer.evaluate(
        findings=findings,
        security_result=security_res,
        is_tty=None,
    )

    # 11. Compile Report
    report_data = AuditReportData(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        repo_path=str(repo_path),
        baseline_sha=baseline_sha,
        proposed_sha=proposed_sha,
        task_title=task_context.title,
        task_source=task_context.source,
        enforcement_mode=cfg.mode,
        decision=decision.decision_label,
        decision_reason=decision.reason,
        routes_checked=routes_checked_count,
        workflows_checked=workflows_checked_count,
        visual_checks=len(visual_results),
        passed_count=passed_cnt,
        expected_count=expected_cnt,
        regressions_count=regressions_cnt,
        not_tested_count=not_tested_cnt,
        findings=findings,
        visual_results=visual_results,
        security_result=security_res,
        ai_summary=ai_summary,
    )

    # Print and Save
    reporter = ReportGenerator(repo_path, cfg.report_retention)
    reporter.print_terminal_report(report_data)
    saved_paths = reporter.save_reports(report_data)

    console.print(f"[dim]Evidence saved:[/dim] [cyan]{saved_paths['html']}[/cyan]")
    if decision.allowed and not any(f.is_critical or f.category == "REGRESSION" for f in findings):
        baseline_mgr.record_passed_audit(proposed_sha)
    if decision.exit_code != 0:
        branch = active_update.remote_ref.replace("refs/heads/", "")
        _notify(cfg, repo_path, f"ProofPush blocked a push to {branch}", decision.reason, findings,
                saved_paths["html"], {"event": "push_blocked", "branch": branch, "commit": proposed_sha})
    if decision.exit_code != 0:
        # Plain-text reasons at the very end of the output, where AI CLIs (and people) look
        console.print("\n" + why_blocked("WHY THIS PUSH WAS BLOCKED", decision.reason, findings, security_res, saved_paths),
                      markup=False)
    if decision.exit_code != 0 and detect_unattended():
        console.print(
            f"[proofpush] Push blocked for {detect_unattended()}.\n"
            "AI agents: do not bypass with --no-verify or by changing ProofPush settings. Fix the items listed above, "
            "or show them to the user and ask how to proceed. The user can approve one push with "
            "`PROOFPUSH_MODE=advisory git push`.",
            markup=False,
        )

    return decision.exit_code


@main.group()
def api():
    """API endpoint discovery and payload testing (configured under api_tests in .proofpush.yml)."""


@api.command("discover")
@click.option("--url", default=None, help="Running app to read the OpenAPI schema from (e.g. http://localhost:8000)")
@click.option("--env", "env_name", default="local", help="Credentials (api_tests.auth.<env> / api_headers) for --url")
def api_discover(url: Optional[str], env_name: str):
    """List the endpoints ProofPush can find, where each came from, and how confident that is."""
    repo_path = Path.cwd().resolve()
    cfg = load_config(repo_path)
    endpoints, origin = _discover(cfg, [repo_path], url, _api_headers_for(cfg, repo_path, env_name))
    table = Table(title=f"{len(endpoints)} endpoint(s)" + (f" (OpenAPI: {origin})" if origin else " (no OpenAPI schema found)"))
    for col in ("Method", "Path", "Source", "Confidence", "Body schema", "Auth"):
        table.add_column(col)
    for e in sorted(endpoints, key=lambda e: (e.path, e.method)):
        table.add_row(e.method, e.path, e.source + (f" ({e.source_file})" if e.source_file else ""), f"{e.confidence:.0%}",
                      "yes" if e.body_schema else "-", {True: "required", False: "public"}.get(e.requires_auth, "unknown"))
    console.print(table)
    if not origin:
        console.print("Without an OpenAPI schema, request bodies can't be generated: only GET routes and your api_tests.cases "
                      "are fully testable. FastAPI serves one at /openapi.json; for DRF add drf-spectacular.", markup=False)


@main.group()
def baseline():
    """Manage application baselines."""


@baseline.command(name="capture")
@click.option("--name", default="default", help="Descriptive name for the captured baseline")
def baseline_capture_cmd(name: str):
    """Capture and validate a baseline for the current Git HEAD revision."""
    _execute_baseline_capture(name)


@baseline.command(name="list")
def baseline_list_cmd():
    """List locally stored baselines."""
    _execute_baseline_list()


@main.command(name="baseline-capture", hidden=True)
@click.option("--name", default="default", help="Descriptive name for the captured baseline")
def baseline_capture_alias(name: str):
    _execute_baseline_capture(name)


@main.command(name="baseline-list", hidden=True)
def baseline_list_alias():
    _execute_baseline_list()


def _execute_baseline_capture(name: str):
    repo_path = Path.cwd().resolve()
    interceptor = GitInterceptor(repo_path)
    head_sha = interceptor.get_current_head_sha()

    cfg = load_config(repo_path)
    route_discoverer = RouteDiscoverer(repo_path, cfg.routes.base_routes, cfg.routes.excluded_routes, cfg.routes.auto_discover)
    routes = route_discoverer.discover_source_routes()

    bm = BaselineManager(repo_path)
    meta = BaselineMetadata(
        repo_id=bm.repo_id,
        repo_path=str(repo_path),
        git_revision=head_sha,
        created_at=datetime.now().isoformat(),
        passed=True,
        name=name,
        routes=routes,
        startup_command=cfg.application.start_command,
        app_type=cfg.application.type,
    )
    dest = bm.save_baseline(meta)
    console.print(f"[bold green]✔ Baseline captured successfully for commit {head_sha[:10]}[/bold green]")
    console.print(f"  Saved metadata to: [dim]{dest}[/dim]")


def _execute_baseline_list():
    repo_path = Path.cwd().resolve()
    bm = BaselineManager(repo_path)
    baselines = bm.list_baselines()

    if not baselines:
        console.print("[yellow]No baselines stored for this repository yet.[/yellow]")
        return

    table = Table(title=f"Stored Baselines ({bm.repo_id})", show_lines=True)
    table.add_column("Commit", style="cyan")
    table.add_column("Name")
    table.add_column("Created At")
    table.add_column("Passed", justify="center")
    table.add_column("Routes")

    for b in baselines:
        p_str = "[green]✔[/green]" if b.passed else "[red]✘[/red]"
        table.add_row(b.git_revision[:10], b.name, b.created_at[:19], p_str, str(len(b.routes)))

    console.print(table)


@main.command()
@click.option("--json", "json_format", is_flag=True, help="Print raw JSON report")
@click.option("--open", "open_browser", is_flag=True, help="Open HTML report in default browser")
def report(json_format: bool, open_browser: bool):
    """View the latest audit report."""
    repo_path = Path.cwd().resolve()
    reports_dir = repo_path / ".proofpush" / "reports"
    latest_html = reports_dir / "latest.html"

    if not latest_html.is_file():
        console.print("[yellow]No reports found. Run `proofpush review` first.[/yellow]")
        return

    if open_browser:
        console.print(f"Opening report: {latest_html}")
        webbrowser.open(f"file://{latest_html}")
    elif json_format:
        # Find latest json file
        json_files = sorted(reports_dir.glob("report_*.json"), key=os.path.getmtime, reverse=True)
        if json_files:
            console.print(json_files[0].read_text(encoding="utf-8"))
        else:
            console.print("[yellow]No JSON report found.[/yellow]")
    else:
        # Find latest markdown file
        md_files = sorted(reports_dir.glob("report_*.md"), key=os.path.getmtime, reverse=True)
        if md_files:
            console.print(md_files[0].read_text(encoding="utf-8"))
        else:
            console.print(f"Report available at: {latest_html}")


@main.command()
def config():
    """Inspect effective configuration."""
    repo_path = Path.cwd().resolve()
    cfg = load_config(repo_path)
    import yaml
    from dataclasses import asdict
    console.print(yaml.dump(asdict(cfg), default_flow_style=False))


@main.command()
def test():
    """Run the auditor's automated test suite."""
    console.print("[bold]Running automated test suite...[/bold]")
    import subprocess
    tests_dir = Path(__file__).resolve().parent.parent / "tests"
    if not tests_dir.is_dir():
        console.print("[yellow]Test suite is not bundled with installed packages. Run `proofpush test` from a source checkout (pip install -e .).[/yellow]")
        sys.exit(1)
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", str(tests_dir), "-p", "test_*.py"]
    res = subprocess.run(cmd)
    sys.exit(res.returncode)


if __name__ == "__main__":
    main()
