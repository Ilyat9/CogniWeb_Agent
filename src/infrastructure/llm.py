import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx
from openai import (
    APIConnectionError,
    AsyncOpenAI,
    RateLimitError as OpenAIRateLimitError,
)
from pydantic import ValidationError as PydanticValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import Settings
from ..core.exceptions import LLMError, NetworkError
from ..core.models import AgentAction

logger = logging.getLogger(__name__)


class LLMRateLimiter:
    """
    Shared request pacer: one clock + one lock per LLM client.

    Fix (rate limit not coordinated between parallel agents): pacing used
    to live per-orchestrator (an instance attribute `last_call_time` plus
    a per-instance asyncio.Lock), so N orchestrators started via
    run_parallel_agents() each kept their OWN clock - together they hit
    the provider N times as often as RATE_LIMIT_SECONDS allows, with only
    a docstring comment asking the operator to raise the interval
    manually. Living on LLMService (which every orchestrator of a run
    shares), the clock is shared: concurrent callers queue on the lock
    and each respects the full interval between actual API calls.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_call_time = 0.0

    async def acquire(self, rate_limit_seconds: float) -> None:
        """Wait until `rate_limit_seconds` has passed since the previous
        acquire(); stamps the slot BEFORE the actual request runs, so a
        caller that acquires next sees a fresh timestamp even while this
        request is still in flight."""
        if rate_limit_seconds <= 0:
            return
        # Hold the lock across read -> sleep -> write so two concurrent
        # callers cannot both observe the same stale timestamp and both
        # skip the pause.
        async with self._lock:
            time_since_last = time.time() - self._last_call_time
            if time_since_last < rate_limit_seconds:
                delay = rate_limit_seconds - time_since_last
                print(f"⏳ Rate limiting: waiting {delay:.1f}s before next LLM request...")
                await asyncio.sleep(delay)
            self._last_call_time = time.time()


class LLMService:

    def __init__(self, settings: Settings):
        self.settings = settings
        self._http_client: httpx.AsyncClient | None = None

        if settings.proxy_url:
            self._http_client = httpx.AsyncClient(
                proxy=settings.proxy_url, timeout=httpx.Timeout(settings.http_timeout)
            )

        self.client = AsyncOpenAI(
            api_key=settings.api_key, base_url=settings.api_base_url, http_client=self._http_client
        )

        self._connection_verified = False

        # 3.1: real token accounting from the API's usage block. The
        # OpenAI-compatible response has always carried usage.prompt_tokens
        # / completion_tokens, but nothing read it - the operator had no
        # in-process view of a run's actual token cost.
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

        # Shared pacing clock for every caller of this service (all
        # orchestrators of a run, including run_parallel_agents).
        self.rate_limiter = LLMRateLimiter()

    async def wait_for_rate_limit(self) -> None:
        """Pace requests per the configured interval for the active provider
        mode. Safe to call concurrently: all callers share one clock (see
        LLMRateLimiter), so N parallel orchestrators cannot exceed the
        configured rate in aggregate."""
        rate = (
            self.settings.local_rate_limit_seconds
            if self.settings.llm_provider_mode == "local"
            else self.settings.rate_limit_seconds
        )
        await self.rate_limiter.acquire(rate)

    def _record_usage(self, response: Any) -> None:
        """Accumulate usage stats if the provider returned them (mock/local
        servers may omit the block - absence is not an error)."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        try:
            self.total_prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.total_completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
        except (TypeError, ValueError):
            logger.debug("Malformed usage block in LLM response; skipped")

    async def close(self) -> None:
        """
        Close HTTP client(s).

        FIX (3.2 Resource Leaks): previously only self._http_client (created
        when proxy_url was set) was closed. In the default scenario
        (proxy_url=None, the common case), AsyncOpenAI(...) creates its own
        internal httpx.AsyncClient that was never closed. For a one-shot CLI
        process this leaked only until interpreter exit, but it becomes a
        real accumulating leak if LLMService is reused across multiple runs
        (e.g. future multi-agent use per ARCHITECTURE.md).
        """
        try:
            await self.client.close()
        except Exception as e:
            logger.debug(f"Error closing OpenAI client: {e}")

        if self._http_client:
            await self._http_client.aclose()

    async def __aenter__(self) -> "LLMService":
        """Context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        await self.close()
        return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        # FIX (2.3 llm.py:56-60): the previous set only covered NetworkError
        # and httpx.TimeoutException. httpx.ConnectError, OpenAI 429
        # (RateLimitError) and general APIConnectionError are also
        # transient/retryable, but were being wrapped into the non-retried
        # LLMError below and silently never retried, contradicting
        # ARCHITECTURE.md's "tenacity handles network retries" claim.
        retry=retry_if_exception_type((NetworkError, httpx.TimeoutException)),
    )
    async def generate_action(
        self, messages: list[dict[str, Any]], temperature: float = 0.1
    ) -> AgentAction:
        # NOTE: message content is typed as Any (not str) because Task 4's
        # vision fallback sends OpenAI-style multimodal content (a list of
        # {"type": "text"/"image_url", ...} parts) for a single message
        # rather than a plain string. This method doesn't need to care -
        # it's forwarded to the API as-is - only the response is parsed.
        try:
            response = await self.client.chat.completions.create(
                model=self.settings.model_name,
                messages=messages,
                temperature=temperature,
                max_tokens=self.settings.max_tokens,
            )

        except httpx.TimeoutException as e:
            raise NetworkError(f"Timeout connecting to LLM: {e}") from e
        except httpx.ConnectError as e:
            # FIX: wrap as NetworkError (retryable) instead of LLMError
            raise NetworkError(f"Connection error contacting LLM: {e}") from e
        except APIConnectionError as e:
            # FIX: OpenAI SDK's own connection-error wrapper - also retryable
            raise NetworkError(f"API connection error: {e}") from e
        except OpenAIRateLimitError as e:
            # FIX: HTTP 429 - retryable with backoff, not a fatal LLMError
            raise NetworkError(f"Rate limited by LLM provider (429): {e}") from e
        except NetworkError:
            # FIX (1.2): a NetworkError surfacing from the call itself must
            # reach tenacity as NetworkError - the generic handler below
            # would re-wrap it into a non-retried LLMError.
            raise
        except Exception as e:
            raise LLMError(f"LLM request failed: {e}", model_name=self.settings.model_name) from e

        self._connection_verified = True
        self._record_usage(response)

        # content = response.choices[0].message.content

        choices = getattr(response, "choices", [])
        if not choices:
            logger.warning(f"Model {self.settings.model_name} returned response without choices")
            content = None
        else:
            content = choices[0].message.content

        if not content or not content.strip():
            logger.warning(f"Model {self.settings.model_name} returned empty response")
            raise LLMError(
                f"Empty response from model {self.settings.model_name}",
                model_name=self.settings.model_name,
            )

        json_str = self._extract_json_from_response(content)

        if not json_str:
            raise LLMError(
                f"No valid JSON found in LLM response. Content: {content[:200]}",
                model_name=self.settings.model_name,
            )

        try:
            action_dict = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode JSON from model {self.settings.model_name}: {e}")
            raise LLMError(f"JSON decode error: {e}", model_name=self.settings.model_name) from e

        # FIX (2.3 / Critical): this call was previously unguarded. Any
        # schema mismatch (unknown tool, missing required args, wrong
        # arg type) raised a raw pydantic_core.ValidationError that
        # propagated past every except-block in orchestrator.run() and
        # main.py's except Exception, killing the whole task and
        # discarding context_data. Wrapping it in LLMError lets
        # orchestrator.run()'s existing `except LLMError` branch recover
        # and continue the loop instead.
        try:
            action = AgentAction.model_validate(action_dict)
        except PydanticValidationError as e:
            raise LLMError(
                f"LLM returned an action that failed schema validation: {e}",
                model_name=self.settings.model_name,
            ) from e

        return action

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((NetworkError, httpx.TimeoutException)),
    )
    async def generate_text(self, messages: list[dict[str, Any]], temperature: float = 0.1) -> str:
        """
        Generic freeform completion call - returns raw text, no AgentAction
        JSON parsing/validation.

        FIX (Task 3 - context compaction): compaction needs the LLM to
        write a short natural-language status summary, not a tool-call
        action, so generate_action() (which enforces the AgentAction
        schema end to end) is the wrong shape for this. This reuses the
        same connection-error handling and retry policy as
        generate_action(), just without the JSON extraction/validation
        step at the end.

        Args:
            messages: Chat messages. Content may be a plain string OR (for
                Task 4's vision fallback) a list of OpenAI-style multimodal
                content parts (text + image_url) - this method doesn't
                interpret content, it only forwards it to the API.
            temperature: Sampling temperature.

        Returns:
            The raw text content of the model's response.

        Raises:
            NetworkError: Transient/retryable connection issues.
            LLMError: Non-retryable failures (empty response, no choices).
        """
        try:
            response = await self.client.chat.completions.create(
                model=self.settings.model_name,
                messages=messages,
                temperature=temperature,
                max_tokens=self.settings.max_tokens,
            )
        except httpx.TimeoutException as e:
            raise NetworkError(f"Timeout connecting to LLM: {e}") from e
        except httpx.ConnectError as e:
            raise NetworkError(f"Connection error contacting LLM: {e}") from e
        except APIConnectionError as e:
            raise NetworkError(f"API connection error: {e}") from e
        except OpenAIRateLimitError as e:
            raise NetworkError(f"Rate limited by LLM provider (429): {e}") from e
        except NetworkError:
            # FIX (1.2): a NetworkError surfacing from the call itself must
            # reach tenacity as NetworkError - the generic handler below
            # would re-wrap it into a non-retried LLMError.
            raise
        except Exception as e:
            raise LLMError(f"LLM request failed: {e}", model_name=self.settings.model_name) from e

        self._record_usage(response)

        choices = getattr(response, "choices", [])
        if not choices:
            raise LLMError(
                f"Model {self.settings.model_name} returned response without choices",
                model_name=self.settings.model_name,
            )

        content = choices[0].message.content
        if not content or not content.strip():
            raise LLMError(
                f"Empty response from model {self.settings.model_name}",
                model_name=self.settings.model_name,
            )

        return content

    def _extract_json_from_response(self, content: str) -> str:
        if not content:
            return ""

        code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if code_block_match:
            try:
                candidate = code_block_match.group(1).strip()
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError as e:
                logger.debug(f"Failed to parse JSON from code block: {e}")
                pass

        first_brace = content.find("{")
        last_brace = content.rfind("}")

        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            potential_json = content[first_brace : last_brace + 1]
            potential_json = re.sub(r",\s*}", "}", potential_json)
            potential_json = potential_json.replace("’", "'").replace("“", '"').replace("”", '"')
            try:
                potential_json = potential_json.strip()
                json.loads(potential_json)
                return potential_json
            except json.JSONDecodeError:
                try:
                    cleaned_potential = re.sub(r"\n", " ", potential_json)
                    json.loads(cleaned_potential)
                    return cleaned_potential
                except json.JSONDecodeError as e:
                    logger.debug(f"Failed to parse cleaned JSON: {e}")
                    pass

        try:
            cleaned = re.sub(r"^[^{]*", "", content)
            cleaned = re.sub(r"[^}]*$", "", cleaned)
            json.loads(cleaned)
            return cleaned
        except json.JSONDecodeError as e:
            logger.debug(f"Failed to parse aggressively cleaned JSON: {e}")
            pass

        return ""
