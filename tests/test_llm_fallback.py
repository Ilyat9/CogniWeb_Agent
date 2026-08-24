"""Task 2 (LLM resilience) tests: provider health-check + controlled
failover in LLMService.

All network I/O is mocked at the OpenAI-client / httpx level - no real
provider is contacted. The health-check ping is stubbed via
_check_provider_health except where the test specifically exercises it.
"""

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import tenacity
from pydantic import ValidationError
from tenacity import wait_fixed

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config.settings import Settings  # noqa: E402
from src.core.exceptions import NetworkError  # noqa: E402
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


def _resp(content="ok"):
    r = MagicMock()
    r.choices = [MagicMock()]
    r.choices[0].message.content = content
    r.usage = None
    return r


def _mock_client(service, side_effect=None, return_value=None):
    service.client = MagicMock()
    service.client.chat = MagicMock()
    service.client.chat.completions = MagicMock()
    if side_effect is not None:
        service.client.chat.completions.create = AsyncMock(side_effect=side_effect)
    else:
        service.client.chat.completions.create = AsyncMock(return_value=return_value)


def _zero_backoff(service):
    # Keep the suite fast; retry COUNTS are still fully exercised.
    service.generate_action.retry.wait = wait_fixed(0)
    service.generate_text.retry.wait = wait_fixed(0)


class TestFallbackSettingsValidation:
    def test_fallback_disabled_by_default(self, tmp_path):
        s = make_settings(tmp_path)
        assert s.llm_fallback_provider_mode == ""
        assert s.llm_fallback_base_url is None

    def test_complete_local_fallback_passes(self, tmp_path):
        s = make_settings(
            tmp_path,
            llm_fallback_provider_mode="local",
            llm_fallback_base_url="http://localhost:1234/v1",
            llm_fallback_api_key="lm-studio",
            llm_fallback_model="mistral-7b-instruct",
        )
        assert s.llm_fallback_provider_mode == "local"

    def test_partial_fallback_config_rejected(self, tmp_path):
        with pytest.raises(ValidationError, match="LLM_FALLBACK_BASE_URL"):
            make_settings(
                tmp_path,
                llm_fallback_provider_mode="cloud",
                llm_fallback_api_key="sk-fallback-key-12345",
                llm_fallback_model="provider/model",
            )

    def test_invalid_fallback_mode_rejected(self, tmp_path):
        with pytest.raises(ValidationError, match="LLM_FALLBACK_PROVIDER_MODE"):
            make_settings(tmp_path, llm_fallback_provider_mode="somewhere")

    def test_cloud_fallback_requires_https(self, tmp_path):
        with pytest.raises(ValidationError, match="HTTPS"):
            make_settings(
                tmp_path,
                llm_fallback_provider_mode="cloud",
                llm_fallback_base_url="http://api.fallback.com/v1",
                llm_fallback_api_key="sk-fallback-key-12345",
                llm_fallback_model="provider/model",
            )

    def test_local_fallback_allows_http_localhost(self, tmp_path):
        s = make_settings(
            tmp_path,
            llm_fallback_provider_mode="local",
            llm_fallback_base_url="http://127.0.0.1:11434/v1",
            llm_fallback_api_key="ollama",
            llm_fallback_model="llama3:8b",
        )
        assert s.llm_fallback_base_url.startswith("http://")


class TestFailover:
    def _service_with_dead_primary(self, tmp_path, **fallback_overrides):
        settings = make_settings(
            tmp_path,
            llm_fallback_provider_mode="local",
            llm_fallback_base_url="http://localhost:9999/v1",
            llm_fallback_api_key="lm-studio",
            llm_fallback_model="backup-model",
            **fallback_overrides,
        )
        service = LLMService(settings)
        _zero_backoff(service)
        _mock_client(service, side_effect=httpx.ConnectError("primary down"))
        return service

    @pytest.mark.asyncio
    async def test_failover_on_connect_error_serves_request(self, tmp_path, monkeypatch):
        service = self._service_with_dead_primary(tmp_path)

        async def healthy(base_url, api_key):
            return True

        monkeypatch.setattr(service, "_check_provider_health", healthy)
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(return_value=_resp("from fallback"))
        monkeypatch.setattr("src.infrastructure.llm.AsyncOpenAI", lambda **kwargs: fake_client)

        assert await service.generate_text(messages=[]) == "from fallback"
        assert service._fallback_active is True
        assert service.active_provider_mode == "local"
        # one failed attempt on primary, one successful on fallback
        assert service.client.chat.completions.create.await_count == 1
        assert fake_client.chat.completions.create.await_count == 1
        # the fallback call must use the FALLBACK model name
        kwargs = fake_client.chat.completions.create.await_args.kwargs
        assert kwargs["model"] == "backup-model"
        await service.close()

    @pytest.mark.asyncio
    async def test_failover_is_sticky(self, tmp_path, monkeypatch):
        service = self._service_with_dead_primary(tmp_path)

        async def healthy(base_url, api_key):
            return True

        monkeypatch.setattr(service, "_check_provider_health", healthy)
        fake_client = MagicMock()
        fake_client.chat.completions.create = AsyncMock(return_value=_resp("fb"))
        monkeypatch.setattr("src.infrastructure.llm.AsyncOpenAI", lambda **kwargs: fake_client)

        assert await service.generate_text(messages=[]) == "fb"
        assert await service.generate_text(messages=[]) == "fb"
        # primary was tried exactly ONCE across both calls - after the
        # switch every request goes straight to the fallback.
        assert service.client.chat.completions.create.await_count == 1
        assert fake_client.chat.completions.create.await_count == 2
        await service.close()

    @pytest.mark.asyncio
    async def test_unhealthy_fallback_raises_original_error(self, tmp_path, monkeypatch):
        service = self._service_with_dead_primary(tmp_path)

        async def dead(base_url, api_key):
            return False

        monkeypatch.setattr(service, "_check_provider_health", dead)
        # tenacity wraps the final failure in RetryError (project-wide
        # convention - see TestOpenAIV3ExceptionMapping); the wrapped
        # cause must be the ORIGINAL NetworkError, not something new.
        with pytest.raises(tenacity.RetryError) as exc_info:
            await service.generate_text(messages=[])
        with pytest.raises(NetworkError, match="Connection error"):
            exc_info.value.last_attempt.result()
        assert service._fallback_active is False
        # cooldown prevents per-retry health checks: exactly ONE ping
        assert service._failover_attempts_used == 1
        await service.close()

    @pytest.mark.asyncio
    async def test_no_fallback_configured_old_behavior(self, tmp_path):
        settings = make_settings(tmp_path)
        service = LLMService(settings)
        _zero_backoff(service)
        _mock_client(service, side_effect=httpx.ConnectError("down"))
        with pytest.raises(tenacity.RetryError) as exc_info:
            await service.generate_text(messages=[])
        with pytest.raises(NetworkError, match="Connection error"):
            exc_info.value.last_attempt.result()
        assert service._fallback_active is False
        assert service._failover_attempts_used == 0
        assert service.client.chat.completions.create.await_count == 3
        await service.close()

    @pytest.mark.asyncio
    async def test_rate_limit_429_does_not_trigger_failover(self, tmp_path, monkeypatch):
        from openai import RateLimitError as OpenAIRateLimitError

        service = self._service_with_dead_primary(tmp_path)
        _mock_client(
            service,
            side_effect=OpenAIRateLimitError(
                "429", response=MagicMock(status_code=429, headers={}), body=None
            ),
        )
        health_checks = []

        async def spy(base_url, api_key):
            health_checks.append(base_url)
            return True

        monkeypatch.setattr(service, "_check_provider_health", spy)
        with pytest.raises(tenacity.RetryError):
            await service.generate_text(messages=[])
        assert health_checks == []
        assert service._fallback_active is False
        await service.close()

    @pytest.mark.asyncio
    async def test_failover_budget_bounds_attempts(self, tmp_path, monkeypatch):
        service = self._service_with_dead_primary(tmp_path, llm_fallback_max_switches=2)
        # Zero the cooldown so EVERY tenacity attempt gets to evaluate the
        # budget - otherwise the 30s post-failure cooldown (not the budget)
        # would be what stops the evaluations in this fast test.
        monkeypatch.setattr("src.infrastructure.llm._FAILOVER_COOLDOWN_SECONDS", 0)

        async def dead(base_url, api_key):
            return False

        monkeypatch.setattr(service, "_check_provider_health", dead)
        for _ in range(5):
            with pytest.raises(tenacity.RetryError):
                await service.generate_text(messages=[])
        # budget=2 -> exactly two health-check evaluations ever, despite
        # five calls x three tenacity attempts each
        assert service._failover_attempts_used == 2
        await service.close()


class TestHealthCheckPing:
    @pytest.mark.asyncio
    async def test_http_response_counts_as_alive_even_401(self, tmp_path):
        service = LLMService(make_settings(tmp_path))
        try:
            mock_http = AsyncMock()
            mock_resp = MagicMock(status_code=401)
            mock_http.get = AsyncMock(return_value=mock_resp)
            service._health_client = mock_http
            assert await service._check_provider_health("http://x/v1", "k") is True
            url = mock_http.get.await_args.args[0]
            assert url.endswith("/models")
        finally:
            if service._health_client is not None:
                await service._health_client.aclose()

    @pytest.mark.asyncio
    async def test_transport_failure_means_dead(self, tmp_path):
        service = LLMService(make_settings(tmp_path))
        try:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=httpx.ConnectError("nope"))
            service._health_client = mock_http
            assert await service._check_provider_health("http://x/v1", "k") is False
        finally:
            if service._health_client is not None:
                await service._health_client.aclose()


class TestPacingAfterFailover:
    @pytest.mark.asyncio
    async def test_pacing_follows_fallback_class(self, tmp_path, monkeypatch):
        # primary LOCAL (no pacing) -> fallback CLOUD (0.25s pacing):
        # after failover, concurrent acquires must serialize on the cloud
        # interval, proving wait_for_rate_limit reads the ACTIVE mode.
        settings = make_settings(
            tmp_path,
            llm_provider_mode="local",
            local_rate_limit_seconds=0.0,
            rate_limit_seconds=0.25,
            llm_fallback_provider_mode="cloud",
            llm_fallback_base_url="https://api.fallback.com/v1",
            llm_fallback_api_key="sk-fallback-key-12345",
            llm_fallback_model="provider/model",
        )
        service = LLMService(settings)
        service._fallback_active = True  # simulate an already-switched state
        start = time.monotonic()
        await service.wait_for_rate_limit()
        await service.wait_for_rate_limit()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.2, "post-failover pacing must use the fallback's interval"
