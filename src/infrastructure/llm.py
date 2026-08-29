"""LLM service: OpenAI-compatible client with a two-tier retry strategy.

RETRY OWNERSHIP (deliberate two-level split - keep new retry logic on the
level that owns it, do not duplicate):

1. TRANSPORT level (this module, tenacity @retry): retries TRANSIENT
   infrastructure failures only - httpx timeouts/connect errors, OpenAI
   APIConnectionError and HTTP 429 RateLimitError. These are wrapped into
   NetworkError, which is the single exception type tenacity retries on.
   Policy: stop_after_attempt(3), exponential backoff 2-10s.

2. SEMANTIC level (orchestrator.run(), ad-hoc): retries "No valid JSON"
   parse failures by trimming conversation history and re-asking. These are
   LLMError (non-retryable here BY DESIGN): no amount of immediate
   re-sending fixes a malformed/truncated completion - the input context
   must change first, which only the orchestrator can do.

Consequence for future edits: a NEW transport-ish failure belongs in the
except-chain of _chat_completion() below (mapped to NetworkError); a NEW
"model answered but unusable" failure belongs in the orchestrator's
recovery path (LLMError). Never add a second tenacity layer or retry
LLMError here.

FAILOVER (Task 2 - LLM resilience): when a fallback provider is
configured (LLM_FALLBACK_* settings) and the ACTIVE provider fails at the
CONNECTION level (timeout / connect error / APIConnectionError - NOT 429,
see _chat_completion), _consider_failover() health-checks the fallback's
/models endpoint and, if it answers, transparently switches subsequent
attempts to it. The switch rides the EXISTING tenacity retry of
generate_action/generate_text: attempt 1 fails on primary -> failover ->
attempts 2-3 run on the fallback. No new retry layer is introduced.
Failover is sticky (no automatic switch-back - no ping-pong between two
flapping providers within one process lifetime) and bounded
(LLM_FALLBACK_MAX_SWITCHES attempts + a cooldown after an unhealthy
check). Unconfigured fallback = byte-for-byte old behavior.
"""

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
from . import metrics as _metrics

logger = logging.getLogger(__name__)

# After one failed fallback health check, wait this long before pinging
# the fallback again - so the tenacity retries of a single generate_*
# call don't fire a health check per attempt against a dead server.
_FAILOVER_COOLDOWN_SECONDS = 30.0


def _observe_retry_before_sleep(retry_state: Any) -> None:
    """tenacity before_sleep hook: count one transport-level retry.

    Observability for the TRANSPORT retry layer only (see module docstring):
    each exponential-backoff sleep before a re-attempt increments
    cogniweb_llm_retries_total{provider}. `provider` is the ACTIVE provider
    mode ('cloud' | 'local') - a closed settings value, NOT the model name
    string. Bound instance arrives as the first positional arg because
    generate_action/generate_text are called as bound methods; the hook
    must never raise (metrics failures are logged at debug, swallowed)."""
    try:
        instance = retry_state.args[0] if retry_state.args else None
        provider = str(getattr(instance, "active_provider_mode", "unknown") or "unknown")
        _metrics.observe_llm_retry(provider)
    except Exception:  # noqa: BLE001 - observability must never crash the app
        logger.debug("metrics retry hook failed", exc_info=True)


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

        # Task 2 (failover): optional fallback provider state. Everything
        # stays inert until a connection-level failure occurs AND
        # LLM_FALLBACK_PROVIDER_MODE is configured.
        self._fallback_client: AsyncOpenAI | None = None
        self._fallback_http_client: httpx.AsyncClient | None = None
        self._health_client: httpx.AsyncClient | None = None
        self._fallback_active = False
        self._failover_attempts_used = 0
        # After one unhealthy check, back off before pinging again - so a
        # single generate_action() call's tenacity retries don't hammer a
        # dead fallback server with a health check per attempt.
        self._failover_cooldown_until = 0.0

        # 3.1: real token accounting from the API's usage block. The
        # OpenAI-compatible response has always carried usage.prompt_tokens
        # / completion_tokens, but nothing read it - the operator had no
        # in-process view of a run's actual token cost.
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

        # Shared pacing clock for every caller of this service (all
        # orchestrators of a run, including run_parallel_agents).
        self.rate_limiter = LLMRateLimiter()

        # FIX (local reasoning models break JSON parsing): parse the
        # configured REASONING_STRIP_TAGS once. Angle brackets are optional
        # in the setting ("think" or "<think>"); empty entries and an empty
        # setting (stripping disabled) are both tolerated.
        raw_tags = str(getattr(self.settings, "reasoning_strip_tags", "") or "")
        self._reasoning_strip_tags: tuple[str, ...] = tuple(
            tag.strip().strip("<>").lower()
            for tag in raw_tags.split(",")
            if tag.strip().strip("<>")
        )

    @property
    def active_provider_mode(self) -> str:
        """Provider mode requests currently go to ('cloud' | 'local') -
        the fallback once failover has happened, else the primary."""
        if self._fallback_active:
            return self.settings.llm_fallback_provider_mode
        return self.settings.llm_provider_mode

    async def wait_for_rate_limit(self) -> None:
        """Pace requests per the configured interval for the ACTIVE provider
        mode (the fallback's pacing class applies after failover - e.g. a
        local->cloud failover must start respecting the cloud interval).
        Safe to call concurrently: all callers share one clock (see
        LLMRateLimiter), so N parallel orchestrators cannot exceed the
        configured rate in aggregate."""
        rate = (
            self.settings.local_rate_limit_seconds
            if self.active_provider_mode == "local"
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

    async def _check_provider_health(self, base_url: str, api_key: str) -> bool:
        """Task 2: lightweight liveness ping - GET {base_url}/models, which
        every OpenAI-compatible server exposes (Ollama's OpenAI-compatible
        endpoint, LM Studio, vLLM, OpenRouter). ANY HTTP response counts as
        'alive' (even 401/404: auth/routing problems will surface on the
        real call and are counted by the failover budget anyway); only a
        transport-level failure (connect error / timeout) means 'dead'."""
        timeout = float(getattr(self.settings, "llm_health_check_timeout_seconds", 5.0))
        if self._health_client is None:
            self._health_client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        url = base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            response = await self._health_client.get(url, headers=headers, timeout=timeout)
            alive = response.status_code < 500
            logger.debug(f"LLM health check {url} -> {response.status_code} (alive={alive})")
            return alive
        except Exception as e:
            logger.debug(f"LLM health check failed for {url}: {e}")
            return False

    async def health_check(self) -> bool:
        """Observability (/health): liveness of the ACTIVE provider (the
        fallback after a failover), same 'any HTTP response = alive' rule
        as the internal failover ping. Never raises."""
        if self._fallback_active:
            cfg = self.settings
            base_url, api_key = str(cfg.llm_fallback_base_url), str(cfg.llm_fallback_api_key)
        else:
            base_url, api_key = str(self.settings.api_base_url), str(self.settings.api_key)
        try:
            return await self._check_provider_health(base_url, api_key)
        except Exception:  # noqa: BLE001 - health checks never raise
            return False

    async def _consider_failover(self, reason: str) -> None:
        """Decide whether to switch to the fallback provider after a
        connection-level failure of the active one. Never raises: failover
        is best-effort resilience, and any problem here must degrade to
        'raise the original error', not mask it with a new one.

        Guards, in order:
        - fallback not configured -> no-op (byte-for-byte old behavior);
        - already on fallback -> no-op (sticky; no ping-pong);
        - attempt budget exhausted (LLM_FALLBACK_MAX_SWITCHES) -> no-op;
        - inside the post-unhealthy-check cooldown -> no-op.
        Each evaluation consumes one budget unit, so even a permanently
        dead pair of providers terminates instead of looping."""
        cfg = self.settings
        mode = getattr(cfg, "llm_fallback_provider_mode", "") or ""
        if not mode or self._fallback_active:
            return
        max_switches = int(getattr(cfg, "llm_fallback_max_switches", 3))
        if self._failover_attempts_used >= max_switches:
            logger.debug("LLM failover budget exhausted (%d attempts)", max_switches)
            return
        if time.monotonic() < self._failover_cooldown_until:
            return
        self._failover_attempts_used += 1

        base_url = str(cfg.llm_fallback_base_url)
        api_key = str(cfg.llm_fallback_api_key)
        if not await self._check_provider_health(base_url, api_key):
            self._failover_cooldown_until = time.monotonic() + _FAILOVER_COOLDOWN_SECONDS
            logger.warning(
                "Primary LLM provider unavailable (%s) but fallback health "
                "check failed too - staying on the primary for now "
                "(cooldown %.0fs, %d/%d failover attempts used)",
                reason,
                _FAILOVER_COOLDOWN_SECONDS,
                self._failover_attempts_used,
                max_switches,
            )
            return

        if self._fallback_client is None:
            self._fallback_http_client = (
                httpx.AsyncClient(proxy=cfg.proxy_url, timeout=httpx.Timeout(cfg.http_timeout))
                if cfg.proxy_url
                else None
            )
            self._fallback_client = AsyncOpenAI(
                api_key=api_key, base_url=base_url, http_client=self._fallback_http_client
            )
        self._fallback_active = True
        # Observability: count only ACTUAL switches (not failed health
        # checks / cooldown skips above) - the dashboard signal is "how
        # often did we leave the primary provider".
        _metrics.observe_llm_failover()
        logger.warning(
            "Primary LLM provider unavailable (%s) - FAILED OVER to fallback: "
            "mode=%s base=%s model=%s (pacing now follows the fallback's "
            "rate-limit class)",
            reason,
            mode,
            base_url,
            cfg.llm_fallback_model,
        )

    async def _chat_completion(self, messages: list[dict[str, Any]], temperature: float) -> Any:
        """Single chat.completions.create against the ACTIVE provider
        (primary, or fallback after failover) with the shared error
        mapping. Both generate_action() and generate_text() route through
        here so the mapping exists exactly once.

        Failover trigger scope (deliberate): ONLY connection-level
        failures (timeout / connect error / APIConnectionError). A 429 is
        transient BY DESIGN - tenacity's backoff handles it, and switching
        providers mid-run because of rate limiting would silently mask a
        pacing misconfiguration rather than fix it."""
        try:
            model = (
                self.settings.llm_fallback_model
                if self._fallback_active
                else self.settings.model_name
            )
            client = self._fallback_client if self._fallback_active else self.client
            return await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=self.settings.max_tokens,
            )
        except httpx.TimeoutException as e:
            _metrics.observe_llm_error("timeout")
            await self._consider_failover(f"timeout: {e}")
            raise NetworkError(f"Timeout connecting to LLM: {e}") from e
        except httpx.ConnectError as e:
            _metrics.observe_llm_error("connect")
            await self._consider_failover(f"connection error: {e}")
            raise NetworkError(f"Connection error contacting LLM: {e}") from e
        except APIConnectionError as e:
            _metrics.observe_llm_error("api_connection")
            await self._consider_failover(f"API connection error: {e}")
            raise NetworkError(f"API connection error: {e}") from e
        except OpenAIRateLimitError as e:
            # FIX: HTTP 429 - retryable with backoff, not a fatal LLMError.
            # Deliberately NOT a failover trigger (see docstring).
            _metrics.observe_llm_error("rate_limit")
            raise NetworkError(f"Rate limited by LLM provider (429): {e}") from e
        except NetworkError:
            # FIX (1.2): a NetworkError surfacing from the call itself must
            # reach tenacity as NetworkError - the generic handler below
            # would re-wrap it into a non-retried LLMError.
            raise
        except Exception as e:
            _metrics.observe_llm_error("api_error")
            raise LLMError(f"LLM request failed: {e}", model_name=self.settings.model_name) from e

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

        # Task 2 (failover): also release the lazily-created fallback and
        # health-check clients.
        if self._fallback_client is not None:
            try:
                await self._fallback_client.close()
            except Exception as e:
                logger.debug(f"Error closing fallback OpenAI client: {e}")
        if self._fallback_http_client is not None:
            await self._fallback_http_client.aclose()
        if self._health_client is not None:
            await self._health_client.aclose()

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
        #
        # This decorator is the ONLY tenacity layer in the project (see the
        # module docstring for the transport-vs-semantic retry ownership
        # split). LLMError must stay non-retryable HERE - JSON-parse
        # recovery is the orchestrator's job.
        retry=retry_if_exception_type((NetworkError, httpx.TimeoutException)),
        before_sleep=_observe_retry_before_sleep,
    )
    async def generate_action(
        self, messages: list[dict[str, Any]], temperature: float = 0.1
    ) -> AgentAction:
        # NOTE: message content is typed as Any (not str) because Task 4's
        # vision fallback sends OpenAI-style multimodal content (a list of
        # {"type": "text"/"image_url", ...} parts) for a single message
        # rather than a plain string. This method doesn't need to care -
        # it's forwarded to the API as-is - only the response is parsed.
        #
        # Task 2 (failover): the call + error mapping + failover decision
        # live in _chat_completion(); tenacity retries THIS method, so an
        # attempt that fails on the primary is transparently retried on
        # the fallback once _consider_failover() has switched.
        response = await self._chat_completion(messages, temperature)

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
        before_sleep=_observe_retry_before_sleep,
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
        # Task 2 (failover): same shared call/mapping/failover path as
        # generate_action() - see _chat_completion().
        response = await self._chat_completion(messages, temperature)

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

    def _strip_reasoning_blocks(self, content: str) -> str:
        """FIX (local reasoning models break JSON parsing): remove
        reasoning/thinking blocks (<think>...</think> and friends) BEFORE
        any JSON extraction runs.

        Why: DeepSeek R1 distills, Qwen3 with thinking enabled (and other
        local reasoning models) emit deliberation wrapped in such tags
        before the final answer. That deliberation often MENTIONS JSON
        ("the format is {\"tool\": ...}"), so the brace-scanning fallbacks
        below could lock onto a brace pair from inside the reasoning -
        producing either a parse failure or, worse, a technically valid but
        semantically wrong action.

        Two passes per configured tag:
        1. paired blocks: <tag ...>...</tag> (non-greedy, DOTALL - handles
           multiple blocks and attributes/whitespace in the opener);
        2. an UNPAIRED leftover opener (truncated generation that never
           emitted the closing tag): everything from the first remaining
           opener to the end is treated as unfinished reasoning. If a final
           answer existed there, the response was truncated mid-thought
           anyway and would not contain a complete action.

        This is defense-in-depth: operators of local deployments should
        ALSO disable thinking at the server level where possible (see
        docs/LOCAL_MODELS.md). Configure via REASONING_STRIP_TAGS; empty
        setting disables stripping entirely.
        """
        stripped = content
        for tag in self._reasoning_strip_tags:
            paired = re.compile(rf"<{tag}\b[^>]*>.*?</{tag}\s*>", re.DOTALL | re.IGNORECASE)
            if paired.search(stripped):
                stripped = paired.sub("", stripped)
            # Truncated generation: an opener with no closing tag anywhere.
            leftover = re.search(rf"<{tag}\b[^>]*>", stripped, re.IGNORECASE)
            if leftover:
                logger.debug(
                    "Stripped unclosed <%(tag)s> block (no </%(tag)s>) from LLM response",
                    {"tag": tag},
                )
                stripped = stripped[: leftover.start()]
        return stripped

    def _extract_json_from_response(self, content: str) -> str:
        if not content:
            return ""

        # Reasoning-block strip FIRST - before code-block regex or any
        # brace scanning can see deliberation text as candidate JSON.
        content = self._strip_reasoning_blocks(content)

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
