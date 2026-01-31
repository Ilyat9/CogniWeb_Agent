"""
Custom exception hierarchy for the browser agent.

This module defines a clear exception hierarchy that allows:
1. Granular error handling at different system levels
2. Defensive programming by catching specific exceptions
3. Rich error context for debugging (screenshots, HTML dumps)
4. Clean separation between retryable and fatal errors

Why custom exceptions?
- Better than generic Exception: provides semantic meaning
- Enables different recovery strategies per error type
- Allows middleware to intercept and handle errors appropriately
- Facilitates debugging with structured error data
"""

from typing import Optional, Dict, Any
from pathlib import Path


class AgentBaseException(Exception):
    """
    Base exception for all agent errors.
    
    Why a base class?
    - Allows catching ALL agent errors with: except AgentBaseException
    - Provides common interface for error context
    - Enables polymorphic error handling
    """
    
    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize base exception.
        
        Args:
            message: Human-readable error description
            error_code: Machine-readable error code for classification
            context: Additional context data for debugging
        """
        super().__init__(message)
        self.message = message
        self.error_code = error_code or self.__class__.__name__
        self.context = context or {}
    
    def __str__(self) -> str:
        """String representation with error code."""
        if self.context:
            return f"[{self.error_code}] {self.message} | Context: {self.context}"
        return f"[{self.error_code}] {self.message}"


class ConfigurationError(AgentBaseException):
    """
    Raised when configuration is invalid or missing.
    
    Examples:
    - Missing required environment variables
    - Invalid configuration values
    - Conflicting settings
    
    This is a FATAL error - the application should not start.
    """
    pass


class NetworkError(AgentBaseException):
    """
    Network-related errors (HTTP, proxy, timeouts).
    
    Why separate from other errors?
    - Network errors are often RETRYABLE with exponential backoff
    - Different handling strategy: wait and retry vs. immediate failure
    - Allows circuit breaker patterns for failing services
    
    Examples:
    - API request timeout
    - Proxy connection failure
    - DNS resolution error
    """
    
    def __init__(
        self,
        message: str,
        url: Optional[str] = None,
        status_code: Optional[int] = None,
        **kwargs
    ):
        """
        Initialize network error.
        
        Args:
            message: Error description
            url: URL that caused the error
            status_code: HTTP status code if applicable
        """
        context = kwargs.get("context", {})
        context.update({
            "url": url,
            "status_code": status_code
        })
        super().__init__(message, context=context, **kwargs)


class BrowserError(AgentBaseException):
    """
    Browser/Playwright-related errors.
    
    Examples:
    - Browser crash
    - Page navigation failure
    - Context creation failure
    - Browser not installed
    
    Recovery strategy: Create new browser instance
    """
    pass


class SelectorError(AgentBaseException):
    """
    Element selector not found or ambiguous.
    
    Why separate?
    - Very common error type in web automation
    - Often requires different recovery strategy (scroll, wait, retry)
    - Can benefit from retry with exponential backoff
    
    Examples:
    - Element not in DOM
    - Multiple elements match selector
    - Element exists but not visible/interactable
    """
    
    def __init__(
        self,
        message: str,
        selector: Optional[str] = None,
        **kwargs
    ):
        """
        Initialize selector error.
        
        Args:
            message: Error description
            selector: CSS/XPath selector that failed
        """
        context = kwargs.get("context", {})
        context.update({"selector": selector})
        super().__init__(message, context=context, **kwargs)


class LoopDetectedError(AgentBaseException):
    """
    Agent is stuck in an infinite loop.
    
    Why critical?
    - Wastes compute resources
    - Indicates fundamental strategy failure
    - Requires human intervention to break
    
    Recovery strategy: Prompt user for guidance or alternative strategy
    """
    
    def __init__(
        self,
        message: str,
        loop_count: int = 0,
        repeated_actions: Optional[list] = None,
        **kwargs
    ):
        """
        Initialize loop error.
        
        Args:
            message: Error description
            loop_count: Number of identical states detected
            repeated_actions: List of actions that repeated
        """
        context = kwargs.get("context", {})
        context.update({
            "loop_count": loop_count,
            "repeated_actions": repeated_actions or []
        })
        super().__init__(message, context=context, **kwargs)


class LLMError(AgentBaseException):
    """
    LLM API or parsing errors.
    
    Examples:
    - API rate limit exceeded
    - Invalid JSON in response
    - Model refused to respond
    - Token limit exceeded
    
    Recovery strategy: Retry with exponential backoff, fallback to simpler prompt
    """
    
    def __init__(
        self,
        message: str,
        model_name: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
        **kwargs
    ):
        """
        Initialize LLM error.
        
        Args:
            message: Error description
            model_name: Model that encountered the error
            prompt_tokens: Number of tokens in prompt
        """
        context = kwargs.get("context", {})
        context.update({
            "model_name": model_name,
            "prompt_tokens": prompt_tokens
        })
        super().__init__(message, context=context, **kwargs)


class ActionError(AgentBaseException):
    """
    Error executing browser action (click, type, etc.).
    
    Examples:
    - Element not interactable
    - Element detached from DOM
    - Action timeout
    - Invalid action parameters
    
    Recovery strategy: Wait for element, retry, or try alternative action
    """
    pass


class ValidationError(AgentBaseException):
    """
    Data validation error (Pydantic models, action schemas).
    
    Examples:
    - LLM returned invalid action structure
    - Missing required fields in action
    - Type mismatch in action parameters
    
    This is usually FATAL - indicates LLM is not following instructions
    """
    pass


class CaptchaDetectedError(AgentBaseException):
    """
    Captcha detected on page.
    
    Why separate?
    - Requires HUMAN intervention (cannot be automated ethically)
    - Different UX flow: pause and wait for user
    - Not a failure, but a state that requires manual handling
    
    Recovery strategy: Pause execution, notify user, wait for manual solve
    """
    
    def __init__(
        self,
        message: str,
        page_url: Optional[str] = None,
        **kwargs
    ):
        """
        Initialize captcha error.
        
        Args:
            message: Error description
            page_url: URL where captcha was detected
        """
        context = kwargs.get("context", {})
        context.update({"page_url": page_url})
        super().__init__(message, context=context, **kwargs)


class AgentCriticalError(AgentBaseException):
    """
    Critical error requiring immediate shutdown with diagnostics.
    
    When raised:
    - Automatically takes screenshot
    - Dumps current page HTML
    - Logs full error context
    - Initiates graceful shutdown
    
    Why special handling?
    - These errors indicate unrecoverable states
    - Diagnostics are essential for debugging production issues
    - Clean shutdown prevents zombie processes
    
    Examples:
    - Browser crashed and cannot restart
    - Filesystem errors (cannot write screenshots)
    - Out of memory
    """
    
    def __init__(
        self,
        message: str,
        screenshot_path: Optional[Path] = None,
        html_dump_path: Optional[Path] = None,
        **kwargs
    ):
        """
        Initialize critical error with diagnostic paths.
        
        Args:
            message: Error description
            screenshot_path: Path to error screenshot
            html_dump_path: Path to HTML dump
        """
        context = kwargs.get("context", {})
        context.update({
            "screenshot_path": str(screenshot_path) if screenshot_path else None,
            "html_dump_path": str(html_dump_path) if html_dump_path else None
        })
        super().__init__(message, context=context, **kwargs)


class TimeoutError(AgentBaseException):
    """
    Operation exceeded timeout.
    
    Examples:
    - Page load timeout
    - Element wait timeout
    - Action execution timeout
    
    Recovery strategy: Retry with longer timeout, or skip action
    """
    
    def __init__(
        self,
        message: str,
        timeout_seconds: Optional[float] = None,
        **kwargs
    ):
        """
        Initialize timeout error.
        
        Args:
            message: Error description
            timeout_seconds: Timeout that was exceeded
        """
        context = kwargs.get("context", {})
        context.update({"timeout_seconds": timeout_seconds})
        super().__init__(message, context=context, **kwargs)
