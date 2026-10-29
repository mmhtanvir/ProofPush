"""
Playwright-based Browser Functional Testing Engine.
Executes browser operations, workflows, route visits, console error tracking,
network failure monitoring, and safe interactive element inspection.
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Set
from playwright.async_api import async_playwright, Browser, Page, Response, Request

from change_auditor.config import WorkflowConfig, WorkflowStep

DESTRUCTIVE_KEYWORDS = [
    "delete", "remove", "destroy", "drop", "purge", "truncate", "terminate",
    "pay", "checkout", "charge", "purchase", "order now", "send email", "send sms"
]


@dataclass
class NetworkFailure:
    url: str
    method: str
    status: Optional[int] = None
    error_text: Optional[str] = None


@dataclass
class PageVisitResult:
    route: str
    url: str
    status: int
    title: str
    console_errors: List[str] = field(default_factory=list)
    network_failures: List[NetworkFailure] = field(default_factory=list)
    discovered_links: List[str] = field(default_factory=list)
    interactive_elements: List[Dict[str, str]] = field(default_factory=list)
    screenshot_bytes: Optional[bytes] = None
    dom_text_snippet: str = ""
    error_message: Optional[str] = None
    passed: bool = True


@dataclass
class WorkflowExecutionResult:
    workflow_name: str
    route: str
    passed: bool
    steps_completed: int
    total_steps: int
    final_url: str = ""
    error_message: Optional[str] = None
    console_errors: List[str] = field(default_factory=list)
    network_failures: List[NetworkFailure] = field(default_factory=list)
    step_details: List[Dict[str, Any]] = field(default_factory=list)


class BrowserEngine:
    def __init__(self, headless: bool = True, timeout: int = 15000):
        self.headless = headless
        self.timeout = timeout

    async def visit_page(
        self,
        base_url: str,
        route: str,
        viewport: Optional[Dict[str, int]] = None,
        capture_screenshot: bool = True,
        discover_elements: bool = True,
    ) -> PageVisitResult:
        """Visit a single route, capture console logs, network errors, interactive elements, and screenshot."""
        full_url = f"{base_url.rstrip('/')}{route}"
        console_errors = []
        network_failures = []
        status_code = 200

        vp = viewport or {"width": 1280, "height": 800}

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                viewport=vp,
                ignore_https_errors=True,
                user_agent="change-auditor-functional-agent",
            )
            page = await context.new_page()

            # Listen to console messages
            def on_console(msg):
                if msg.type in ("error", "warning") and "favicon" not in msg.text:
                    console_errors.append(f"[{msg.type.upper()}] {msg.text}")

            page.on("console", on_console)

            # Listen to failed network requests
            def on_request_failed(req: Request):
                # Ignore favicons or analytics trackers
                if "favicon" not in req.url:
                    network_failures.append(
                        NetworkFailure(
                            url=req.url,
                            method=req.method,
                            error_text=req.failure or "Failed request",
                        )
                    )

            def on_response(resp: Response):
                nonlocal status_code
                if resp.url == page.url or resp.url == full_url:
                    status_code = resp.status
                if resp.status >= 400 and "favicon" not in resp.url:
                    network_failures.append(
                        NetworkFailure(
                            url=resp.url,
                            method=resp.request.method,
                            status=resp.status,
                            error_text=f"HTTP {resp.status} {resp.status_text}",
                        )
                    )

            page.on("requestfailed", on_request_failed)
            page.on("response", on_response)

            try:
                resp = await page.goto(full_url, timeout=self.timeout, wait_until="domcontentloaded")
                if resp:
                    status_code = resp.status

                # Wait short delay for client hydration / dynamic rendering
                await page.wait_for_timeout(400)

                title = await page.title()
                dom_text = await page.evaluate("() => document.body ? document.body.innerText.substring(0, 500) : ''")

                # Extract local links
                discovered_links = []
                try:
                    links = await page.eval_on_selector_all(
                        "a[href]",
                        "elements => elements.map(e => e.getAttribute('href'))"
                    )
                    for l in links:
                        if l and (l.startswith("/") or l.startswith(base_url)):
                            clean = l.replace(base_url, "")
                            if not clean.startswith("/"):
                                clean = "/" + clean
                            clean = clean.split("#")[0].split("?")[0]
                            if clean and clean not in discovered_links:
                                discovered_links.append(clean)
                except Exception:
                    pass

                # Inspect interactive elements
                elements = []
                if discover_elements:
                    try:
                        raw_elems = await page.evaluate("""() => {
                            const items = [];
                            document.querySelectorAll('button, a, input, select, textarea').forEach(el => {
                                const tag = el.tagName.toLowerCase();
                                const text = (el.innerText || el.value || el.getAttribute('placeholder') || el.getAttribute('aria-label') || '').trim();
                                const role = el.getAttribute('role') || tag;
                                const id = el.id || '';
                                const testId = el.getAttribute('data-testid') || '';
                                const type = el.getAttribute('type') || '';
                                items.push({ tag, text: text.substring(0, 50), role, id, testId, type });
                            });
                            return items.slice(0, 40);
                        }""")
                        elements = raw_elems
                    except Exception:
                        pass

                screenshot = None
                if capture_screenshot:
                    # Stabilize animations before capturing
                    await page.add_style_tag(content="""
                        *, *::before, *::after {
                            animation: none !important;
                            transition: none !important;
                            caret-color: transparent !important;
                        }
                    """)
                    screenshot = await page.screenshot(full_page=True)

                await browser.close()

                is_pass = (status_code < 400) and (len(console_errors) == 0)
                return PageVisitResult(
                    route=route,
                    url=full_url,
                    status=status_code,
                    title=title,
                    console_errors=console_errors,
                    network_failures=network_failures,
                    discovered_links=discovered_links,
                    interactive_elements=elements,
                    screenshot_bytes=screenshot,
                    dom_text_snippet=dom_text,
                    passed=is_pass,
                )

            except Exception as exc:
                await browser.close()
                return PageVisitResult(
                    route=route,
                    url=full_url,
                    status=status_code or 500,
                    title="",
                    console_errors=console_errors,
                    network_failures=network_failures,
                    error_message=str(exc),
                    passed=False,
                )

    async def execute_workflow(
        self,
        base_url: str,
        workflow: WorkflowConfig,
        viewport: Optional[Dict[str, int]] = None,
    ) -> WorkflowExecutionResult:
        """Execute a multi-step user workflow with accessibility and semantic selectors."""
        console_errors = []
        network_failures = []
        step_details = []

        vp = viewport or {"width": 1280, "height": 800}

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                viewport=vp,
                ignore_https_errors=True,
                user_agent="change-auditor-workflow-runner",
            )
            page = await context.new_page()

            def on_console(msg):
                if msg.type in ("error", "warning"):
                    console_errors.append(f"[{msg.type.upper()}] {msg.text}")

            def on_request_failed(req: Request):
                network_failures.append(
                    NetworkFailure(url=req.url, method=req.method, error_text=req.failure or "Failed")
                )

            page.on("console", on_console)
            page.on("requestfailed", on_request_failed)

            steps_completed = 0
            total_steps = len(workflow.steps)
            workflow_passed = True
            error_message = None

            initial_url = f"{base_url.rstrip('/')}{workflow.path}"
            try:
                await page.goto(initial_url, timeout=self.timeout, wait_until="domcontentloaded")
                await page.wait_for_timeout(300)
            except Exception as exc:
                await browser.close()
                return WorkflowExecutionResult(
                    workflow_name=workflow.name,
                    route=workflow.path,
                    passed=False,
                    steps_completed=0,
                    total_steps=total_steps,
                    error_message=f"Failed to navigate to initial route {workflow.path}: {exc}",
                )

            for idx, step in enumerate(workflow.steps):
                step_res = {"step_index": idx, "action": step.action, "passed": False}
                try:
                    if step.action == "navigate":
                        nav_url = step.value if step.value.startswith("http") else f"{base_url.rstrip('/')}{step.value}"
                        await page.goto(nav_url, timeout=self.timeout)

                    elif step.action == "click":
                        # Safety check: avoid destructive operations unless explicitly confirmed
                        if step.selector and any(k in step.selector.lower() for k in DESTRUCTIVE_KEYWORDS):
                            step_res["warning"] = "Skipped potentially destructive click action"
                        else:
                            await page.click(step.selector, timeout=self.timeout)

                    elif step.action == "fill":
                        await page.fill(step.selector, step.value or "", timeout=self.timeout)

                    elif step.action == "select":
                        await page.select_option(step.selector, step.value or "", timeout=self.timeout)

                    elif step.action == "wait_for":
                        await page.wait_for_selector(step.selector, timeout=self.timeout)

                    elif step.action == "assert_text":
                        if step.selector:
                            text = await page.inner_text(step.selector, timeout=self.timeout)
                        else:
                            text = await page.inner_text("body", timeout=self.timeout)
                        if step.expected and step.expected not in text:
                            raise AssertionError(f"Expected text '{step.expected}' not found in '{text[:100]}'")

                    elif step.action == "assert_url":
                        current_url = page.url
                        if step.expected and step.expected not in current_url:
                            raise AssertionError(f"Expected URL to contain '{step.expected}', but got '{current_url}'")

                    step_res["passed"] = True
                    steps_completed += 1
                except Exception as step_exc:
                    step_res["error"] = str(step_exc)
                    if step.optional:
                        step_res["skipped_optional"] = True
                    else:
                        workflow_passed = False
                        error_message = f"Step {idx + 1} ({step.action}) failed: {step_exc}"
                        step_details.append(step_res)
                        break

                step_details.append(step_res)

            final_url = page.url
            await browser.close()

            return WorkflowExecutionResult(
                workflow_name=workflow.name,
                route=workflow.path,
                passed=workflow_passed,
                steps_completed=steps_completed,
                total_steps=total_steps,
                final_url=final_url,
                error_message=error_message,
                console_errors=console_errors,
                network_failures=network_failures,
                step_details=step_details,
            )
