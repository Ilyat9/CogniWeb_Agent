"""Core domain models and exceptions."""

from .exceptions import (
    ActionError,
    AgentBaseException,
    AgentCriticalError,
    BrowserError,
    CaptchaDetectedError,
    ConfigurationError,
    LLMError,
    LoopDetectedError,
    NetworkError,
    SelectorError,
    TimeoutError,
    ValidationError,
)
from .models import (
    ActionResult,
    AgentAction,
    AgentState,
    ConversationMessage,
    ObservationState,
    TaskResult,
)

__all__ = [
    # Exceptions
    "AgentBaseException",
    "ConfigurationError",
    "NetworkError",
    "BrowserError",
    "SelectorError",
    "LoopDetectedError",
    "LLMError",
    "ActionError",
    "ValidationError",
    "CaptchaDetectedError",
    "AgentCriticalError",
    "TimeoutError",
    # Models
    "AgentAction",
    "ActionResult",
    "ObservationState",
    "AgentState",
    "ConversationMessage",
    "TaskResult",
]
