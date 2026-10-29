"""
Regression Classifier.
Compares observed website behavior between baseline and proposed versions,
evaluates findings against task context, and classifies outcomes into standard categories.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from proofpush.task_resolver import TaskContext
from proofpush.browser_engine import InteractionResult, PageVisitResult, WorkflowExecutionResult
from proofpush.visual_comparator import VisualComparisonResult


@dataclass
class Finding:
    category: str  # UNCHANGED, EXPECTED_CHANGE, NEW_FUNCTIONALITY, REGRESSION, REMOVED_FUNCTIONALITY, POSSIBLE_REGRESSION, NOT_TESTED, BLOCKED
    target: str  # Route or Workflow name
    before_behavior: str
    after_behavior: str
    expected_behavior: str
    actual_result: str
    confidence: float  # 0.0 to 1.0
    recommended_action: str
    reproduction_steps: List[str] = field(default_factory=list)
    console_errors: List[str] = field(default_factory=list)
    network_failures: List[Dict[str, Any]] = field(default_factory=list)
    visual_evidence_path: Optional[str] = None
    is_critical: bool = False


class RegressionClassifier:
    def __init__(self, task_context: TaskContext):
        self.task_context = task_context

    def classify_route_visit(
        self,
        route: str,
        base_result: Optional[PageVisitResult],
        prop_result: Optional[PageVisitResult],
        visual_result: Optional[VisualComparisonResult] = None,
    ) -> Finding:
        """Classify comparison between baseline page visit and proposed page visit."""

        def crashed(r: Optional[PageVisitResult]) -> bool:
            return not r or r.status >= 500 or bool(r.error_message and "ERR_" in r.error_message)

        # Case 0: Already broken before this change: worth knowing, not this change's fault
        same_error = bool(base_result and prop_result and base_result.status >= 400 and base_result.status == prop_result.status)
        if base_result and (same_error or (crashed(base_result) and crashed(prop_result))):
            return Finding(
                category="NOTICE",
                target=route,
                before_behavior=f"HTTP {base_result.status}: already failing before this change",
                after_behavior=f"HTTP {prop_result.status if prop_result else 'unreachable'}: still failing",
                expected_behavior="Page should load and respond normally.",
                actual_result=f"Pre-existing error on {route}",
                confidence=0.9,
                recommended_action=f"Not caused by this change, but {route} is broken; fix it separately.",
                is_critical=False,
                reproduction_steps=[f"Navigate to {route}"],
            )

        # Case 1: Testing blocked (e.g. server crash or network timeout)
        if not prop_result or prop_result.status >= 500 or (prop_result.error_message and "ERR_" in prop_result.error_message):
            return Finding(
                category="BLOCKED",
                target=route,
                before_behavior=f"HTTP {base_result.status if base_result else 'N/A'}",
                after_behavior=f"Failed with error: {prop_result.error_message if prop_result else 'Unreachable'}",
                expected_behavior="Page should load and respond normally.",
                actual_result=f"Application error on {route}",
                confidence=0.9,
                recommended_action="Investigate application startup or route crashing issue before pushing.",
                is_critical=True,
                console_errors=prop_result.console_errors if prop_result else [],
                reproduction_steps=[f"Navigate to {route}"],
            )

        # Case 2: New route added
        if not base_result or base_result.status == 404:
            if prop_result.status == 200:
                is_expected = any(term in route.lower() for term in self.task_context.title.lower().split())
                cat = "NEW_FUNCTIONALITY" if is_expected else "POSSIBLE_REGRESSION"
                return Finding(
                    category=cat,
                    target=route,
                    before_behavior="Route returned 404 Not Found in baseline",
                    after_behavior=f"Route loads successfully (HTTP 200, title: '{prop_result.title}')",
                    expected_behavior=f"New functionality requested by task '{self.task_context.title}'" if is_expected else "New route introduced",
                    actual_result="Page accessible in proposed revision",
                    confidence=0.85 if is_expected else 0.6,
                    recommended_action="Verify this new route was intended as part of your task.",
                    is_critical=False,
                    reproduction_steps=[f"Open {route}"],
                    visual_evidence_path=visual_result.proposed_image_path if visual_result else None,
                )

        # Case 3: Route removed (404 in proposed, 200 in baseline)
        if base_result.status == 200 and prop_result.status == 404:
            # Check if task intended to remove this route
            is_removal_intended = "remove" in self.task_context.title.lower() and any(k in route.lower() for k in self.task_context.title.lower().split())
            cat = "EXPECTED_CHANGE" if is_removal_intended else "REMOVED_FUNCTIONALITY"
            return Finding(
                category=cat,
                target=route,
                before_behavior="Route returned HTTP 200 OK in baseline",
                after_behavior="Route returned HTTP 404 Not Found in proposed version",
                expected_behavior="Route removed intentionally" if is_removal_intended else "Existing routes should remain available",
                actual_result=f"Route {route} is no longer reachable",
                confidence=0.95,
                recommended_action="Confirm whether deleting this route was intentional." if not is_removal_intended else "No action needed.",
                is_critical=not is_removal_intended,
                reproduction_steps=[f"Navigate to {route}"],
            )

        # Case 4: Console errors introduced in proposed version
        new_console_errors = [e for e in prop_result.console_errors if e not in base_result.console_errors]
        if new_console_errors:
            return Finding(
                category="REGRESSION",
                target=route,
                before_behavior=f"Clean console with {len(base_result.console_errors)} errors",
                after_behavior=f"Encountered {len(new_console_errors)} new browser console errors",
                expected_behavior="Pages should execute without unhandled JavaScript exceptions.",
                actual_result=f"Console error: {new_console_errors[0][:150]}",
                confidence=0.9,
                recommended_action="Resolve the unhandled JavaScript exception on this page.",
                is_critical=True,
                console_errors=new_console_errors,
                reproduction_steps=[f"Open {route} in browser", "Open DevTools Console to inspect errors"],
            )

        # Case 5: Network requests failed in proposed version
        new_network_failures = [
            f for f in prop_result.network_failures
            if not any(bf.url == f.url for bf in base_result.network_failures)
        ]
        if new_network_failures:
            return Finding(
                category="REGRESSION",
                target=route,
                before_behavior="All network assets and API requests succeeded in baseline",
                after_behavior=f"{len(new_network_failures)} network request(s) failed on proposed page",
                expected_behavior="API requests and page assets should load successfully.",
                actual_result=f"Failed request to {new_network_failures[0].url}: {new_network_failures[0].error_text}",
                confidence=0.88,
                recommended_action="Fix failing API endpoint or missing asset URL.",
                is_critical=True,
                reproduction_steps=[f"Open {route}", f"Observe request to {new_network_failures[0].url}"],
            )

        # Case 6: Visual difference check
        if visual_result and not visual_result.passed:
            is_task_area = any(w in route.lower() for w in self.task_context.title.lower().split())
            cat = "EXPECTED_CHANGE" if is_task_area else "POSSIBLE_REGRESSION"
            return Finding(
                category=cat,
                target=route,
                before_behavior="Baseline page layout and styling",
                after_behavior=f"Visual appearance changed by {visual_result.diff_percentage:.2%}",
                expected_behavior="Visual changes should match the intended task requirements.",
                actual_result=visual_result.summary,
                confidence=0.8 if is_task_area else 0.65,
                recommended_action="Review visual diff screenshot in evidence report." if not is_task_area else "Looks aligned with task.",
                is_critical=False,
                visual_evidence_path=visual_result.diff_image_path,
                reproduction_steps=[f"Open {route} and inspect visual difference"],
            )

        # Case 7: Unchanged
        return Finding(
            category="UNCHANGED",
            target=route,
            before_behavior="Page loaded successfully",
            after_behavior="Page loaded successfully with identical behavior",
            expected_behavior="Page should continue working as before",
            actual_result="No functional or visual regressions detected",
            confidence=0.98,
            recommended_action="No action required.",
            is_critical=False,
        )

    def classify_interaction(self, route: str, base: Optional[InteractionResult],
                             prop: Optional[InteractionResult]) -> Optional[Finding]:
        """Compare one element's click behaviour between versions. None when nothing worth reporting changed."""
        if not base or base.error or not base.had_effect:
            return None  # new element, or one that did nothing before either
        label = base.label
        problems, category, critical = [], "POSSIBLE_REGRESSION", False
        if prop is None:
            problems.append(f"'{label}' is no longer on the page")
            category = "REMOVED_FUNCTIONALITY"
        elif prop.error or not prop.clicked:
            problems.append(f"'{label}' can't be clicked anymore ({prop.error})")
            category, critical = "REGRESSION", True
        elif not prop.had_effect:
            problems.append(f"'{label}' stopped working: clicking it no longer changes anything")
            category, critical = "REGRESSION", True
        else:
            backend = [e for e in prop.api_errors if e not in base.api_errors]
            if backend:
                problems.append(f"'{label}' now fails on the backend: {', '.join(backend[:3])}")
                category, critical = "REGRESSION", True
            new_errors = [e for e in prop.console_errors if e not in base.console_errors]
            if new_errors:
                problems.append(f"'{label}' now throws errors when clicked: {new_errors[0][:120]}")
                category, critical = "REGRESSION", True
            for name, before, after, extra in (("responds", base.response_ms, prop.response_ms, 300),
                                               ("finishes updating", base.settle_ms, prop.settle_ms, 500)):
                if before is not None and after is not None and after > max(2 * before, before + extra):
                    problems.append(f"'{label}' {name} slower: {before:.0f} ms -> {after:.0f} ms")
                    break  # one timing line is enough
            if prop.blocking_ms - base.blocking_ms > 200:
                problems.append(f"'{label}' freezes the page: main thread blocked {base.blocking_ms:.0f} ms -> {prop.blocking_ms:.0f} ms")
            if prop.layout_shift > 0.1 and prop.layout_shift - base.layout_shift > 0.05:
                problems.append(f"'{label}' makes the layout jump (shift {base.layout_shift} -> {prop.layout_shift})")
        if not problems:
            return None
        return Finding(
            category=category,
            target=f"{route} -> {label}",
            before_behavior=f"Worked: {_effect(base)}",
            after_behavior="; ".join(problems),
            expected_behavior="Buttons that worked keep working, just as fast",
            actual_result=problems[0],
            confidence=0.85,
            recommended_action=f"Click '{label}' on {route} in the new version and compare with the live one.",
            is_critical=critical,
            console_errors=prop.console_errors if prop else [],
            reproduction_steps=[f"Open {route}", f"Click '{label}'"],
        )

    def classify_layout(self, where: str, base: Dict[str, Any], prop: Dict[str, Any]) -> Optional[Finding]:
        """Responsive layout and load speed at one screen size: report only what the new version made worse."""
        if not base or not prop:
            return None
        broken, warn = [], []

        def new_items(key):
            return [x for x in prop.get(key, []) if x not in set(base.get(key, []))]

        if prop.get("overflow_px", 0) > 10 and base.get("overflow_px", 0) <= 10:
            broken.append(f"the page now scrolls sideways (content {prop['overflow_px']} px wider than the screen)")
        if new_items("offscreen"):
            broken.append("pushed off the screen: " + ", ".join(new_items("offscreen")[:5]))
        if new_items("overlaps"):
            warn.append("controls now overlap: " + ", ".join(new_items("overlaps")[:3]))
        if new_items("clipped"):
            warn.append("text now cut off in: " + ", ".join(new_items("clipped")[:3]))
        if len(new_items("small")) >= 3:
            warn.append(f"{len(new_items('small'))} more tap targets under 24 px: " + ", ".join(new_items("small")[:3]))
        gone = [c for c in base.get("controls", []) if c not in set(prop.get("controls", []))]
        if 0 < len(gone) <= 10:  # more than that is a redesign, not something silently missing
            warn.append("no longer shown: " + ", ".join(gone[:5]))
        for key, name, extra in (("load_ms", "loads", 1000), ("lcp_ms", "shows its main content", 1000)):
            before, after = base.get(key), prop.get(key)
            if before and after and after > max(2 * before, before + extra):
                warn.append(f"{name} slower: {before} ms -> {after} ms")
        if prop.get("load_blocking_ms", 0) - base.get("load_blocking_ms", 0) > 300:
            warn.append(f"freezes while loading: main thread blocked {base.get('load_blocking_ms', 0)} ms -> {prop['load_blocking_ms']} ms")
        if prop.get("load_shift", 0) > 0.1 and base.get("load_shift", 0) <= 0.1:
            warn.append(f"layout jumps while loading (shift {base.get('load_shift', 0)} -> {prop['load_shift']})")
        problems = broken + warn
        if not problems:
            return None
        return Finding(
            category="REGRESSION" if broken else "POSSIBLE_REGRESSION",
            target=where,
            before_behavior="Layout and loading were fine on the baseline",
            after_behavior="; ".join(problems),
            expected_behavior="Pages keep working at every screen size, as fast as before",
            actual_result=problems[0],
            confidence=0.8,
            recommended_action=f"Open {where} at that screen size in the new version and compare with the live one.",
            is_critical=bool(broken),
        )

    def classify_api(self, page: str, base: Dict[str, Dict[str, Any]], prop: Dict[str, Dict[str, Any]]) -> Optional[Finding]:
        """Backend calls a page makes, old vs new: tells a backend break apart from a frontend one."""
        broken, warn = [], []
        for key, b in base.items():
            p = prop.get(key)
            if p is None:
                continue  # the page no longer makes this call: a frontend change, judged by the page checks
            if b["status"] < 400 <= p["status"]:
                broken.append(f"{key} now returns {p['status']} (was {b['status']})")
                continue
            lost = [k for k in b.get("keys", []) if k not in set(p.get("keys", []))]
            if lost:
                warn.append(f"{key} no longer returns fields: {', '.join(lost[:6])}")
            if b.get("length", 0) > 0 and p.get("length") == 0:
                warn.append(f"{key} now returns an empty list (was {b['length']} items)")
            for name, n in b.get("lists", {}).items():
                if n > 0 and p.get("lists", {}).get(name) == 0:
                    warn.append(f"{key} now returns an empty '{name}' (was {n} items)")
            if b.get("ms") and p.get("ms") and p["ms"] > max(2 * b["ms"], b["ms"] + 500):
                warn.append(f"{key} got slower: {b['ms']} ms -> {p['ms']} ms")
        for key, p in prop.items():
            if key not in base and p["status"] >= 500:
                broken.append(f"{key} (new call) fails with {p['status']}")
        problems = broken + warn
        if not problems:
            return None
        return Finding(
            category="REGRESSION" if broken else "POSSIBLE_REGRESSION",
            target=f"Backend (seen on {page})",
            before_behavior="The page's API calls succeeded with the same data shape",
            after_behavior="; ".join(problems),
            expected_behavior="Backend responses keep their status, fields, data and speed",
            actual_result=problems[0],
            confidence=0.85,
            recommended_action="The backend changed, not just the page: check the API handler behind each call listed.",
            is_critical=bool(broken),
            reproduction_steps=[f"Open {page}", f"Watch the network tab for: {problems[0].split(' ')[0]} {problems[0].split(' ')[1]}"],
        )

    def classify_workflow(
        self,
        base_wf: Optional[WorkflowExecutionResult],
        prop_wf: WorkflowExecutionResult,
        guard: bool = False,
    ) -> Finding:
        """Classify workflow results between baseline and proposed versions."""
        target_name = f"Workflow: {prop_wf.workflow_name}"

        # Guard workflows pin previously fixed bugs: failure is a regression even if the baseline failed too
        if guard and not prop_wf.passed:
            return Finding(
                category="REGRESSION",
                target=target_name,
                before_behavior="Bug was fixed previously (guard workflow)",
                after_behavior=f"Failed at step {prop_wf.steps_completed + 1} of {prop_wf.total_steps}: {prop_wf.error_message}",
                expected_behavior="Previously fixed bug must stay fixed.",
                actual_result=f"Previously fixed bug reappeared: {prop_wf.error_message}",
                confidence=0.99,
                recommended_action=f"A previously fixed bug is back ('{prop_wf.workflow_name}'). Re-apply the fix before pushing.",
                is_critical=True,
                console_errors=prop_wf.console_errors,
                reproduction_steps=[f"Run workflow '{prop_wf.workflow_name}' on {prop_wf.route}", f"Step error: {prop_wf.error_message}"],
            )

        # If proposed workflow passed completely
        if prop_wf.passed:
            if base_wf and not base_wf.passed:
                # Was broken in baseline, now works!
                return Finding(
                    category="NEW_FUNCTIONALITY",
                    target=target_name,
                    before_behavior=f"Failed in baseline: {base_wf.error_message}",
                    after_behavior="All steps completed successfully in proposed revision",
                    expected_behavior=f"Implement feature requested in task '{self.task_context.title}'",
                    actual_result="Workflow passed successfully",
                    confidence=0.95,
                    recommended_action="Feature verified successfully.",
                    is_critical=False,
                    reproduction_steps=[f"Execute workflow '{prop_wf.workflow_name}'"],
                )
            else:
                return Finding(
                    category="UNCHANGED",
                    target=target_name,
                    before_behavior="Workflow passed in baseline",
                    after_behavior="Workflow passed in proposed revision",
                    expected_behavior="Workflow should continue working",
                    actual_result="All steps passed without error",
                    confidence=0.98,
                    recommended_action="No action required.",
                    is_critical=False,
                )

        # If proposed workflow failed
        if base_wf and base_wf.passed:
            # Worked in baseline, broke in proposed -> REGRESSION!
            return Finding(
                category="REGRESSION",
                target=target_name,
                before_behavior=f"All {base_wf.total_steps} steps succeeded in baseline",
                after_behavior=f"Failed at step {prop_wf.steps_completed + 1} of {prop_wf.total_steps}: {prop_wf.error_message}",
                expected_behavior="Existing workflow functionality must continue to work without regression.",
                actual_result=f"Step failed: {prop_wf.error_message}",
                confidence=0.99,
                recommended_action=f"Fix regression in '{prop_wf.workflow_name}' before pushing.",
                is_critical=True,
                console_errors=prop_wf.console_errors,
                reproduction_steps=[f"Run workflow '{prop_wf.workflow_name}' on {prop_wf.route}", f"Step error: {prop_wf.error_message}"],
            )
        else:
            # Failed in both or new failing test
            is_new = any(k in prop_wf.workflow_name.lower() for k in self.task_context.title.lower().split())
            return Finding(
                category="REGRESSION" if not is_new else "POSSIBLE_REGRESSION",
                target=target_name,
                before_behavior=base_wf.error_message if base_wf else "Workflow not present in baseline",
                after_behavior=f"Failed: {prop_wf.error_message}",
                expected_behavior="Workflow should complete successfully",
                actual_result=f"Execution error: {prop_wf.error_message}",
                confidence=0.85,
                recommended_action="Review workflow implementation.",
                is_critical=True,
                console_errors=prop_wf.console_errors,
                reproduction_steps=[f"Execute workflow '{prop_wf.workflow_name}'"],
            )


def _effect(r) -> str:
    parts = []
    if r.url_changed:
        parts.append("navigated")
    if r.mutations:
        parts.append(f"page updated in {r.response_ms} ms")
    if r.requests:
        parts.append(f"{r.requests} request(s)")
    return ", ".join(parts) or "no effect"
