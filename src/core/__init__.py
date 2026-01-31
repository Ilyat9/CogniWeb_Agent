"""Core domain models and exceptions."""

from .exceptions import (
    AgentBaseException,
    ConfigurationError,
    NetworkError,
    BrowserError,
    SelectorError,
    LoopDetectedError,
    LLMError,
    ActionError,
    ValidationError,
    CaptchaDetectedError,
    AgentCriticalError,
    TimeoutError,
)
from .models import (
    AgentAction,
    ActionResult,
    ObservationState,
    AgentState,
    ConversationMessage,
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
