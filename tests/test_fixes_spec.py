"""
Regression tests for the fixes-spec round (see SELF_REVIEW.md follow-ups).

Covers, one class per fix:
- unified numeric-arg normalization in _execute_action (element_id/index/
  timeout_ms/seconds arriving as JSON strings)
- type_text long-text instant fill() fast path
- per-task browser lifecycle hooks (create_app on_startup/on_shutdown)
- finished-task store pruning (TTL + max-kept)
- public-bind-without-auth fail-fast guard
- shared LLM rate limiter (LLMService-level clock across orchestrators)
- openai v3 SDK exception mapping still retryable (429 / connection)
- in-memory history hard cap with compaction disabled
- element safety: javascript: in onclick/formaction
- heartbeat file + CLI-mode docker healthcheck freshness logic
"""

import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

sys.path.insert(0, str(Path(__file__).parent.parent))

import openai  # noqa: E402
import tenacity  # noqa: E402
from tenacity import wait_fixed  # noqa: E402

import src.api.app as api_app  # noqa: E402
from src.agent.orchestrator import AgentOrchestrator  # noqa: E402
from src.api.app import (  # noqa: E402
    _detect_public_bind,
    _enforce_public_bind_auth_policy,
    create_app,
)
from src.config.settings import Settings  # noqa: E402
from src.core.exceptions import ConfigurationError, NetworkError  # noqa: E402
from src.core.models import ActionResult, AgentAction, TaskResult  # noqa: E402
from src.infrastructure.browser import BrowserService  # noqa: E402
from src.infrastructure.llm import LLMRateLimiter, LLMService  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent


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
        # Task 1 (persistence): per-test SQLite store - never the shared
        # ./data/tasks.db.
        "task_db_path": tmp_path / "tasks.db",
        "agent_step_delay": 0.0,
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


def make_llm():
    llm = AsyncMock()
    llm.generate_action = AsyncMock(return_value=AgentAction(tool="done", args={"summary": "ok"}))
    return llm


def _orch(tmp_path, **overrides):
    settings = make_settings(tmp_path, **overrides)
    browser = make_browser()
    browser.element_map[1] = "sel"
    return AgentOrchestrator(settings, browser, make_llm())


# ============================================================================
# Unified numeric-arg normalization (element_id / index / timeout_ms / seconds)
# ============================================================================


class TestArgNormalization:
    @pytest.mark.asyncio
    async def test_type_text_accepts_string_element_id(self, tmp_path):
        orch = _orch(tmp_path)
        orch.browser.type_text = AsyncMock(return_value=ActionResult(success=True, message="t"))
        r = await orch._execute_action(
            AgentAction(tool="type_text", args={"element_id": "1", "text": "hi"})
        )
        assert r.success is True, r.message
        assert orch.browser.type_text.await_args.args[0] == 1

    @pytest.mark.asyncio
    async def test_select_option_accepts_string_element_id(self, tmp_path):
        orch = _orch(tmp_path)
        orch.browser.select_option = AsyncMock(return_value=ActionResult(success=True, message="s"))
        r = await orch._execute_action(
            AgentAction(tool="select_option", args={"element_id": "1", "value": "v"})
        )
        assert r.success is True, r.message
        assert orch.browser.select_option.await_args.args[0] == 1

    @pytest.mark.asyncio
    async def test_switch_tab_accepts_string_index(self, tmp_path):
        orch = _orch(tmp_path)
        orch.browser.switch_tab = AsyncMock(return_value=ActionResult(success=True, message="sw"))
        r = await orch._execute_action(AgentAction(tool="switch_tab", args={"index": "0"}))
        assert r.success is True, r.message
        assert orch.browser.switch_tab.await_args.args[0] == 0

    @pytest.mark.asyncio
    async def test_wait_accepts_string_seconds(self, tmp_path):
        orch = _orch(tmp_path)
        r = await orch._execute_action(AgentAction(tool="wait", args={"seconds": "1"}))
        assert r.success is True, r.message

    @pytest.mark.asyncio
    async def test_download_file_accepts_string_ids_and_timeout(self, tmp_path):
        orch = _orch(tmp_path)
        orch.browser.download_file = AsyncMock(return_value=ActionResult(success=True, message="d"))
        r = await orch._execute_action(
            AgentAction(tool="download_file", args={"element_id": "1", "timeout_ms": "7000"})
        )
        assert r.success is True, r.message
        args, kwargs = orch.browser.download_file.await_args
        assert args[0] == 1
        assert args[1] == 7000  # coerced from "7000" by the schema/normalizer

    @pytest.mark.asyncio
    async def test_garbage_values_still_fail_loudly(self, tmp_path):
        orch = _orch(tmp_path)
        r = await orch._execute_action(
            AgentAction(tool="click_element", args={"element_id": "abc"})
        )
        assert r.error == "InvalidType"
        r = await orch._execute_action(AgentAction(tool="wait", args={"seconds": "soon"}))
        assert r.error == "InvalidType"

    @pytest.mark.asyncio
    async def test_wait_for_element_accepts_string_timeout(self, tmp_path):
        orch = _orch(tmp_path)
        orch.browser.wait_for_element = AsyncMock(
            return_value=ActionResult(success=True, message="w")
        )
        r = await orch._execute_action(
            AgentAction(tool="wait_for_element", args={"element_id": "1", "timeout_ms": "5000"})
        )
        assert r.success is True, r.message
        assert orch.browser.wait_for_element.await_args.kwargs.get("timeout_ms") == 5000.0

    @pytest.mark.asyncio
    async def test_press_enter_string_forms_accepted(self, tmp_path):
        orch = _orch(tmp_path)
        orch.browser.type_text = AsyncMock(return_value=ActionResult(success=True, message="t"))
        r = await orch._execute_action(
            AgentAction(
                tool="type_text", args={"element_id": 1, "text": "hi", "press_enter": "true"}
            )
        )
        assert r.success is True
        assert orch.browser.type_text.await_args.args[2] is True


# ============================================================================
# type_text long-text instant fill() fast path
# ============================================================================


class TestTypeTextFastPath:
    def _service(self, tmp_path, **overrides):
        service = BrowserService(make_settings(tmp_path, **overrides))
        service.element_map[1] = "[data-agent-id='1']"
        # Explicit get_attribute mocks on both locator and .first: auto
        # child mocks of AsyncMock don't await correctly on this Python
        # (see locator_mock note in test_browser_units.py).
        locator = AsyncMock()
        locator.get_attribute = AsyncMock(return_value=None)
        first = AsyncMock()
        first.get_attribute = AsyncMock(return_value=None)
        locator.first = first
        service.page = AsyncMock()
        service.page.url = "https://example.com/"
        service.page.locator = MagicMock(return_value=locator)
        return service

    @pytest.mark.asyncio
    async def test_long_text_uses_instant_fill(self, tmp_path):
        service = self._service(tmp_path, typing_slow_path_max_chars=5)
        r = await service.type_text(1, "abcdefgh")
        assert r.success is True, r.message
        service.page.locator().first.fill.assert_awaited_once_with(
            "abcdefgh", timeout=service.settings.action_timeout
        )
        service.page.keyboard.type.assert_not_awaited()
        assert r.warning and "fill" in r.warning

    @pytest.mark.asyncio
    async def test_short_text_keeps_keystroke_loop(self, tmp_path):
        service = self._service(
            tmp_path, typing_slow_path_max_chars=50, typing_speed_min=10, typing_speed_max=50
        )
        r = await service.type_text(1, "hi")
        assert r.success is True, r.message
        assert service.page.keyboard.type.await_count == 2
        service.page.locator().first.fill.assert_not_awaited()
        assert r.warning is None


# ============================================================================
# App lifecycle hooks (browser/LLM live for the app's whole lifetime)
# ============================================================================


class TestLifecycleHooks:
    def test_hooks_run_on_startup_and_shutdown(self, tmp_path):
        events = []

        async def on_startup():
            events.append("startup")

        async def on_shutdown():
            events.append("shutdown")

        async def runner(task, starting_url):
            return TaskResult(success=True, summary="ok", steps_taken=1, total_duration_seconds=0)

        app = create_app(
            runner, settings=make_settings(tmp_path), on_startup=on_startup, on_shutdown=on_shutdown
        )
        with fastapi_testclient.TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert events == ["startup"]
        assert events == ["startup", "shutdown"]

    def test_no_hooks_still_works(self, tmp_path):
        async def runner(task, starting_url):
            return TaskResult(success=True, summary="ok", steps_taken=1, total_duration_seconds=0)

        app = create_app(runner, settings=make_settings(tmp_path))
        with fastapi_testclient.TestClient(app) as client:
            assert client.get("/health").status_code == 200


# ============================================================================
# Finished-task store pruning (TTL + max-kept)
# ============================================================================


class TestTaskStorePruning:
    # FIX (persistence): TASK_TTL_HOURS / MAX_FINISHED_TASKS moved from
    # app-module constants into Settings fields (task_ttl_hours /
    # max_finished_tasks) - retention is tuned via settings overrides now,
    # not by monkeypatching module attributes.
    def _client(self, tmp_path, **overrides):
        async def runner(task, starting_url):
            return TaskResult(success=True, summary="ok", steps_taken=1, total_duration_seconds=0)

        client = fastapi_testclient.TestClient(
            create_app(runner, settings=make_settings(tmp_path, **overrides))
        )
        client.__enter__()
        return client

    def test_lru_cap_keeps_newest_finished(self, tmp_path):
        client = self._client(tmp_path, max_finished_tasks=2)
        try:
            ids = []
            for _ in range(3):
                task_id = client.post("/task", json={"task": "t"}).json()["task_id"]
                ids.append(task_id)
            deadline = time.time() + 5
            while time.time() < deadline:
                if all(client.get(f"/task/{i}").json()["state"] == "finished" for i in ids):
                    break
                time.sleep(0.05)
            # Fourth submit triggers pruning: only 2 newest finished survive
            client.post("/task", json={"task": "t4"})
            listed = [t["task_id"] for t in client.get("/tasks").json()["tasks"]]
            assert ids[0] not in listed
            assert ids[1] in listed and ids[2] in listed
        finally:
            client.close()

    def test_ttl_drops_ancient_finished(self, tmp_path):
        # task_ttl_hours=0 disables the age check... so instead use a tiny
        # TTL: every real submission is "ancient" relative to it.
        client = self._client(tmp_path, task_ttl_hours=0.000001)
        try:
            task_id = client.post("/task", json={"task": "t"}).json()["task_id"]
            deadline = time.time() + 5
            while time.time() < deadline:
                if client.get(f"/task/{task_id}").json()["state"] == "finished":
                    break
                time.sleep(0.05)
            client.post("/task", json={"task": "t2"})
            listed = [t["task_id"] for t in client.get("/tasks").json()["tasks"]]
            assert task_id not in listed
        finally:
            client.close()

    def test_running_task_never_pruned(self, tmp_path):
        release = asyncio.Event()

        async def slow_runner(task, starting_url):
            await release.wait()
            return TaskResult(success=True, summary="ok", steps_taken=1, total_duration_seconds=0)

        client = fastapi_testclient.TestClient(
            create_app(slow_runner, settings=make_settings(tmp_path))
        )
        client.__enter__()
        try:
            blocked = client.post("/task", json={"task": "blocked"}).json()["task_id"]
            client.post("/task", json={"task": "second"})
            listed = [t["task_id"] for t in client.get("/tasks").json()["tasks"]]
            assert blocked in listed  # running/queued records are untouchable
        finally:
            # unblock in the app's loop via the portal, then close
            try:
                client.app.state.queue.put_nowait(None)
            except Exception:
                pass
            release.set()
            client.close()


# ============================================================================
# Public bind without auth -> fail fast
# ============================================================================


def _bind_cfg(host="127.0.0.1", token=None, allow=False):
    return SimpleNamespace(
        api_bind_host=host, api_auth_token=token, allow_unauthenticated_public_bind=allow
    )


class TestPublicBindGuard:
    def test_loopback_without_token_is_fine(self):
        _enforce_public_bind_auth_policy(_bind_cfg("127.0.0.1", token=None))  # no raise

    def test_public_without_token_refuses(self):
        with pytest.raises(ConfigurationError):
            _enforce_public_bind_auth_policy(_bind_cfg("0.0.0.0", token=None))

    def test_public_ipv6_without_token_refuses(self):
        with pytest.raises(ConfigurationError):
            _enforce_public_bind_auth_policy(_bind_cfg("::", token=None))

    def test_public_with_token_is_fine(self):
        _enforce_public_bind_auth_policy(_bind_cfg("0.0.0.0", token="x" * 20))

    def test_explicit_override_allowed(self):
        _enforce_public_bind_auth_policy(_bind_cfg("0.0.0.0", token=None, allow=True))

    def test_uvicorn_host_argv_detected(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["uvicorn", "--host", "0.0.0.0"])
        assert _detect_public_bind(_bind_cfg("127.0.0.1")) is True
        with pytest.raises(ConfigurationError):
            _enforce_public_bind_auth_policy(_bind_cfg("127.0.0.1", token=None))

    def test_uvicorn_host_localhost_argv_not_flagged(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["uvicorn", "--host", "127.0.0.1"])
        assert _detect_public_bind(_bind_cfg("127.0.0.1")) is False


# ============================================================================
# Shared rate limiter on LLMService
# ============================================================================


class TestSharedRateLimiter:
    @pytest.mark.asyncio
    async def test_limiter_serializes_concurrent_acquires(self):
        limiter = LLMRateLimiter()
        start = time.monotonic()
        await asyncio.gather(limiter.acquire(0.2), limiter.acquire(0.2), limiter.acquire(0.2))
        assert time.monotonic() - start >= 0.35

    @pytest.mark.asyncio
    async def test_zero_rate_means_no_pause(self):
        limiter = LLMRateLimiter()
        start = time.monotonic()
        await asyncio.gather(limiter.acquire(0), limiter.acquire(0))
        assert time.monotonic() - start < 0.1

    @pytest.mark.asyncio
    async def test_orchestrators_share_one_clock(self, tmp_path):
        settings = make_settings(tmp_path, rate_limit_seconds=0.3)
        llm = LLMService(settings)
        orch1 = AgentOrchestrator(settings, make_browser(), llm)
        orch2 = AgentOrchestrator(settings, make_browser(), llm)
        start = time.monotonic()
        await asyncio.gather(orch1._wait_for_rate_limit(), orch2._wait_for_rate_limit())
        assert time.monotonic() - start >= 0.25

    @pytest.mark.asyncio
    async def test_local_mode_uses_local_interval(self, tmp_path):
        settings = make_settings(
            tmp_path,
            llm_provider_mode="local",
            local_rate_limit_seconds=0.2,
            rate_limit_seconds=15.0,
        )
        llm = LLMService(settings)
        start = time.monotonic()
        await asyncio.gather(llm.wait_for_rate_limit(), llm.wait_for_rate_limit())
        elapsed = time.monotonic() - start
        assert 0.15 <= elapsed < 5  # local interval, NOT the 15s cloud one


# ============================================================================
# openai v3 SDK exception mapping (429 / connection drop are retryable)
# ============================================================================


def _http_response(status):
    request = httpx.Request("POST", "https://api.test.com/v1/chat/completions")
    return httpx.Response(status, request=request)


def _llm_with_create_error(tmp_path, error):
    llm = LLMService(make_settings(tmp_path))
    llm.client = MagicMock()
    llm.client.chat = MagicMock()
    llm.client.chat.completions = MagicMock()
    llm.client.chat.completions.create = AsyncMock(side_effect=error)
    # Keep the suite fast: zero tenacity backoff (retry behavior itself -
    # attempt count - is still fully exercised).
    llm.generate_action.retry.wait = wait_fixed(0)
    llm.generate_text.retry.wait = wait_fixed(0)
    return llm


class TestOpenAIV3ExceptionMapping:
    """The except-branches in llm.py must keep catching the ACTUAL openai
    v3 SDK exception classes (a major-version rename would silently turn
    them into dead branches falling through to generic LLMError with no
    retry). After 3 failed attempts tenacity raises RetryError whose
    last_attempt carries the wrapped NetworkError - we assert both the
    mapping and the retry count."""

    @pytest.mark.asyncio
    async def test_rate_limit_429_wrapped_as_retryable_network_error(self, tmp_path):
        err = openai.RateLimitError("429", response=_http_response(429), body=None)
        llm = _llm_with_create_error(tmp_path, err)
        with pytest.raises(tenacity.RetryError) as exc_info:
            await llm.generate_action(messages=[{"role": "user", "content": "x"}])
        with pytest.raises(NetworkError):
            exc_info.value.last_attempt.result()
        assert llm.client.chat.completions.create.await_count == 3  # retried by tenacity

    @pytest.mark.asyncio
    async def test_api_connection_error_wrapped_and_retried(self, tmp_path):
        # openai v3 constructor: message is derived, `request` is the only
        # (keyword-only) argument.
        request = httpx.Request("POST", "https://api.test.com/v1/chat/completions")
        err = openai.APIConnectionError(request=request)
        llm = _llm_with_create_error(tmp_path, err)
        with pytest.raises(tenacity.RetryError) as exc_info:
            await llm.generate_text(messages=[{"role": "user", "content": "x"}])
        with pytest.raises(NetworkError):
            exc_info.value.last_attempt.result()
        assert llm.client.chat.completions.create.await_count == 3

    @pytest.mark.asyncio
    async def test_httpx_connect_error_wrapped_and_retried(self, tmp_path):
        llm = _llm_with_create_error(tmp_path, httpx.ConnectError("boom"))
        with pytest.raises(tenacity.RetryError) as exc_info:
            await llm.generate_text(messages=[{"role": "user", "content": "x"}])
        with pytest.raises(NetworkError):
            exc_info.value.last_attempt.result()
        assert llm.client.chat.completions.create.await_count == 3


# ============================================================================
# In-memory history hard cap (compaction disabled)
# ============================================================================


class TestHistoryHardCap:
    @pytest.mark.asyncio
    async def test_cap_applies_without_compaction(self, tmp_path):
        settings = make_settings(
            tmp_path, enable_context_compaction=False, history_hard_cap_messages=10
        )
        orch = AgentOrchestrator(settings, make_browser(), make_llm())
        orch.conversation_history = [{"role": "system", "content": "sys"}] + [
            {"role": "user", "content": f"m{i}"} for i in range(20)
        ]
        await orch._maybe_compact_history()
        assert len(orch.conversation_history) == 10
        assert orch.conversation_history[0]["role"] == "system"
        assert orch.conversation_history[-1]["content"] == "m19"

    @pytest.mark.asyncio
    async def test_no_cap_below_limit(self, tmp_path):
        settings = make_settings(
            tmp_path, enable_context_compaction=False, history_hard_cap_messages=200
        )
        orch = AgentOrchestrator(settings, make_browser(), make_llm())
        orch.conversation_history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        await orch._maybe_compact_history()
        assert len(orch.conversation_history) == 2


# ============================================================================
# Element safety: javascript: payloads in onclick/formaction
# ============================================================================


class TestElementSafetyAttributes:
    def _service(self, tmp_path, attributes):
        service = BrowserService(make_settings(tmp_path))
        locator = AsyncMock()

        async def get_attribute(name):
            return attributes.get(name)

        locator.get_attribute = get_attribute
        locator.first = locator
        service.page = AsyncMock()
        service.page.url = "https://example.com/"
        service.page.locator = MagicMock(return_value=locator)
        return service

    @pytest.mark.asyncio
    async def test_javascript_onclick_blocked(self, tmp_path):
        service = self._service(
            tmp_path, {"href": None, "onclick": "location='javascript:alert(1)'"}
        )
        msg = await service._check_element_safety("[data-agent-id='1']")
        assert msg is not None and "javascript" in msg.lower()

    @pytest.mark.asyncio
    async def test_javascript_formaction_blocked(self, tmp_path):
        service = self._service(tmp_path, {"href": None, "formaction": "javascript:steal()"})
        msg = await service._check_element_safety("[data-agent-id='1']")
        assert msg is not None

    @pytest.mark.asyncio
    async def test_clean_element_passes(self, tmp_path):
        service = self._service(
            tmp_path,
            {"href": "https://example.com/next", "onclick": "setColor('red')"},
        )
        assert await service._check_element_safety("[data-agent-id='1']") is None


# ============================================================================
# Heartbeat + CLI-mode docker healthcheck
# ============================================================================


def _run_healthcheck(env_overrides):
    env = {**os.environ, "MODE": "cli", **env_overrides}
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "docker-healthcheck.py")],
        env=env,
        capture_output=True,
        timeout=30,
    ).returncode


class TestHeartbeatHealthcheck:
    def test_heartbeat_written_each_touch(self, tmp_path):
        settings = make_settings(tmp_path, heartbeat_file=tmp_path / "logs" / "hb")
        orch = AgentOrchestrator(settings, make_browser(), make_llm())
        orch._touch_heartbeat()
        assert settings.heartbeat_file.is_file()
        datetime.fromisoformat(settings.heartbeat_file.read_text())  # parses

    def test_healthcheck_fresh_beat_healthy(self, tmp_path):
        hb = tmp_path / "hb"
        hb.write_text(datetime.now().isoformat())
        assert _run_healthcheck({"HEARTBEAT_FILE": str(hb)}) == 0

    def test_healthcheck_stale_beat_unhealthy(self, tmp_path):
        hb = tmp_path / "hb"
        hb.write_text((datetime.now() - timedelta(seconds=900)).isoformat())
        assert _run_healthcheck({"HEARTBEAT_FILE": str(hb), "HEARTBEAT_STALE_SECONDS": "600"}) == 1

    def test_healthcheck_missing_beat_healthy(self, tmp_path):
        assert _run_healthcheck({"HEARTBEAT_FILE": str(tmp_path / "nope")}) == 0

    def test_healthcheck_garbage_beat_stays_healthy(self, tmp_path):
        hb = tmp_path / "hb"
        hb.write_text("not-a-timestamp")
        assert _run_healthcheck({"HEARTBEAT_FILE": str(hb)}) == 0
