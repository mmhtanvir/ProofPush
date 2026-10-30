"""
Playwright-based Browser Functional Testing Engine.
Executes browser operations, workflows, route visits, console error tracking,
network failure monitoring, and safe interactive element inspection.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse
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
# CAPTCHA widgets (Turnstile, reCAPTCHA, hCaptcha) probe the browser on purpose: requests that fail (a 401,
# a decoy hostname) under random URLs, and console noise from inside their own frame.
CAPTCHA_HOST = re.compile(r"(^|\.)(challenges\.cloudflare\.com|hcaptcha\.com|recaptcha\.net)$")


def console_entry(msg) -> Optional[str]:
    """An error/warning the page logged, as "[TYPE] text"; None for other levels, favicons and CAPTCHA frames."""
    if msg.type not in ("error", "warning") or "favicon" in msg.text:
        return None
    source = (msg.location or {}).get("url") or ""
    if CAPTCHA_HOST.search(urlparse(source).hostname or "") or "/recaptcha/" in source:
        return None
    return f"[{msg.type.upper()}] {msg.text}"
# Chromium-based browsers a person logs in with (any of them talks CDP), most common first
_PF = [os.environ.get(v, "") for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
BROWSER_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    *(f"{base}\\{rel}" for base in _PF if base for rel in (
        "Google\\Chrome\\Application\\chrome.exe", "Microsoft\\Edge\\Application\\msedge.exe",
        "BraveSoftware\\Brave-Browser\\Application\\brave.exe")),
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "brave-browser",
]


def installed_browser() -> Optional[str]:
    """The person's own Chromium-based browser (PROOFPUSH_BROWSER overrides), or None."""
    for candidate in [os.environ.get("PROOFPUSH_BROWSER", ""), *BROWSER_CANDIDATES]:
        found = candidate and (candidate if Path(candidate).is_file() else shutil.which(candidate))
        if found:
            return found
    return None

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
    layout: Dict[str, Any] = field(default_factory=dict)  # responsive-layout and load-speed measurements
    api_calls: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # "GET /api/x/:id" -> status, ms, JSON shape


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


@dataclass
class InteractionResult:
    """What one click did: used to spot buttons that stop working, slow down, or become janky."""
    key: str  # stable identity across versions: data-testid, id, or role + label
    label: str
    clicked: bool = True
    error: Optional[str] = None
    url_changed: bool = False
    mutations: int = 0
    requests: int = 0
    response_ms: Optional[float] = None  # click -> first visible change
    settle_ms: Optional[float] = None  # click -> no more DOM changes for 300 ms
    blocking_ms: float = 0.0  # main-thread long-task time beyond 50 ms each (jank)
    layout_shift: float = 0.0
    console_errors: List[str] = field(default_factory=list)
    locator: Dict[str, Any] = field(default_factory=dict)  # how to find it again (testid / id / role + label)
    api_errors: List[str] = field(default_factory=list)  # backend calls the click made that failed, e.g. "POST /api/star -> 500"

    @property
    def had_effect(self) -> bool:
        return self.url_changed or self.mutations > 0 or self.requests > 0


# Elements a crawler may click: visible, enabled, not submit buttons, nothing that sounds destructive
_CANDIDATES_JS = r"""([limit, extraSkip]) => {
  const unsafe = new RegExp([
    'delete|remove|destroy|drop|purge|truncate|terminate|wipe|reset|archive|ban|block',
    'pay|checkout|charge|purchase|order|refund|transfer|subscribe|upgrade|downgrade|book',
    'send|submit|save|confirm|approve|accept|reject|decline|invite|share|publish|deploy|upload|import|export|sync',
    'logout|log out|sign ?out|deactivate',
  ].join('|') + (extraSkip ? '|' + extraSkip : ''), 'i');
  const els = [...document.querySelectorAll('button, [role=button], [role=tab], [role=switch], [role=checkbox], [role=menuitem], summary, input[type=checkbox]')];
  const seen = new Set(), out = [];
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    if (el.closest('form') && el.type === 'submit') continue;  // a plain <button> reports type 'submit' too; only forms submit
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.title || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    const testid = el.getAttribute('data-testid'), id = el.id;
    if (unsafe.test([label, testid, id, el.className].join(' '))) continue;
    const role = el.getAttribute('role') || (el.tagName === 'INPUT' ? 'checkbox' : el.tagName === 'SUMMARY' ? 'summary' : 'button');
    const key = testid ? `testid:${testid}` : id ? `id:${id}` : label ? `${role}:${label}` : null;
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push({key, label: label || key, testid, id, role});
    if (out.length >= limit) break;
  }
  return out;
}"""

# Installed before each click: counts DOM changes, long tasks and layout shifts from that moment on
_MEASURE_JS = r"""() => {
  const m = window.__pp = {start: performance.now(), mutations: 0, first: null, last: null, blocking: 0, shift: 0};
  new MutationObserver(list => { const t = performance.now(); m.mutations += list.length; if (m.first === null) m.first = t; m.last = t; })
    .observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  try { new PerformanceObserver(l => l.getEntries().forEach(e => { if (e.startTime >= m.start) m.blocking += Math.max(0, e.duration - 50); })).observe({type: 'longtask', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => l.getEntries().forEach(e => { if (!e.hadRecentInput && e.startTime >= m.start) m.shift += e.value; })).observe({type: 'layout-shift', buffered: true}); } catch (e) {}
}"""


_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{24}|[0-9a-f]{32,})$", re.I)


def api_key(method: str, url: str) -> str:
    """'GET /api/patients/:id?page=': ids and query values dropped so the same call matches across versions."""
    parsed = urlparse(url)
    path = "/".join(":id" if _ID_SEGMENT.match(seg) else seg for seg in parsed.path.split("/"))
    query = "&".join(sorted(f"{k}=" for k in parse_qs(parsed.query)))
    return f"{method} {path}" + (f"?{query}" if query else "")


def json_shape(data: Any) -> Dict[str, Any]:
    """Field names and list sizes of a JSON response: enough to notice fields or data disappearing."""
    if isinstance(data, dict):
        return {"keys": sorted(data)[:100], "lists": {k: len(v) for k, v in data.items() if isinstance(v, list)}}
    if isinstance(data, list):
        first = data[0] if data and isinstance(data[0], dict) else {}
        return {"keys": sorted(first)[:100], "length": len(data)}
    return {}


async def _record_api_call(req, into: Dict[str, Dict[str, Any]]) -> None:
    """Status, time and JSON shape of one finished XHR/fetch call (the page's backend requests)."""
    if req.resource_type not in ("xhr", "fetch", "document"):
        return
    try:
        resp = await req.response()
    except Exception:
        return  # the page closed before this call finished: nothing to record
    if resp is None:
        return
    if req.resource_type == "document" and "json" not in (resp.headers.get("content-type") or ""):
        return  # HTML pages are judged by the page checks; JSON routes (API-only backends) are API calls
    call = {"status": resp.status, "ms": round(req.timing.get("responseEnd", 0) or 0)}
    if (urlparse(req.url).hostname or "") not in LOOPBACK:
        call["remote"] = True  # a live API: its speed varies on its own, not with the push
    if req.method == "GET":
        # A concrete, known-good URL the API tests can replay (sensitive query values masked)
        parsed = urlparse(req.url)
        query = "&".join(f"{k}={'***' if re.search(r'token|key|secret|pass|session|auth', k, re.I) else v[0]}"
                         for k, v in parse_qs(parsed.query).items())
        call["sample"] = parsed.path + (f"?{query}" if query else "")
    if "json" in (resp.headers.get("content-type") or ""):
        try:
            call.update(json_shape(await resp.json()))
        except Exception:
            call["no_body"] = True  # body gone (the page moved on first): shape unknown, not empty
    key = api_key(req.method, req.url)
    # Keep the worst outcome if a call repeats on the page
    if key not in into or call["status"] > into[key]["status"]:
        into[key] = call


# Installed before any page script runs: what loading the page cost (biggest paint, jank, layout jumps)
_LOAD_METRICS_JS = r"""(() => {
  const m = window.__ppLoad = {lcp: null, cls: 0, blocking: 0};
  try { new PerformanceObserver(l => l.getEntries().forEach(e => { m.lcp = e.startTime; })).observe({type: 'largest-contentful-paint', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => l.getEntries().forEach(e => { if (!e.hadRecentInput) m.cls += e.value; })).observe({type: 'layout-shift', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => l.getEntries().forEach(e => { m.blocking += Math.max(0, e.duration - 50); })).observe({type: 'longtask', buffered: true}); } catch (e) {}
})();"""

# Responsive layout health at the current viewport: sideways scroll, off-screen / overlapping / tiny controls,
# clipped text, which controls are visible, and load metrics
# True while the page still shows it is loading: a visible spinner/skeleton/progressbar, aria-busy, a
# Tailwind skeleton block or "Loading..." text. Data pages behind a login fetch after the HTML arrives.
_STILL_LOADING_JS = r"""() => {
  const shown = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const busy = '[aria-busy="true"], [role="progressbar"], .animate-spin, .spinner, .loading, .skeleton';
  if ([...document.querySelectorAll(busy)].some(shown)) return true;
  // Tailwind skeleton blocks (empty .animate-pulse placeholders), not small pulsing status dots
  if ([...document.querySelectorAll('.animate-pulse')].some((el) => shown(el) && el.offsetHeight >= 12 && !el.innerText.trim())) return true;
  return /\bloading\b[^\n]{0,20}(\.\.\.|…)/i.test(document.body ? document.body.innerText.slice(0, 5000) : '');
}"""
CRAWL_CONCURRENCY = 4  # pages loaded at once while crawling
LOAD_WAIT_MS = 8000  # most a page waits for its loading indicators to go away
NETWORK_QUIET_MS = 2000  # then for the network to go quiet (pages that poll never do)

_LAYOUT_JS = r"""() => {
  const vw = document.documentElement.clientWidth;
  const docW = Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0);
  const label = el => (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder') || el.id || el.tagName)
    .trim().replace(/\s+/g, ' ').slice(0, 40);
  const shown = el => { const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity !== 0 && r.width > 0 && r.height > 0; };
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [role=tab], [role=menuitem], summary';
  const rects = [...document.querySelectorAll(sel)].filter(shown).slice(0, 300).map(el => [el, el.getBoundingClientRect()]);
  const offscreen = rects.filter(([, r]) => r.right > vw + 1 || r.left < -1).map(([el]) => label(el));
  const small = rects.filter(([, r]) => r.width < 24 || r.height < 24).map(([el]) => label(el));  // WCAG 2.5.8 minimum
  const overlaps = [];
  for (let i = 0; i < rects.length && overlaps.length < 20; i++) for (let j = i + 1; j < rects.length; j++) {
    const [a, ra] = rects[i], [b, rb] = rects[j];
    if (a.contains(b) || b.contains(a)) continue;
    const ix = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left), iy = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
    if (ix > 4 && iy > 4 && ix * iy > 0.3 * Math.min(ra.width * ra.height, rb.width * rb.height)) overlaps.push(label(a) + ' / ' + label(b));
  }
  const clipped = [...document.querySelectorAll('body *')].slice(0, 3000).filter(el => {
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) return false;
    const s = getComputedStyle(el);
    return (s.overflow === 'hidden' || s.overflowX === 'hidden') && s.textOverflow !== 'ellipsis' && el.scrollWidth > el.clientWidth + 2;
  }).map(label).slice(0, 20);
  const nav = performance.getEntriesByType('navigation')[0], load = window.__ppLoad || {};
  return {
    overflow_px: Math.max(0, docW - vw), offscreen, small, overlaps, clipped,
    controls: [...new Set(rects.map(([el]) => label(el)))].slice(0, 200),
    load_ms: nav ? Math.round(nav.loadEventEnd || nav.domContentLoadedEventEnd || 0) : null,
    lcp_ms: load.lcp == null ? null : Math.round(load.lcp), load_blocking_ms: Math.round(load.blocking || 0),
    load_shift: Math.round((load.cls || 0) * 1000) / 1000,
  };
}"""


class BrowserEngine:
    def __init__(self, headless: bool = True, timeout: int = 15000):
        self.headless = headless
        self.timeout = timeout
        self._playwright = None
        self._browser = None
        self._loop = None
        self._launch_lock: Optional[asyncio.Lock] = None
        self._lock_loop = None
        # Logged-in browser state (cookies, localStorage) per base URL, from login()
        self.sessions: Dict[str, Dict[str, Any]] = {}
        # Sent with every request (e.g. an API token for backends without a login page)
        self.extra_headers: Dict[str, str] = {}

    @asynccontextmanager
    async def _shared_browser(self):
        """One Chromium for every visit in this event loop; launching per page cost ~1s each."""
        loop = asyncio.get_running_loop()
        if self._lock_loop is not loop:  # an asyncio.Lock belongs to one event loop
            self._launch_lock, self._lock_loop = asyncio.Lock(), loop
        async with self._launch_lock:  # concurrent visits share one launch
            if self._browser is None or self._loop is not loop or not self._browser.is_connected():
                self._playwright = await async_playwright().start()
                try:
                    self._browser = await self._playwright.chromium.launch(headless=self.headless)
                except Exception:
                    await self._playwright.stop()  # otherwise the driver process outlives the event loop
                    self._playwright = None
                    raise
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
            await asyncio.sleep(0.1)  # let the driver's pipe transports finish closing while the loop still runs
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
                ignore_https_errors=True, extra_http_headers=self.extra_headers or None,
                user_agent="proofpush-functional-agent",
                storage_state=self.sessions.get(base_url),
            )
            page = await context.new_page()
            await page.add_init_script(_LOAD_METRICS_JS)

            # Listen to console messages
            def on_console(msg):
                entry = console_entry(msg)
                if entry:
                    console_errors.append(entry)

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
            api_calls: Dict[str, Dict[str, Any]] = {}
            pending: List[Any] = []
            page.on("requestfinished", lambda req: pending.append(asyncio.ensure_future(_record_api_call(req, api_calls))))

            try:
                resp = await page.goto(full_url, timeout=self.timeout, wait_until="domcontentloaded")
                if resp:
                    status_code = resp.status

                await self._wait_until_loaded(page)
                try:
                    layout = await page.evaluate(_LAYOUT_JS)
                except Exception:
                    layout = {}
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

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
                    layout=layout,
                    api_calls=api_calls,
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
                    context = await browser.new_context(ignore_https_errors=True, extra_http_headers=self.extra_headers or None, user_agent="proofpush-functional-agent",
                                                        storage_state=retarget_session(saved_session, base_url))
                    page = await context.new_page()
                    # Still valid if a page behind the login doesn't bounce us to a login form
                    await page.goto(f"{base_url.rstrip('/')}{probe_route}", timeout=self.timeout, wait_until="domcontentloaded")
                    await self._settle(page)
                    if not await self._on_login_form(page):
                        self.sessions[base_url] = await context.storage_state()
                        return None
                    if not creds:
                        env_name = saved_session.stem.removeprefix("session-")
                        return f"Saved session {saved_session.name} has expired; run `proofpush login --env {env_name}` again"
                if not creds:
                    return "No test account credentials or saved session"

                # 2. Automated login with the test account
                context = await browser.new_context(ignore_https_errors=True, extra_http_headers=self.extra_headers or None, user_agent="proofpush-functional-agent")
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

    async def _wait_until_loaded(self, page) -> None:
        """Let client rendering and data requests finish: loading indicators gone, then the network briefly quiet.
        Both are capped; a page that never settles is still checked as it is."""
        await page.wait_for_timeout(400)  # hydration
        for wait in (lambda: page.wait_for_function(f"() => !({_STILL_LOADING_JS})()", timeout=LOAD_WAIT_MS, polling=200),
                     lambda: page.wait_for_load_state("networkidle", timeout=NETWORK_QUIET_MS)):
            try:
                await wait()
            except Exception:
                pass

    async def _on_login_form(self, page) -> bool:
        return await page.locator(f"input[type=password]:visible, {OTP_SELECTOR}").count() > 0

    async def save_session_interactively(self, url: str, out: Path, wait_for_user, headless: bool = False) -> Optional[str]:
        """Open a visible browser at the login page, let the person log in (OTP, SSO...), then save the session.
        Returns the path the person landed on (a page behind the login), or None if it still shows a login form."""
        browser_path = None if headless else installed_browser()
        if browser_path:
            return await self._save_session_from_chrome(browser_path, url, out, wait_for_user)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(ignore_https_errors=True)
            page = await context.new_page()
            await page.goto(url, timeout=self.timeout)
            await asyncio.get_running_loop().run_in_executor(None, wait_for_user)
            out.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(out))
            out.chmod(0o600)
            landed = None if await self._on_login_form(page) else (urlparse(page.url).path or "/")
            await browser.close()
            return landed

    async def _save_session_from_chrome(self, browser_path: str, url: str, out: Path, wait_for_user) -> Optional[str]:
        """Bot checks (Turnstile, reCAPTCHA) refuse a Playwright-launched browser (navigator.webdriver), so the person
        logs in to their own Chrome/Edge/Brave with a throwaway profile; ProofPush attaches over CDP only afterwards to save it."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with tempfile.TemporaryDirectory() as profile:
            chrome = subprocess.Popen([browser_path, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                                       "--no-first-run", "--no-default-browser-check", url],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                await asyncio.get_running_loop().run_in_executor(None, wait_for_user)
                async with async_playwright() as p:
                    browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    context = browser.contexts[0]
                    page = next((pg for pg in context.pages if pg.url.startswith("http")), context.pages[0])
                    state = await context.storage_state()
                    # storage_state over CDP leaves out localStorage, where many SPAs keep their token
                    state["origins"] = [{"origin": await page.evaluate("location.origin"), "localStorage": await page.evaluate(
                        "Object.entries(localStorage).map(([name, value]) => ({name, value}))")}]
                    landed = None if await self._on_login_form(page) else (urlparse(page.url).path or "/")
            finally:
                chrome.terminate()
                chrome.wait(timeout=10)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(state))
        out.chmod(0o600)
        return landed

    async def crawl(self, base_url: str, start_routes: List[str], max_pages: int, excluded=lambda route: False,
                    capture_screenshot: bool = False, viewport: Optional[Dict[str, int]] = None,
                    mask_selectors: Optional[List[str]] = None) -> List[PageVisitResult]:
        """Visit every page reachable by same-site links (as the logged-in account, if any), up to max_pages. GET only.
        CRAWL_CONCURRENCY pages load at once: pages behind a login spend seconds waiting for their data."""
        queue, visits, seen = list(dict.fromkeys(start_routes or ["/"])), [], set()
        while queue and len(visits) < max_pages:
            batch = []
            while queue and len(visits) + len(batch) < max_pages and len(batch) < CRAWL_CONCURRENCY:
                route = queue.pop(0)
                if route in seen or excluded(route) or UNSAFE_LINK.search(route) or NON_PAGE.search(route):
                    continue
                seen.add(route)
                batch.append(route)
            for visit in await asyncio.gather(*(
                    self.visit_page(base_url, route, viewport=viewport, capture_screenshot=capture_screenshot,
                                    discover_elements=False, mask_selectors=mask_selectors) for route in batch)):
                visits.append(visit)
                queue += [link for link in visit.discovered_links if link not in seen]
        return visits

    def _locator(self, page, c: Dict[str, Any]):
        if c.get("testid"):
            return page.locator(f'[data-testid="{c["testid"]}"]').first
        if c.get("id"):
            return page.locator(f'[id="{c["id"]}"]').first
        if c["role"] in ("summary",):
            return page.locator("summary", has_text=c["label"]).first
        return page.get_by_role(c["role"], name=c["label"], exact=True).first

    async def probe_interactions(self, base_url: str, route: str, max_elements: int = 8,
                                 viewport: Optional[Dict[str, int]] = None, skip: Optional[List[str]] = None,
                                 only: Optional[List["InteractionResult"]] = None) -> Dict[str, InteractionResult]:
        """Click each safe interactive element on a page and measure what it does.

        One page per call: after each click the element is clicked again to put state back (star, menu, tab),
        and the page is reloaded only when a click navigated away. `only` probes specific elements (by locator)
        seen in the other version; ones not on this page are left out.
        """
        url = f"{base_url.rstrip('/')}{route}"
        results: Dict[str, InteractionResult] = {}
        async with self._shared_browser() as browser:
            context = await browser.new_context(viewport=viewport or {"width": 1280, "height": 800}, ignore_https_errors=True, extra_http_headers=self.extra_headers or None,
                                                user_agent="proofpush-functional-agent", storage_state=self.sessions.get(base_url))
            try:
                page = await context.new_page()
                await self._load_quiet(page, url)
                if only is None:
                    extra = "|".join(re.escape(w) for w in skip or [])
                    candidates = await page.evaluate(_CANDIDATES_JS, [max_elements, extra])
                else:
                    candidates = [w.locator for w in only]
                for c in candidates:
                    if page.url != url:
                        await self._load_quiet(page, url)
                    res = await self._probe_one(page, c)
                    if not (only is not None and res.error == "element not found"):
                        results[c["key"]] = res
            finally:
                await context.close()
        return results

    async def _load_quiet(self, page, url: str) -> None:
        await page.goto(url, timeout=self.timeout, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

    async def _wait_quiet(self, page, before_url: str) -> None:
        """Until the DOM has been quiet for 300 ms, nothing changed for 800 ms, or the page navigated (max 3 s)."""
        for _ in range(30):
            await page.wait_for_timeout(100)
            if page.url != before_url:
                return
            m, now = await page.evaluate("() => [window.__pp, performance.now()]")
            if (m["last"] is None and now - m["start"] > 800) or (m["last"] is not None and now - m["last"] > 300):
                return

    async def _probe_one(self, page, c: Dict[str, Any]) -> InteractionResult:
        res = InteractionResult(key=c["key"], label=c["label"], locator=c)
        requests: List[str] = []

        def on_request(r):
            if r.resource_type in ("xhr", "fetch", "document"):
                requests.append(r.url)

        def on_console(m):
            if m.type == "error":
                res.console_errors.append(m.text)

        def on_error(e):
            res.console_errors.append(str(e))

        def on_response(r):
            if r.request.resource_type in ("xhr", "fetch") and r.status >= 400:
                res.api_errors.append(f"{api_key(r.request.method, r.url)} -> {r.status}")

        try:
            target = self._locator(page, c)
            if await target.count() == 0:
                res.clicked, res.error = False, "element not found"
                return res
            page.on("request", on_request)
            page.on("console", on_console)
            page.on("pageerror", on_error)
            page.on("response", on_response)
            await page.evaluate(_MEASURE_JS)
            before_url = page.url
            await target.click(timeout=3000)
            await self._wait_quiet(page, before_url)
            res.url_changed = page.url != before_url
            res.requests = len(requests)
            if not res.url_changed:
                m = await page.evaluate("() => window.__pp")
                res.mutations = m["mutations"]
                res.response_ms = round(m["first"] - m["start"]) if m["first"] is not None else None
                res.settle_ms = round(m["last"] - m["start"]) if m["last"] is not None else None
                res.blocking_ms = round(m["blocking"])
                res.layout_shift = round(m["shift"], 3)
                # Toggles (star, like, switch, menu): click again so the test leaves state as it found it
                if (res.mutations or requests) and await target.count():
                    page.remove_listener("request", on_request)
                    page.remove_listener("response", on_response)
                    try:
                        await page.evaluate(_MEASURE_JS)
                        await target.click(timeout=2000)
                        await self._wait_quiet(page, before_url)
                    except Exception:
                        pass
        except Exception as exc:
            res.error = str(exc).splitlines()[0][:200]
        finally:
            for event, handler in (("request", on_request), ("console", on_console), ("pageerror", on_error),
                                   ("response", on_response)):
                try:
                    page.remove_listener(event, handler)
                except Exception:
                    pass
        return res

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
                ignore_https_errors=True, extra_http_headers=self.extra_headers or None,
                user_agent="proofpush-workflow-runner",
                storage_state=self.sessions.get(base_url),
            )
            page = await context.new_page()

            def on_console(msg):
                entry = console_entry(msg)
                if entry:
                    console_errors.append(entry)

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
