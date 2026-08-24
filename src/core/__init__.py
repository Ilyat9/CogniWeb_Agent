"""Core domain models and exceptions."""

from .exceptions import (
    ActionError,
    AgentBaseException,
    AgentCriticalError,
    AgentTimeoutError,
    BrowserError,
    CaptchaDetectedError,
    ConfigurationError,
    LLMError,
    LoopDetectedError,
    NetworkError,
    SelectorError,
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
    "AgentTimeoutError",
    # Models
    "AgentAction",
    "ActionResult",
    "ObservationState",
    "AgentState",
    "ConversationMessage",
    "TaskResult",
]
