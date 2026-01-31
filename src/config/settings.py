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

from typing import Optional
from pathlib import Path
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from urllib.parse import urlparse
import warnings


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
        extra="ignore"  # Ignore unknown env vars
    )
    
    # ===== API Configuration =====
    api_key: str = Field(
        ...,  # Required field
        alias="OPENAI_API_KEY",
        description="OpenRouter API key (from https://openrouter.ai/keys)"
    )
    
    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: str) -> str:
        """Validate API key is not placeholder."""
        placeholders = [
            "your_api_key_here",
            "your_openrouter_api_key_here",
            "sk-your-key-here",
            "ollama",
            "test",
            "none",
            ""
        ]
        
        if v.lower() in placeholders or len(v) < 10:
            raise ValueError(
                "Invalid API key detected.\n"
                "Please set OPENAI_API_KEY in .env file.\n"
                "Get your key from: https://openrouter.ai/keys"
            )
        
        return v
    
    api_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        alias="API_BASE_URL",
        description="LLM API base URL (OpenRouter/OpenAI compatible)"
    )
    
    @field_validator("api_base_url")
    @classmethod
    def validate_api_url(cls, v: str) -> str:
        """Validate API URL to prevent Ollama misconfigurations."""
        # Check for Ollama patterns
        ollama_patterns = [
            "localhost:11434",
            "127.0.0.1:11434",
            "0.0.0.0:11434"
        ]
        
        for pattern in ollama_patterns:
            if pattern in v.lower():
                raise ValueError(
                    f"Ollama localhost URL detected: {v}\n"
                    "This codebase uses OpenRouter, not Ollama.\n"
                    "Set API_BASE_URL=https://openrouter.ai/api/v1"
                )
        
        # Enforce HTTPS (except localhost for dev)
        if not v.startswith("https://") and "localhost" not in v and "127.0.0.1" not in v:
            raise ValueError(f"API_BASE_URL must use HTTPS. Got: {v}")
        
        # Validate URL format
        parsed = urlparse(v)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid API_BASE_URL format: {v}")
        
        return v
    
    model_name: str = Field(
        default="upstage/solar-pro",
        alias="MODEL_NAME",
        description="Model to use (OpenRouter format: provider/model:version)"
    )
    
    @field_validator("model_name")
    @classmethod
    def validate_model_name(cls, v: str) -> str:
        """Validate model name format."""
        # Check for Ollama models
        ollama_patterns = ["llama3:", "mistral:", "codellama:", "phi3:"]
        for pattern in ollama_patterns:
            if v.lower().startswith(pattern):
                raise ValueError(
                    f"Ollama model name detected: {v}\n"
                    "Use OpenRouter format: provider/model:version\n"
                    "Example: upstage/solar-pro"
                )
        
        # Validate format (provider/model) unless legacy OpenAI
        if "/" not in v and v not in ["gpt-4", "gpt-3.5-turbo", "gpt-4o-mini"]:
            raise ValueError(
                f"Invalid model format: {v}\n"
                "Use: provider/model:version"
            )
        
        return v
    
    # ===== Network Configuration =====
    proxy_url: Optional[str] = Field(
        default=None,
        alias="PROXY_URL",
        description="HTTP proxy URL for network requests"
    )
    
    http_timeout: float = Field(
        default=120.0,
        alias="HTTP_TIMEOUT",
        description="HTTP request timeout in seconds"
    )
    
    @field_validator("http_timeout")
    @classmethod
    def validate_timeout(cls, v: float) -> float:
        """Validate timeout is reasonable."""
        if v > 300:
            warnings.warn(
                f"HTTP_TIMEOUT is very high: {v}s\n"
                "Recommended for cloud APIs: 60-120 seconds"
            )
        
        if v < 10:
            raise ValueError("HTTP_TIMEOUT too low (min 10s)")
        
        return v
    
    # ===== Browser Configuration =====
    user_data_dir: Path = Field(
        default=Path("./browser_data"),
        alias="USER_DATA_DIR",
        description="Directory for browser session persistence"
    )
    
    headless: bool = Field(
        default=False,
        alias="HEADLESS",
        description="Run browser in headless mode"
    )
    
    slow_mo: int = Field(
        default=50,
        ge=0,
        le=1000,
        alias="SLOW_MO",
        description="Milliseconds delay between actions (anti-fingerprint)"
    )
    
    page_load_timeout: int = Field(
        default=60000,
        ge=5000,
        alias="PAGE_LOAD_TIMEOUT",
        description="Page load timeout in milliseconds"
    )
    
    action_timeout: int = Field(
        default=20000,
        ge=1000,
        alias="ACTION_TIMEOUT",
        description="Individual action timeout in milliseconds"
    )
    
    # ===== Agent Configuration =====
    max_steps: int = Field(
        default=50,
        ge=1,
        le=200,
        alias="MAX_STEPS",
        description="Maximum reasoning-action steps before giving up"
    )
    
    max_retry_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        alias="MAX_RETRY_ATTEMPTS",
        description="Retry attempts for failed actions"
    )
    agent_step_delay: float = Field(
    default=1.0,
    ge=0.0,
    le=10.0,
    alias="AGENT_STEP_DELAY",
    description="Seconds to wait between agent steps to avoid overload"
)
    # ===== LLM Configuration =====
    max_tokens: int = Field(
        default=2000,
        ge=100,
        alias="MAX_TOKENS",
        description="Maximum tokens in LLM response"
    )
    
    temperature: float = Field(
        default=0.1,
        ge=0.0,
        le=2.0,
        alias="TEMPERATURE",
        description="LLM temperature (lower = more deterministic)"
    )
    
    # ===== DOM Processing =====
    text_block_max_length: int = Field(
        default=200,
        ge=50,
        alias="TEXT_BLOCK_MAX_LENGTH",
        description="Maximum characters per text block in DOM"
    )
    
    dom_max_tokens_estimate: int = Field(
        default=10000,
        ge=1000,
        alias="DOM_MAX_TOKENS_ESTIMATE",
        description="Maximum estimated tokens for DOM representation"
    )
    
    # ===== Loop Detection =====
    loop_detection_window: int = Field(
        default=3,
        ge=2,
        le=10,
        alias="LOOP_DETECTION_WINDOW",
        description="Number of states to check for loops"
    )
    
    max_identical_states: int = Field(
        default=5,
        ge=2,
        alias="MAX_IDENTICAL_STATES",
        description="Maximum identical states before intervention"
    )
    
    # ===== Stealth Configuration =====
    enable_stealth: bool = Field(
        default=True,
        alias="ENABLE_STEALTH",
        description="Enable playwright-stealth mode"
    )
    
    typing_speed_min: int = Field(
        default=50,
        ge=10,
        alias="TYPING_SPEED_MIN",
        description="Minimum ms delay between keystrokes"
    )
    
    typing_speed_max: int = Field(
        default=150,
        ge=50,
        alias="TYPING_SPEED_MAX",
        description="Maximum ms delay between keystrokes"
    )
    
    # ===== Debugging =====
    debug_mode: bool = Field(
        default=False,
        alias="DEBUG_MODE",
        description="Enable debug logging and screenshots on error"
    )
    
    screenshot_dir: Path = Field(
        default=Path("./screenshots"),
        alias="SCREENSHOT_DIR",
        description="Directory for error screenshots"
    )
    
    @model_validator(mode='after')
    def create_directories(self) -> 'Settings':
        """
        Post-validation directory setup.
        
        Uses Pydantic v2's model_validator instead of __post_init__.
        Creates required directories if they don't exist.
        """
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
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