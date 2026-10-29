"""
Report Generator and Evidence Viewer.
Generates:
1. Terminal report (using Rich tables, colors, and panels)
2. Markdown summary report (.md)
3. Structured JSON report (.json)
4. Interactive standalone HTML Evidence Viewer (.html)
"""

import os
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Dict, Any, Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from change_auditor.task_resolver import TaskContext
from change_auditor.classifier import Finding
from change_auditor.visual_comparator import VisualComparisonResult
from change_auditor.unrelated_analyzer import SecurityAuditResult


@dataclass
class AuditReportData:
    timestamp: str
    repo_path: str
    baseline_sha: str
    proposed_sha: str
    task_title: str
    task_source: str
    enforcement_mode: str
    decision: str  # ALLOWED or BLOCKED
    decision_reason: str
    routes_checked: int = 0
    workflows_checked: int = 0
    visual_checks: int = 0
    passed_count: int = 0
    expected_count: int = 0
    regressions_count: int = 0
    not_tested_count: int = 0
    findings: List[Finding] = field(default_factory=list)
    visual_results: List[VisualComparisonResult] = field(default_factory=list)
    security_result: Optional[SecurityAuditResult] = None
    ai_summary: str = ""


class ReportGenerator:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()
        self.reports_dir = self.repo_path / ".proofpush" / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.console = Console()

    def print_terminal_report(self, data: AuditReportData) -> None:
        """Render terminal report matching specification."""
        print("\n" + "=" * 60)
        print("                         PROOFPUSH")
        print("              Local Website Regression Auditor")
        print("=" * 60)
        print(f"Repository:        {data.repo_path}")
        print(f"Task:              {data.task_title}")
        print(f"Baseline:          {data.baseline_sha[:10]}")
        print(f"Proposed revision: {data.proposed_sha[:10]}")
        print(f"Mode:              {data.enforcement_mode.upper()}")
        print("-" * 60)
        print(f"ROUTES CHECKED:       {data.routes_checked}")
        print(f"FUNCTIONAL WORKFLOWS: {data.workflows_checked}")
        print(f"VISUAL COMPARISONS:   {data.visual_checks}")
        print("-" * 60)
        print("RESULTS:")
        print(f"  Passed:             {data.passed_count}")
        print(f"  Expected changes:   {data.expected_count}")
        print(f"  Regressions:        {data.regressions_count}")
        print(f"  Not tested:         {data.not_tested_count}")

        # Print Regressions
        regressions = [f for f in data.findings if f.category == "REGRESSION"]
        if regressions:
            for r in regressions:
                print("\n" + "-" * 60)
                print("REGRESSION")
                print(f"Target: {r.target}")
                print(f"Before:\n{r.before_behavior}")
                print(f"After:\n{r.after_behavior}")
                print("Evidence:")
                if r.console_errors:
                    print(f"- Console errors: {r.console_errors[0][:120]}")
                for step in r.reproduction_steps:
                    print(f"- {step}")
                print(f"Recommended action: {r.recommended_action}")

        # Print Unrelated Changes
        if data.security_result and data.security_result.unrelated_files:
            for u in data.security_result.unrelated_files[:3]:
                print("\n" + "-" * 60)
                print("UNRELATED CHANGE")
                print(f"File: {u.file_path}")
                print(f"Reason: {u.reason}")

        # Print Secrets if found
        if data.security_result and data.security_result.secrets_found:
            for s in data.security_result.secrets_found:
                print("\n" + "-" * 60)
                print("CRITICAL SECURITY FINDING: SECRET DETECTED")
                print(f"File: {s.file_path}:{s.line_number}")
                print(f"Type: {s.pattern_type}")

        # Print Visual Differences
        vis_diffs = [v for v in data.visual_results if not v.passed]
        if vis_diffs:
            for v in vis_diffs[:2]:
                print("\n" + "-" * 60)
                print("VISUAL DIFFERENCE")
                print(f"Page: {v.route} ({v.viewport_name})")
                print(f"Details: {v.summary}")

        print("\n" + "=" * 60)
        decision_str = f"RECOMMENDATION: PUSH {data.decision}"
        print(decision_str)
        if data.decision_reason:
            print(f"Reason: {data.decision_reason}")
        print("=" * 60 + "\n")

    def save_reports(self, data: AuditReportData) -> Dict[str, str]:
        """Save Markdown, JSON, and HTML evidence reports."""
        ts = int(time.time())
        base_name = f"report_{ts}"

        json_file = self.reports_dir / f"{base_name}.json"
        md_file = self.reports_dir / f"{base_name}.md"
        html_file = self.reports_dir / f"{base_name}.html"

        # 1. JSON Report
        raw_dict = {
            "timestamp": data.timestamp,
            "repo_path": data.repo_path,
            "baseline_sha": data.baseline_sha,
            "proposed_sha": data.proposed_sha,
            "task_title": data.task_title,
            "task_source": data.task_source,
            "enforcement_mode": data.enforcement_mode,
            "decision": data.decision,
            "decision_reason": data.decision_reason,
            "metrics": {
                "routes_checked": data.routes_checked,
                "workflows_checked": data.workflows_checked,
                "visual_checks": data.visual_checks,
                "passed": data.passed_count,
                "expected_changes": data.expected_count,
                "regressions": data.regressions_count,
                "not_tested": data.not_tested_count,
            },
            "findings": [asdict(f) for f in data.findings],
            "visual_results": [asdict(v) for v in data.visual_results],
            "security": asdict(data.security_result) if data.security_result else None,
            "ai_summary": data.ai_summary,
        }
        json_file.write_text(json.dumps(raw_dict, indent=2), encoding="utf-8")

        # 2. Markdown Report
        md_content = self._generate_markdown(data)
        md_file.write_text(md_content, encoding="utf-8")

        # 3. HTML Evidence Viewer
        html_content = self._generate_html(data)
        html_file.write_text(html_content, encoding="utf-8")

        # Update symlink or pointer to latest
        latest_html = self.reports_dir / "latest.html"
        latest_html.write_text(html_content, encoding="utf-8")

        return {
            "json": str(json_file),
            "markdown": str(md_file),
            "html": str(html_file),
        }

    def _generate_markdown(self, data: AuditReportData) -> str:
        lines = [
            f"# ProofPush Audit Report",
            f"",
            f"- **Date**: {data.timestamp}",
            f"- **Repository**: `{data.repo_path}`",
            f"- **Task**: {data.task_title}",
            f"- **Baseline Commit**: `{data.baseline_sha}`",
            f"- **Proposed Commit**: `{data.proposed_sha}`",
            f"- **Enforcement Decision**: **{data.decision}** ({data.decision_reason})",
            f"",
            f"## Results Summary",
            f"",
            f"| Metric | Count |",
            f"|---|---|",
            f"| Routes Checked | {data.routes_checked} |",
            f"| Functional Workflows | {data.workflows_checked} |",
            f"| Visual Comparisons | {data.visual_checks} |",
            f"| Passed | {data.passed_count} |",
            f"| Expected Changes | {data.expected_count} |",
            f"| Regressions | {data.regressions_count} |",
            f"| Not Tested | {data.not_tested_count} |",
            f"",
        ]

        if data.findings:
            lines.append("## Findings\n")
            for f in data.findings:
                badge = "🔴" if f.category == "REGRESSION" else ("🟢" if f.category == "UNCHANGED" else "🟡")
                lines.append(f"### {badge} {f.category}: {f.target}")
                lines.append(f"- **Before**: {f.before_behavior}")
                lines.append(f"- **After**: {f.after_behavior}")
                lines.append(f"- **Recommended Action**: {f.recommended_action}")
                if f.console_errors:
                    lines.append(f"- **Console Errors**: `{f.console_errors[0]}`")
                lines.append("")

        return "\n".join(lines)

    def _generate_html(self, data: AuditReportData) -> str:
        decision_color = "#10b981" if data.decision == "ALLOWED" else "#ef4444"
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ProofPush Evidence Report</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 2rem; }}
        .container {{ max-width: 1000px; margin: 0 auto; }}
        .header {{ border-bottom: 2px solid #334155; padding-bottom: 1.5rem; margin-bottom: 2rem; }}
        .badge {{ display: inline-block; padding: 0.25rem 0.75rem; border-radius: 9999px; font-weight: bold; background: {decision_color}; color: white; }}
        .card {{ background: #1e293b; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; border: 1px solid #334155; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
        .stat-card {{ background: #1e293b; padding: 1rem; border-radius: 6px; text-align: center; border: 1px solid #334155; }}
        .stat-val {{ font-size: 2rem; font-weight: bold; margin-top: 0.5rem; }}
        .regression {{ border-left: 4px solid #ef4444; }}
        .success {{ border-left: 4px solid #10b981; }}
        .warning {{ border-left: 4px solid #f59e0b; }}
        pre {{ background: #0f172a; padding: 1rem; border-radius: 6px; overflow-x: auto; color: #38bdf8; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <span class="badge">PUSH {data.decision}</span>
            <h1>ProofPush Evidence Report</h1>
            <p>Task: <strong>{data.task_title}</strong></p>
            <p>Baseline: <code>{data.baseline_sha[:10]}</code> &rarr; Proposed: <code>{data.proposed_sha[:10]}</code></p>
        </div>

        <div class="grid">
            <div class="stat-card"><div>Passed</div><div class="stat-val" style="color: #10b981;">{data.passed_count}</div></div>
            <div class="stat-card"><div>Expected Changes</div><div class="stat-val" style="color: #38bdf8;">{data.expected_count}</div></div>
            <div class="stat-card"><div>Regressions</div><div class="stat-val" style="color: #ef4444;">{data.regressions_count}</div></div>
            <div class="stat-card"><div>Not Tested</div><div class="stat-val" style="color: #94a3b8;">{data.not_tested_count}</div></div>
        </div>

        <h2>Detailed Findings</h2>
        {"".join(f'''
        <div class="card {'regression' if f.category == 'REGRESSION' else ('success' if f.category == 'UNCHANGED' else 'warning')}">
            <h3>{f.category}: {f.target}</h3>
            <p><strong>Before:</strong> {f.before_behavior}</p>
            <p><strong>After:</strong> {f.after_behavior}</p>
            <p><strong>Recommendation:</strong> {f.recommended_action}</p>
            {f'<pre>{f.console_errors[0]}</pre>' if f.console_errors else ''}
        </div>
        ''' for f in data.findings)}
    </div>
</body>
</html>
"""
