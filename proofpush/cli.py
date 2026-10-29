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
from typing import Optional, List

import click
from rich.console import Console
from rich.table import Table

from proofpush.config import load_config, resolve_account, session_file, AuditorConfig
from proofpush.git_interceptor import GitInterceptor, PushRefUpdate
from proofpush.task_resolver import TaskResolver
from proofpush.baseline_manager import BaselineManager, BaselineMetadata
from proofpush.environment_manager import EnvironmentManager
from proofpush.route_discoverer import RouteDiscoverer
from proofpush.fix_history import FixHistoryGuard
from proofpush import notifier, push_rules
from proofpush.browser_engine import BrowserEngine
from proofpush.visual_comparator import VisualComparator
from proofpush.unrelated_analyzer import UnrelatedAnalyzer
from proofpush.classifier import RegressionClassifier, Finding
from proofpush.ai_engine import get_ai_adapter
from proofpush.reporter import ReportGenerator, AuditReportData
from proofpush.policy_enforcer import PolicyEnforcer
from proofpush.doctor import Doctor
from proofpush.utils import detect_unattended, exclude_from_git, run_command


console = Console(soft_wrap=True)


@click.group()
def main():
    """ProofPush: Audits website functionality and visual regressions before Git push."""


def ensure_dependencies():
    """Ensure required browser automation binaries and dependencies are downloaded and verified."""
    console.print("[dim]Verifying required browser dependencies...[/dim]")
    browser_ready = False
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

    pages = 0
    if base_url and candidate_url:
        engine = BrowserEngine(headless=True)
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

    decision = PolicyEnforcer("strict", cfg.policies).evaluate(findings=findings, security_result=security_res, is_tty=False)
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
    console.print(f"[proofpush] Deployment REJECTED: {decision.reason} Live stays on {base_sha[:10]}.", markup=False)
    _notify(cfg, repo_path, f"ProofPush rejected a deployment of {branch}", decision.reason, findings, html,
            {"event": "deploy_rejected", "branch": branch, "live": base_sha, "candidate": candidate_sha})
    sys.exit(reject)


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

    failed = [f for f in findings if f.category != "UNCHANGED"]
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
                    if cfg.routes.crawl:
                        crawled = await browser_engine.crawl(base_url_p, all_routes, cfg.routes.max_pages, route_discoverer._is_excluded)
                        crawled += await browser_engine.crawl(base_url_b, all_routes, cfg.routes.max_pages, route_discoverer._is_excluded)
                        routes = list(dict.fromkeys(v.route for v in crawled))[: cfg.routes.max_pages]
                    # Audit routes
                    for r in routes:
                        routes_checked_count += 1
                        base_visit = await browser_engine.visit_page(base_url_b, r, capture_screenshot=cfg.visual.enabled)
                        prop_visit = await browser_engine.visit_page(base_url_p, r, capture_screenshot=cfg.visual.enabled)

                        vis_res = None
                        if cfg.visual.enabled and base_visit.screenshot_bytes and prop_visit.screenshot_bytes:
                            vis_res = vis_comparator.compare_images(
                                baseline_bytes=base_visit.screenshot_bytes,
                                proposed_bytes=prop_visit.screenshot_bytes,
                                route=r,
                                viewport_name="desktop",
                            )
                            visual_results.append(vis_res)

                        f = classifier.classify_route_visit(
                            route=r,
                            base_result=base_visit,
                            prop_result=prop_visit,
                            visual_result=vis_res,
                        )
                        findings.append(f)

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

                try:
                    asyncio.run(run_and_close())
                except Exception as exc:
                    findings.append(Finding(
                        category="BLOCKED",
                        target="Browser Engine",
                        before_behavior="Browser checks not run",
                        after_behavior="Browser checks not run",
                        expected_behavior="Headless Chromium launches",
                        actual_result=str(exc).splitlines()[0],
                        confidence=1.0,
                        recommended_action="Run `python3 -m playwright install chromium` (or `proofpush doctor`).",
                        is_critical=True,
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
    enforcer = PolicyEnforcer(cfg.mode, cfg.policies)
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
    if decision.exit_code != 0:
        branch = active_update.remote_ref.replace("refs/heads/", "")
        _notify(cfg, repo_path, f"ProofPush blocked a push to {branch}", decision.reason, findings,
                saved_paths["html"], {"event": "push_blocked", "branch": branch, "commit": proposed_sha})
    if decision.exit_code != 0 and detect_unattended():
        # Plain text for AI agents reading the push output
        console.print(
            f"\n[proofpush] Push blocked for {detect_unattended()}. Full report: {saved_paths['markdown']}\n"
            "AI agents: do not bypass with --no-verify or by changing ProofPush settings. Fix the findings above, "
            "or show them to the user and ask how to proceed. The user can approve one push with "
            "`PROOFPUSH_MODE=advisory git push`.",
            markup=False,
        )

    return decision.exit_code


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
    route_discoverer = RouteDiscoverer(repo_path, cfg.routes.base_routes)
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
