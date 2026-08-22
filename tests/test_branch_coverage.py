"""Extra branch coverage: LLMService error paths and the orchestrator's
_execute_action tool handlers / recovery branches."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import APIConnectionError, RateLimitError as OpenAIRateLimitError

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent.orchestrator import AgentOrchestrator
from src.config.settings import Settings
from src.core.exceptions import LLMError
from src.core.models import ActionResult, AgentAction
from src.infrastructure.llm import LLMService


def make_settings(tmp_path, **overrides):
    base = {
        "api_key": "sk-test-key-not-real",
        "api_base_url": "https://api.test.com/v1",
        "model_name": "test-provider/test-model",
        "user_data_dir": tmp_path / "b",
        "screenshot_dir": tmp_path / "s",
        "checkpoint_dir": tmp_path / "c",
        "reports_dir": tmp_path / "r",
        "upload_allowed_dir": tmp_path / "u",
        "agent_step_delay": 0.0,
        "enable_context_compaction": False,
    }
    base.update(overrides)
    return Settings(**base)


def make_browser():
    browser = AsyncMock()
    browser.element_map = {}
    browser.navigate = AsyncMock(return_value=ActionResult(success=True, message="ok"))
    browser.click_element_safe = AsyncMock(return_value=ActionResult(success=True, message="ok"))
    browser.type_text = AsyncMock(return_value=ActionResult(success=True, message="ok"))
    browser.select_option = AsyncMock(return_value=ActionResult(success=True, message="ok"))
    browser.upload_file = AsyncMock(return_value=ActionResult(success=True, message="ok"))
    browser.get_current_url = AsyncMock(return_value="https://example.com")
    browser.get_page_title = AsyncMock(return_value="T")
    browser.detect_captcha = AsyncMock(return_value=False)
    browser.page = AsyncMock()
    return browser


def _resp(content, usage=None):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    r.usage = usage
    return r


def _mock_client(service, side_effect=None, return_value=None):
    service.client = MagicMock()
    service.client.chat = MagicMock()
    service.client.chat.completions = MagicMock()
    if side_effect is not None:
        service.client.chat.completions.create = AsyncMock(side_effect=side_effect)
    else:
        service.client.chat.completions.create = AsyncMock(return_value=return_value)


class TestLLMServiceBranches:
    @pytest.mark.asyncio
    async def test_generate_text_success_and_usage(self, tmp_path):
        llm = LLMService(make_settings(tmp_path))
        usage = MagicMock(prompt_tokens=7, completion_tokens=3)
        _mock_client(llm, return_value=_resp("hello", usage=usage))
        assert await llm.generate_text(messages=[]) == "hello"
        assert llm.total_prompt_tokens == 7
        assert llm.total_completion_tokens == 3

    @pytest.mark.asyncio
    async def test_generate_text_wraps_and_retries_transient_errors(self, tmp_path):
        llm = LLMService(make_settings(tmp_path))
        # each of these is wrapped as retryable NetworkError; two failures
        # then success stays within tenacity's stop_after_attempt(3)
        _mock_client(
            llm,
            side_effect=[
                httpx.TimeoutException("t"),
                APIConnectionError(request=MagicMock()),
                _resp("finally"),
            ],
        )
        assert await llm.generate_text(messages=[]) == "finally"
        assert llm.client.chat.completions.create.await_count == 3
        # 429 rate limit also maps to NetworkError on its own
        llm2 = LLMService(make_settings(tmp_path))
        _mock_client(
            llm2,
            side_effect=[
                OpenAIRateLimitError(
                    "rl", response=MagicMock(status_code=429, headers={}), body=None
                ),
                _resp("ok"),
            ],
        )
        assert await llm2.generate_text(messages=[]) == "ok"

    @pytest.mark.asyncio
    async def test_generate_text_empty_and_no_choices(self, tmp_path):
        llm = LLMService(make_settings(tmp_path))
        _mock_client(llm, return_value=_resp("   "))
        with pytest.raises(LLMError):
            await llm.generate_text(messages=[])
        no_choice = MagicMock()
        no_choice.choices = []
        _mock_client(llm, return_value=no_choice)
        with pytest.raises(LLMError):
            await llm.generate_text(messages=[])

    @pytest.mark.asyncio
    async def test_generate_action_empty_and_schema_errors(self, tmp_path):
        llm = LLMService(make_settings(tmp_path))
        _mock_client(llm, return_value=_resp(""))
        with pytest.raises(LLMError, match="Empty response"):
            await llm.generate_action(messages=[])
        _mock_client(llm, return_value=_resp("no json here at all"))
        with pytest.raises(LLMError, match="No valid JSON"):
            await llm.generate_action(messages=[])
        _mock_client(llm, return_value=_resp('{"tool": "bogus_tool", "args": {}}'))
        with pytest.raises(LLMError, match="schema validation"):
            await llm.generate_action(messages=[])
        _mock_client(llm, return_value=_resp('{"tool": "navigate", "args": {"url": "x"}}'))
        assert (await llm.generate_action(messages=[])).tool == "navigate"

    @pytest.mark.asyncio
    async def test_close_and_context_manager(self, tmp_path):
        llm = LLMService(make_settings(tmp_path))
        llm.client = MagicMock()
        llm.client.close = AsyncMock()
        async with llm as service:
            assert service is llm
        llm.client.close.assert_awaited()

    def test_json_extraction_variants(self, tmp_path):
        llm = LLMService(make_settings(tmp_path))
        assert llm._extract_json_from_response("") == ""
        # braces present but unparseable even after cleaning
        assert llm._extract_json_from_response("{ broken") == ""
        # code block that fails to parse falls through to brace scan
        out = llm._extract_json_from_response('```json\n{"a": 1}\n```')
        assert json.loads(out) == {"a": 1}
        # smart quotes and newlines inside
        out = llm._extract_json_from_response('{"a": "x",\n"b": “c”}')
        assert out


class TestExecuteActionTools:
    def _orch(self, tmp_path, browser=None):
        settings = make_settings(tmp_path)
        return AgentOrchestrator(settings, browser or make_browser(), AsyncMock())

    @pytest.mark.asyncio
    async def test_navigate_missing_url(self, tmp_path):
        r = await self._orch(tmp_path)._execute_action(
            AgentAction(tool="navigate", args={"url": ""})
        )
        assert r.error == "MissingUrl"

    @pytest.mark.asyncio
    async def test_click_invalid_and_type_errors(self, tmp_path):
        orch = self._orch(tmp_path)
        r = await orch._execute_action(AgentAction(tool="click_element", args={"element_id": None}))
        assert r.error == "MissingElementId"
        r = await orch._execute_action(
            AgentAction(tool="click_element", args={"element_id": "abc"})
        )
        assert r.error == "InvalidType"
        orch.browser.element_map[1] = "sel"
        r = await orch._execute_action(AgentAction(tool="click_element", args={"element_id": 77}))
        assert r.error == "InvalidElementId"
        r = await orch._execute_action(
            AgentAction(tool="type_text", args={"element_id": 1, "text": ""})
        )
        assert r.error == "MissingText"

    @pytest.mark.asyncio
    async def test_click_refreshes_observation(self, tmp_path, monkeypatch):
        orch = self._orch(tmp_path)
        orch.browser.element_map[1] = "sel"

        async def fake_obs():
            return "URL: x"

        monkeypatch.setattr(AgentOrchestrator, "_get_observation", lambda self: fake_obs())
        r = await orch._execute_action(AgentAction(tool="click_element", args={"element_id": 1}))
        assert r.success is True
        assert orch.previous_observation == "URL: x"

    @pytest.mark.asyncio
    async def test_type_select_upload_dispatch(self, tmp_path):
        orch = self._orch(tmp_path)
        orch.browser.element_map[1] = orch.browser.element_map[2] = "sel"
        r = await orch._execute_action(
            AgentAction(tool="type_text", args={"element_id": 1, "text": "hi"})
        )
        assert r.success is True
        r = await orch._execute_action(
            AgentAction(tool="select_option", args={"element_id": 1, "value": "v"})
        )
        assert r.success is True
        f = orch.settings.upload_allowed_dir / "f.txt"
        f.write_text("x")
        r = await orch._execute_action(
            AgentAction(tool="upload_file", args={"element_id": 1, "file_path": "f.txt"})
        )
        assert r.success is True

    @pytest.mark.asyncio
    async def test_scroll_direction_validation(self, tmp_path):
        orch = self._orch(tmp_path)
        a = AgentAction(tool="scroll_page", args={})
        a.args["direction"] = "left"  # bypass model validation: raw handler check
        r = await orch._execute_action(a)
        assert r.error == "InvalidDirection"

    @pytest.mark.asyncio
    async def test_wait_caps_and_invalid_type(self, tmp_path):
        orch = self._orch(tmp_path)
        r = await orch._execute_action(AgentAction(tool="wait", args={"seconds": "soon"}))
        assert r.error == "InvalidType"
        r = await orch._execute_action(AgentAction(tool="wait", args={"seconds": 999}))
        assert r.success is True  # capped, but still a success

    @pytest.mark.asyncio
    async def test_go_back_and_screenshot(self, tmp_path):
        orch = self._orch(tmp_path)
        r = await orch._execute_action(AgentAction(tool="go_back", args={}))
        assert r.success is True
        orch.browser.page.screenshot = AsyncMock(side_effect=RuntimeError("no"))
        r = await orch._execute_action(AgentAction(tool="take_screenshot", args={}))
        assert r.success is False

    @pytest.mark.asyncio
    async def test_query_dom_branches(self, tmp_path):
        orch = self._orch(tmp_path)
        r = await orch._execute_action(AgentAction(tool="query_dom", args={"query": "x"}))
        assert r.error == "NoObservation"
        orch.previous_observation = "[1] BUTTON Apply\n[2] LINK Contact us"
        r = await orch._execute_action(AgentAction(tool="query_dom", args={}))
        assert r.error == "MissingQuery"
        r = await orch._execute_action(AgentAction(tool="query_dom", args={"query": "contact"}))
        assert r.success is True
        r = await orch._execute_action(AgentAction(tool="query_dom", args={"query": "zzz"}))
        assert r.error == "NotFound"

    @pytest.mark.asyncio
    async def test_store_context_formats(self, tmp_path):
        orch = self._orch(tmp_path)
        r = await orch._execute_action(
            AgentAction(tool="store_context", args={"key": "k", "value": "v"})
        )
        assert r.success is True
        r = await orch._execute_action(AgentAction(tool="store_context", args={}))
        assert r.error == "NoDataProvided"
        r = await orch._execute_action(AgentAction(tool="store_context", args={"a": 1, "b": 2}))
        assert r.success is True
        assert orch.context_data == {"k": "v", "a": 1, "b": 2}

    @pytest.mark.asyncio
    async def test_unknown_tool(self, tmp_path):
        orch = self._orch(tmp_path)
        a = AgentAction(tool="wait", args={})
        a.tool = "teleport"  # bypass literal typing for the raw handler
        r = await orch._execute_action(a)
        assert r.error == "UnknownTool"


class TestOrchestratorRecovery:
    @pytest.mark.asyncio
    async def test_non_json_llm_error_continues(self, tmp_path, monkeypatch):
        from tests.test_phase_features import patch_dom_empty

        patch_dom_empty(monkeypatch)
        settings = make_settings(tmp_path, max_steps=6)
        llm = AsyncMock()
        llm.generate_action = AsyncMock(
            side_effect=[
                LLMError("API quota exceeded permanently"),
                AgentAction(tool="done", args={"summary": "ok"}),
            ]
        )
        orch = AgentOrchestrator(settings, make_browser(), llm)
        result = await orch.run("t")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_context_compaction(self, tmp_path, monkeypatch):
        from tests.test_phase_features import patch_dom_empty

        settings = make_settings(
            tmp_path, enable_context_compaction=True, compaction_trigger_messages=5
        )
        patch_dom_empty(monkeypatch)
        llm = AsyncMock()
        llm.generate_action = AsyncMock(
            return_value=AgentAction(tool="done", args={"summary": "ok"})
        )
        llm.generate_text = AsyncMock(return_value="Status: nothing done yet.")

        # Run the compaction trigger check directly on a fat history
        orch = AgentOrchestrator(settings, make_browser(), llm)
        orch.task = "t"
        orch.conversation_history = [
            {"role": "system", "content": "SYS"},
            *[{"role": "user", "content": f"msg{i}"} for i in range(10)],
        ]
        await orch._maybe_compact_history()
        assert len(orch.conversation_history) == 2
        assert orch.conversation_history[0]["role"] == "system"
        assert "nothing done yet" in orch.conversation_history[1]["content"]

    @pytest.mark.asyncio
    async def test_compaction_failure_is_non_fatal(self, tmp_path):
        settings = make_settings(tmp_path)
        llm = AsyncMock()
        llm.generate_text = AsyncMock(side_effect=RuntimeError("net down"))
        orch = AgentOrchestrator(settings, make_browser(), llm)
        orch.task = "t"
        orch.conversation_history = [
            {"role": "system", "content": "SYS"},
            *[{"role": "user", "content": f"m{i}"} for i in range(10)],
        ]
        await orch._maybe_compact_history()  # must not raise
        assert len(orch.conversation_history) == 11  # untouched

    def test_should_use_vision_fallback_gates(self, tmp_path):
        settings = make_settings(tmp_path, enable_vision_fallback=True, model_supports_vision=True)
        orch = AgentOrchestrator(settings, make_browser(), AsyncMock())
        orch._last_elements = [{"text": "x"}]  # healthy page: no fallback
        assert orch._should_use_vision_fallback() is False
        orch._last_extraction_error = "js broke"
        assert orch._should_use_vision_fallback() is True
        orch._last_extraction_error = None
        orch._last_elements = []  # empty page
        assert orch._should_use_vision_fallback() is True
        orch._last_elements = [{"text": "x"}] * 100  # many, mostly textless
        orch._last_elements[0]["text"] = ""
        assert orch._should_use_vision_fallback() is True
        settings2 = make_settings(
            tmp_path, enable_vision_fallback=False, model_supports_vision=True
        )
        orch2 = AgentOrchestrator(settings2, make_browser(), AsyncMock())
        orch2._last_extraction_error = "js broke"
        assert orch2._should_use_vision_fallback() is False

    @pytest.mark.asyncio
    async def test_build_compaction_prompt_multimodal(self, tmp_path):
        settings = make_settings(tmp_path)
        orch = AgentOrchestrator(settings, make_browser(), AsyncMock())
        orch.task = "t"
        orch.context_data = {"a": 1}
        history = [
            {"role": "user", "content": [{"type": "text", "text": "img"}]},
            {"role": "assistant", "content": "did"},
        ]
        prompt = orch._build_compaction_prompt(history)
        assert "[screenshot-based vision step]" in prompt
        assert "Original task: t" in prompt
