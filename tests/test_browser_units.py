"""
Unit tests for BrowserService internals (mocked Page) and DOMProcessor -
no real browser launches. Goal: cover the pure helpers and the action
methods' decision logic rather than Playwright itself.
"""

import socket
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.settings import Settings
from src.infrastructure.browser import (
    BrowserService,
    _check_navigation_host_policy,
    _host_is_allowed,
    _ip_is_private_network,
    _is_dangerous_url,
    _redact_sensitive_html,
)
from src.utils.dom import DOMProcessor


def _mock_public_dns():
    """Patch asyncio.get_running_loop so hostname resolution returns a public IP.

    Hermetic on purpose: these tests must not depend on the machine's real
    DNS. VPNs/proxies with fake-IP mode (e.g. 198.18.0.0/15) resolve public
    hosts into benchmark ranges that ipaddress correctly reports as private,
    so the SSRF guard would legitimately block navigation and break the test.
    """
    loop = MagicMock()
    loop.getaddrinfo = AsyncMock(
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    )
    return patch("asyncio.get_running_loop", return_value=loop)


def make_settings(tmp_path, **overrides):
    base = {
        "api_key": "sk-test-key-not-real",
        "api_base_url": "https://api.test.com/v1",
        "model_name": "test-provider/test-model",
        "user_data_dir": tmp_path / "browser_data",
        "screenshot_dir": tmp_path / "screenshots",
        "checkpoint_dir": tmp_path / "checkpoints",
        "reports_dir": tmp_path / "reports",
        "upload_allowed_dir": tmp_path / "uploads",
    }
    base.update(overrides)
    return Settings(**base)


def make_page():
    page = AsyncMock()
    page.url = "https://example.com/"
    return page


def locator_mock(href=None):
    """A locator mock with an explicit `.first` (auto-child mocks of
    AsyncMock don't await correctly on this Python version)."""
    loc = AsyncMock()
    first = AsyncMock()
    first.get_attribute = AsyncMock(return_value=href)
    loc.get_attribute = AsyncMock(return_value=href)
    loc.first = first
    return loc


class TestPureHelpers:
    def test_is_dangerous_url(self):
        assert _is_dangerous_url("javascript:alert(1)") == "javascript:"
        assert _is_dangerous_url("DATA:text/html,x") == "data:"
        assert _is_dangerous_url("file:///etc/passwd") == "file:"
        assert _is_dangerous_url("https://ok.com") is None
        assert _is_dangerous_url(None) is None
        assert _is_dangerous_url("") is None

    def test_host_is_allowed_exact_match_only(self):
        allowed = ["Example.com"]
        assert _host_is_allowed("example.com", allowed) is True
        assert _host_is_allowed("sub.example.com", allowed) is False
        assert _host_is_allowed("example.com", None) is False
        assert _host_is_allowed("notevil-example.com", allowed) is False

    def test_ip_is_private_network(self):
        import ipaddress

        private = [
            "10.0.0.1",
            "192.168.1.1",
            "172.16.0.1",
            "127.0.0.1",
            "169.254.169.254",
            "::1",
            "fe80::1",
            "0.0.0.0",
            "224.0.0.1",
        ]
        for addr in private:
            assert _ip_is_private_network(ipaddress.ip_address(addr)), addr
        assert not _ip_is_private_network(ipaddress.ip_address("93.184.216.34"))
        assert not _ip_is_private_network(ipaddress.ip_address("2606:2800:220:1::1"))

    @pytest.mark.asyncio
    async def test_host_policy_variants(self):
        # no restrictions configured, guard off -> never blocks
        assert await _check_navigation_host_policy("https://169.254.169.254/", None, False) is None
        # unknown-host DNS failure is not a policy block (goto will report it).
        # Hermetic: the resolver is mocked to fail - on this machine a VPN
        # fake-IP resolver "resolves" even .invalid names into 198.18.0.0/15,
        # which the guard would legitimately block.
        loop = MagicMock()
        loop.getaddrinfo = AsyncMock(side_effect=OSError("DNS failure"))
        with patch("asyncio.get_running_loop", return_value=loop):
            assert (
                await _check_navigation_host_policy("https://nonexistent.invalid/", None, True)
                is None
            )
        # IP literal host
        assert "private" in await _check_navigation_host_policy("https://10.0.0.5/", None, True)
        # allowlisted host bypasses private guard
        assert await _check_navigation_host_policy("https://10.0.0.5/", ["10.0.0.5"], True) is None
        # URL without host (shouldn't happen for http(s) but must not crash)
        assert await _check_navigation_host_policy("https:///", None, True) is None

    def test_redact_sensitive_html(self):
        html = (
            '<input type="password" name="pw" value="hunter2">'
            '<input value="secret123" type="password">'
            '<input type="hidden" name="csrf_token" value="abc123def">'
            '<input type="text" name="login" value="bob">'
        )
        out = _redact_sensitive_html(html)
        assert "hunter2" not in out
        assert "secret123" not in out
        assert "abc123def" not in out
        assert "bob" in out


class TestBrowserServiceActions:
    def _service(self, tmp_path, **overrides):
        service = BrowserService(make_settings(tmp_path, **overrides))
        service.page = make_page()
        return service

    @pytest.mark.asyncio
    async def test_navigate_type_and_empty_checks(self, tmp_path):
        service = self._service(tmp_path)
        r = await service.navigate(12345)
        assert r.error == "InvalidType"
        r = await service.navigate("   ")
        assert r.error == "InvalidURL"
        r = await service.navigate("javascript:alert(1)")
        assert r.error == "BlockedProtocol"
        service.page.goto.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_navigate_adds_scheme_and_waits(self, tmp_path):
        service = self._service(tmp_path)
        with _mock_public_dns():
            r = await service.navigate("example.com")
        assert r.success is True
        url = service.page.goto.await_args.args[0]
        assert url == "https://example.com"

    @pytest.mark.asyncio
    async def test_navigate_retries_then_times_out(self, tmp_path):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        service = self._service(tmp_path)
        service.page.goto = AsyncMock(side_effect=PlaywrightTimeoutError("t/o"))
        with _mock_public_dns():
            r = await service.navigate("https://example.com")
        assert r.success is False
        assert r.error == "NavigationTimeout"
        assert service.page.goto.await_count == 3

    @pytest.mark.asyncio
    async def test_navigate_generic_error_snapshots(self, tmp_path):
        service = self._service(tmp_path)
        service.page.goto = AsyncMock(side_effect=RuntimeError("dns broke"))
        r = await service.navigate("https://example.com")
        assert r.success is False

    @pytest.mark.asyncio
    async def test_navigate_blocked_after_redirect(self, tmp_path):
        service = self._service(tmp_path)
        service.page.url = "javascript:evil()"
        r = await service.navigate("https://redirector.example")
        assert r.error == "BlockedProtocol"

    @pytest.mark.asyncio
    async def test_element_safety_blocks_dangerous_href(self, tmp_path):
        service = self._service(tmp_path)
        service.element_map[7] = "[data-agent-id='7']"
        locator = locator_mock(href="javascript:steal()")
        service.page.locator = MagicMock(return_value=locator)
        r = await service.click_element_safe(7)
        assert r.error == "BlockedProtocol"
        r = await service.type_text(7, "hi")
        assert r.error == "BlockedProtocol"
        r = await service.select_option(7, "v")
        assert r.error == "BlockedProtocol"

    @pytest.mark.asyncio
    async def test_unknown_element_id_rejected(self, tmp_path):
        service = self._service(tmp_path)
        r = await service.click_element_safe(404)
        assert r.error == "InvalidElementID"
        r = await service.type_text(404, "hi")
        assert r.error == "InvalidElementID"
        r = await service.select_option(404, "v")
        assert r.error == "InvalidElementID"
        r = await service.upload_file(404, "f.txt")
        assert r.error == "InvalidElementID"

    @pytest.mark.asyncio
    async def test_click_success_path(self, tmp_path):
        service = self._service(tmp_path)
        service.element_map[1] = "[data-agent-id='1']"
        locator = locator_mock(href=None)
        service.page.locator = MagicMock(return_value=locator)
        r = await service.click_element_safe(1)
        assert r.success is True

    @pytest.mark.asyncio
    async def test_click_strict_mode_violation_uses_first(self, tmp_path):
        from playwright.async_api import Error as PlaywrightError

        service = self._service(tmp_path)
        service.element_map[1] = "[data-agent-id='1']"
        locator = locator_mock(href=None)
        service.page.locator = MagicMock(return_value=locator)
        service.page.click = AsyncMock(side_effect=PlaywrightError("strict mode violation"))
        r = await service.click_element_safe(1)
        assert r.success is True
        assert "fallback" in r.message.lower()

    @pytest.mark.asyncio
    async def test_click_timeout_after_retries(self, tmp_path):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        service = self._service(tmp_path)
        service.element_map[1] = "[data-agent-id='1']"
        locator = locator_mock(href=None)
        service.page.locator = MagicMock(return_value=locator)
        service.page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeoutError("t/o"))
        r = await service.click_element_safe(1)
        assert r.success is False
        assert r.error == "ElementNotFound"

    @pytest.mark.asyncio
    async def test_click_intercepted_uses_force_and_js_dispatch(self, tmp_path):
        from playwright.async_api import Error as PlaywrightError

        service = self._service(tmp_path)
        service.element_map[1] = "[data-agent-id='1']"
        locator = locator_mock(href=None)
        locator.click = AsyncMock(
            side_effect=[
                PlaywrightError("element is outside of the viewport"),
                PlaywrightError("still intercepted"),
                PlaywrightError("forced click intercepted too"),
            ]
        )
        locator.dispatch_event = AsyncMock()
        service.page.locator = MagicMock(return_value=locator)
        service.page.click = AsyncMock(side_effect=PlaywrightError("intercepts pointer events"))
        r = await service.click_element_safe(1)
        assert r.success is True
        assert "dispatch" in r.message.lower()

    @pytest.mark.asyncio
    async def test_type_text_success_with_typing_delays(self, tmp_path):
        service = self._service(tmp_path, typing_speed_min=10, typing_speed_max=50)
        service.element_map[2] = "[data-agent-id='2']"
        locator = locator_mock(href=None)
        service.page.locator = MagicMock(return_value=locator)
        r = await service.type_text(2, "hi", press_enter=True)
        assert r.success is True
        service.page.keyboard.press.assert_awaited_with("Enter")

    @pytest.mark.asyncio
    async def test_select_option_success(self, tmp_path):
        service = self._service(tmp_path)
        service.element_map[3] = "[data-agent-id='3']"
        locator = locator_mock(href=None)
        service.page.locator = MagicMock(return_value=locator)
        r = await service.select_option(3, "opt1")
        assert r.success is True

    @pytest.mark.asyncio
    async def test_upload_file_path_traversal_blocked(self, tmp_path):
        service = self._service(tmp_path)
        service.element_map[4] = "[data-agent-id='4']"
        locator = locator_mock(href=None)
        service.page.locator = MagicMock(return_value=locator)
        r = await service.upload_file(4, "../../etc/passwd")
        assert r.success is False
        assert r.error == "PathTraversalBlocked"

    @pytest.mark.asyncio
    async def test_upload_file_missing_file(self, tmp_path):
        service = self._service(tmp_path)
        service.element_map[4] = "[data-agent-id='4']"
        locator = locator_mock(href=None)
        service.page.locator = MagicMock(return_value=locator)
        r = await service.upload_file(4, "does_not_exist.txt")
        assert r.error == "FileNotFound"

    @pytest.mark.asyncio
    async def test_upload_file_success(self, tmp_path):
        service = self._service(tmp_path)
        service.element_map[4] = "[data-agent-id='4']"
        f = service.settings.upload_allowed_dir / "cv.pdf"
        f.write_bytes(b"%PDF")
        locator = locator_mock(href=None)
        service.page.locator = MagicMock(return_value=locator)
        r = await service.upload_file(4, "cv.pdf")
        assert r.success is True

    @pytest.mark.asyncio
    async def test_scroll(self, tmp_path):
        service = self._service(tmp_path)
        assert (await service.scroll("down")).success is True
        assert (await service.scroll("up")).success is True
        service.page.evaluate = AsyncMock(side_effect=RuntimeError("no js"))
        r = await service.scroll("down")
        assert r.success is False

    @pytest.mark.asyncio
    async def test_detect_captcha_and_metadata(self, tmp_path):
        service = self._service(tmp_path)
        service.page.query_selector = AsyncMock(side_effect=[None, None])
        assert await service.detect_captcha() is False
        service.page.query_selector = AsyncMock(return_value=AsyncMock())
        assert await service.detect_captcha() is True
        service.page.title = AsyncMock(return_value="T")
        assert await service.get_page_title() == "T"
        assert await service.get_current_url() == "https://example.com/"

    @pytest.mark.asyncio
    async def test_detect_captcha_never_raises(self, tmp_path):
        service = self._service(tmp_path)
        service.page.query_selector = AsyncMock(side_effect=RuntimeError("page gone"))
        assert await service.detect_captcha() is False

    @pytest.mark.asyncio
    async def test_capture_error_snapshot_writes_files(self, tmp_path):
        service = self._service(tmp_path)

        # page.screenshot(path=...) is mocked - it doesn't write anything,
        # but page.content() does return HTML for the dump. Make the
        # screenshot actually land so both assertions are real.
        async def fake_screenshot(path=None, **kw):
            Path(path).write_bytes(b"png")
            return b"png"

        service.page.screenshot = fake_screenshot
        service.page.content = AsyncMock(return_value="<html><body>dump</body></html>")
        shot, html = await service._capture_error_snapshot("unittest")
        assert shot is not None and shot.exists()
        assert html is not None and html.exists()

    @pytest.mark.asyncio
    async def test_capture_annotated_screenshot(self, tmp_path):
        service = self._service(tmp_path)
        service.page.screenshot = AsyncMock(return_value=b"png")
        elements = [{"id": 1}, {"id": 2}]
        data = await service.capture_annotated_screenshot(elements)
        assert data == b"png"
        # overlay was drawn and removed again
        assert service.page.evaluate.await_count == 2

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, tmp_path):
        service = self._service(tmp_path)
        await service.close()
        await service.close()
        assert service.page is None and service.context is None

    @pytest.mark.asyncio
    async def test_overlay_dismiss(self, tmp_path):
        service = self._service(tmp_path)
        visible = AsyncMock()
        visible.is_visible = AsyncMock(return_value=True)
        locator = MagicMock()
        locator.first = visible
        service.page.locator = MagicMock(return_value=locator)
        assert await service._try_dismiss_overlay() is True
        invisible = AsyncMock()
        invisible.is_visible = AsyncMock(return_value=False)
        service.page.locator = MagicMock(return_value=MagicMock(first=invisible))
        assert await service._try_dismiss_overlay() is False


class TestDOMProcessor:
    def test_annotate_duplicate_text(self):
        p = DOMProcessor()
        elements = [
            {"id": 0, "tag": "button", "text": "Apply", "selector": "a"},
            {"id": 1, "tag": "button", "text": "Apply", "selector": "b"},
            {"id": 2, "tag": "button", "text": "Unique", "selector": "c"},
            {"id": 3, "tag": "button", "text": "", "selector": "d"},
            {"id": 4, "tag": "button", "text": "", "selector": "e"},
        ]
        out = p._annotate_duplicate_text(elements)
        assert "(#1 of 2 similar)" in out[0]["text"]
        assert "(#2 of 2 similar)" in out[1]["text"]
        assert out[2]["text"] == "Unique"
        # empty text stays empty (no "(#1 of 2)" noise)
        assert out[3]["text"] == ""

    @pytest.mark.asyncio
    async def test_get_interactive_elements_uses_page_evaluate(self):
        p = DOMProcessor()
        page = AsyncMock()
        page.evaluate = AsyncMock(
            return_value=[{"id": 0, "tag": "a", "text": "Home", "selector": "[data-agent-id='0']"}]
        )
        elements, err = await p.get_interactive_elements(page)
        assert err is None
        assert elements[0]["text"] == "Home"
        # duplicate annotation applied
        assert isinstance(elements, list)

    @pytest.mark.asyncio
    async def test_get_interactive_elements_error_reported(self):
        p = DOMProcessor()
        page = AsyncMock()
        page.evaluate = AsyncMock(side_effect=RuntimeError("js broke"))
        elements, err = await p.get_interactive_elements(page)
        assert elements == []
        assert err is not None
