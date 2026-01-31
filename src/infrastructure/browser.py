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
import random
import time
from typing import Optional, Dict, Any, List
from pathlib import Path
from datetime import datetime

from playwright.async_api import (
    async_playwright,
    Page,
    BrowserContext,
    Browser,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError
)

from ..config import Settings
from ..core.exceptions import (
    BrowserError,
    SelectorError,
    ActionError,
    AgentCriticalError,
    TimeoutError,
    CaptchaDetectedError
)
from ..core.models import ActionResult

# Stealth import with graceful degradation
try:
    from playwright_stealth import stealth_async
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False


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
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        
        # Element mapping for ID-based selectors
        self.element_map: Dict[int, str] = {}
        self.next_element_id = 0
    
    async def __aenter__(self) -> 'BrowserService':
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
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            else:
                # Non-persistent mode
                self.browser = await self.playwright.chromium.launch(
                    headless=self.settings.headless,
                    slow_mo=self.settings.slow_mo,
                )
                self.context = await self.browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                self.page = await self.context.new_page()
            
            # Apply stealth patches if available
            if STEALTH_AVAILABLE and self.settings.enable_stealth:
                await stealth_async(self.page)
            
            # Set default timeouts
            self.page.set_default_timeout(self.settings.action_timeout)
            self.page.set_default_navigation_timeout(self.settings.page_load_timeout)
            
        except Exception as e:
            raise BrowserError(
                f"Failed to launch browser: {e}",
                context={"settings": self.settings.model_dump(exclude={"api_key"})}
            )
    
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
            print(f"Warning during browser cleanup: {e}")
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
        # URL validation - security critical
        if not url or not url.strip():
            return ActionResult(
                success=False,
                message="URL cannot be empty",
                error="InvalidURL"
            )
        
        url = url.strip()
        
        # Block dangerous protocols
        dangerous_protocols = ['javascript:', 'data:', 'file:', 'about:', 'chrome:']
        url_lower = url.lower()
        for protocol in dangerous_protocols:
            if url_lower.startswith(protocol):
                return ActionResult(
                    success=False,
                    message=f"Protocol '{protocol}' not allowed for security reasons",
                    error="BlockedProtocol"
                )
        
        # Ensure HTTP/HTTPS
        if not (url_lower.startswith('http://') or url_lower.startswith('https://')):
            # Auto-add https:// if missing
            url = f'https://{url}'
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await self.page.goto(url, wait_until="domcontentloaded")
                
                # Wait for page to stabilize
                await self.page.wait_for_load_state("networkidle", timeout=10000)
                
                return ActionResult(
                    success=True,
                    message=f"Navigated to {url}"
                )
                
            except PlaywrightTimeoutError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                    continue
                else:
                    return ActionResult(
                        success=False,
                        message=f"Navigation timeout after {max_retries} attempts",
                        error="NavigationTimeout"
                    )
            
            except Exception as e:
                return ActionResult(
                    success=False,
                    message=f"Navigation failed: {e}",
                    error=str(e)
                )
    
    async def click_element_safe(self, element_id: int) -> ActionResult:
        """
        Click element with retry and visibility check.
        
        BATTLE-READY CLICKING:
        - Handles Playwright strict mode violations
        - Falls back to .first locator when selector matches multiple elements
        - Logs warnings when fallback is used
        - Still waits for visibility and scrolls into view
        
        Why defensive clicking?
        - Elements might not be ready immediately after page load
        - Prevents "element not interactable" errors
        - Waits for element to be in stable state
        - Gracefully handles non-unique selectors
        
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
                error="InvalidElementID"
            )
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Wait for element to be visible and enabled
                await self.page.wait_for_selector(
                    selector,
                    state="visible",
                    timeout=self.settings.action_timeout
                )
                
                # Try to click with strict mode first
                try:
                    # Scroll into view
                    await self.page.locator(selector).scroll_into_view_if_needed()
                    
                    # Small delay for animations
                    await asyncio.sleep(0.3)
                    
                    # Click with strict mode
                    await self.page.click(selector)
                    
                    return ActionResult(
                        success=True,
                        message=f"Clicked element {element_id}"
                    )
                
                except PlaywrightError as strict_error:
                    # Check if this is a strict mode violation
                    if "strict mode violation" in str(strict_error).lower():
                        # FALLBACK: Use .first locator strategy
                        print(f"Warning: Selector '{selector}' matched multiple elements. Using .first fallback.")
                        
                        locator = self.page.locator(selector).first
                        await locator.scroll_into_view_if_needed()
                        await asyncio.sleep(0.3)
                        await locator.click()
                        
                        return ActionResult(
                            success=True,
                            message=f"Clicked element {element_id} (used .first fallback)",
                            warning="Selector matched multiple elements"
                        )
                    else:
                        # Re-raise if not strict mode violation
                        raise
                
            except PlaywrightTimeoutError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                else:
                    await self._capture_error_snapshot("click_timeout")
                    return ActionResult(
                        success=False,
                        message=f"Element {element_id} not found or not clickable",
                        error="ElementNotFound"
                    )
            
            except Exception as e:
                await self._capture_error_snapshot("click_error")
                return ActionResult(
                    success=False,
                    message=f"Click failed: {e}",
                    error=str(e)
                )
    
    async def type_text(self, element_id: int, text: str, press_enter: bool = False) -> ActionResult:
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
                error="InvalidElementID"
            )
        
        try:
            await self.page.wait_for_selector(selector, state="visible")
            
            # Try to click/focus with strict mode first
            try:
                await self.page.click(selector)  # Focus element
            except PlaywrightError as strict_error:
                if "strict mode violation" in str(strict_error).lower():
                    # FALLBACK: Use .first locator
                    print(f"Warning: Selector '{selector}' matched multiple elements for typing. Using .first fallback.")
                    locator = self.page.locator(selector).first
                    await locator.click()
                else:
                    raise
            
            # Type with random delays between keystrokes
            for char in text:
                await self.page.keyboard.type(char)
                delay = random.randint(
                    self.settings.typing_speed_min,
                    self.settings.typing_speed_max
                )
                await asyncio.sleep(delay / 1000.0)  # Convert ms to seconds
            
            if press_enter:
                await self.page.keyboard.press("Enter")
            
            return ActionResult(
                success=True,
                message=f"Typed text into element {element_id}"
            )
            
        except Exception as e:
            await self._capture_error_snapshot("type_error")
            return ActionResult(
                success=False,
                message=f"Typing failed: {e}",
                error=str(e)
            )
    
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
                error="InvalidElementID"
            )
        
        try:
            await self.page.wait_for_selector(selector, state="visible")
            
            # Try to select with strict mode first
            try:
                await self.page.select_option(selector, value=value)
            except PlaywrightError as strict_error:
                if "strict mode violation" in str(strict_error).lower():
                    # FALLBACK: Use .first locator
                    print(f"Warning: Selector '{selector}' matched multiple elements for select. Using .first fallback.")
                    locator = self.page.locator(selector).first
                    await locator.select_option(value=value)
                else:
                    raise
            
            return ActionResult(
                success=True,
                message=f"Selected option '{value}' in element {element_id}"
            )
            
        except Exception as e:
            await self._capture_error_snapshot("select_error")
            return ActionResult(
                success=False,
                message=f"Select failed: {e}",
                error=str(e)
            )
    
    async def _capture_error_snapshot(self, error_type: str) -> tuple[Optional[Path], Optional[Path]]:
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
            html_file = self.settings.screenshot_dir / f"error_{error_type}_{timestamp}.html"
            html_content = await self.page.content()
            html_file.write_text(html_content, encoding="utf-8")
            html_path = html_file
            
        except Exception as e:
            print(f"Warning: Failed to capture error snapshot: {e}")
        
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
            
            return ActionResult(
                success=True,
                message=f"Scrolled {direction}"
            )
        except Exception as e:
            return ActionResult(
                success=False,
                message=f"Scroll failed: {e}",
                error=str(e)
            )
