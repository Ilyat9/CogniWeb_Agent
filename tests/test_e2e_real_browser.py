"""
E2E smoke test with a REAL Chromium browser - no AsyncMock anywhere.

This closes the "83% coverage is mock coverage" gap: the unit suite
exercises the orchestrator/browser decision logic against mocks, while
this module proves the pieces actually work against a live Playwright
browser: launch, navigate, live DOM extraction (data-agent-id marking),
click degradation, typing, and the long-text fill() fast path.

It is fully self-contained: the page is served by an in-process
asyncio TCP server on 127.0.0.1 (navigate_block_private_networks is
disabled for this settings instance ONLY, since the SSRF guard would
otherwise - correctly - refuse a loopback target). No internet access,
no external sites, deterministic content.

Run explicitly:
    pytest tests/test_e2e_real_browser.py -m e2e

Skipped (not failed) when Playwright's Chromium is not installed:
    python -m playwright install chromium
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.settings import Settings
from src.core.exceptions import BrowserError
from src.infrastructure.browser import BrowserService
from src.utils.dom import DOMProcessor

pytestmark = pytest.mark.e2e

_HTML = """<!doctype html><html><head><title>E2E Test</title></head><body>
<h1>Hello CogniWeb E2E</h1>
<a href="/next">Go next page</a>
<button id="btn" onclick="document.title='clicked'">Click me</button>
<input id="field" placeholder="type here">
</body></html>"""


@pytest.fixture
def settings(tmp_path):
    return Settings(
        api_key="sk-test-key-not-real",
        api_base_url="https://api.test.com/v1",
        model_name="test-provider/test-model",
        user_data_dir=tmp_path / "browser_data",
        headless=True,
        enable_stealth_mode=False,
        captcha_avoidance_mode=False,
        agent_step_delay=0.0,
        screenshot_dir=tmp_path / "shots",
        checkpoint_dir=tmp_path / "cp",
        reports_dir=tmp_path / "reports",
        upload_allowed_dir=tmp_path / "up",
        # The test page is served on 127.0.0.1 - the SSRF guard would (by
        # design) refuse loopback; opt out for THIS instance only.
        navigate_block_private_networks=False,
    )


@pytest.fixture
async def http_port():
    """Serve _HTML for every request on an ephemeral 127.0.0.1 port."""

    async def handle(reader, writer):
        try:
            await reader.read(4096)
            body = _HTML.encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                b"Connection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield port
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_real_chromium_navigate_observe_click_type(settings, http_port):
    browser = BrowserService(settings)
    try:
        async with browser:
            nav = await browser.navigate(f"http://127.0.0.1:{http_port}/")
            assert nav.success, f"navigate failed: {nav.message}"
            assert await browser.get_page_title() == "E2E Test"

            elements, extraction_error = await DOMProcessor(settings).get_interactive_elements(
                browser.page
            )
            assert extraction_error is None
            by_tag = {elem["tag"]: elem["id"] for elem in elements}
            assert {"a", "button", "input"} <= set(
                by_tag
            ), f"live DOM extraction missed interactive elements: {by_tag}"
            # In the real loop the orchestrator registers the extracted
            # selectors into browser.element_map as the single source of
            # truth (see AgentOrchestrator._get_observation) - mirror that
            # here before clicking by id.
            browser.element_map.update({elem["id"]: elem["selector"] for elem in elements})

            click = await browser.click_element_safe(by_tag["button"])
            assert click.success, f"click failed: {click.message}"

            typed = await browser.type_text(by_tag["input"], "hello e2e")
            assert typed.success, f"short type failed: {typed.message}"

            long_text = "x" * (settings.typing_slow_path_max_chars + 1)
            filled = await browser.type_text(by_tag["input"], long_text)
            assert filled.success, f"long-text fill failed: {filled.message}"
            assert filled.warning, "long-text path should explain the instant fill"
            value = await browser.page.locator("#field").first.input_value()
            assert value == long_text, "fill() fast path did not enter the full text"
    except BrowserError as e:
        message = str(e)
        if "Executable doesn't exist" in message or "playwright install" in message.lower():
            pytest.skip(
                "Playwright Chromium not installed - run: " "python -m playwright install chromium"
            )
        raise


@pytest.mark.asyncio
async def test_real_chromium_stale_agent_ids_cleared(settings, http_port):
    """Each observation re-mints data-agent-id from 0 and must first CLEAR
    stale attributes - otherwise an element that leaves the visible
    selection keeps a stale id that collides with a fresh one."""
    browser = BrowserService(settings)
    try:
        async with browser:
            await browser.navigate(f"http://127.0.0.1:{http_port}/")
            first, _ = await DOMProcessor(settings).get_interactive_elements(browser.page)
            stale_count = await browser.page.evaluate(
                "document.querySelectorAll('[data-agent-id]').length"
            )
            assert stale_count == len(first)

            # Hide one element, then re-observe: its stale attribute must
            # not survive the fresh pass (ids restart at 0, a stale id
            # would collide with a fresh element's id).
            await browser.page.evaluate("document.querySelector('button').style.display = 'none'")
            second, _ = await DOMProcessor(settings).get_interactive_elements(browser.page)
            assert all(e["tag"] != "button" for e in second)
            hidden_has_id = await browser.page.evaluate(
                "document.querySelector('button').hasAttribute('data-agent-id')"
            )
            assert not hidden_has_id, "stale data-agent-id survived re-observation"
    except BrowserError as e:
        message = str(e)
        if "Executable doesn't exist" in message or "playwright install" in message.lower():
            pytest.skip(
                "Playwright Chromium not installed - run: " "python -m playwright install chromium"
            )
        raise
