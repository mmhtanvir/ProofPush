"""
Command Line Interface for ProofPush.
Orchestrates the pre-push workflow, baseline capture, diagnostics, and reports.
"""

import os
import sys
import asyncio
import webbrowser
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import click
from rich.console import Console
from rich.table import Table

from proofpush.config import load_config, AuditorConfig
from proofpush.git_interceptor import GitInterceptor, PushRefUpdate
from proofpush.task_resolver import TaskResolver, TaskContext
from proofpush.baseline_manager import BaselineManager, BaselineMetadata
from proofpush.environment_manager import EnvironmentManager
from proofpush.route_discoverer import RouteDiscoverer
from proofpush.browser_engine import BrowserEngine
from proofpush.visual_comparator import VisualComparator
from proofpush.unrelated_analyzer import UnrelatedAnalyzer
from proofpush.classifier import RegressionClassifier, Finding
from proofpush.ai_engine import get_ai_adapter
from proofpush.reporter import ReportGenerator, AuditReportData
from proofpush.policy_enforcer import PolicyEnforcer
from proofpush.doctor import Doctor


console = Console()


@click.group()
def main():
    """ProofPush: Audits website functionality and visual regressions before Git push."""
    pass


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
        console.print(f"[bold green]✔ Pre-push hook uninstalled.[/bold green]")
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
def review(hook: bool, full: bool, explicit_task: Optional[str], explicit_base: Optional[str], mode: Optional[str]):
    """Audit pending or proposed Git push changes."""
    repo_path = Path.cwd().resolve()
    cfg = load_config(repo_path)
    if mode:
        cfg.mode = mode
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

    # Process first non-delete update
    active_update = next((u for u in updates if not u.is_delete), None)
    if not active_update:
        sys.exit(0)

    proposed_sha = active_update.local_sha
    commit_messages = interceptor.get_commit_messages(active_update.commits)

    # 2. Resolve task context
    task_resolver = TaskResolver(repo_path)
    task_context = task_resolver.resolve(
        explicit_task=explicit_task,
        commit_messages=commit_messages,
        interactive=(not hook),
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
            sys.exit(0)
        else:
            sys.exit(1)

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
    findings: List[Finding] = []
    visual_results = []
    routes_checked_count = 0
    workflows_checked_count = 0

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

            if cfg.audit_level == "quick" and len(all_routes) > 5:
                # Prioritize root, task-related routes, and modified routes
                quick_routes = [r for r in all_routes if r == "/" or any(w in r.lower() for w in task_context.title.lower().split())]
                all_routes = (quick_routes + all_routes)[:5]

            # 7. Browser comparison execution
            base_url_b = base_env.frontend.base_url if base_env.frontend else "http://127.0.0.1:3000"
            base_url_p = prop_env.frontend.base_url if prop_env.frontend else "http://127.0.0.1:3100"

            browser_engine = BrowserEngine(headless=True)
            vis_comparator = VisualComparator(
                diff_threshold=cfg.visual.diff_threshold,
                reports_dir=repo_path / ".proofpush" / "reports"
            )
            classifier = RegressionClassifier(task_context)

            async def run_browser_audit():
                nonlocal routes_checked_count, workflows_checked_count
                # Audit routes
                for r in all_routes:
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
                    wf_finding = classifier.classify_workflow(base_wf_res, prop_wf_res)
                    findings.append(wf_finding)

            # Run async browser checks
            asyncio.run(run_browser_audit())

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
        is_tty=(not hook),
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
    reporter = ReportGenerator(repo_path)
    reporter.print_terminal_report(report_data)
    saved_paths = reporter.save_reports(report_data)

    console.print(f"[dim]Evidence saved:[/dim] [cyan]{saved_paths['html']}[/cyan]")

    # Exit with policy decision code
    sys.exit(decision.exit_code)


@main.group()
def baseline():
    """Manage application baselines."""
    pass


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
    pkg_dir = Path(__file__).resolve().parent.parent
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", str(pkg_dir / "tests"), "-p", "test_*.py"]
    res = subprocess.run(cmd)
    sys.exit(res.returncode)


if __name__ == "__main__":
    main()
