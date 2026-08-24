"""
Configuration Management with Pydantic v2 Settings.

This module uses Pydantic's BaseSettings for type-safe configuration management.
Environment variables are loaded and validated automatically, providing immediate
feedback on misconfiguration rather than runtime failures.

Why Pydantic Settings?
- Type validation at startup prevents runtime errors
- Environment variable loading with sensible defaults
- Documentation through field descriptions
- Easy testing via model instantiation with overrides
"""

import re
import warnings
from pathlib import Path
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    Uses Pydantic v2 Settings for automatic environment variable parsing
    with type validation. This prevents runtime configuration errors.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore unknown env vars
        # FIX (5.1 test fixture bug): without this, Settings(api_key=...)
        # (using the Python field name) silently ignores the kwarg and falls
        # through to requiring OPENAI_API_KEY from the environment, because
        # aliases are populated by alias only by default. This caused 11/19
        # tests to fail locally (fixture-level, unrelated to code under
        # test) unless OPENAI_API_KEY was exported - a pure DX/test-hygiene
        # bug, now fixed at the source instead of papering over it in CI.
        populate_by_name=True,
    )

    # ===== Provider Mode =====
    # FIX (Task 1 - local LLM providers): previously local/self-hosted
    # OpenAI-compatible servers (LM Studio, text-generation-webui, etc.)
    # were only reachable "by accident" - the validators below existed
    # purely to *block* one specific local case (Ollama on its default
    # port) and had no notion of "the user genuinely wants a local
    # server". This field makes local usage a deliberate, explicit choice
    # instead of a side effect: it must be set to "local" on purpose, and
    # doing so relaxes exactly the guards that were written for the cloud
    # (OpenRouter) scenario, without touching the default cloud behavior
    # at all.
    #
    # NOTE: this field is declared before api_key/api_base_url/model_name
    # on purpose - Pydantic v2 field_validators can only read already-
    # validated sibling fields via `info.data`, which only contains
    # fields declared earlier in the class.
    llm_provider_mode: str = Field(
        default="cloud",
        alias="LLM_PROVIDER_MODE",
        description=(
            "'cloud' (default): keep the existing OpenRouter-oriented "
            "guards - real-looking API key required, provider/model name "
            "format enforced, accidental local-Ollama URLs blocked. "
            "'local': explicitly targets a local OpenAI-compatible server "
            "(LM Studio, text-generation-webui, vLLM, etc.) - relaxes the "
            "API key / model name / URL checks that only make sense for a "
            "hosted cloud API, and switches request pacing to "
            "LOCAL_RATE_LIMIT_SECONDS instead of RATE_LIMIT_SECONDS."
        ),
    )

    @field_validator("llm_provider_mode")
    @classmethod
    def validate_llm_provider_mode(cls, v: str) -> str:
        v = (v or "cloud").strip().lower()
        if v not in ("cloud", "local"):
            raise ValueError(f"LLM_PROVIDER_MODE must be 'cloud' or 'local', got: '{v}'")
        return v

    # ===== API Configuration =====
    api_key: str = Field(
        ...,  # Required field
        alias="OPENAI_API_KEY",
        description="OpenRouter API key (from https://openrouter.ai/keys), or any "
        "non-empty placeholder when LLM_PROVIDER_MODE=local",
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: str, info) -> str:
        """Validate API key is not placeholder (cloud mode only)."""
        if info.data.get("llm_provider_mode") == "local":
            # Local OpenAI-compatible servers (LM Studio and similar)
            # typically don't check the key at all - conventionally people
            # pass something like "lm-studio" or "not-needed". The OpenAI
            # SDK still requires *some* non-empty string, so that's the
            # only thing we still enforce here.
            if not v or not v.strip():
                raise ValueError(
                    "OPENAI_API_KEY cannot be empty even in local mode - the "
                    "OpenAI SDK requires a non-empty string. Use a placeholder "
                    "like 'lm-studio' if your local server doesn't check it."
                )
            return v

        placeholders = [
            "your_api_key_here",
            "your_openrouter_api_key_here",
            "sk-your-key-here",
            "ollama",
            "test",
            "none",
            "",
        ]

        if v.lower() in placeholders or len(v) < 10:
            raise ValueError(
                "Invalid API key detected.\n"
                "Please set OPENAI_API_KEY in .env file.\n"
                "Get your key from: https://openrouter.ai/keys\n"
                "(Configuring a local server instead? Set LLM_PROVIDER_MODE=local.)"
            )

        return v

    api_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        alias="API_BASE_URL",
        description="LLM API base URL (OpenRouter/OpenAI compatible, or a local "
        "server's URL when LLM_PROVIDER_MODE=local)",
    )

    @field_validator("api_base_url")
    @classmethod
    def validate_api_url(cls, v: str, info) -> str:
        """Validate API URL. Cloud mode blocks accidental-Ollama / enforces
        HTTPS; local mode trusts the explicit opt-in and only checks the
        URL is well-formed http(s)."""
        parsed = urlparse(v)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid API_BASE_URL format: {v}")

        if info.data.get("llm_provider_mode") == "local":
            if parsed.scheme not in ("http", "https"):
                raise ValueError(f"API_BASE_URL must use http:// or https://. Got: {v}")
            return v

        # Check for Ollama patterns
        ollama_patterns = ["localhost:11434", "127.0.0.1:11434", "0.0.0.0:11434"]

        for pattern in ollama_patterns:
            if pattern in v.lower():
                raise ValueError(
                    f"Ollama localhost URL detected: {v}\n"
                    "This codebase uses OpenRouter, not Ollama, by default.\n"
                    "Set API_BASE_URL=https://openrouter.ai/api/v1, or if a "
                    "local server is genuinely intended, set "
                    "LLM_PROVIDER_MODE=local to opt in explicitly."
                )

        # Enforce HTTPS (except localhost for dev)
        if not v.startswith("https://") and "localhost" not in v and "127.0.0.1" not in v:
            raise ValueError(f"API_BASE_URL must use HTTPS. Got: {v}")

        return v

    model_name: str = Field(
        default="upstage/solar-pro",
        alias="MODEL_NAME",
        description="Model to use (OpenRouter format: provider/model:version, or "
        "any local model name when LLM_PROVIDER_MODE=local)",
    )

    @field_validator("model_name")
    @classmethod
    def validate_model_name(cls, v: str, info) -> str:
        """Validate model name format."""
        if not v or len(v) < 2:
            raise ValueError("MODEL_NAME cannot be empty")

        if info.data.get("llm_provider_mode") == "local":
            # Local servers commonly expose arbitrary model names/paths
            # with no "provider/" prefix (e.g. "mistral-7b-instruct",
            # "TheBloke/Mistral-7B-GGUF"). Only the non-empty check above
            # applies here - deliberately no format/allowlist enforcement.
            return v

        # Check for Ollama models
        ollama_patterns = ["llama3:", "mistral:", "codellama:", "phi3:"]
        for pattern in ollama_patterns:
            if v.lower().startswith(pattern):
                raise ValueError(
                    f"Ollama model name detected: {v}\n"
                    "Use OpenRouter format: provider/model:version\n"
                    "Example: upstage/solar-pro\n"
                    "(Targeting a local model on purpose? Set LLM_PROVIDER_MODE=local.)"
                )

        # FIX (5.4): this format check was dead (commented out) in the
        # original code, so a meaningless value like "xx" or "gibberish"
        # passed validation despite the field description explicitly
        # promising "OpenRouter format: provider/model:version". Re-enabled,
        # with an allowlist for legacy bare OpenAI model names.
        legacy_openai_models = {"gpt-4", "gpt-3.5-turbo", "gpt-4o-mini", "gpt-4o", "gpt-4-turbo"}
        if "/" not in v and v not in legacy_openai_models:
            raise ValueError(
                f"Invalid model format: '{v}'\n"
                "Use OpenRouter format: provider/model (e.g. upstage/solar-pro), "
                "or a known legacy OpenAI model name."
            )

        return v

    # ===== Network Configuration =====
    proxy_url: str | None = Field(
        default=None, alias="PROXY_URL", description="HTTP proxy URL for network requests"
    )

    http_timeout: float = Field(
        default=120.0, alias="HTTP_TIMEOUT", description="HTTP request timeout in seconds"
    )

    @field_validator("http_timeout")
    @classmethod
    def validate_timeout(cls, v: float) -> float:
        """Validate timeout is reasonable."""
        if v > 300:
            warnings.warn(
                f"HTTP_TIMEOUT is very high: {v}s\n" "Recommended for cloud APIs: 60-120 seconds",
                stacklevel=2,
            )

        if v < 10:
            raise ValueError("HTTP_TIMEOUT too low (min 10s)")

        return v

    # ===== Browser Configuration =====
    user_data_dir: Path = Field(
        default=Path("./browser_data"),
        alias="USER_DATA_DIR",
        description="Directory for browser session persistence",
    )

    headless: bool = Field(
        default=False, alias="HEADLESS", description="Run browser in headless mode"
    )

    slow_mo: int = Field(
        default=50,
        ge=0,
        le=1000,
        alias="SLOW_MO",
        description="Milliseconds delay between actions (anti-fingerprint)",
    )

    page_load_timeout: int = Field(
        default=60000,
        ge=5000,
        alias="PAGE_LOAD_TIMEOUT",
        description="Page load timeout in milliseconds",
    )

    action_timeout: int = Field(
        default=20000,
        ge=1000,
        alias="ACTION_TIMEOUT",
        description="Individual action timeout in milliseconds",
    )

    # ===== Agent Configuration =====
    max_steps: int = Field(
        default=50,
        ge=1,
        le=200,
        alias="MAX_STEPS",
        description="Maximum reasoning-action steps before giving up",
    )

    max_retry_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        alias="MAX_RETRY_ATTEMPTS",
        description="Retry attempts for failed actions",
    )
    agent_step_delay: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        alias="AGENT_STEP_DELAY",
        description="Seconds to wait between agent steps to avoid overload",
    )

    # FIX (README claim "Rate Limiting: Настраиваемый ... (по умолчанию 15
    # сек)"): RATE_LIMIT_SECONDS was previously hardcoded inside
    # orchestrator.py with no corresponding Settings field, so it was NOT
    # actually configurable via .env despite the README's claim. Now a real
    # setting.
    rate_limit_seconds: float = Field(
        default=15.0,
        ge=0.0,
        le=120.0,
        alias="RATE_LIMIT_SECONDS",
        description="Minimum seconds between LLM calls in cloud mode "
        "(rate limiting for free-tier APIs)",
    )

    # FIX (Task 1 - local LLM providers): a local server has no external
    # rate limit to respect - RATE_LIMIT_SECONDS' 15s default exists
    # purely to avoid OpenRouter free-tier 429s and would just make local
    # runs pointlessly slow. But zero pacing isn't free either: hammering
    # a local model process back-to-back with no gap at all can cause real
    # perf/thermal issues on consumer hardware running inference. This
    # gives local mode its own, independently configurable (and much
    # smaller by default) pacing delay instead of reusing the cloud one or
    # dropping pacing entirely. See orchestrator._wait_for_rate_limit().
    local_rate_limit_seconds: float = Field(
        default=1.0,
        ge=0.0,
        le=60.0,
        alias="LOCAL_RATE_LIMIT_SECONDS",
        description="Minimum seconds between LLM calls when "
        "LLM_PROVIDER_MODE=local. Set to 0 to disable pacing entirely.",
    )

    # ===== LLM Fallback Provider (health-check + controlled failover) =====
    # FIX (Task 2 - LLM resilience): LLM_PROVIDER_MODE is a static flag -
    # if the configured server (local Ollama/vLLM or even a cloud endpoint)
    # dies mid-run, the agent just accumulated connection errors. These
    # fields define an OPTIONAL backup provider: on repeated connection-
    # level failures of the primary, LLMService pings the fallback's
    # /models endpoint and, if it answers, transparently continues on it.
    #
    # Principle kept from the original provider-mode work: EXPLICIT flags,
    # never auto-detection by URL/port (auto-detection was a past source
    # of bugs - see SELF_REVIEW.md). Empty LLM_FALLBACK_PROVIDER_MODE
    # (default) disables the whole mechanism byte-for-byte.
    llm_fallback_provider_mode: str = Field(
        default="",
        alias="LLM_FALLBACK_PROVIDER_MODE",
        description="Optional fallback provider mode: '' (default, "
        "failover disabled), 'cloud' or 'local'. When set, LLMService may "
        "switch to this provider after connection-level failures of the "
        "primary one (health-checked first).",
    )

    @field_validator("llm_fallback_provider_mode")
    @classmethod
    def validate_llm_fallback_provider_mode(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in ("", "cloud", "local"):
            raise ValueError(
                f"LLM_FALLBACK_PROVIDER_MODE must be '', 'cloud' or 'local', got: '{v}'"
            )
        return v

    llm_fallback_base_url: str | None = Field(
        default=None,
        alias="LLM_FALLBACK_BASE_URL",
        description="Base URL of the fallback OpenAI-compatible server "
        "(required when LLM_FALLBACK_PROVIDER_MODE is set).",
    )

    llm_fallback_api_key: str | None = Field(
        default=None,
        alias="LLM_FALLBACK_API_KEY",
        description="API key for the fallback provider (any non-empty "
        "placeholder for a local fallback server).",
    )

    llm_fallback_model: str | None = Field(
        default=None,
        alias="LLM_FALLBACK_MODEL",
        description="Model name served by the fallback provider.",
    )

    @model_validator(mode="after")
    def validate_llm_fallback_config(self) -> "Settings":
        """Fallback fields are all-or-nothing: a half-configured fallback
        would fail exactly when it is needed most (mid-outage), so refuse
        it at startup instead."""
        if not self.llm_fallback_provider_mode:
            return self
        missing = [
            env_name
            for env_name, value in (
                ("LLM_FALLBACK_BASE_URL", self.llm_fallback_base_url),
                ("LLM_FALLBACK_API_KEY", self.llm_fallback_api_key),
                ("LLM_FALLBACK_MODEL", self.llm_fallback_model),
            )
            if value is None or not str(value).strip()
        ]
        if missing:
            raise ValueError(
                f"LLM_FALLBACK_PROVIDER_MODE='{self.llm_fallback_provider_mode}' "
                f"requires {', '.join(missing)} to be set as well"
            )
        parsed = urlparse(str(self.llm_fallback_base_url))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"Invalid LLM_FALLBACK_BASE_URL format: {self.llm_fallback_base_url}"
            )
        if self.llm_fallback_provider_mode == "cloud":
            base = str(self.llm_fallback_base_url)
            if (
                not base.startswith("https://")
                and "localhost" not in base
                and "127.0.0.1" not in base
            ):
                raise ValueError(f"LLM_FALLBACK_BASE_URL must use HTTPS for cloud mode: {base}")
            key = str(self.llm_fallback_api_key)
            if key.lower() in ("your_api_key_here", "test", "none", "") or len(key) < 10:
                raise ValueError("LLM_FALLBACK_API_KEY looks like a placeholder (cloud mode)")
        return self

    llm_health_check_timeout_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        alias="LLM_HEALTH_CHECK_TIMEOUT_SECONDS",
        description="Timeout for the lightweight GET /models liveness ping "
        "used before switching to the fallback provider.",
    )

    llm_fallback_max_switches: int = Field(
        default=3,
        ge=1,
        le=20,
        alias="LLM_FALLBACK_MAX_SWITCHES",
        description="Maximum failover attempts per LLMService lifetime - "
        "bounds thrashing between two dead providers instead of looping.",
    )

    # FIX (2.2 Major): with no independent wall-clock timeout, an
    # undetected thrash loop could run up to MAX_STEPS * RATE_LIMIT_SECONDS
    # (~12.5 minutes at defaults) before MAX_STEPS kicked in. This gives an
    # explicit upper bound regardless of step count.
    max_wall_clock_seconds: float = Field(
        default=1800.0,
        ge=30.0,
        alias="MAX_WALL_CLOCK_SECONDS",
        description="Absolute wall-clock timeout for a single task run, independent of step count",
    )

    # FIX (2.1 doc drift): README/SELF_REVIEW document window_size=10 as the
    # token-budget baseline, but the live loop actually called
    # get_trimmed_history(window_size=5). Both numbers are now real,
    # independently configurable settings instead of magic numbers baked
    # into orchestrator.run().
    conversation_window_size: int = Field(
        default=10,
        ge=1,
        le=50,
        alias="CONVERSATION_WINDOW_SIZE",
        description="Number of recent messages (plus system prompt) kept in context per step",
    )

    json_retry_window_size: int = Field(
        default=2,
        ge=1,
        le=10,
        alias="JSON_RETRY_WINDOW_SIZE",
        description="Aggressively-trimmed window size used when retrying after a JSON parse failure",
    )

    # FIX (Security 1.3 / upload_file): directory that uploaded files must
    # resolve inside of. Prevents path traversal via a hallucinated or
    # attacker-influenced file_path.
    upload_allowed_dir: Path = Field(
        default=Path("./uploads"),
        alias="UPLOAD_ALLOWED_DIR",
        description="Directory that upload_file() paths must resolve within (prevents path traversal)",
    )
    # ===== LLM Configuration =====
    max_tokens: int = Field(
        default=2000, ge=100, alias="MAX_TOKENS", description="Maximum tokens in LLM response"
    )

    temperature: float = Field(
        default=0.1,
        ge=0.0,
        le=2.0,
        alias="TEMPERATURE",
        description="LLM temperature (lower = more deterministic)",
    )

    # ===== DOM Processing =====
    text_block_max_length: int = Field(
        default=200,
        ge=50,
        alias="TEXT_BLOCK_MAX_LENGTH",
        description="Maximum characters per text block in DOM",
    )

    dom_max_tokens_estimate: int = Field(
        default=10000,
        ge=1000,
        alias="DOM_MAX_TOKENS_ESTIMATE",
        description="Maximum estimated tokens for DOM representation",
    )

    # ===== Loop Detection =====
    loop_detection_window: int = Field(
        default=3,
        ge=2,
        le=10,
        alias="LOOP_DETECTION_WINDOW",
        description="Number of states to check for loops",
    )

    max_identical_states: int = Field(
        default=5,
        ge=2,
        alias="MAX_IDENTICAL_STATES",
        description="Maximum identical states before intervention",
    )

    # FIX (2.2 Critical/Major): previously the "5 failures in a row" check
    # in _check_for_loops() depended transitively on loop_detection_window
    # because action_history was truncated to that same length - making the
    # check mathematically unreachable at the documented default
    # (loop_detection_window=3 < 5). This gives that check its own
    # independent history buffer length, decoupled from
    # loop_detection_window, so raising/lowering one does not silently
    # disable the other.
    failure_streak_window: int = Field(
        default=5,
        ge=2,
        le=20,
        alias="FAILURE_STREAK_WINDOW",
        description="Number of recent actions checked for the 'all failed' thrash-detection rule",
    )

    # ===== Stealth Configuration (Task 4: stealth browser mode) =====
    # Master switch. Unlike every other opt-in feature flag in this project,
    # this one defaults to True: stealth mode does not change WHAT the agent
    # does functionally (same tools, same task semantics) - it only makes the
    # legit automation session less likely to be misclassified as a bot by
    # anti-fingerprinting heuristics (which causes spurious captchas/blocks).
    # Set ENABLE_STEALTH_MODE=false to get the "raw" Playwright profile back
    # (e.g. for before/after debugging screenshots).
    # Alias ENABLE_STEALTH is kept so pre-existing .env files keep working.
    enable_stealth_mode: bool = Field(
        default=True,
        validation_alias=AliasChoices("ENABLE_STEALTH_MODE", "ENABLE_STEALTH"),
        description="Apply the stealth browser profile (fingerprint init "
        "scripts, consistent UA/locale/timezone/viewport, human-like mouse "
        "and typing patterns, optional playwright-stealth patches). This "
        "lowers false-positive bot detection for LEGITIMATE sessions; it "
        "never solves or bypasses an already-presented captcha.",
    )

    # The stealth profile must be internally CONSISTENT, not "random": a
    # mismatched fingerprint (latest-Chrome-on-Windows UA + headless WebGL
    # renderer, or en-US UA + ru-RU Accept-Language) is itself a stronger
    # bot signal than an imperfect-but-coherent profile. All four fields
    # below are applied together whenever ENABLE_STEALTH_MODE=true.
    stealth_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        alias="STEALTH_USER_AGENT",
        description="User-Agent used for the browser context. Must stay "
        "consistent with STEALTH_LOCALE / STEALTH_TIMEZONE / the WebGL "
        "renderer patched in by init scripts.",
        min_length=20,
    )

    stealth_locale: str = Field(
        default="en-US",
        alias="STEALTH_LOCALE",
        description="Browser locale (ICU tag, e.g. 'en-US', 'ru-RU'). Drives "
        "context locale, navigator.languages and the Accept-Language header "
        "so all three agree.",
    )

    stealth_timezone: str = Field(
        default="America/New_York",
        alias="STEALTH_TIMEZONE",
        description="IANA timezone id for the browser context (e.g. "
        "'America/New_York', 'Europe/Moscow'). Should be plausible for "
        "STEALTH_LOCALE.",
    )

    stealth_viewport_width: int = Field(
        default=1920,
        ge=320,
        le=7680,
        alias="STEALTH_VIEWPORT_WIDTH",
        description="Viewport width for the stealth profile (keep it a "
        "common desktop resolution).",
    )

    stealth_viewport_height: int = Field(
        default=1080,
        ge=240,
        le=4320,
        alias="STEALTH_VIEWPORT_HEIGHT",
        description="Viewport height for the stealth profile.",
    )

    @field_validator("stealth_locale")
    @classmethod
    def validate_stealth_locale(cls, v: str) -> str:
        """Locale must look like an ICU tag (xx or xx-XX) - it is passed to
        Playwright's context locale and into Accept-Language verbatim."""
        v = (v or "").strip()
        if not re.fullmatch(r"[a-z]{2,3}(-[A-Za-z]{2,8})*", v):
            raise ValueError(
                f"STEALTH_LOCALE must be an ICU tag like 'en-US' or 'ru-RU', got: '{v}'"
            )
        return v

    # FIX (3.3, captcha handling - human-in-the-loop scope): reduces how
    # often captchas are triggered in the first place (varied mouse
    # movement before interacting with a page, human-like headers).
    # This is NOT captcha solving/bypass of an active challenge - it only
    # lowers automated-traffic fingerprinting signals a site's bot
    # detection may key off of. See BrowserService._human_mouse_warmup().
    captcha_avoidance_mode: bool = Field(
        default=True,
        alias="CAPTCHA_AVOIDANCE_MODE",
        description="Apply light anti-fingerprinting warmup (randomized mouse "
        "movement, human-like Accept-Language/sec-ch-ua-platform headers) "
        "after each browser start, to reduce captcha trigger frequency. "
        "Does not solve or bypass an already-presented captcha.",
    )

    typing_speed_min: int = Field(
        default=50,
        ge=10,
        alias="TYPING_SPEED_MIN",
        description="Minimum ms delay between keystrokes",
    )

    typing_speed_max: int = Field(
        default=150,
        ge=50,
        alias="TYPING_SPEED_MAX",
        description="Maximum ms delay between keystrokes",
    )

    typing_slow_path_max_chars: int = Field(
        default=200,
        ge=1,
        alias="TYPING_SLOW_PATH_MAX_CHARS",
        description="Texts longer than this many characters are entered with a "
        "single instant fill() instead of the per-keystroke human-like loop "
        "(which at the default 50-150ms/char would take minutes for long "
        "texts and always exceed ACTION_TIMEOUT). Anti-fingerprinting "
        "timing matters for short human-like inputs, not for pasting long "
        "content.",
    )

    # ===== Context Compaction (Task 3) =====
    # FIX (Task 3): get_trimmed_history() (conversation_window_size /
    # json_retry_window_size above) is deliberately dumb, cheap hard
    # truncation - it just drops the oldest raw messages. That's a fine
    # safety net, but on a genuinely long session it can silently discard
    # task-critical facts (an early store_context result, or *why* a
    # previous approach already failed). Compaction is a separate,
    # additional mechanism: instead of dropping old messages, it asks the
    # LLM to compress them into a short status report first. See
    # AgentOrchestrator._maybe_compact_history().
    enable_context_compaction: bool = Field(
        default=True,
        alias="ENABLE_CONTEXT_COMPACTION",
        description="Enable LLM-driven history summarization for long sessions, "
        "on top of (not instead of) the existing hard-truncation safety net.",
    )

    compaction_trigger_messages: int = Field(
        default=30,
        ge=5,
        le=500,
        alias="COMPACTION_TRIGGER_MESSAGES",
        description="Compact once conversation_history (excluding the system "
        "prompt) exceeds this many messages.",
    )

    compaction_trigger_tokens_estimate: int = Field(
        default=12000,
        ge=1000,
        alias="COMPACTION_TRIGGER_TOKENS_ESTIMATE",
        description="Compact once the estimated token size of the working "
        "history exceeds this. Uses the same cheap chars/4 heuristic the "
        "project already relies on elsewhere (see SELF_REVIEW.md's 'why NOT "
        "tiktoken' rationale) rather than a real tokenizer.",
    )

    # Hard in-memory bound on conversation_history, applied INDEPENDENTLY
    # of compaction: with enable_context_compaction=False (valid, but
    # non-default) the old code's only protection was get_trimmed_history()
    # - which trims what is SENT to the LLM, not what accumulates in the
    # process. A max_steps=200 run would grow conversation_history without
    # limit. When the cap is hit the oldest messages (after the system
    # prompt) are dropped without any LLM summarization.
    history_hard_cap_messages: int = Field(
        default=200,
        ge=10,
        le=1000,
        alias="HISTORY_HARD_CAP_MESSAGES",
        description="Absolute maximum number of messages kept in "
        "conversation_history in memory. Excess oldest messages (system "
        "prompt excluded) are dropped - even when ENABLE_CONTEXT_COMPACTION"
        "=false, so a long run cannot grow process memory without bound.",
    )

    # ===== Vision / Visual Fallback (Task 4 + Browser-Use ideas) =====
    # FIX (Task 4): on heavy/poorly-structured pages, text-based DOM
    # extraction can come back empty (extraction failed) or so large and
    # text-sparse that the LLM has no real signal for what's relevant. As
    # a fallback ONLY for those cases, the agent can switch to sending an
    # annotated screenshot (numbered boxes over interactive elements,
    # reusing the same element_id used in text mode) to a vision-capable
    # model. See AgentOrchestrator._should_use_vision_fallback() /
    # _get_action_via_vision().
    #
    # Task 3 (Browser-Use set-of-marks): the same fallback additionally
    # triggers after N consecutive SelectorError-ish step failures
    # (visual_fallback_error_streak below) - the case where the DOM snapshot
    # exists but keeps failing to ground the element the model wants.
    # Default is False (off): vision calls are slower/pricier, and the flag
    # is additionally gated by MODEL_SUPPORTS_VISION. Both env spellings
    # (ENABLE_VISION_FALLBACK / ENABLE_VISUAL_FALLBACK) are accepted.
    # NOTE on the default: this flag PRE-DATES the visual-fallback task
    # and its default was already `true` there, so it stays `true` - the
    # project convention is that existing defaults never change. The
    # effective default behavior is still "off": MODEL_SUPPORTS_VISION
    # (default false) gates every vision call, so text-only providers are
    # never affected. The new ENABLE_VISUAL_FALLBACK spelling is just an
    # accepted alias, not a new flag.
    enable_vision_fallback: bool = Field(
        default=True,
        validation_alias=AliasChoices("ENABLE_VISION_FALLBACK", "ENABLE_VISUAL_FALLBACK"),
        description="Allow falling back to an annotated screenshot (set-of-marks "
        "style, numbered boxes = element_id) when text-based DOM extraction "
        "is empty/failed/too noisy, or when the same element-targeting step "
        "keeps failing (VISUAL_FALLBACK_ERROR_STREAK). A no-op unless "
        "MODEL_SUPPORTS_VISION is also enabled.",
    )

    model_supports_vision: bool = Field(
        default=False,
        alias="MODEL_SUPPORTS_VISION",
        description="Whether MODEL_NAME accepts image content blocks (GPT-4o, "
        "Claude 3+, Gemini, local vision models such as LLaVA/Qwen-VL, etc). "
        "Defaults to False so text-only/cloud providers are never silently "
        "sent an image they can't handle - this must be turned on explicitly.",
    )

    vision_fallback_max_elements: int = Field(
        default=80,
        ge=10,
        le=500,
        alias="VISION_FALLBACK_MAX_ELEMENTS",
        description="If text extraction returns more than this many interactive "
        "elements AND most of them carry no useful text, treat the page as too "
        "noisy for reliable text-only reasoning and consider vision fallback.",
    )

    # Task 3 (Browser-Use visual fallback): after this many consecutive
    # steps ending in an element-targeting failure (InvalidElementId /
    # SelectorError-style), switch the next step to the annotated-
    # screenshot mode instead of feeding the same failing text snapshot.
    visual_fallback_error_streak: int = Field(
        default=2,
        ge=1,
        le=10,
        alias="VISUAL_FALLBACK_ERROR_STREAK",
        description="Number of consecutive element-targeting failures "
        "(InvalidElementId etc.) before the visual (set-of-marks) fallback "
        "kicks in. Only relevant when ENABLE_VISUAL_FALLBACK and "
        "MODEL_SUPPORTS_VISION are both on.",
    )

    # ===== Post-MVP features (opt-in; defaults preserve existing behavior) =====

    # 2.1: how tokens are estimated when budgeting the DOM observation.
    token_counter_mode: str = Field(
        default="heuristic",
        alias="TOKEN_COUNTER_MODE",
        description="'heuristic' (default): cheap chars/4 estimate, no extra "
        "dependency. 'tiktoken': real tokenizer via lazy import; silently "
        "falls back to the heuristic (one log per run) if the package is "
        "not installed.",
    )

    @field_validator("token_counter_mode")
    @classmethod
    def validate_token_counter_mode(cls, v: str) -> str:
        v = (v or "heuristic").strip().lower()
        if v not in ("heuristic", "tiktoken"):
            raise ValueError(f"TOKEN_COUNTER_MODE must be 'heuristic' or 'tiktoken', got: '{v}'")
        return v

    # 2.2: self-critique evaluator on 'done'. Off by default.
    enable_evaluator: bool = Field(
        default=False,
        alias="ENABLE_EVALUATOR",
        description="When True, a 'done' action triggers one extra LLM "
        "self-critique call (VERDICT:PASS/FAIL). FAIL pushes a corrective "
        "message and the loop continues, up to evaluator_max_retries times.",
    )

    evaluator_max_retries: int = Field(
        default=1,
        ge=0,
        le=5,
        alias="EVALUATOR_MAX_RETRIES",
        description="How many times the evaluator may reject a 'done' and "
        "send the agent back to work before the result is returned as-is.",
    )

    # 2.3: parallel multi-page task execution.
    enable_multi_page: bool = Field(
        default=False,
        alias="ENABLE_MULTI_PAGE",
        description="Allow run_parallel_agents(): multiple orchestrators on "
        "separate pages (shared browser context, isolated per-task state).",
    )

    # 2.4: host-level navigation policy (SSRF / lateral-movement guard).
    navigate_allowed_domains: list[str] | None = Field(
        default=None,
        alias="NAVIGATE_ALLOWED_DOMAINS",
        description="Optional allowlist of hostnames the agent may navigate "
        "to. None (default) = no domain restriction. Example: "
        "['example.com', 'api.example.com'] (subdomains must be listed "
        "explicitly).",
    )

    navigate_block_private_networks: bool = Field(
        default=True,
        alias="NAVIGATE_BLOCK_PRIVATE_NETWORKS",
        description="Resolve the target host before navigation and refuse "
        "RFC1918/loopback/link-local addresses (including "
        "169.254.169.254 cloud metadata) unless the host is explicitly "
        "listed in NAVIGATE_ALLOWED_DOMAINS. Set to False to opt out "
        "(e.g. when the operator genuinely targets internal services).",
    )

    # 2.5: circuit breaker for repeated captcha checkpoints.
    captcha_circuit_breaker_threshold: int = Field(
        default=3,
        ge=1,
        le=100,
        alias="CAPTCHA_CIRCUIT_BREAKER_THRESHOLD",
        description="Stop opening blocking human-in-the-loop checkpoints "
        "after this many captcha events in a single run; return a "
        "CaptchaCircuitBreaker TaskResult instead of hanging indefinitely.",
    )

    # 3.1: run report output directory.
    reports_dir: Path = Field(
        default=Path("./reports"),
        alias="REPORTS_DIR",
        description="Directory for per-run JSON reports (tokens, steps, "
        "loop triggers, captcha events, errors).",
    )

    # ===== Task 2 (new tools) =====
    # download_file(): downloads are saved here. Like UPLOAD_ALLOWED_DIR,
    # this keeps agent-initiated writes to a single operator-controlled
    # directory.
    download_allowed_dir: Path = Field(
        default=Path("./downloads"),
        alias="DOWNLOAD_ALLOWED_DIR",
        description="Directory where download_file() saves files (mirrors "
        "UPLOAD_ALLOWED_DIR for downloads).",
    )

    # ===== Task 3 (Crawl4AI approach: clean Markdown extraction) =====
    # extract_page_content(): page HTML -> cleaned Markdown/text with the
    # noise (nav/script/style/ads boilerplate) filtered out. Massively
    # cheaper in tokens than the raw DOM snapshot for read/analyze tasks.
    # Off by default (opt-in, like the other post-MVP tools). When on, uses
    # crawl4ai's HTML->Markdown conversion if the optional package is
    # installed (requirements-tools.txt), else the built-in lightweight
    # heuristic cleaner - never launches a second browser.
    enable_markdown_extraction: bool = Field(
        default=False,
        alias="ENABLE_MARKDOWN_EXTRACTION",
        description="Enable the extract_page_content tool (cleaned "
        "Markdown/text of the current page). Uses crawl4ai if installed "
        "(requirements-tools.txt), otherwise a built-in heuristic cleaner.",
    )

    # ===== API service mode: access control (hardening) =====
    # The agent drives a real browser and runs arbitrary text tasks; an
    # API bound to a public interface without auth would let anyone submit
    # tasks as the server. Defaults are the SAFE ones: localhost-only
    # binding, no token (backwards compatible with already-deployed
    # installations). External exposure = two explicit opt-ins.
    api_bind_host: str = Field(
        default="127.0.0.1",
        alias="API_BIND_HOST",
        description="Network interface the API/UI server binds to. Default "
        "127.0.0.1 = localhost only; set 0.0.0.0 (explicit opt-in) only "
        "behind a firewall/reverse proxy or on a trusted network.",
    )

    @field_validator("api_bind_host")
    @classmethod
    def validate_api_bind_host(cls, v: str) -> str:
        v = (v or "").strip()
        if not v or any(ch.isspace() for ch in v):
            raise ValueError(f"API_BIND_HOST must be a host/IP without spaces, got: '{v}'")
        return v

    api_auth_token: str | None = Field(
        default=None,
        alias="API_AUTH_TOKEN",
        description="Optional bearer token. When set, every /task* "
        "endpoint (plus /config, /reports and the WebSocket channel) "
        "requires 'Authorization: Bearer <token>'; /health stays open for "
        "container/orchestrator liveness probes. Default None = auth off "
        "(backwards compatible). Use a long random string.",
        min_length=16,
    )

    # Fix (0.0.0.0 bind without mandatory auth): the API refuses to start
    # when it would bind to all interfaces with API_AUTH_TOKEN unset (see
    # src/api/app.py::_enforce_public_bind_auth_policy). This flag is the
    # operator's explicit acknowledgement of that risk - e.g. an isolated
    # trusted network or an authenticating reverse proxy in front.
    allow_unauthenticated_public_bind: bool = Field(
        default=False,
        alias="ALLOW_UNAUTHENTICATED_PUBLIC_BIND",
        description="Explicitly allow binding to all interfaces WITHOUT "
        "API_AUTH_TOKEN. DANGEROUS: anyone who can reach the port controls "
        "the agent's browser, including its persistent cookies. The API "
        "otherwise refuses to start in that configuration.",
    )

    # ===== Task persistence (SQLite, API service mode) =====
    # FIX (Task 1 - persistence): the API's task history (state, result,
    # step events) used to live only in an in-memory dict - a container
    # restart or redeploy wiped it. The durable copy now lives in a
    # single-file SQLite database written through on every mutation. Path
    # is explicit so Docker can point it at a mounted volume.
    task_db_path: Path = Field(
        default=Path("./data/tasks.db"),
        alias="TASK_DB_PATH",
        description="SQLite file for persistent API task history. Default "
        "keeps it in the project working directory; in Docker set this to "
        "a path under a mounted volume so history survives container "
        "recreation.",
    )

    # TTL / cap for finished-task records. Previously hardcoded constants
    # in src/api/app.py (TASK_TTL_HOURS=24 / MAX_FINISHED_TASKS=200); now
    # real settings so the operator can tune retention without code edits.
    # Each record carries its full steps buffer, so retention bounds both
    # memory and DB size.
    task_ttl_hours: float = Field(
        default=24.0,
        ge=0.0,
        alias="TASK_TTL_HOURS",
        description="Finished tasks older than this many hours are pruned "
        "(from memory and SQLite) on every submit and by a background "
        "sweep. 0 disables the age-based prune.",
    )

    max_finished_tasks: int = Field(
        default=200,
        ge=1,
        alias="MAX_FINISHED_TASKS",
        description="Keep at most this many newest finished tasks after "
        "each pruning sweep.",
    )

    task_prune_interval_seconds: float = Field(
        default=600.0,
        ge=10.0,
        alias="TASK_PRUNE_INTERVAL_SECONDS",
        description="How often the background pruner sweeps finished tasks "
        "(an idle API receives no submits, so submit-time pruning alone "
        "would never fire).",
    )

    # ===== Task intake policy (sanitization, API service mode) =====
    # Basic sanity/abuse checks applied BEFORE a submission enters the
    # execution queue. The always-on part is pure input hygiene (length,
    # emptiness, control chars); the content filter is a separate opt-in.
    task_max_length: int = Field(
        default=10000,
        ge=1,
        alias="TASK_MAX_LENGTH",
        description="Maximum accepted length of the 'task' text. 0 is not "
        "allowed - use a very large value instead of disabling: an "
        "unbounded task text is a memory/token-burn abuse vector.",
    )

    enable_task_content_filter: bool = Field(
        default=False,
        alias="ENABLE_TASK_CONTENT_FILTER",
        description="Opt-in regex blocklist over the submitted task text. "
        "OFF by default so single-operator behavior is unchanged. This is "
        "basic abuse protection, NOT moderation (see README.md).",
    )

    task_forbidden_patterns: str = Field(
        default="",
        alias="TASK_FORBIDDEN_PATTERNS",
        description="Newline-separated case-insensitive regular expressions; "
        "a match rejects the submission with rule=forbidden_pattern. Only "
        "used when ENABLE_TASK_CONTENT_FILTER=true. Invalid regexes are "
        "skipped with a warning, never crash intake.",
    )

    task_audit_log_path: Path = Field(
        default=Path("./logs/rejected_tasks.log"),
        alias="TASK_AUDIT_LOG_PATH",
        description="Dedicated JSONL audit trail of policy rejections "
        "(ts/rule/tenant_id/preview), separate from agent.log on purpose.",
    )

    # ===== Multi-tenancy (API service mode) =====
    # Each tenant gets an isolated persistent browser profile under
    # {USER_DATA_DIR}/tenants/{tenant_id}/ - cookies/localStorage never
    # cross tenants. Default 1 keeps the historical single-context,
    # strictly-sequential behavior; raise it only when you actually serve
    # several tenants AND can afford one Chromium process per open context
    # (roughly 100-300MB RAM each).
    max_concurrent_tenant_contexts: int = Field(
        default=1,
        ge=1,
        le=16,
        alias="MAX_CONCURRENT_TENANT_CONTEXTS",
        description="Maximum simultaneously OPEN persistent browser "
        "contexts (= max tasks running in parallel, one per tenant). "
        "Tasks beyond the limit are QUEUED, never rejected because of "
        "this setting. Default 1 = legacy single-browser behavior.",
    )

    tenant_context_idle_ttl_seconds: float = Field(
        default=600.0,
        ge=30.0,
        alias="TENANT_CONTEXT_IDLE_TTL_SECONDS",
        description="Close a tenant's persistent browser context after "
        "this many seconds without a task (frees the Chromium process; "
        "the profile directory survives, so cookies/sessions do too).",
    )

    tenant_context_sweep_interval_seconds: float = Field(
        default=60.0,
        ge=10.0,
        alias="TENANT_CONTEXT_SWEEP_INTERVAL_SECONDS",
        description="How often the background sweeper checks for idle "
        "tenant contexts to close.",
    )

    # ===== Rate limiting / usage accounting (API service mode) =====
    rate_limit_concurrent_per_tenant: int = Field(
        default=2,
        ge=0,
        alias="RATE_LIMIT_CONCURRENT_PER_TENANT",
        description="Max simultaneously RUNNING tasks per tenant; a submit "
        "beyond it is rejected with HTTP 429 (concurrent_limit). 0 disables "
        "this check.",
    )

    rate_limit_tasks_per_hour: int = Field(
        default=60,
        ge=0,
        alias="RATE_LIMIT_TASKS_PER_HOUR",
        description="Sliding-window cap on ACCEPTED submissions per tenant "
        "(default window: 1 hour); beyond it - HTTP 429 with Retry-After. "
        "0 disables the window check.",
    )

    tenant_token_budget: int = Field(
        default=0,
        ge=0,
        alias="TENANT_TOKEN_BUDGET",
        description="OPTIONAL hard quota: once a tenant's cumulative LLM "
        "tokens reach this, new submissions are refused (quota_exceeded). "
        "0 = disabled (default). Process-lifetime counter, not calendar-"
        "monthly - see SELF_REVIEW.md.",
    )

    token_cost_per_1k_usd: float = Field(
        default=0.0,
        ge=0.0,
        alias="TOKEN_COST_PER_1K_USD",
        description="Price per 1000 tokens used ONLY for the estimated_cost "
        "figure in GET /usage/{tenant_id} - informational, never enforced.",
    )

    # ===== Observability =====
    sentry_dsn: str = Field(
        default="",
        alias="SENTRY_DSN",
        description="Optional Sentry DSN for unhandled-exception tracking. "
        "Empty (default) = integration completely inactive. Requires the "
        "sentry-sdk package; without it a set DSN is logged and skipped.",
    )



    # ===== Debugging =====
    debug_mode: bool = Field(
        default=False,
        alias="DEBUG_MODE",
        description="Enable debug logging and screenshots on error",
    )

    screenshot_dir: Path = Field(
        default=Path("./screenshots"),
        alias="SCREENSHOT_DIR",
        description="Directory for error screenshots",
    )

    # FIX (3.3, captcha handling - human-in-the-loop scope): where captcha
    # checkpoints (task, conversation state, context_data, url, step) are
    # persisted while waiting for a human to solve the captcha manually.
    # Lets Ctrl+C / process restart during a captcha wait recover instead
    # of silently losing all progress (see AgentOrchestrator._handle_captcha).
    checkpoint_dir: Path = Field(
        default=Path("./checkpoints"),
        alias="CHECKPOINT_DIR",
        description="Directory for captcha human-in-the-loop checkpoints",
    )

    # Liveness heartbeat for docker-healthcheck.py in CLI (batch) mode.
    # The orchestrator touches this file once per reasoning step; the
    # healthcheck flags the container unhealthy when the file exists but
    # has not been updated for HEARTBEAT_STALE_SECONDS - catching a hung
    # browser/step loop that a "process alive" check can never see.
    # (API mode probes the real /health endpoint instead.)
    heartbeat_file: Path = Field(
        default=Path("./logs/heartbeat"),
        alias="HEARTBEAT_FILE",
        description="File touched once per agent step; docker-healthcheck.py "
        "uses its mtime as the CLI-mode liveness signal.",
    )

    heartbeat_stale_seconds: float = Field(
        default=600.0,
        ge=30.0,
        alias="HEARTBEAT_STALE_SECONDS",
        description="A CLI-mode heartbeat older than this many seconds "
        "makes docker-healthcheck.py report the container unhealthy. Sized "
        "to comfortably exceed one full step (rate-limit pause + LLM HTTP "
        "timeout + browser action).",
    )

    @model_validator(mode="after")
    def create_directories(self) -> "Settings":
        """
        Post-validation directory setup.

        Uses Pydantic v2's model_validator instead of __post_init__.
        Creates required directories if they don't exist.
        """
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.upload_allowed_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.download_allowed_dir.mkdir(parents=True, exist_ok=True)
        self.task_db_path.parent.mkdir(parents=True, exist_ok=True)
        # Task intake policy: the audit trail's directory must exist before
        # the first rejection tries to write there.
        self.task_audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        return self


def load_settings() -> Settings:
    """
    Load and validate settings from environment.

    Why a factory function?
    - Single source of truth for settings instantiation
    - Easier to mock in tests
    - Clear error messages at application startup

    Returns:
        Validated Settings instance

    Raises:
        ValidationError: If required settings are missing or invalid
    """
    return Settings()
