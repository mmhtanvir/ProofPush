"""
Functional Regression and Workflow Tests.
Covers:
- Scenario 3: Relevant change that introduces no regression (UNCHANGED)
- Scenario 4: Feature that works in new version but not in baseline (NEW_FUNCTIONALITY)
- Scenario 5: Existing feature breaks after change (REGRESSION)
- Scenario 8: Relevant change causes regression in another workflow
- Scenario 9: Renamed route
- Scenario 10: Removed route
- Scenario 11: Changed authentication requirement
- Scenario 16: Failed browser test
- Scenario 17: Intentionally changed workflow (EXPECTED_CHANGE)
- Scenario 31: Guard workflow for a previously fixed bug fails (REGRESSION even if baseline failed)
- Scenario 90: Third-party network flakes (ORB blocks, timeouts on other hosts) are not blamed on the push
"""

import unittest
from proofpush.task_resolver import TaskContext
from proofpush.classifier import RegressionClassifier
from proofpush.browser_engine import PageVisitResult, WorkflowExecutionResult, NetworkFailure


class TestFunctionalRegressions(unittest.TestCase):
    def setUp(self):
        self.task = TaskContext(
            title="Add coupon support to checkout",
            source="explicit",
            expected_behaviors=["Allow customer to enter coupon SAVE10", "Update order total"],
            expected_areas=["checkout"],
        )
        self.classifier = RegressionClassifier(self.task)

    def test_scenario_3_no_regression(self):
        """Scenario 3: A relevant change that introduces no regression (UNCHANGED)."""
        base_visit = PageVisitResult(
            route="/checkout",
            url="http://127.0.0.1:3000/checkout",
            status=200,
            title="Checkout",
            passed=True,
        )
        prop_visit = PageVisitResult(
            route="/checkout",
            url="http://127.0.0.1:3100/checkout",
            status=200,
            title="Checkout",
            passed=True,
        )
        finding = self.classifier.classify_route_visit("/checkout", base_visit, prop_visit)
        self.assertEqual(finding.category, "UNCHANGED")
        self.assertFalse(finding.is_critical)

    def test_scenario_4_new_feature_works_in_proposed_only(self):
        """Scenario 4: A feature that works in proposed revision but not in baseline."""
        base_wf = WorkflowExecutionResult(
            workflow_name="Apply Coupon SAVE10",
            route="/checkout",
            passed=False,
            steps_completed=1,
            total_steps=3,
            error_message="Coupon input element not found in baseline",
        )
        prop_wf = WorkflowExecutionResult(
            workflow_name="Apply Coupon SAVE10",
            route="/checkout",
            passed=True,
            steps_completed=3,
            total_steps=3,
        )
        finding = self.classifier.classify_workflow(base_wf, prop_wf)
        self.assertEqual(finding.category, "NEW_FUNCTIONALITY")
        self.assertFalse(finding.is_critical)

    def test_scenario_5_existing_feature_breaks_after_change(self):
        """Scenario 5: An existing feature that breaks after a change (REGRESSION)."""
        base_wf = WorkflowExecutionResult(
            workflow_name="Standard Checkout",
            route="/checkout",
            passed=True,
            steps_completed=4,
            total_steps=4,
        )
        prop_wf = WorkflowExecutionResult(
            workflow_name="Standard Checkout",
            route="/checkout",
            passed=False,
            steps_completed=2,
            total_steps=4,
            error_message="Order total failed to recalculate: NaN displayed",
        )
        finding = self.classifier.classify_workflow(base_wf, prop_wf)
        self.assertEqual(finding.category, "REGRESSION")
        self.assertTrue(finding.is_critical)
        self.assertIn("NaN displayed", finding.after_behavior)

    def test_scenario_8_change_causes_regression_in_another_workflow(self):
        """Scenario 8: A relevant checkout change causes a regression in user login workflow."""
        base_wf = WorkflowExecutionResult(
            workflow_name="User Login",
            route="/login",
            passed=True,
            steps_completed=3,
            total_steps=3,
        )
        prop_wf = WorkflowExecutionResult(
            workflow_name="User Login",
            route="/login",
            passed=False,
            steps_completed=1,
            total_steps=3,
            error_message="Submit button unresponsive after global script modification",
        )
        finding = self.classifier.classify_workflow(base_wf, prop_wf)
        self.assertEqual(finding.category, "REGRESSION")
        self.assertTrue(finding.is_critical)

    def test_scenario_9_renamed_route(self):
        """Scenario 9: Renamed route returns 404 on old path and 200 on new path."""
        # Old route /plans removed
        base_old = PageVisitResult(route="/plans", url="http://127.0.0.1:3000/plans", status=200, title="Plans")
        prop_old = PageVisitResult(route="/plans", url="http://127.0.0.1:3100/plans", status=404, title="Not Found")
        finding_old = self.classifier.classify_route_visit("/plans", base_old, prop_old)
        self.assertEqual(finding_old.category, "REMOVED_FUNCTIONALITY")

        # New route /pricing added
        base_new = PageVisitResult(route="/pricing", url="http://127.0.0.1:3000/pricing", status=404, title="Not Found")
        prop_new = PageVisitResult(route="/pricing", url="http://127.0.0.1:3100/pricing", status=200, title="Pricing")
        finding_new = self.classifier.classify_route_visit("/pricing", base_new, prop_new)
        self.assertIn(finding_new.category, ["NEW_FUNCTIONALITY", "POSSIBLE_REGRESSION"])

    def test_scenario_10_removed_route(self):
        """Scenario 10: Unexpectedly removed route flagged as critical REMOVED_FUNCTIONALITY."""
        base_visit = PageVisitResult(route="/help", url="http://127.0.0.1:3000/help", status=200, title="Help")
        prop_visit = PageVisitResult(route="/help", url="http://127.0.0.1:3100/help", status=404, title="Not Found")

        finding = self.classifier.classify_route_visit("/help", base_visit, prop_visit)
        self.assertEqual(finding.category, "REMOVED_FUNCTIONALITY")
        self.assertTrue(finding.is_critical)

    def test_scenario_11_changed_auth_requirement(self):
        """Scenario 11: Route public in baseline now returns 401/403 or redirects to login."""
        base_visit = PageVisitResult(route="/docs", url="http://127.0.0.1:3000/docs", status=200, title="Docs")
        prop_visit = PageVisitResult(
            route="/docs",
            url="http://127.0.0.1:3100/login?redirect=/docs",
            status=401,
            title="Login Required",
            network_failures=[NetworkFailure(url="/docs", method="GET", status=401, error_text="Unauthorized")]
        )
        finding = self.classifier.classify_route_visit("/docs", base_visit, prop_visit)
        self.assertEqual(finding.category, "REGRESSION")
        self.assertTrue(finding.is_critical)

    def test_scenario_16_failed_browser_test_due_to_console_error(self):
        """Scenario 16: Route introduces unhandled JavaScript exceptions in browser console."""
        base_visit = PageVisitResult(route="/shop", url="http://127.0.0.1:3000/shop", status=200, title="Shop", console_errors=[])
        prop_visit = PageVisitResult(
            route="/shop",
            url="http://127.0.0.1:3100/shop",
            status=200,
            title="Shop",
            console_errors=["[ERROR] Uncaught ReferenceError: couponTracker is not defined"]
        )
        finding = self.classifier.classify_route_visit("/shop", base_visit, prop_visit)
        self.assertEqual(finding.category, "REGRESSION")
        self.assertTrue(finding.is_critical)
        self.assertIn("couponTracker is not defined", finding.actual_result)

    def test_scenario_17_intentionally_changed_workflow(self):
        """Scenario 17: Intended change requested by task is classified as EXPECTED_CHANGE."""
        removal_task = TaskContext(
            title="Remove legacy checkout banner and deprecate old checkout route",
            source="explicit",
            expected_behaviors=["Remove old checkout route"],
        )
        classifier = RegressionClassifier(removal_task)
        base_visit = PageVisitResult(route="/checkout", url="http://127.0.0.1:3000/checkout", status=200, title="Checkout")
        prop_visit = PageVisitResult(route="/checkout", url="http://127.0.0.1:3100/checkout", status=404, title="Not Found")

        finding = classifier.classify_route_visit("/checkout", base_visit, prop_visit)
        self.assertEqual(finding.category, "EXPECTED_CHANGE")
        self.assertFalse(finding.is_critical)

    def test_scenario_31_guard_workflow_previously_fixed_bug(self):
        """Scenario 31: Guard workflow failing is a REGRESSION even when baseline also failed and name matches task."""
        failed = WorkflowExecutionResult(
            workflow_name="Checkout coupon total bug",
            route="/checkout",
            passed=False,
            steps_completed=2,
            total_steps=3,
            error_message="Total not updated after coupon",
        )
        self.assertEqual(self.classifier.classify_workflow(failed, failed).category, "POSSIBLE_REGRESSION")

        finding = self.classifier.classify_workflow(failed, failed, guard=True)
        self.assertEqual(finding.category, "REGRESSION")
        self.assertTrue(finding.is_critical)
        self.assertIn("Previously fixed bug reappeared", finding.actual_result)

    def test_scenario_90_third_party_network_flakes_are_not_regressions(self):
        """Scenario 90: an image on another host blocked by the browser or timing out (network, not code) is not
        blamed on the push; failures on the app's own host, HTTP errors and misspelled domains still are."""
        base = PageVisitResult(route="/contacts", url="http://127.0.0.1:3100/contacts", status=200, title="Contacts")

        def with_failure(f):
            return PageVisitResult(route="/contacts", url="http://127.0.0.1:3200/contacts", status=200, title="Contacts",
                                   network_failures=[f])

        avatar = "https://lh3.googleusercontent.com/contacts/abc=s100"
        for flaky in [NetworkFailure(url=avatar, method="GET", error_text="net::ERR_BLOCKED_BY_ORB"),
                      NetworkFailure(url="https://fonts.gstatic.com/x.woff2", method="GET", error_text="net::ERR_TIMED_OUT")]:
            self.assertEqual(self.classifier.classify_route_visit("/contacts", base, with_failure(flaky)).category, "UNCHANGED")
        for real in [NetworkFailure(url="http://127.0.0.1:3200/assets/app.js", method="GET", error_text="net::ERR_TIMED_OUT"),
                     NetworkFailure(url="https://api.example.com/contacts", method="GET", status=500, error_text="HTTP 500"),
                     NetworkFailure(url="https://cdn.exampel.com/lib.js", method="GET", error_text="net::ERR_NAME_NOT_RESOLVED")]:
            self.assertEqual(self.classifier.classify_route_visit("/contacts", base, with_failure(real)).category, "REGRESSION")
