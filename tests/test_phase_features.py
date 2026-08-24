"""
Tests for the post-MVP revision: async hygiene (1.x), budget-based DOM
selection (2.1), evaluator (2.2), multi-page (2.3), navigation policy
(2.4), captcha circuit breaker (2.5), structured logging/token tracking
(3.1), ReAct loop + failure injection (3.2), API service mode (3.3).

All tests use mocks - no real API calls, no browser launches.
"""

import asyncio
import json
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent.orchestrator import AgentOrchestrator, run_parallel_agents
from src.config.settings import Settings
from src.core.exceptions import LLMError, NetworkError
from src.core.models import ActionResult, AgentAction, TaskResult
from src.infrastructure.browser import BrowserService
from src.infrastructure.llm import LLMService
from src.utils.dom import DOMProcessor

# ============================================================================
# FIXTURES
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
        "agent_step_delay": 0.0,
        "enable_context_compaction": False,
    }
    base.update(overrides)
    return Settings(**base)


def make_browser():
    browser = AsyncMock()
    browser.element_map = {}
    browser.navigate = AsyncMock(return_value=ActionResult(success=True, message="Navigated"))
    browser.click_element_safe = AsyncMock(
        return_value=ActionResult(success=True, message="Clicked")
    )
    browser.get_current_url = AsyncMock(return_value="https://example.com")
    browser.get_page_title = AsyncMock(return_value="Test Page")
    browser.detect_captcha = AsyncMock(return_value=False)
    browser.page = AsyncMock()
    return browser


def action(tool, **args):
    return AgentAction(thought="t", tool=tool, args=args)


def patch_dom_empty(monkeypatch, elements=None):
    """Make DOMProcessor.get_interactive_elements return canned elements."""

    async def fake(self, page):
        return (elements or [], None)

    monkeypatch.setattr(DOMProcessor, "get_interactive_elements", fake)


# ============================================================================
# 1.1 Rate limiter: lock + total pause under concurrency
# ============================================================================


class TestRateLimitConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_calls_respect_total_pause(self, tmp_path):
        settings = make_settings(tmp_path, rate_limit_seconds=0.5)
        call_times = []

        llm = AsyncMock()
        llm.generate_action = AsyncMock(
            side_effect=lambda **kw: call_times.append(time.monotonic())
            or asyncio.sleep(0)
            or action("done", summary="ok")
        )
        orch = AgentOrchestrator(settings, make_browser(), llm)

        await asyncio.gather(
            orch._call_llm_with_rate_limit([{"role": "user", "content": "a"}]),
            orch._call_llm_with_rate_limit([{"role": "user", "content": "b"}]),
        )

        assert len(call_times) == 2
        # Gap between the two real API calls must be >= rate_limit_seconds
        # (small tolerance for event-loop scheduling).
        assert call_times[1] - call_times[0] >= 0.45


class TestNewPageIsolation:
    @pytest.mark.asyncio
    async def test_new_page_returns_isolated_view(self, tmp_path):
        settings = make_settings(tmp_path)
        service = BrowserService(settings)
        # Simulate a started service: context with a working new_page()
        service.context = MagicMock()
        service.context.new_page = AsyncMock(return_value=AsyncMock())

        view = await service.new_page()

        assert view.page is not None
        assert view.element_map == {}
        assert view.element_map is not service.element_map

        # Mutating the view's map must not leak into the parent
        view.element_map[99] = "[data-agent-id='99']"
        assert 99 not in service.element_map

    @pytest.mark.asyncio
    async def test_new_page_requires_started_browser(self, tmp_path):
        from src.core.exceptions import BrowserError

        service = BrowserService(make_settings(tmp_path))
        with pytest.raises(BrowserError):
            await service.new_page()


# ============================================================================
# 1.2 Retry on generate_action (NetworkError is transient, LLMError is not)
# ============================================================================


def _llm_response(content='{"tool": "done", "args": {"summary": "ok"}}', usage=None):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = usage
    return resp


class TestGenerateActionRetry:
    @pytest.mark.asyncio
    async def test_network_error_retried_then_succeeds(self, tmp_path):
        llm = LLMService(make_settings(tmp_path))
        good = _llm_response()
        llm.client = MagicMock()
        llm.client.chat = MagicMock()
        llm.client.chat.completions = MagicMock()
        llm.client.chat.completions.create = AsyncMock(
            side_effect=[
                NetworkError("transient 1"),
                NetworkError("transient 2"),
                good,
            ]
        )

        result = await llm.generate_action(messages=[{"role": "user", "content": "x"}])
        assert isinstance(result, AgentAction)
        assert result.tool == "done"
        assert llm.client.chat.completions.create.await_count == 3

    @pytest.mark.asyncio
    async def test_llm_error_not_retried(self, tmp_path):
        llm = LLMService(make_settings(tmp_path))
        llm.client = MagicMock()
        llm.client.chat = MagicMock()
        llm.client.chat.completions = MagicMock()
        llm.client.chat.completions.create = AsyncMock(side_effect=LLMError("No valid JSON found"))

        with pytest.raises(LLMError):
            await llm.generate_action(messages=[{"role": "user", "content": "x"}])
        assert llm.client.chat.completions.create.await_count == 1


# ============================================================================
# 2.1 Budget-based DOM element selection + token counter modes
# ============================================================================


class TestBudgetElementSelection:
    def _elements(self, n):
        return [
            {
                "id": i,
                "tag": "a",
                "text": f" filler link {i} ",
                "selector": f"[data-agent-id='{i}']",
            }
            for i in range(n)
        ]

    def test_relevant_element_beyond_position_50_is_included(self, tmp_path):
        settings = make_settings(tmp_path, dom_max_tokens_estimate=1000)
        orch = AgentOrchestrator(settings, make_browser(), AsyncMock())

        elements = self._elements(200)
        # the ONLY task-relevant element sits far beyond the old 50-element cut
        elements[150]["text"] = "Find the pricing page contact email"

        selected = orch._select_elements_within_budget(elements, task="find the contact email")

        assert len(selected) <= 200
        assert any(e["id"] == 150 for e in selected)
        # budget respected: the selected block's token estimate must stay
        # within budget + one element line of slack (the greedy loop may
        # add one final element that crosses the threshold before breaking)
        block = "\n".join(f"[{e['id']}] {e['tag'].upper()} {e['text'][:80]}" for e in selected)
        assert len(block) // 4 <= 1000 * 1.05 + 20

    def test_empty_page_returns_empty(self, tmp_path):
        settings = make_settings(tmp_path)
        orch = AgentOrchestrator(settings, make_browser(), AsyncMock())
        assert orch._select_elements_within_budget([], task="anything") == []

    def test_tiktoken_mode_falls_back_without_package(self, tmp_path):
        # tiktoken is not installed in CI - the lazy import must fall back
        # to chars/4 exactly once (warned), not raise.
        settings = make_settings(tmp_path, token_counter_mode="tiktoken")
        orch = AgentOrchestrator(settings, make_browser(), AsyncMock())
        assert orch._estimate_tokens("x" * 400) == 100
        assert orch._tiktoken_warned is True
        # second call still works (no repeated import attempts needed)
        assert orch._estimate_tokens("abcd") == 1


# ============================================================================
# 2.2 Evaluator (self-critique), opt-in
# ============================================================================


class TestEvaluator:
    @pytest.mark.asyncio
    async def test_fail_on_empty_context_then_accept_second_done(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, enable_evaluator=True, evaluator_max_retries=1)
        patch_dom_empty(monkeypatch)
        browser = make_browser()
        llm = AsyncMock()
        llm.generate_action = AsyncMock(return_value=action("done", summary="finished"))
        llm.generate_text = AsyncMock(return_value="VERDICT:FAIL - context_data is empty")

        orch = AgentOrchestrator(settings, browser, llm)
        result = await orch.run("extract the contact email")

        # First done rejected, second done accepted (max_retries=1)
        assert llm.generate_text.await_count == 1
        assert result.success is True
        assert result.steps_taken == 2

    @pytest.mark.asyncio
    async def test_disabled_by_default_no_extra_calls(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path)
        patch_dom_empty(monkeypatch)
        llm = AsyncMock()
        llm.generate_action = AsyncMock(return_value=action("done", summary="finished"))

        orch = AgentOrchestrator(settings, make_browser(), llm)
        result = await orch.run("any task")

        llm.generate_text.assert_not_awaited()
        assert result.success is True
        assert result.steps_taken == 1


# ============================================================================
# 2.3 Multi-page parallel agents
# ============================================================================


class TestParallelAgents:
    @pytest.mark.asyncio
    async def test_isolation_and_failure_tolerance(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, enable_multi_page=True)
        patch_dom_empty(monkeypatch)

        views = []

        async def fake_new_page():
            view = make_browser()
            views.append(view)
            return view

        parent = make_browser()
        parent.new_page = fake_new_page

        llm = AsyncMock()
        llm.generate_action = AsyncMock(return_value=action("done", summary="ok"))

        async def fake_run(self, task, starting_url=None):
            if task == "crash":
                raise RuntimeError("boom")
            return TaskResult(success=True, summary="ok", steps_taken=1, total_duration_seconds=0.1)

        monkeypatch.setattr(AgentOrchestrator, "run", fake_run)

        results = await run_parallel_agents(settings, parent, llm, ["crash", "good task"])

        assert len(results) == 2
        assert results[0].success is False and results[0].error == "RuntimeError"
        assert results[1].success is True
        # separate page views were created per task
        assert len(views) == 2
        views[0].element_map[1] = "x"
        assert 1 not in views[1].element_map

    @pytest.mark.asyncio
    async def test_requires_opt_in(self, tmp_path):
        settings = make_settings(tmp_path)  # enable_multi_page=False default
        from src.core.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError):
            await run_parallel_agents(settings, make_browser(), AsyncMock(), ["t"])


# ============================================================================
# 2.4 Navigation host policy
# ============================================================================


class TestNavigationPolicy:
    @pytest.mark.asyncio
    async def test_cloud_metadata_blocked_by_default(self, tmp_path):
        service = BrowserService(make_settings(tmp_path))
        service.page = AsyncMock()
        result = await service.navigate("http://169.254.169.254/latest/meta-data/")
        assert result.success is False
        assert result.error == "BlockedByPolicy"
        service.page.goto.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_localhost_blocked_by_default(self, tmp_path):
        service = BrowserService(make_settings(tmp_path))
        service.page = AsyncMock()
        result = await service.navigate("http://localhost:8080/")
        assert result.success is False
        assert result.error == "BlockedByPolicy"

    @pytest.mark.asyncio
    async def test_public_host_unaffected(self, tmp_path):
        # Hermetic DNS: the machine's resolver may map public hosts into
        # private/benchmark ranges (VPN fake-IP mode), which the SSRF guard
        # would legitimately block. Pin resolution to a public IP.
        loop = MagicMock()
        loop.getaddrinfo = AsyncMock(
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        )
        service = BrowserService(make_settings(tmp_path))
        page = AsyncMock()
        page.url = "https://example.com/"
        service.page = page
        with patch("asyncio.get_running_loop", return_value=loop):
            result = await service.navigate("https://example.com")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_private_network_opt_out_allows_metadata(self, tmp_path):
        settings = make_settings(tmp_path, navigate_block_private_networks=False)
        service = BrowserService(settings)
        page = AsyncMock()
        page.url = "http://169.254.169.254/"
        service.page = page
        result = await service.navigate("http://169.254.169.254/latest/meta-data/")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_allowlist_rejects_other_hosts(self, tmp_path):
        settings = make_settings(tmp_path, navigate_allowed_domains=["example.com"])
        service = BrowserService(settings)
        service.page = AsyncMock()
        result = await service.navigate("https://other.com")
        assert result.success is False
        assert result.error == "BlockedByPolicy"

    @pytest.mark.asyncio
    async def test_allowlist_entry_bypasses_private_guard(self, tmp_path):
        settings = make_settings(
            tmp_path,
            navigate_allowed_domains=["localhost", "169.254.169.254"],
        )
        service = BrowserService(settings)
        page = AsyncMock()
        page.url = "http://localhost/"
        service.page = page
        result = await service.navigate("http://localhost:8080/")
        assert result.success is True


# ============================================================================
# 2.5 Captcha circuit breaker
# ============================================================================


class TestCaptchaCircuitBreaker:
    @pytest.mark.asyncio
    async def test_breaker_fires_without_human_wait(self, tmp_path, monkeypatch):
        # threshold=1: the very first captcha trips the breaker, so the
        # human-wait input() must never be reached at all
        settings = make_settings(tmp_path, captcha_circuit_breaker_threshold=1)
        patch_dom_empty(monkeypatch)
        browser = make_browser()
        browser.detect_captcha = AsyncMock(return_value=True)

        def _no_input(prompt):
            raise AssertionError("circuit breaker must not open a human-wait input()")

        monkeypatch.setattr("builtins.input", _no_input)

        llm = AsyncMock()
        llm.generate_action = AsyncMock(return_value=action("wait", seconds=1))

        orch = AgentOrchestrator(settings, browser, llm)
        result = await orch.run("some task")

        assert result.success is False
        assert result.error == "CaptchaCircuitBreaker"
        assert result.summary  # explains why the agent stopped
        assert orch._captcha_count == 1

    @pytest.mark.asyncio
    async def test_below_threshold_still_uses_human_loop(self, tmp_path, monkeypatch):
        # one captcha, then cleared: normal L2 human-in-the-loop flow resumes
        settings = make_settings(tmp_path, captcha_circuit_breaker_threshold=3)
        patch_dom_empty(monkeypatch)
        browser = make_browser()
        browser.detect_captcha = AsyncMock(side_effect=[True, False, False, False])
        monkeypatch.setattr("builtins.input", lambda prompt="": "")  # human pressed Enter
        monkeypatch.setattr(
            "src.agent.orchestrator.AgentOrchestrator._open_file_nonblocking", lambda self, p: None
        )

        llm = AsyncMock()
        llm.generate_action = AsyncMock(return_value=action("done", summary="ok"))

        orch = AgentOrchestrator(settings, browser, llm)
        result = await orch.run("some task")

        assert result.success is True
        assert orch._captcha_count == 1


# ============================================================================
# 3.1 Token tracking, JSON logs, run report
# ============================================================================


class TestTokenTrackingAndReports:
    @pytest.mark.asyncio
    async def test_usage_accumulated_in_llm_service(self, tmp_path):
        llm = LLMService(make_settings(tmp_path))
        usage = MagicMock(prompt_tokens=100, completion_tokens=50)
        llm.client = MagicMock()
        llm.client.chat = MagicMock()
        llm.client.chat.completions = MagicMock()
        llm.client.chat.completions.create = AsyncMock(return_value=_llm_response(usage=usage))
        await llm.generate_action(messages=[{"role": "user", "content": "x"}])
        await llm.generate_action(messages=[{"role": "user", "content": "x"}])
        assert llm.total_prompt_tokens == 200
        assert llm.total_completion_tokens == 100

    @pytest.mark.asyncio
    async def test_tokens_used_and_report_written(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path)
        patch_dom_empty(monkeypatch)
        llm = AsyncMock()
        llm.generate_action = AsyncMock(return_value=action("done", summary="ok"))
        llm.total_prompt_tokens = 10
        llm.total_completion_tokens = 5

        orch = AgentOrchestrator(settings, make_browser(), llm)
        result = await orch.run("any task")

        assert result.tokens_used == 15
        reports = list(settings.reports_dir.glob("run_*.json"))
        assert len(reports) == 1
        report = json.loads(reports[0].read_text())
        assert report["tokens"] == 15
        assert report["success"] is True
        assert report["task"] == "any task"
        assert "captcha_events" in report and "errors_by_type" in report

    def test_agent_log_is_json_lines(self):
        import logging

        from main import JsonLineFormatter

        formatter = JsonLineFormatter()
        record = logging.LogRecord(
            "src.agent.orchestrator", logging.INFO, __file__, 1, "step event", None, None
        )
        line = formatter.format(record)
        parsed = json.loads(line)
        assert parsed["logger"] == "src.agent.orchestrator"
        assert parsed["message"] == "step event"


# ============================================================================
# 3.2 ReAct loop integration + failure injection
# ============================================================================


class TestReactLoopIntegration:
    @pytest.mark.asyncio
    async def test_full_cycle_navigate_click_store_done(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path)
        elements = [{"id": 5, "tag": "button", "text": "Apply", "selector": "[data-agent-id='5']"}]
        patch_dom_empty(monkeypatch, elements)

        browser = make_browser()
        browser.element_map = {5: "[data-agent-id='5']"}
        browser.click_element_safe = AsyncMock(
            return_value=ActionResult(success=True, message="Clicked")
        )

        script = [
            action("navigate", url="https://example.com"),
            action("click_element", element_id=5),
            action("store_context", company="Tech Solutions"),
            action("done", summary="all done"),
        ]
        llm = AsyncMock()
        llm.generate_action = AsyncMock(side_effect=script)

        orch = AgentOrchestrator(settings, browser, llm)
        result = await orch.run("apply to the job")

        assert result.success is True
        assert result.context_data == {"company": "Tech Solutions"}
        assert result.steps_taken == 4
        browser.navigate.assert_awaited_with("https://example.com")
        browser.click_element_safe.assert_awaited_with(5)

    @pytest.mark.asyncio
    async def test_broken_json_twice_then_recovery(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, max_steps=10)
        patch_dom_empty(monkeypatch)
        llm = AsyncMock()
        llm.generate_action = AsyncMock(
            side_effect=[
                LLMError("No valid JSON found in LLM response. Content: blah"),
                LLMError("No valid JSON found in LLM response. Content: blah"),
                action("done", summary="recovered"),
            ]
        )
        orch = AgentOrchestrator(settings, make_browser(), llm)
        result = await orch.run("any task")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_shutdown_check_gives_graceful_result(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path)
        patch_dom_empty(monkeypatch)
        llm = AsyncMock()
        llm.generate_action = AsyncMock(side_effect=[action("scroll_page", direction="down")])
        calls = {"n": 0}

        def shutdown_after_first_step():
            calls["n"] += 1
            return calls["n"] > 1  # simulate SIGINT arriving during step 1

        orch = AgentOrchestrator(
            settings, make_browser(), llm, shutdown_check=shutdown_after_first_step
        )
        result = await orch.run("any task")
        assert result.success is False
        assert result.error == "ShutdownRequested"

    @pytest.mark.asyncio
    async def test_navigation_failure_reported_not_fatal(self, tmp_path, monkeypatch):
        settings = make_settings(tmp_path, max_steps=5)
        patch_dom_empty(monkeypatch)
        browser = make_browser()
        browser.navigate = AsyncMock(
            return_value=ActionResult(
                success=False, message="Navigation failed", error="NavigationTimeout"
            )
        )
        llm = AsyncMock()
        llm.generate_action = AsyncMock(
            side_effect=[
                action("navigate", url="https://example.com"),
                action("done", summary="give up"),
            ]
        )
        orch = AgentOrchestrator(settings, browser, llm)
        result = await orch.run("any task")
        assert result.success is True  # agent saw the error and decided to finish
        assert orch._errors_by_type["NavigationTimeout"] == 1


# ============================================================================
# 3.3 API service mode (guarded: skipped when fastapi is not installed)
# ============================================================================


class TestApiService:
    @pytest.fixture
    def client_and_app(self, tmp_path):
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        from src.api.app import create_app

        async def fake_runner(task, starting_url):
            await asyncio.sleep(0.05)
            return TaskResult(
                success=True, summary=f"did: {task}", steps_taken=1, total_duration_seconds=0.1
            )

        app = create_app(fake_runner)
        # `with` keeps the app's portal (and the queue worker task) alive
        # across requests; without it, on_event("startup") never fires.
        client = fastapi_testclient.TestClient(app)
        client.__enter__()
        return client, app

    def test_health_ok(self, client_and_app):
        client, _ = client_and_app
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_task_submit_and_poll(self, client_and_app):
        client, app = client_and_app
        resp = client.post("/task", json={"task": "do something"})
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        deadline = time.time() + 5
        while time.time() < deadline:
            status = client.get(f"/task/{task_id}").json()
            if status["state"] == "finished":
                break
            time.sleep(0.05)
        assert status["state"] == "finished"
        assert status["result"]["success"] is True
        assert status["result"]["summary"] == "did: do something"

    def test_unknown_task_404(self, client_and_app):
        client, _ = client_and_app
        assert client.get("/task/nope").status_code == 404

    def test_draining_refuses_new_tasks(self, client_and_app):
        client, app = client_and_app
        app.state.draining = True  # what SIGTERM sets via the drain handler
        assert client.get("/health").status_code == 503
        assert client.post("/task", json={"task": "x"}).status_code == 503

    def test_empty_task_rejected(self, client_and_app):
        # FastAPI validates the body against TaskSubmission (min_length=1)
        client, _ = client_and_app
        assert client.post("/task", json={"task": ""}).status_code == 422

    def test_pending_task_backpressure_429(self, client_and_app):
        """Fix (unbounded queue growth): past MAX_PENDING_TASKS queued-or-
        running tasks, submissions are rejected with 429 instead of being
        buffered forever behind a single worker."""
        from src.api.app import MAX_PENDING_TASKS

        client, app = client_and_app
        # Pre-seed pending records directly (no submission race with the
        # worker): only the "state" field participates in the cap check.
        # submitted_at is required because marking one finished below puts
        # it in the pruner's scope.
        for i in range(MAX_PENDING_TASKS):
            app.state.tasks[f"seeded-{i}"] = {
                "state": "queued",
                "submitted_at": datetime.now().isoformat(),
            }
        resp = client.post("/task", json={"task": "one too many"})
        assert resp.status_code == 429

        # finished tasks do NOT count toward the pending cap
        app.state.tasks["seeded-0"]["state"] = "finished"
        assert client.post("/task", json={"task": "fits again"}).status_code == 202
