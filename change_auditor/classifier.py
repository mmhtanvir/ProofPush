"""
Regression Classifier.
Compares observed website behavior between baseline and proposed versions,
evaluates findings against task context, and classifies outcomes into standard categories.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from change_auditor.task_resolver import TaskContext
from change_auditor.browser_engine import PageVisitResult, WorkflowExecutionResult
from change_auditor.visual_comparator import VisualComparisonResult


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

    def classify_workflow(
        self,
        base_wf: Optional[WorkflowExecutionResult],
        prop_wf: WorkflowExecutionResult,
    ) -> Finding:
        """Classify workflow results between baseline and proposed versions."""
        target_name = f"Workflow: {prop_wf.workflow_name}"

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
