"""Regression tests for the remaining-fixes iteration (ТЗ: локальные модели,
element_id унификация, metrics graceful-degradation coverage):

1. Reasoning/thinking blocks (<think>...</think>) are stripped BEFORE JSON
   extraction - local reasoning models must not poison brace-scanning.
2. element_id type handling is IDENTICAL across every tool branch
   (click/type_text/select_option/hover/upload/download all accept 5 and "5",
   all reject "abc" with InvalidType).
3. infrastructure/metrics.py graceful-degradation branches (no
   prometheus_client) and its swallow-all error paths are covered.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

try:  # prometheus-client is an OPTIONAL extra (requirements/api.txt); CI's
    # unit-test and lock-verify jobs intentionally install a minimal env
    # without it, so the error-path tests below must skip, not fail.
    import prometheus_client  # noqa: F401

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

from src.agent.orchestrator import AgentOrchestrator  # noqa: E402
from src.config.settings import Settings  # noqa: E402
from src.core.models import ActionResult, AgentAction  # noqa: E402
from src.infrastructure.llm import LLMService  # noqa: E402


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


# ============================================================================
# FIX 1: reasoning/thinking blocks break JSON parsing
# ============================================================================


class TestReasoningBlockStripping:
    """<think>-strip must run before code-block regex / brace scanning."""

    def _llm(self, tmp_path, **overrides):
        return LLMService(make_settings(tmp_path, **overrides))

    def test_tz_reproduction_case(self, tmp_path):
        """Exact scenario from the review: deliberation quotes an example
        JSON action, then the final answer follows after </think>."""
        llm = self._llm(tmp_path)
        response = (
            "<think>Хорошо, мне нужно кликнуть на элемент. Формат такой: "
            '{"tool": "click_element"}. Но подожду, элемент 5 или 7?</think>\n'
            '{"tool": "click_element", "args": {"element_id": 7}}'
        )
        result = llm._extract_json_from_response(response)
        parsed = json.loads(result)
        # Must be the FINAL action, not the example quoted inside <think>.
        assert parsed["args"]["element_id"] == 7

    def test_multiple_think_blocks(self, tmp_path):
        llm = self._llm(tmp_path)
        response = (
            '<think>first {"a": 1}</think>'
            '<think>second {"b": 2}</think>'
            '{"tool": "wait", "args": {"seconds": 1}}'
        )
        parsed = json.loads(llm._extract_json_from_response(response))
        assert parsed["tool"] == "wait"

    def test_reasoning_tag_also_stripped(self, tmp_path):
        llm = self._llm(tmp_path)
        response = (
            "<reasoning>The user wants a navigation. "
            'Example: {"tool": "navigate"}</reasoning>'
            '{"tool": "navigate", "args": {"url": "https://example.com"}}'
        )
        parsed = json.loads(llm._extract_json_from_response(response))
        assert parsed["args"]["url"] == "https://example.com"

    def test_unclosed_think_block(self, tmp_path):
        """Truncated generation: opener with no closing tag anywhere."""
        llm = self._llm(tmp_path)
        response = '<think>I should probably click {"tool": "click_elem'
        # Everything after the unpaired opener is treated as unfinished
        # reasoning - no bogus partial action may be extracted.
        assert llm._extract_json_from_response(response) == ""

    def test_code_block_after_think_wins(self, tmp_path):
        llm = self._llm(tmp_path)
        response = (
            '<think>Format is like {"tool": "x"}</think>\n' '```json\n{"tool": "scroll_page"}\n```'
        )
        parsed = json.loads(llm._extract_json_from_response(response))
        assert parsed["tool"] == "scroll_page"

    def test_custom_strip_tags_via_settings(self, tmp_path):
        llm = self._llm(tmp_path, reasoning_strip_tags="thought")
        response = '<thought>deliberation {"decoy": true}</thought>' '{"tool": "wait"}'
        parsed = json.loads(llm._extract_json_from_response(response))
        assert parsed["tool"] == "wait"

    def test_angle_brackets_optional_in_setting(self, tmp_path):
        llm = self._llm(tmp_path, reasoning_strip_tags="<think>, <Reasoning>")
        response = '<REASONING>{"decoy": 1}</REASONING>' '{"tool": "wait"}'
        assert json.loads(llm._extract_json_from_response(response))["tool"] == "wait"

    def test_empty_setting_disables_stripping(self, tmp_path):
        llm = self._llm(tmp_path, reasoning_strip_tags="")
        plain = '{"tool": "wait", "args": {"seconds": 1}}'
        assert json.loads(llm._extract_json_from_response(plain))["tool"] == "wait"
        assert llm._reasoning_strip_tags == ()

    def test_plain_response_unchanged(self, tmp_path):
        llm = self._llm(tmp_path)
        response = 'Some preamble\n{"tool": "wait"}\nSome epilogue'
        assert json.loads(llm._extract_json_from_response(response))["tool"] == "wait"


# ============================================================================
# FIX 2: unified element_id type handling across tools
# ============================================================================


def make_browser():
    browser = AsyncMock()
    browser.element_map = {}
    browser.click_element_safe = AsyncMock(return_value=ActionResult(success=True, message="ok"))
    browser.type_text = AsyncMock(return_value=ActionResult(success=True, message="ok"))
    browser.select_option = AsyncMock(return_value=ActionResult(success=True, message="ok"))
    browser.hover_element = AsyncMock(return_value=ActionResult(success=True, message="ok"))
    browser.upload_file = AsyncMock(return_value=ActionResult(success=True, message="ok"))
    browser.download_file = AsyncMock(return_value=ActionResult(success=True, message="ok"))
    browser.get_current_url = AsyncMock(return_value="https://example.com")
    browser.page = AsyncMock()
    return browser


@pytest.fixture
def no_observation(monkeypatch):
    """click_element refreshes the observation after a successful click;
    stub that out so unit tests exercise only the dispatch logic."""

    async def fake_obs():
        return "URL: https://example.com"

    monkeypatch.setattr(AgentOrchestrator, "_get_observation", lambda self: fake_obs())


ELEMENT_TOOLS = [
    ("click_element", {"element_id": "5"}),
    ("type_text", {"element_id": "5", "text": "hi"}),
    ("select_option", {"element_id": "5", "value": "v"}),
    ("hover_element", {"element_id": "5"}),
    ("download_file", {"element_id": "5"}),
]


class TestUnifiedElementIdCoercion:
    def _orch(self, tmp_path, **overrides):
        return AgentOrchestrator(make_settings(tmp_path, **overrides), make_browser(), AsyncMock())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool,args", ELEMENT_TOOLS)
    async def test_string_element_id_accepted_everywhere(
        self, tmp_path, tool, args, no_observation
    ):
        """The SAME model output ("5") succeeds on EVERY tool - previously
        click coerced while type_text/select_option hard-rejected."""
        orch = self._orch(tmp_path)
        orch.browser.element_map[5] = "sel"
        r = await orch._execute_action(AgentAction(tool=tool, args=args))
        assert r.success is True, f"{tool} rejected string element_id: {r.message}"

    @pytest.mark.asyncio
    async def test_upload_file_accepts_string_element_id(self, tmp_path):
        orch = self._orch(tmp_path)
        orch.browser.element_map[5] = "sel"
        target = orch.settings.upload_allowed_dir / "f.txt"
        target.write_text("x")
        r = await orch._execute_action(
            AgentAction(
                tool="upload_file",
                args={"element_id": "5", "file_path": str(target)},
            )
        )
        assert r.success is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool,args", ELEMENT_TOOLS)
    async def test_garbage_element_id_invalid_type_everywhere(self, tmp_path, tool, args):
        """Non-coercible values still fail LOUDLY on every tool."""
        orch = self._orch(tmp_path)
        bad_args = dict(args, element_id="abc")
        r = await orch._execute_action(AgentAction(tool=tool, args=bad_args))
        assert r.error == "InvalidType"

    @pytest.mark.asyncio
    async def test_missing_element_id_still_reported(self, tmp_path):
        """None passes model-level validation but must be rejected here.
        (A fully absent key is already refused by AgentAction itself.)"""
        orch = self._orch(tmp_path)
        for tool in ("click_element", "type_text", "select_option"):
            args = {"element_id": None}
            if tool == "type_text":
                args["text"] = "hi"  # model-level validator requires the key
            elif tool == "select_option":
                args["value"] = "v"
            r = await orch._execute_action(AgentAction(tool=tool, args=args))
            assert r.error == "MissingElementId"

    def test_coerce_helper_contract(self):
        assert AgentOrchestrator._coerce_element_id("5") == 5
        assert AgentOrchestrator._coerce_element_id(" 7 ") == 7
        assert AgentOrchestrator._coerce_element_id(3) == 3
        assert AgentOrchestrator._coerce_element_id(4.0) == 4
        assert AgentOrchestrator._coerce_element_id("abc") is None
        assert AgentOrchestrator._coerce_element_id([1]) is None
        assert AgentOrchestrator._coerce_element_id(None) is None


# ============================================================================
# FIX 7: metrics.py graceful-degradation + error-path coverage
# ============================================================================


@pytest.fixture
def metrics_no_prometheus(monkeypatch):
    """Load src/infrastructure/metrics.py under a THROWAWAY module name
    with the prometheus_client import forced to fail (sys.modules[...] =
    None -> ImportError), yielding the degraded module.

    Deliberately NOT importlib.reload() on the shared module: reloading
    back would re-register the same metric names in the global REGISTRY
    and raise DuplicateTimeseries. A throwaway module object leaves the
    real one untouched."""
    import src.infrastructure.metrics as mm

    spec = importlib.util.spec_from_file_location("metrics_degraded_under_test", mm.__file__)
    mod = importlib.util.module_from_spec(spec)

    saved = sys.modules.get("prometheus_client")
    sys.modules["prometheus_client"] = None
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        if saved is not None:
            sys.modules["prometheus_client"] = saved
        else:
            del sys.modules["prometheus_client"]


class TestMetricsWithoutLibrary:
    def test_degraded_module_state(self, metrics_no_prometheus):
        assert metrics_no_prometheus.AVAILABLE is False

    def test_all_observers_are_no_ops(self, metrics_no_prometheus):
        metrics_no_prometheus.observe_task_queued("t")
        metrics_no_prometheus.observe_task_running("t")
        metrics_no_prometheus.observe_task_finished("t", success=True, duration_seconds=1.0)
        metrics_no_prometheus.observe_task("t", "failed", duration_seconds=None)
        metrics_no_prometheus.observe_llm_error("timeout")
        metrics_no_prometheus.set_browser_contexts(3)

    def test_render_returns_none_and_fallback_content_type(self, metrics_no_prometheus):
        assert metrics_no_prometheus.render() is None
        assert metrics_no_prometheus.CONTENT_TYPE_LATEST == "text/plain; charset=utf-8"


@pytest.mark.skipif(
    not PROMETHEUS_AVAILABLE,
    reason="prometheus-client (optional extra) not installed; degraded path is covered by TestMetricsWithoutLibrary",
)
class TestMetricsErrorPaths:
    """With the library present, observer failures must never propagate.

    Skipped when prometheus-client is not installed: the module degrades to
    no-op stubs without it (that path is covered by TestMetricsWithoutLibrary
    above), and the metric objects these tests monkeypatch simply do not
    exist in the degraded module."""

    def test_observe_task_swallows_metric_errors(self, monkeypatch):
        import src.infrastructure.metrics as mm

        def boom(*a, **kw):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(mm.TASKS_TOTAL, "labels", boom)
        monkeypatch.setattr(mm.TASK_DURATION, "labels", boom)
        mm.observe_task("t", "finished", duration_seconds=1.0)  # must not raise
        mm.observe_task_queued("t")  # short wrappers too

    def test_observe_llm_error_swallows(self, monkeypatch):
        import src.infrastructure.metrics as mm

        def boom(*a, **kw):
            raise RuntimeError("x")

        monkeypatch.setattr(mm.LLM_ERRORS, "labels", boom)
        mm.observe_llm_error("api_error")  # must not raise

    def test_set_browser_contexts_swallows(self, monkeypatch):
        import src.infrastructure.metrics as mm

        def boom(v):
            raise RuntimeError("x")

        monkeypatch.setattr(mm.BROWSER_CONTEXTS, "set", boom)
        mm.set_browser_contexts(2)  # must not raise

    def test_render_failure_returns_none(self, monkeypatch):
        import src.infrastructure.metrics as mm

        def boom(registry):
            raise RuntimeError("x")

        monkeypatch.setattr(mm, "generate_latest", boom)
        assert mm.render() is None
