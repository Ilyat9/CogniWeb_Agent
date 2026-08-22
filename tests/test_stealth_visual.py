"""
Task 4 (stealth browser mode) + Task 3 (visual/set-of-marks fallback)
tests. Everything is mocked at the Playwright API boundary - what is
verified is that the right patches/options/calls are APPLIED, never real
bot-detection outcomes (non-deterministic, unfit for CI).

Regression guarantee: with ENABLE_STEALTH_MODE=false the launch path must
behave exactly like the pre-stealth code (no init scripts, no profile
options, no mouse choreography).
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.infrastructure.browser as browser_mod
from src.agent.orchestrator import AgentOrchestrator
from src.config.settings import Settings
from src.infrastructure.browser import BrowserService
from src.utils.dom import DOMProcessor


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
        "agent_step_delay": 0.0,
        "rate_limit_seconds": 0.0,
        "enable_context_compaction": False,
    }
    base.update(overrides)
    return Settings(**base)


def make_fake_playwright(context):
    """async_playwright() -> .start() -> playwright with a mocked chromium."""
    page = AsyncMock()
    context.pages = [page]
    context.add_init_script = AsyncMock()
    context.set_extra_http_headers = AsyncMock()
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
    starter = MagicMock()
    starter.start = AsyncMock(return_value=playwright)
    return starter, playwright, page


def bb_locator(href=None, bounding_box=None):
    """Locator mock with an explicit .first and a controllable bounding_box."""
    loc = AsyncMock()
    first = AsyncMock()
    first.get_attribute = AsyncMock(return_value=href)
    loc.get_attribute = AsyncMock(return_value=href)
    loc.bounding_box = AsyncMock(return_value=bounding_box)
    loc.first = first
    return loc


# ============================================================================
# Settings: stealth profile fields + aliases
# ============================================================================


class TestStealthSettings:
    def test_defaults(self, tmp_path):
        s = make_settings(tmp_path)
        assert s.enable_stealth_mode is True  # the one flag defaulting to ON
        assert "Chrome/131" in s.stealth_user_agent
        assert s.stealth_locale == "en-US"
        assert s.stealth_timezone == "America/New_York"
        assert (s.stealth_viewport_width, s.stealth_viewport_height) == (1920, 1080)

    def test_legacy_env_aliases_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_STEALTH", "false")
        monkeypatch.setenv("ENABLE_VISUAL_FALLBACK", "true")
        monkeypatch.chdir(tmp_path)
        s = Settings(
            api_key="sk-test-key-not-real",
            api_base_url="https://api.test.com/v1",
            model_name="test-provider/test-model",
            user_data_dir=tmp_path / "bd",
        )
        assert s.enable_stealth_mode is False
        assert s.enable_vision_fallback is True
        assert s.visual_fallback_error_streak == 2

    def test_legacy_alias_works_via_real_env_file(self, tmp_path, monkeypatch):
        """Old deployments spell the flag ENABLE_STEALTH in their .env file
        (not as an exported env var) - the alias must work through the
        dotenv source too, or existing .env files would silently stop
        disabling/enabling stealth."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "OPENAI_API_KEY=sk-test-key-not-real\n"
            "API_BASE_URL=https://api.test.com/v1\n"
            "MODEL_NAME=test-provider/test-model\n"
            "ENABLE_STEALTH=false\n",
            encoding="utf-8",
        )
        s = Settings(user_data_dir=tmp_path / "bd")
        assert s.enable_stealth_mode is False

        (tmp_path / ".env").write_text(
            "OPENAI_API_KEY=sk-test-key-not-real\n"
            "API_BASE_URL=https://api.test.com/v1\n"
            "MODEL_NAME=test-provider/test-model\n"
            "ENABLE_STEALTH_MODE=false\n",
            encoding="utf-8",
        )
        s2 = Settings(user_data_dir=tmp_path / "bd2")
        assert s2.enable_stealth_mode is False

    def test_visual_fallback_default_preserved(self, tmp_path):
        """The flag predates the visual-fallback work; its default stays
        `true` (project rule: existing defaults never change). Effective
        behavior is still off because MODEL_SUPPORTS_VISION defaults to
        false and gates every vision call."""
        s = make_settings(tmp_path)
        assert s.enable_vision_fallback is True
        assert s.model_supports_vision is False

    def test_bad_locale_rejected(self, tmp_path):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            make_settings(tmp_path, stealth_locale="not a locale!!")


# ============================================================================
# BrowserService.start(): stealth applied / not applied
# ============================================================================


class TestStealthLaunch:
    @pytest.mark.asyncio
    async def test_enabled_applies_consistent_profile(self, tmp_path, monkeypatch):
        settings = make_settings(
            tmp_path,
            stealth_locale="ru-RU",
            stealth_timezone="Europe/Moscow",
            stealth_user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        context = MagicMock()
        starter, playwright, page = make_fake_playwright(context)
        monkeypatch.setattr(browser_mod, "async_playwright", lambda: starter)
        # make the optional playwright-stealth package "unavailable":
        # built-in patches must still all apply
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "playwright_stealth", None)

        service = BrowserService(settings)
        try:
            await service.start()
            kwargs = playwright.chromium.launch_persistent_context.await_args.kwargs
            assert kwargs["user_agent"] == settings.stealth_user_agent
            assert kwargs["locale"] == "ru-RU"
            assert kwargs["timezone_id"] == "Europe/Moscow"
            assert kwargs["viewport"] == {"width": 1920, "height": 1080}
            assert "--disable-blink-features=AutomationControlled" in kwargs["args"]

            context.add_init_script.assert_awaited_once()
            script = context.add_init_script.await_args.args[0]
            assert "webdriver" in script
            assert "plugins" in script
            assert "ru-RU" in script  # languages follow the locale
            assert "37445" in script  # WebGL UNMASKED_VENDOR patch

            headers = context.set_extra_http_headers.await_args.args[0]
            assert headers["Accept-Language"].startswith("ru-RU")
            assert headers["sec-ch-ua-platform"] == '"Windows"'
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_disabled_keeps_pre_stealth_behavior(self, tmp_path, monkeypatch):
        """Regression: ENABLE_STEALTH_MODE=false must not change the launch
        path - no profile options, no init scripts."""
        settings = make_settings(tmp_path, enable_stealth_mode=False, captcha_avoidance_mode=False)
        context = MagicMock()
        starter, playwright, page = make_fake_playwright(context)
        monkeypatch.setattr(browser_mod, "async_playwright", lambda: starter)

        service = BrowserService(settings)
        try:
            await service.start()
            kwargs = playwright.chromium.launch_persistent_context.await_args.kwargs
            for absent in ("user_agent", "locale", "timezone_id"):
                assert absent not in kwargs
            assert kwargs["viewport"] == {"width": 1920, "height": 1080}  # original constant
            context.add_init_script.assert_not_awaited()
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_stealth_async_lazy_import_paths(self, monkeypatch):
        import sys as _sys

        # package "uninstalled" -> False, never raises
        monkeypatch.setitem(_sys.modules, "playwright_stealth", None)
        assert await browser_mod._stealth_async(AsyncMock()) is False

        # package present but apply fails -> False, never raises
        fake_mod = MagicMock()
        fake_mod.stealth_async = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setitem(_sys.modules, "playwright_stealth", fake_mod)
        assert await browser_mod._stealth_async(AsyncMock()) is False

        # healthy package -> True
        fake_mod.stealth_async = AsyncMock(return_value=None)
        assert await browser_mod._stealth_async(AsyncMock()) is True

    @pytest.mark.asyncio
    async def test_human_click_trajectory_applied(self, tmp_path):
        """With stealth on, a click is preceded by a jittered multi-point
        mouse move; with stealth off, the mouse is untouched."""
        box = {"x": 100, "y": 100, "width": 60, "height": 30}

        settings = make_settings(tmp_path, enable_stealth_mode=True)
        service = BrowserService(settings)
        service.page = AsyncMock()
        service.page.url = "https://example.com/"
        service.element_map[1] = "[data-agent-id='1']"
        service.page.locator = MagicMock(return_value=bb_locator(href=None, bounding_box=box))
        r = await service.click_element_safe(1)
        assert r.success is True
        assert service.page.mouse.move.await_count >= 4  # intermediate points + final

        # stealth off -> no choreography
        settings2 = make_settings(tmp_path, enable_stealth_mode=False)
        service2 = BrowserService(settings2)
        service2.page = AsyncMock()
        service2.page.url = "https://example.com/"
        service2.element_map[1] = "[data-agent-id='1']"
        service2.page.locator = MagicMock(return_value=bb_locator(href=None, bounding_box=box))
        r = await service2.click_element_safe(1)
        assert r.success is True
        assert service2.page.mouse.move.await_count == 0


# ============================================================================
# Task 3: visual fallback trigger by consecutive element-targeting failures
# ============================================================================


def make_orchestrator(settings, actions_sequence, elements=None):
    """elements: list of DOM element dicts; their ids become the live
    element_map entries."""
    elements = elements or []
    browser = AsyncMock()
    browser.element_map = {e["id"]: e["selector"] for e in elements}
    browser.get_current_url = AsyncMock(return_value="https://example.com")
    browser.get_page_title = AsyncMock(return_value="T")
    browser.detect_captcha = AsyncMock(return_value=False)
    browser.page = AsyncMock()
    browser.capture_annotated_screenshot = AsyncMock(return_value=b"png")
    llm = AsyncMock()
    llm.generate_action = AsyncMock(side_effect=list(actions_sequence))
    return AgentOrchestrator(settings, browser, llm), browser


def patch_dom(monkeypatch, elements=None, error=None):
    async def fake(self, page):
        return (elements or [], error)

    monkeypatch.setattr(DOMProcessor, "get_interactive_elements", fake)


class TestVisualFallbackStreak:
    def _settings(self, tmp_path, **over):
        base = {
            "enable_vision_fallback": True,
            "model_supports_vision": True,
        }
        base.update(over)
        return make_settings(tmp_path, visual_fallback_error_streak=2, **base)

    def test_streak_triggers_fallback(self, tmp_path):
        from src.core.models import AgentAction

        elements = [{"id": 1, "tag": "a", "text": "link", "selector": "[data-agent-id='1']"}]
        orch, _ = make_orchestrator(
            self._settings(tmp_path), [AgentAction(tool="done", args={})], elements=elements
        )
        # healthy extraction, but consecutive targeting failures:
        orch._last_elements = elements
        orch._last_extraction_error = None
        orch._invalid_id_streak = 1
        assert orch._should_use_vision_fallback() is False
        orch._invalid_id_streak = 2
        assert orch._should_use_vision_fallback() is True

    def test_flags_off_never_triggers(self, tmp_path):
        from src.core.models import AgentAction

        for over in (
            {"enable_vision_fallback": False},
            {"model_supports_vision": False},
        ):
            orch, _ = make_orchestrator(
                self._settings(tmp_path, **over), [AgentAction(tool="done", args={})]
            )
            orch._invalid_id_streak = 99
            orch._last_extraction_error = "boom"
            assert orch._should_use_vision_fallback() is False

    @pytest.mark.asyncio
    async def test_loop_switches_to_vision_after_two_failures(self, tmp_path, monkeypatch):
        """End-to-end: two InvalidElementId steps in a row -> the third
        LLM call goes through the annotated-screenshot path."""
        from src.core.models import AgentAction

        settings = self._settings(tmp_path)
        actions = [
            AgentAction(thought="t", tool="click_element", args={"element_id": 99}),
            AgentAction(thought="t", tool="click_element", args={"element_id": 99}),
            AgentAction(tool="done", args={"summary": "vision worked"}),
        ]
        elements = [{"id": 1, "tag": "a", "text": "link", "selector": "[data-agent-id='1']"}]
        patch_dom(monkeypatch, elements=elements)

        orch, browser = make_orchestrator(settings, actions, elements=elements)
        result = await orch.run("t")

        assert result.success is True
        assert result.summary == "vision worked"
        # two text-mode clicks failed, the 3rd call consumed by vision mode
        assert orch.llm.generate_action.await_count == 3
        browser.capture_annotated_screenshot.assert_awaited()
        # and the streak was reset by the successful vision-grounded step
        assert orch._invalid_id_streak == 0
