"""
Playwright-based Browser Functional Testing Engine.
Executes browser operations, workflows, route visits, console error tracking,
network failure monitoring, and safe interactive element inspection.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from typing import List, Dict, Any, Optional


def async_playwright():
    """Playwright, imported on first use so git-only commands (gate, history checks) work without the browser extra."""
    try:
        from playwright.async_api import async_playwright as start
    except ImportError as exc:
        raise RuntimeError("Browser checks need the browser extra: pip install 'proofpush[browser]' "
                           "&& python -m playwright install chromium") from exc
    return start()

from proofpush.config import AuthConfig, Credentials, WorkflowConfig, WorkflowStep
from proofpush.utils import redact_secrets, totp

LOOPBACK = {"localhost", "127.0.0.1", "::1"}

# (name, selector, official always-pass test keys) for CAPTCHAs that block automated logins
CAPTCHAS = [
    ("Cloudflare Turnstile", "iframe[src*='challenges.cloudflare.com'], .cf-turnstile, [data-sitekey^='0x']",
     "site key 1x00000000000000000000AA, secret 1x0000000000000000000000000000000AA"),
    ("Google reCAPTCHA", "iframe[src*='recaptcha'], .g-recaptcha, .grecaptcha-badge",
     "site key 6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI, secret 6LeIxAcTAAAAAGG-vFI1TnRWxMZNFuojJ4WifJWe"),
    ("hCaptcha", "iframe[src*='hcaptcha.com'], .h-captcha",
     "site key 10000000-ffff-ffff-ffff-000000000001, secret 0x0000000000000000000000000000000000000000"),
]


def retarget_session(saved_session: Path, base_url: str) -> Dict[str, Any]:
    """Saved browser state, with loopback cookies/storage moved to base_url's host and port.

    Local audits run each version on 127.0.0.1 with a random port, so a session saved against
    localhost:3000 would otherwise never apply. Non-loopback (prod) sessions are untouched.
    """
    state = json.loads(saved_session.read_text(encoding="utf-8"))
    target = urlparse(base_url)
    if target.hostname not in LOOPBACK:
        return state
    for cookie in state.get("cookies", []):
        if cookie.get("domain", "").lstrip(".") in LOOPBACK:
            cookie["domain"] = target.hostname
    for origin in state.get("origins", []):
        if urlparse(origin.get("origin", "")).hostname in LOOPBACK:
            origin["origin"] = f"{target.scheme}://{target.netloc}"
    return state


# Links a crawler must never follow: they end the session or change data even as a GET
UNSAFE_LINK = re.compile(r"log-?out|sign-?out|logoff|delete|remove|destroy|unsubscribe|deactivate|purge|cancel", re.I)
NON_PAGE = re.compile(r"\.(pdf|zip|png|jpe?g|gif|svg|webp|mp4|mov|csv|xlsx?|docx?|ics|json|xml|txt)$", re.I)
OTP_SELECTOR = ", ".join([
    "input[autocomplete=one-time-code]:visible", "input[name*=otp i]:visible", "input[id*=otp i]:visible",
    "input[name*=code i]:not([type=password]):visible", "input[name*=token i]:not([type=hidden]):visible",
])
LOGIN_USER_SELECTOR = ", ".join([
    "input[type=email]", "input[autocomplete=username]", "input[name*=email i]", "input[name*=user i]",
    "input[id*=email i]", "input[id*=user i]", "input[type=text]",
]) + " >> nth=0"

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
        self._playwright = None
        self._browser = None
        self._loop = None
        # Logged-in browser state (cookies, localStorage) per base URL, from login()
        self.sessions: Dict[str, Dict[str, Any]] = {}

    @asynccontextmanager
    async def _shared_browser(self):
        """One Chromium for every visit in this event loop; launching per page cost ~1s each."""
        loop = asyncio.get_running_loop()
        if self._browser is None or self._loop is not loop or not self._browser.is_connected():
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._loop = loop
        yield self._browser

    async def _close_contexts(self, browser) -> None:
        for ctx in list(browser.contexts):
            await ctx.close()

    async def close(self) -> None:
        """Shut the shared browser down; call once at the end of the asyncio.run that used this engine."""
        if self._browser is not None and self._loop is asyncio.get_running_loop():
            await self._browser.close()
            await self._playwright.stop()
        self._browser = self._playwright = self._loop = None

    async def visit_page(
        self,
        base_url: str,
        route: str,
        viewport: Optional[Dict[str, int]] = None,
        capture_screenshot: bool = True,
        discover_elements: bool = True,
        mask_selectors: Optional[List[str]] = None,
    ) -> PageVisitResult:
        """Visit a single route, capture console logs, network errors, interactive elements, and screenshot."""
        full_url = f"{base_url.rstrip('/')}{route}"
        console_errors = []
        network_failures = []
        status_code = 200

        vp = viewport or {"width": 1280, "height": 800}

        async with self._shared_browser() as browser:
            context = await browser.new_context(
                viewport=vp,
                ignore_https_errors=True,
                user_agent="proofpush-functional-agent",
                storage_state=self.sessions.get(base_url),
            )
            page = await context.new_page()

            # Listen to console messages
            def on_console(msg):
                if msg.type in ("error", "warning") and "favicon" not in msg.text:
                    console_errors.append(f"[{msg.type.upper()}] {msg.text}")

            page.on("console", on_console)

            # Listen to failed network requests
            def on_request_failed(req):
                # Ignore favicons or analytics trackers
                if "favicon" not in req.url:
                    network_failures.append(
                        NetworkFailure(
                            url=req.url,
                            method=req.method,
                            error_text=req.failure or "Failed request",
                        )
                    )

            def on_response(resp):
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
                    # Dynamic regions (timestamps, ads, ids) are painted over so they never count as visual changes
                    screenshot = await page.screenshot(full_page=True, mask=[page.locator(s) for s in mask_selectors or []])

                await context.close()

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
                await context.close()
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

    async def _run_step(self, page, base_url: str, step: WorkflowStep, step_res: Optional[Dict[str, Any]] = None) -> None:
        """Run one workflow step; raises on failure."""
        step_res = step_res if step_res is not None else {}
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

    async def login(self, base_url: str, auth: AuthConfig, creds: Optional[Credentials],
                    saved_session: Optional[Path] = None, probe_route: str = "/") -> Optional[str]:
        """Log the test account in (or reuse a saved session) and keep it for later visits. Returns an error, or None."""
        async with self._shared_browser() as browser:
            page = None
            try:
                login_url = f"{base_url.rstrip('/')}{auth.login_path}"
                # 1. A session saved by `proofpush login` (needed for SMS/email codes, SSO, captchas)
                if saved_session and saved_session.is_file():
                    context = await browser.new_context(ignore_https_errors=True, user_agent="proofpush-functional-agent",
                                                        storage_state=retarget_session(saved_session, base_url))
                    page = await context.new_page()
                    # Still valid if a page behind the login doesn't bounce us to a login form
                    await page.goto(f"{base_url.rstrip('/')}{probe_route}", timeout=self.timeout, wait_until="domcontentloaded")
                    await self._settle(page)
                    if not await self._on_login_form(page):
                        self.sessions[base_url] = await context.storage_state()
                        return None
                    if not creds:
                        return f"Saved session {saved_session.name} has expired; run `proofpush login` again"
                if not creds:
                    return "No test account credentials or saved session"

                # 2. Automated login with the test account
                context = await browser.new_context(ignore_https_errors=True, user_agent="proofpush-functional-agent")
                page = await context.new_page()
                await page.goto(login_url, timeout=self.timeout, wait_until="domcontentloaded")
                values = {"{username}": creds.username, "{password}": creds.password}
                if auth.steps:
                    for step in auth.steps:
                        value = step.value or ""
                        if "{otp}" in value:
                            if not creds.otp_secret:
                                return "auth.steps uses {otp} but the account has no otp_secret_env"
                            value = value.replace("{otp}", totp(creds.otp_secret))
                        for k, v in values.items():
                            value = value.replace(k, v)
                        await self._run_step(page, base_url, WorkflowStep(
                            action=step.action, selector=step.selector, optional=step.optional,
                            expected=step.expected, value=value or None))
                else:
                    # Common login forms: first email/username field, first password field, submit
                    await page.fill(LOGIN_USER_SELECTOR, creds.username, timeout=self.timeout)
                    await page.fill("input[type=password]", creds.password, timeout=self.timeout)
                    await page.press("input[type=password]", "Enter")
                    await self._settle(page)
                    # Second step: a one-time code field
                    if await page.locator(OTP_SELECTOR).count() > 0:
                        if not creds.otp_secret:
                            return ("Login asks for a one-time code. Set otp_secret_env for an authenticator-app account, "
                                    "or run `proofpush login` once and type the code yourself")
                        await page.fill(OTP_SELECTOR + " >> nth=0", totp(creds.otp_secret), timeout=self.timeout)
                        await page.press(OTP_SELECTOR + " >> nth=0", "Enter")
                await self._settle(page)
                if await self._on_login_form(page):
                    return await self._captcha_advice(page) or (
                        f"Still on a login form after signing in ({page.url}); check the test account or auth.steps")
                self.sessions[base_url] = await context.storage_state()
                return None
            except Exception as exc:
                advice = await self._captcha_advice(page) if page else None
                if advice:
                    return advice
                # Never echo credentials: Playwright errors can include filled values
                msg = str(exc)
                if creds:
                    msg = msg.replace(creds.password, "***")
                return redact_secrets(msg).splitlines()[0]
            finally:
                await self._close_contexts(browser)

    async def _captcha_advice(self, page) -> Optional[str]:
        """If the login page carries a CAPTCHA, explain how to get past it legitimately (never by solving it)."""
        for name, selector, test_keys in CAPTCHAS:
            try:
                if await page.locator(selector).count() > 0:
                    return (f"The login page has a {name} CAPTCHA, which automated logins cannot pass (by design). "
                            f"Locally: start the app with {name}'s official test keys ({test_keys}) via application.env "
                            "in .proofpush.yml; the backend that verifies the token must use the test secret too. "
                            "Prod: run `proofpush login --env prod` once and solve it yourself.")
            except Exception:
                pass
        return None

    async def _settle(self, page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=self.timeout)
        except Exception:
            pass

    async def _on_login_form(self, page) -> bool:
        return await page.locator(f"input[type=password]:visible, {OTP_SELECTOR}").count() > 0

    async def save_session_interactively(self, url: str, out: Path, wait_for_user, headless: bool = False) -> None:
        """Open a visible browser at the login page, let the person log in (OTP, SSO...), then save the session."""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(ignore_https_errors=True)
            page = await context.new_page()
            await page.goto(url, timeout=self.timeout)
            await asyncio.get_running_loop().run_in_executor(None, wait_for_user)
            out.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(out))
            out.chmod(0o600)
            await browser.close()

    async def crawl(self, base_url: str, start_routes: List[str], max_pages: int, excluded=lambda route: False,
                    capture_screenshot: bool = False, viewport: Optional[Dict[str, int]] = None,
                    mask_selectors: Optional[List[str]] = None) -> List[PageVisitResult]:
        """Visit every page reachable by same-site links (as the logged-in account, if any), up to max_pages. GET only."""
        # Limit: one browser launch per page; reuse a single browser if crawls get slow
        queue, visits, seen = list(dict.fromkeys(start_routes or ["/"])), [], set()
        while queue and len(visits) < max_pages:
            route = queue.pop(0)
            if route in seen or excluded(route) or UNSAFE_LINK.search(route) or NON_PAGE.search(route):
                continue
            seen.add(route)
            visit = await self.visit_page(base_url, route, viewport=viewport, capture_screenshot=capture_screenshot,
                                          discover_elements=False, mask_selectors=mask_selectors)
            visits.append(visit)
            queue += [link for link in visit.discovered_links if link not in seen]
        return visits

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

        async with self._shared_browser() as browser:
            context = await browser.new_context(
                viewport=vp,
                ignore_https_errors=True,
                user_agent="proofpush-workflow-runner",
                storage_state=self.sessions.get(base_url),
            )
            page = await context.new_page()

            def on_console(msg):
                if msg.type in ("error", "warning"):
                    console_errors.append(f"[{msg.type.upper()}] {msg.text}")

            def on_request_failed(req):
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
                await context.close()
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
                    await self._run_step(page, base_url, step, step_res)
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
            await context.close()

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
