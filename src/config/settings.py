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

import warnings
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
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

    # ===== Stealth Configuration =====
    enable_stealth: bool = Field(
        default=True, alias="ENABLE_STEALTH", description="Enable playwright-stealth mode"
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

    # ===== Vision Fallback (Task 4) =====
    # FIX (Task 4): on heavy/poorly-structured pages, text-based DOM
    # extraction can come back empty (extraction failed) or so large and
    # text-sparse that the LLM has no real signal for what's relevant. As
    # a fallback ONLY for those cases, the agent can switch to sending an
    # annotated screenshot (numbered boxes over interactive elements,
    # reusing the same element_id used in text mode) to a vision-capable
    # model. See AgentOrchestrator._should_use_vision_fallback() /
    # _get_action_via_vision().
    enable_vision_fallback: bool = Field(
        default=True,
        alias="ENABLE_VISION_FALLBACK",
        description="Allow falling back to an annotated screenshot when text-based "
        "DOM extraction is empty, failed, or too noisy. Still gated by "
        "MODEL_SUPPORTS_VISION - stays a no-op text-only providers.",
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
