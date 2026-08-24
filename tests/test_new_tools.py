"""
Task 2 (new agent tools) + Task 3 (Crawl4AI-style extraction) tests.

Covers, with mocks only (no real browser / LLM):
- AgentAction schema: every new tool validates (happy path) and rejects
  malformed args (error path).
- BrowserService: each new method's decision logic on a mocked Page.
- Orchestrator dispatch: one branch per new tool, success + failure.
- utils/extract.py: heuristic cleaner behavior + crawl4ai optional path.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError as PydanticValidationError

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent.orchestrator import AgentOrchestrator
from src.config.settings import Settings
from src.core.models import ActionResult, AgentAction
from src.utils.dom import DOMProcessor
from src.utils.extract import heuristic_html_to_markdown, html_to_markdown

# ============================================================================
# FIXTURES (mirrors test_phase_features.py conventions)
# ============================================================================


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
        "download_allowed_dir": tmp_path / "downloads",
        "agent_step_delay": 0.0,
        "rate_limit_seconds": 0.0,
        "enable_context_compaction": False,
    }
    base.update(overrides)
    return Settings(**base)


def make_browser():
    browser = AsyncMock()
    browser.element_map = {}
    browser.get_current_url = AsyncMock(return_value="https://example.com")
    browser.get_page_title = AsyncMock(return_value="Test Page")
    browser.detect_captcha = AsyncMock(return_value=False)
    browser.page = AsyncMock()
    return browser


def action(tool, **args):
    return AgentAction(thought="t", tool=tool, args=args)


def patch_dom_empty(monkeypatch, elements=None):
    async def fake(self, page):
        return (elements or [], None)

    monkeypatch.setattr(DOMProcessor, "get_interactive_elements", fake)


def make_orchestrator(settings, browser, actions_sequence):
    llm = AsyncMock()
    llm.generate_action = AsyncMock(side_effect=list(actions_sequence))
    return AgentOrchestrator(settings, browser, llm)


# ============================================================================
# Schema validation (models.py)
# ============================================================================


class TestNewToolSchema:
    def test_all_new_tools_accepted(self):
        minimal_args = {
            "wait_for_element": {"selector": ".x"},
            "hover_element": {"element_id": 1},
            "press_key": {"key": "Enter"},
            "extract_page_content": {},
            "extract_structured_data": {"key": "k"},
            "list_tabs": {},
            "switch_tab": {"index": 0},
            "download_file": {"element_id": 1},
            "go_forward": {},
            "find_element_by_text": {"text": "x"},
        }
        for tool, args in minimal_args.items():
            assert AgentAction(tool=tool, args=args).tool == tool

    def test_wait_for_element_variants(self):
        assert AgentAction(tool="wait_for_element", args={"element_id": 3})
        assert AgentAction(
            tool="wait_for_element", args={"selector": ".x", "state": "hidden", "timeout_ms": 5}
        )
        with pytest.raises(PydanticValidationError):
            AgentAction(tool="wait_for_element", args={})
        with pytest.raises(PydanticValidationError):
            AgentAction(tool="wait_for_element", args={"selector": ".x", "state": "bogus"})

    def test_hover_and_download_require_element(self):
        with pytest.raises(PydanticValidationError):
            AgentAction(tool="hover_element", args={})
        with pytest.raises(PydanticValidationError):
            AgentAction(tool="download_file", args={})

    def test_press_key_validation(self):
        assert AgentAction(tool="press_key", args={"key": "Enter"}).args["key"] == "Enter"
        with pytest.raises(PydanticValidationError):
            AgentAction(tool="press_key", args={})
        with pytest.raises(PydanticValidationError):
            AgentAction(tool="press_key", args={"key": "x" * 50})

    def test_extract_structured_data_requires_key(self):
        assert AgentAction(tool="extract_structured_data", args={"key": "prices"})
        with pytest.raises(PydanticValidationError):
            AgentAction(tool="extract_structured_data", args={})

    def test_switch_tab_requires_int_index(self):
        assert AgentAction(tool="switch_tab", args={"index": 1})
        with pytest.raises(PydanticValidationError):
            AgentAction(tool="switch_tab", args={})
        with pytest.raises(PydanticValidationError):
            AgentAction(tool="switch_tab", args={"index": "one"})

    def test_find_element_by_text_requires_text(self):
        assert AgentAction(tool="find_element_by_text", args={"text": "Apply"})
        with pytest.raises(PydanticValidationError):
            AgentAction(tool="find_element_by_text", args={"text": "  "})


# ============================================================================
# BrowserService new methods
# ============================================================================


def make_page():
    page = AsyncMock()
    page.url = "https://example.com/"
    return page


def locator_mock(href=None, bounding_box=None):
    loc = AsyncMock()
    first = AsyncMock()
    first.get_attribute = AsyncMock(return_value=href)
    loc.get_attribute = AsyncMock(return_value=href)
    loc.bounding_box = AsyncMock(return_value=bounding_box)
    loc.first = first
    return loc


class FakeExpectDownload:
    """Stand-in for `page.expect_download(...)`: an async context manager
    whose `info.value` is awaitable and yields the download mock. (On this
    Python version an AsyncMock instance itself is not awaitable, so
    .value must be a real coroutine.)"""

    def __init__(self, download=None, enter_error=None):
        self.download = download
        self.enter_error = enter_error

    def __call__(self, timeout=None):
        return self

    async def __aenter__(self):
        if self.enter_error is not None:
            raise self.enter_error

        async def value():
            return self.download

        from types import SimpleNamespace

        return SimpleNamespace(value=value())

    async def __aexit__(self, *exc):
        return False


class TestBrowserNewMethods:
    def _service(self, tmp_path, **overrides):
        from src.infrastructure.browser import BrowserService

        service = BrowserService(make_settings(tmp_path, **overrides))
        service.page = make_page()
        return service

    @pytest.mark.asyncio
    async def test_wait_for_element_success_and_timeout(self, tmp_path):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        service = self._service(tmp_path)
        service.element_map[3] = "[data-agent-id='3']"
        r = await service.wait_for_element(element_id=3, state="visible", timeout_ms=100)
        assert r.success is True
        service.page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeoutError("t/o"))
        r = await service.wait_for_element(element_id=3)
        assert r.error == "WaitForElementTimeout"
        # unknown id and no selector -> immediate error, no page call
        service.page.wait_for_selector.reset_mock()
        r = await service.wait_for_element(element_id=404)
        assert r.error == "InvalidElementID"
        service.page.wait_for_selector.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hover_element(self, tmp_path):
        service = self._service(tmp_path, enable_stealth_mode=False)
        service.element_map[5] = "[data-agent-id='5']"
        service.page.locator = MagicMock(return_value=locator_mock(href=None))
        r = await service.hover_element(5)
        assert r.success is True
        r = await service.hover_element(404)
        assert r.error == "InvalidElementID"

    @pytest.mark.asyncio
    async def test_press_key(self, tmp_path):
        service = self._service(tmp_path)
        r = await service.press_key("Control+a")
        assert r.success is True
        service.page.keyboard.press.assert_awaited_with("Control+a")
        r = await service.press_key("")
        assert r.error == "InvalidKey"

    @pytest.mark.asyncio
    async def test_extract_tables(self, tmp_path):
        service = self._service(tmp_path)
        service.page.evaluate = AsyncMock(
            return_value=[{"headers": ["Name"], "rows": [["Alice"], ["Bob"]]}]
        )
        tables = await service.extract_tables()
        assert tables[0]["headers"] == ["Name"]
        assert len(tables[0]["rows"]) == 2
        service.page.evaluate = AsyncMock(side_effect=RuntimeError("js broke"))
        assert await service.extract_tables() == []

    @pytest.mark.asyncio
    async def test_list_and_switch_tabs(self, tmp_path):
        service = self._service(tmp_path)
        page1, page2 = make_page(), make_page()
        page2.url = "https://example.com/tab2"
        service.context = MagicMock()
        service.context.pages = [page1, page2]
        tabs = await service.list_tabs()
        assert [t["index"] for t in tabs] == [0, 1]

        service.element_map[9] = "sel"
        r = await service.switch_tab(1)
        assert r.success is True
        assert service.page is page2
        assert service.element_map == {}  # ids were page-scoped
        r = await service.switch_tab(7)
        assert r.error == "TabIndexOutOfRange"

    @pytest.mark.asyncio
    async def test_go_forward(self, tmp_path):
        service = self._service(tmp_path)
        r = await service.go_forward()
        assert r.success is True
        service.page.go_forward = AsyncMock(side_effect=RuntimeError("no history"))
        r = await service.go_forward()
        assert r.success is False

    @pytest.mark.asyncio
    async def test_download_file_sanitizes_filename(self, tmp_path):
        service = self._service(tmp_path)
        service.element_map[2] = "[data-agent-id='2']"
        service.page.locator = MagicMock(return_value=locator_mock(href=None))

        download = AsyncMock()
        download.suggested_filename = "../../evil report.bin"
        dest_written = {}

        async def fake_save_as(path):
            dest_written["path"] = path
            Path(path).write_bytes(b"data")

        download.save_as = AsyncMock(side_effect=fake_save_as)
        service.page.expect_download = FakeExpectDownload(download=download)

        r = await service.download_file(2)
        assert r.success is True
        saved = Path(dest_written["path"])
        # Traversal stripped: only the basename, inside the allowed dir
        assert saved.parent.resolve() == service.settings.download_allowed_dir.resolve()
        assert saved.name == "evil report.bin"

    @pytest.mark.asyncio
    async def test_download_file_timeout(self, tmp_path):
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        service = self._service(tmp_path)
        service.element_map[2] = "[data-agent-id='2']"
        service.page.locator = MagicMock(return_value=locator_mock(href=None))
        service.page.expect_download = FakeExpectDownload(enter_error=PlaywrightTimeoutError("t/o"))
        r = await service.download_file(2)
        assert r.error == "DownloadTimeout"

    @pytest.mark.asyncio
    async def test_find_element_by_text_registers_ids(self, tmp_path):
        service = self._service(tmp_path)
        service.element_map[1] = "[data-agent-id='1']"
        matches = [
            {"id": 2, "tag": "button", "text": "Apply now", "selector": "[data-agent-id='2']"},
            {"id": 3, "tag": "a", "text": "Apply", "selector": "[data-agent-id='3']"},
        ]
        service.page.evaluate = AsyncMock(return_value=matches)
        found = await service.find_element_by_text("apply")
        assert [m["id"] for m in found] == [2, 3]
        # fresh ids registered into the live map (continuing after max id 1)
        assert service.element_map[2] == "[data-agent-id='2']"
        assert service.element_map[3] == "[data-agent-id='3']"
        # evaluate got start_id = max(existing)+1
        assert service.page.evaluate.await_args.args[1][3] == 2
        # empty needle -> no page round-trip
        service.page.evaluate.reset_mock()
        assert await service.find_element_by_text("  ") == []
        service.page.evaluate.assert_not_awaited()


# ============================================================================
# Orchestrator dispatch (one happy + one error path per tool)
# ============================================================================


class TestOrchestratorNewTools:
    @pytest.mark.asyncio
    async def test_wait_for_element_dispatch(self, tmp_path, monkeypatch):
        patch_dom_empty(monkeypatch)
        settings = make_settings(tmp_path)
        browser = make_browser()
        browser.wait_for_element = AsyncMock(
            return_value=ActionResult(success=True, message="matched")
        )
        orch = make_orchestrator(
            settings,
            browser,
            [action("wait_for_element", selector=".x"), action("done", summary="ok")],
        )
        result = await orch.run("t")
        assert result.success is True
        browser.wait_for_element.assert_awaited_once_with(
            element_id=None, selector=".x", state="visible", timeout_ms=None
        )

        # null selector passes schema validation but is rejected by the
        # dispatcher without touching the browser
        browser.wait_for_element.reset_mock()
        orch2 = make_orchestrator(
            settings,
            browser,
            [action("wait_for_element", selector=None), action("done", summary="ok")],
        )
        result = await orch2.run("t")
        assert result.success is True
        browser.wait_for_element.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hover_and_press_key(self, tmp_path, monkeypatch):
        # element_map is rebuilt from every observation, so the element
        # must exist in the (patched) DOM extraction, not just the map.
        patch_dom_empty(
            monkeypatch,
            elements=[
                {"id": 4, "tag": "button", "text": "hover me", "selector": "[data-agent-id='4']"}
            ],
        )
        settings = make_settings(tmp_path)
        browser = make_browser()
        browser.hover_element = AsyncMock(
            return_value=ActionResult(success=True, message="hovered")
        )
        browser.press_key = AsyncMock(return_value=ActionResult(success=True, message="Enter"))
        orch = make_orchestrator(
            settings,
            browser,
            [
                action("hover_element", element_id=4),
                action("press_key", key="Enter"),
                action("done", summary="ok"),
            ],
        )
        result = await orch.run("t")
        assert result.success is True
        browser.hover_element.assert_awaited_with(4)
        browser.press_key.assert_awaited_with("Enter")

    @pytest.mark.asyncio
    async def test_extract_page_content_flag_off(self, tmp_path, monkeypatch):
        patch_dom_empty(monkeypatch)
        settings = make_settings(tmp_path, enable_markdown_extraction=False)
        browser = make_browser()
        orch = make_orchestrator(
            settings, browser, [action("extract_page_content"), action("done", summary="ok")]
        )
        await orch.run("t")
        browser.page.content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_extract_page_content_happy_path(self, tmp_path, monkeypatch):
        patch_dom_empty(monkeypatch)
        settings = make_settings(tmp_path, enable_markdown_extraction=True)
        browser = make_browser()
        browser.page.content = AsyncMock(
            return_value="<html><title>Test</title><body><h1>Hi</h1><p>Body text</p></body></html>"
        )
        captured = []
        llm = AsyncMock()
        llm.generate_action = AsyncMock(
            side_effect=[action("extract_page_content"), action("done", summary="ok")]
        )
        orch = AgentOrchestrator(settings, browser, llm, event_sink=captured.append)
        result = await orch.run("t")
        assert result.success is True
        browser.page.content.assert_awaited_once()
        # the step event carries the cleaned markdown, not the raw HTML
        step_msgs = [e.get("message", "") for e in captured if e.get("type") == "step"]
        assert any("# Test" in m and "# Hi" in m for m in step_msgs)
        assert any("Body text" in m for m in step_msgs)

    @pytest.mark.asyncio
    async def test_extract_structured_data(self, tmp_path, monkeypatch):
        patch_dom_empty(monkeypatch)
        settings = make_settings(tmp_path)
        browser = make_browser()
        browser.extract_tables = AsyncMock(return_value=[{"headers": ["Name"], "rows": [["A"]]}])
        orch = make_orchestrator(
            settings,
            browser,
            [action("extract_structured_data", key="prices"), action("done", summary="ok")],
        )
        result = await orch.run("t")
        assert result.success is True
        assert result.context_data["prices"][0]["rows"] == [["A"]]

        # empty page -> NoTablesFound, nothing stored
        browser.extract_tables = AsyncMock(return_value=[])
        orch2 = make_orchestrator(
            settings,
            browser,
            [action("extract_structured_data", key="x"), action("done", summary="ok")],
        )
        await orch2.run("t")
        assert "x" not in orch2.context_data

    @pytest.mark.asyncio
    async def test_list_and_switch_tabs_dispatch(self, tmp_path, monkeypatch):
        patch_dom_empty(monkeypatch)
        settings = make_settings(tmp_path)
        browser = make_browser()
        browser.list_tabs = AsyncMock(return_value=[{"index": 0, "url": "https://a", "title": "A"}])
        browser.switch_tab = AsyncMock(return_value=ActionResult(success=True, message="ok"))
        orch = make_orchestrator(
            settings,
            browser,
            [
                action("list_tabs"),
                action("switch_tab", index=0),
                action("done", summary="ok"),
            ],
        )
        result = await orch.run("t")
        assert result.success is True
        browser.switch_tab.assert_awaited_with(0)

    @pytest.mark.asyncio
    async def test_download_and_find_by_text(self, tmp_path, monkeypatch):
        patch_dom_empty(
            monkeypatch,
            elements=[{"id": 7, "tag": "a", "text": "get file", "selector": "[data-agent-id='7']"}],
        )
        settings = make_settings(tmp_path)
        browser = make_browser()
        browser.download_file = AsyncMock(
            return_value=ActionResult(success=True, message="saved", data={"path": "/tmp/f"})
        )
        browser.find_element_by_text = AsyncMock(
            return_value=[
                {"id": 9, "tag": "button", "text": "Apply", "selector": "[data-agent-id='9']"}
            ]
        )
        orch = make_orchestrator(
            settings,
            browser,
            [
                action("download_file", element_id=7),
                action("find_element_by_text", text="Apply"),
                action("done", summary="ok"),
            ],
        )
        result = await orch.run("t")
        assert result.success is True
        browser.download_file.assert_awaited_once()
        browser.find_element_by_text.assert_awaited_once()

        # invalid element id -> standardized error, streak counter bumped
        orch2 = make_orchestrator(
            settings,
            browser,
            [action("download_file", element_id=999), action("done", summary="ok")],
        )
        await orch2.run("t")
        assert orch2._invalid_id_streak == 1

    @pytest.mark.asyncio
    async def test_go_forward_dispatch(self, tmp_path, monkeypatch):
        patch_dom_empty(monkeypatch)
        settings = make_settings(tmp_path)
        browser = make_browser()
        browser.go_forward = AsyncMock(return_value=ActionResult(success=True, message="fwd"))
        orch = make_orchestrator(
            settings, browser, [action("go_forward"), action("done", summary="ok")]
        )
        result = await orch.run("t")
        assert result.success is True
        browser.go_forward.assert_awaited_once()


# ============================================================================
# utils/extract.py (Crawl4AI approach with local fallback)
# ============================================================================


class TestMarkdownExtraction:
    def test_heuristic_removes_noise(self):
        html = """
        <html><head><title>Pricing</title><style>.x{color:red}</style>
        <script>alert('nope')</script></head>
        <body>
        <nav><a href="/home">Home</a></nav>
        <h1>Pricing</h1>
        <p>Our plans start at 10$.</p>
        <ul><li>Basic</li><li>Pro</li></ul>
        <table><tr><th>Plan</th><th>Price</th></tr><tr><td>Pro</td><td>20$</td></tr></table>
        <a href="/docs">Read docs</a>
        <footer>copyright</footer>
        </body></html>
        """
        md = heuristic_html_to_markdown(html, base_url="https://example.com/pricing")
        assert "# Pricing" in md  # title + h1
        assert "alert" not in md  # script content gone
        assert "color:red" not in md  # style gone
        assert "Home" not in md  # nav stripped
        assert "copyright" not in md  # footer stripped
        assert "- Basic" in md and "- Pro" in md
        assert "[Read docs](https://example.com/docs)" in md
        assert "Plan | Price |" in md  # table header cells
        assert "20$" in md

    def test_heuristic_truncates(self):
        html = "<p>" + "word " * 20000 + "</p>"
        md = heuristic_html_to_markdown(html, max_length=1000)
        assert len(md) < 1200
        assert md.endswith("[... truncated]")

    @pytest.mark.asyncio
    async def test_html_to_markdown_falls_back_when_crawl4ai_missing(self, monkeypatch):
        import src.utils.extract as extract_mod

        async def none_converter(page_html, base_url):
            return None

        monkeypatch.setattr(extract_mod, "_crawl4ai_markdown", none_converter)
        md = await html_to_markdown("<h1>Hello</h1>")
        assert "# Hello" in md

    @pytest.mark.asyncio
    async def test_html_to_markdown_uses_crawl4ai_when_available(self, monkeypatch):
        import src.utils.extract as extract_mod

        async def good_converter(page_html, base_url):
            return "# From crawl4ai"

        monkeypatch.setattr(extract_mod, "_crawl4ai_markdown", good_converter)
        md = await html_to_markdown("<h1>whatever</h1>")
        assert md == "# From crawl4ai"

    @pytest.mark.asyncio
    async def test_crawl4ai_import_failure_warns_once_and_falls_back(self, monkeypatch):
        import src.utils.extract as extract_mod

        monkeypatch.setitem(sys.modules, "crawl4ai.markdown_generation_strategy", None)
        monkeypatch.setattr(extract_mod, "_CRAWL4AI_STATE", {"warned": False, "available": None})
        md = await extract_mod._crawl4ai_markdown("<h1>x</h1>", "")  # returns None -> fallback
        assert md is None
        assert extract_mod._CRAWL4AI_STATE["available"] is False

    @pytest.mark.asyncio
    async def test_html_to_markdown_is_strictly_offline(self, monkeypatch):
        """SSRF guard (audit item 21): conversion must be pure text
        manipulation. A page's <img>/<link>/<a>/base_url must never trigger
        a fetch - otherwise an attacker-controlled page could make the
        agent's process issue requests to internal addresses through the
        converter (the same SSRF class navigate() guards against).

        Prohibitive form: every network-capable primitive is replaced with
        a tripwire that FAILS the test if touched. If a future dependency
        change makes the converter (or the optional crawl4ai path) go
        online, this test breaks loudly instead of silently opening an
        SSRF hole."""
        import socket

        import src.utils.extract as extract_mod

        def _tripwire(name):
            def _fail(*args, **kwargs):
                raise AssertionError(f"html_to_markdown attempted a network call via {name}")

            return _fail

        monkeypatch.setattr(socket.socket, "connect", _tripwire("socket.connect"))
        monkeypatch.setattr(socket.socket, "connect_ex", _tripwire("socket.connect_ex"))
        monkeypatch.setattr(socket, "getaddrinfo", _tripwire("socket.getaddrinfo"))
        monkeypatch.setattr(socket, "create_connection", _tripwire("socket.create_connection"))

        html = (
            "<html><head>"
            '<link rel="stylesheet" href="https://internal.example/steal.css">'
            "</head><body>"
            '<img src="http://169.254.169.254/latest/meta-data/">'
            '<iframe src="http://10.0.0.1/admin"></iframe>'
            '<a href="/relative">Rel</a>'
            "<p>content</p>"
            "</body></html>"
        )
        md = await extract_mod.html_to_markdown(html, base_url="https://example.com/page")
        assert "content" in md
        assert "[Rel](https://example.com/relative)" in md
