"""Real-browser SMOKE tests (marker: real_browser) - no AsyncMock anywhere.

Task 3 (real-browser coverage): the unit suite mocks the Playwright API,
so regressions in REAL Chromium behavior (markup changes, Playwright
upgrades, cookie-banner reality) are invisible to it. This module runs a
genuine Chromium through BrowserService against a LOCAL static page set
served by an in-process asyncio TCP server - deliberately NOT example.com
or any external site, so the tests never depend on (or flake against) the
internet.

Covered minimum:
(a) browser launch + navigation + title read;
(b) live DOM extraction -> element lookup -> click;
(c) form fill + submit -> server-side confirmation page;
(d) clean context shutdown with no zombie state (all handles None AND a
    second sequential launch succeeds - a leaked Chromium would hold the
    persistent-profile lock and fail here).

Run explicitly (NOT part of the default pytest/CI run):
    pytest tests/test_real_browser_smoke.py -m real_browser
or: make test-real-browser

Skipped (not failed) when Playwright's Chromium is not installed:
    python -m playwright install chromium
"""

import asyncio
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.settings import Settings  # noqa: E402
from src.core.exceptions import BrowserError  # noqa: E402
from src.infrastructure.browser import BrowserService  # noqa: E402
from src.utils.dom import DOMProcessor  # noqa: E402

pytestmark = pytest.mark.real_browser

_INDEX_HTML = """<!doctype html><html><head><title>Smoke Index</title></head><body>
<h1>CogniWeb real-browser smoke</h1>
<nav><a href="/page2" id="next-link">Go to page two</a></nav>
<form id="f" action="/submit" method="get">
  <label for="name">Name</label>
  <input id="name" name="name" placeholder="your name">
  <label for="email">Email</label>
  <!-- Deliberately NOT type="email": native constraint validation would
       SILENTLY block form submission on invalid content - a real-Chromium
       behavior worth knowing about, but not what this smoke test is for. -->
  <input id="email" name="email" type="text">
  <button id="submit-btn" type="submit">Send form</button>
</form>
</body></html>"""

_PAGE2_HTML = """<!doctype html><html><head><title>Smoke Page Two</title></head><body>
<button id="ping" onclick="document.getElementById('out').textContent='pong'">
Ping me
</button>
<span id="out"></span>
</body></html>"""


def _submit_html(query: str) -> str:
    params = urllib.parse.parse_qs(query)
    name = params.get("name", [""])[0]
    email = params.get("email", [""])[0]
    return (
        "<!doctype html><html><head><title>Smoke Submitted</title></head><body>"
        f"<h1>Thanks, {name}</h1><p>Email recorded: {email}</p>"
        "</body></html>"
    )


def _serve(path: str, query: str) -> tuple[int, bytes]:
    if path == "/" or path == "/index.html":
        return 200, _INDEX_HTML.encode()
    if path == "/page2":
        return 200, _PAGE2_HTML.encode()
    if path == "/submit":
        return 200, _submit_html(query).encode()
    return 404, b"not found"


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
        # The pages are served on 127.0.0.1; the SSRF guard would (by
        # design) refuse loopback targets - opt out for THIS instance.
        navigate_block_private_networks=False,
    )


# CRLF built from hex escapes: a backslash-r-backslash-n escape written
# literally in this file keeps getting normalized into real line breaks.
_CRLF = b"\x0d\x0a"


@pytest.fixture
async def site_url():
    """Serve the local page set on an ephemeral 127.0.0.1 port."""

    async def handle(reader, writer):
        try:
            raw = await reader.read(8192)
            request_line = raw.split(_CRLF, 1)[0].decode("latin-1")
            parts = request_line.split(" ")
            target = parts[1] if len(parts) > 1 else "/"
            parsed = urllib.parse.urlsplit(target)
            status, body = _serve(parsed.path, parsed.query)
            reason = "OK" if status == 200 else "Not Found"
            # NOTE: the header block must be terminated by a BLANK line
            # (CRLF CRLF) before the body - hence the extra _CRLF appended
            # below (a trailing empty element in join() would only produce
            # one CRLF, and Chromium would see no end-of-headers marker).
            headers = _CRLF.join(
                [
                    f"HTTP/1.1 {status} {reason}".encode(),
                    b"Content-Type: text/html; charset=utf-8",
                    f"Content-Length: {len(body)}".encode(),
                    b"Connection: close",
                ]
            )
            writer.write(headers + _CRLF + _CRLF + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.close()
    await server.wait_closed()


async def _extract_and_register(browser: BrowserService, settings) -> dict:
    """Mirror the orchestrator's observation step: extract interactive
    elements from the LIVE DOM and register their selectors into
    browser.element_map (the single source of truth for element_id)."""
    elements, extraction_error = await DOMProcessor(settings).get_interactive_elements(
        browser.page
    )
    assert extraction_error is None, f"live DOM extraction failed: {extraction_error}"
    browser.element_map.update({elem["id"]: elem["selector"] for elem in elements})
    return {elem["tag"]: elem["id"] for elem in elements}


def _skip_if_no_chromium(e: BrowserError):
    message = str(e)
    if "Executable doesn't exist" in message or "playwright install" in message.lower():
        pytest.skip("Playwright Chromium not installed - run: python -m playwright install chromium")
    raise


# ---------------------------------------------------------------------------
# (a) launch + navigate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smoke_launch_navigate_title(settings, site_url):
    browser = BrowserService(settings)
    try:
        async with browser:
            nav = await browser.navigate(f"{site_url}/")
            assert nav.success, f"navigate failed: {nav.message}"
            title = await browser.get_page_title()
            assert title == "Smoke Index", f"unexpected real page title: {title!r}"
            url = await browser.get_current_url()
            assert url.startswith(site_url)
    except BrowserError as e:
        _skip_if_no_chromium(e)


# ---------------------------------------------------------------------------
# (b) find + click a real element
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smoke_find_and_click_element(settings, site_url):
    browser = BrowserService(settings)
    try:
        async with browser:
            await browser.navigate(f"{site_url}/page2")
            by_tag = await _extract_and_register(browser, settings)
            assert "button" in by_tag, f"button not found in live DOM: {by_tag}"

            click = await browser.click_element_safe(by_tag["button"])
            assert click.success, f"click failed: {click.message}"

            # The click ran REAL JS (onclick handler) in REAL Chromium.
            out_text = await browser.page.locator("#out").first.text_content()
            assert out_text == "pong", f"onclick did not fire: {out_text!r}"
    except BrowserError as e:
        _skip_if_no_chromium(e)


# ---------------------------------------------------------------------------
# (c) fill + submit a real form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smoke_fill_and_submit_form(settings, site_url):
    browser = BrowserService(settings)
    try:
        async with browser:
            await browser.navigate(f"{site_url}/")
            by_tag = await _extract_and_register(browser, settings)
            assert {"input", "button"} <= set(by_tag), f"form parts missing: {by_tag}"

            # Fill fields deterministically: extraction returns ids in DOM
            # order, so inputs[0] = name, inputs[1] = email. (Pitfall found
            # by this very test: picking "the input" via a tag->id map grabs
            # the LAST one - here that was originally an type=email field,
            # whose native constraint validation silently blocked submit.)
            elements, err = await DOMProcessor(settings).get_interactive_elements(
                browser.page
            )
            assert err is None
            input_elems = [e for e in elements if e["tag"] == "input"]
            assert len(input_elems) == 2, f"expected 2 inputs on live page: {elements}"
            browser.element_map.update({e["id"]: e["selector"] for e in input_elems})
            typed_name = await browser.type_text(input_elems[0]["id"], "Ada Lovelace")
            assert typed_name.success, f"type into name field failed: {typed_name.message}"
            typed_email = await browser.type_text(input_elems[1]["id"], "ada@example.com")
            assert typed_email.success, f"type into email field failed: {typed_email.message}"

            submitted = await browser.click_element_safe(by_tag["button"])
            assert submitted.success, f"form submit click failed: {submitted.message}"

            # GET-form submit navigated to /submit; wait for the REAL
            # navigation instead of racing it with an immediate title read.
            await browser.page.wait_for_url("**/submit*", timeout=10_000)

            # ...and the SERVER rendered the values the real browser sent.
            assert await browser.get_page_title() == "Smoke Submitted"
            url = await browser.get_current_url()
            assert "/submit?" in url, f"expected GET-form query string, got: {url}"
            heading = await browser.page.locator("h1").first.text_content()
            assert heading == "Thanks, Ada Lovelace", f"server saw wrong data: {heading!r}"
    except BrowserError as e:
        _skip_if_no_chromium(e)


# ---------------------------------------------------------------------------
# (d) clean shutdown, no zombie processes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smoke_clean_close_no_zombie(settings, site_url):
    """After the context-manager exit every handle must be released, and a
    SECOND sequential launch must succeed on the same persistent profile -
    a zombie Chromium holding the user_data_dir lock would fail right
    here (this is exactly how leaked browsers manifest)."""
    first = BrowserService(settings)
    try:
        async with first:
            await first.navigate(f"{site_url}/")
            assert await first.get_page_title() == "Smoke Index"
    except BrowserError as e:
        _skip_if_no_chromium(e)

    assert first.context is None, "context survived close()"
    assert first.page is None, "page survived close()"
    assert first.playwright is None, "playwright driver survived close()"

    second = BrowserService(settings)
    try:
        async with second:
            nav = await second.navigate(f"{site_url}/page2")
            assert nav.success, f"second launch could not navigate: {nav.message}"
            assert await second.get_page_title() == "Smoke Page Two"
    except BrowserError as e:
        _skip_if_no_chromium(e)
    finally:
        assert second.context is None
        assert second.playwright is None