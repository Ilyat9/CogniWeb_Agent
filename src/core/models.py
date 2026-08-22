"""
Pydantic models for structured agent data.

These models enforce type safety and validation throughout the agent lifecycle.
Using Pydantic instead of plain dicts provides:
1. Automatic validation of LLM outputs
2. Clear documentation of expected data structures
3. IDE autocomplete and type checking
4. Serialization/deserialization without manual JSON handling

Why Pydantic Models?
- LLM outputs are unreliable - validation catches errors immediately
- Converting from dict to model raises ValidationError with clear messages
- Models serve as living documentation of the protocol
- Easy to extend with computed properties and validators
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

# ===== Agent Actions =====


class AgentAction(BaseModel):
    """
    Represents a single action the agent wants to take.

    Why strict typing?
    - LLM must follow exact schema or validation fails fast
    - Prevents downstream errors from malformed actions
    - Self-documenting: developers know exactly what fields exist

    Example JSON from LLM:
    {
        "thought": "I need to click the login button",
        "tool": "click_element",
        "args": {"element_id": 42}
    }
    """

    thought: str = Field(
        default="Thinking...",
        description="Agent's reasoning for this action (required for traceability)",
        min_length=0,
    )

    tool: Literal[
        "navigate",
        "click_element",
        "type_text",
        "upload_file",
        "select_option",
        "scroll_page",
        "take_screenshot",
        "wait",
        "go_back",
        "go_forward",
        "query_dom",
        "store_context",
        "wait_for_element",
        "hover_element",
        "press_key",
        "extract_page_content",
        "extract_structured_data",
        "list_tabs",
        "switch_tab",
        "download_file",
        "find_element_by_text",
        "assert_page_state",
        "set_variable",
        "get_variable",
        "done",
    ] = Field(default="wait", description="Tool name to execute")

    args: dict[str, Any] = Field(
        default_factory=dict, description="Tool arguments as key-value pairs"
    )

    @field_validator("tool")
    @classmethod
    def validate_tool(cls, v: str) -> str:
        """
        Validate tool name is in allowed list.

        Why validator?
        - Provides clear error message if LLM uses unknown tool
        - Centralized validation logic
        - Can be extended to check args match tool signature
        """
        valid_tools = [
            "navigate",
            "click_element",
            "type_text",
            "upload_file",
            "select_option",
            "scroll_page",
            "take_screenshot",
            "wait",
            "go_back",
            "go_forward",
            "query_dom",
            "store_context",
            "wait_for_element",
            "hover_element",
            "press_key",
            "extract_page_content",
            "extract_structured_data",
            "list_tabs",
            "switch_tab",
            "download_file",
            "find_element_by_text",
            "assert_page_state",
            "set_variable",
            "get_variable",
            "done",
        ]
        if v not in valid_tools:
            raise ValueError(f"Invalid tool '{v}'. Valid tools: {valid_tools}")
        return v

    @field_validator("args")
    @classmethod
    def validate_args(cls, v: dict[str, Any], info) -> dict[str, Any]:
        """
        Validate arguments match tool signature.

        Why runtime validation?
        - LLM might pass wrong arg names or types
        - Catches errors before browser execution
        - Provides actionable error messages for debugging
        """
        # Get tool name from context
        tool = info.data.get("tool")

        # Maximum length for any single string value stored via store_context.
        # Prevents unbounded blobs (e.g. accidental page-dump exfiltration)
        # from ending up in TaskResult.context_data.
        MAX_STORE_VALUE_LENGTH = 2000

        # Tool-specific validation.
        # NOTE: type checks below are intentionally permissive (they only
        # verify the value is JSON-scalar-compatible with what the handler
        # expects), because the *final* authority on type coercion lives in
        # BrowserService (defense-in-depth, see browser.py). This validator's
        # job is to catch the common hallucination cases (wrong type key
        # entirely, e.g. an int where a string is required) before an
        # unhandled exception can reach the orchestrator loop.
        if tool == "click_element":
            if "element_id" not in v:
                raise ValueError("click_element requires 'element_id' in args")

        elif tool == "type_text":
            if "element_id" not in v or "text" not in v:
                raise ValueError("type_text requires 'element_id' and 'text' in args")
            if not isinstance(v["text"], str):
                raise ValueError(
                    f"type_text 'text' must be a string, got {type(v['text']).__name__}"
                )

        elif tool == "navigate":
            if "url" not in v:
                raise ValueError("navigate requires 'url' in args")
            if not isinstance(v["url"], str):
                raise ValueError(f"navigate 'url' must be a string, got {type(v['url']).__name__}")

        elif tool == "select_option":
            if "element_id" not in v or "value" not in v:
                raise ValueError("select_option requires 'element_id' and 'value' in args")
            if not isinstance(v["value"], str):
                raise ValueError(
                    f"select_option 'value' must be a string, got {type(v['value']).__name__}"
                )

        elif tool == "upload_file":
            if "element_id" not in v or "file_path" not in v:
                raise ValueError("upload_file requires 'element_id' and 'file_path' in args")
            if not isinstance(v["file_path"], str):
                raise ValueError(
                    f"upload_file 'file_path' must be a string, got {type(v['file_path']).__name__}"
                )
            # Reject obvious path traversal / absolute escape attempts at the
            # schema layer. BrowserService.upload_file() re-validates against
            # the actual allowed-uploads directory (defense-in-depth).
            if ".." in v["file_path"]:
                raise ValueError("upload_file 'file_path' must not contain '..'")

        elif tool == "scroll_page":
            if "direction" in v and v["direction"] not in ["up", "down"]:
                raise ValueError("scroll_page direction must be 'up' or 'down'")

        elif tool == "wait_for_element":
            if "element_id" not in v and "selector" not in v:
                raise ValueError("wait_for_element requires 'element_id' or 'selector'")
            if "state" in v and v["state"] not in ["attached", "visible", "hidden", "detached"]:
                raise ValueError(
                    "wait_for_element state must be one of " "attached/visible/hidden/detached"
                )
            if "timeout_ms" in v and not isinstance(v["timeout_ms"], (int, float)):
                raise ValueError("wait_for_element 'timeout_ms' must be numeric")

        elif tool == "hover_element":
            if "element_id" not in v:
                raise ValueError("hover_element requires 'element_id' in args")

        elif tool == "press_key":
            if "key" not in v or not isinstance(v["key"], str) or not v["key"].strip():
                raise ValueError("press_key requires a non-empty string 'key'")
            if len(v["key"]) > 30:
                raise ValueError("press_key 'key' is too long (max 30 chars)")

        elif tool == "extract_structured_data":
            if "key" not in v or not isinstance(v["key"], str) or not v["key"].strip():
                raise ValueError("extract_structured_data requires a non-empty string 'key'")

        elif tool == "switch_tab":
            if "index" not in v or not isinstance(v["index"], int) or isinstance(v["index"], bool):
                raise ValueError("switch_tab requires an integer 'index'")

        elif tool == "download_file":
            if "element_id" not in v:
                raise ValueError("download_file requires 'element_id' in args")
            if "timeout_ms" in v and not isinstance(v["timeout_ms"], (int, float)):
                raise ValueError("download_file 'timeout_ms' must be numeric")

        elif tool == "find_element_by_text":
            if "text" not in v or not isinstance(v["text"], str) or not v["text"].strip():
                raise ValueError("find_element_by_text requires a non-empty string 'text'")

        elif tool == "assert_page_state":
            # exactly one expectation per call
            expects = [k for k in ("expect_text_present", "expect_url_contains") if k in v]
            expects += ["expect_element_visible"] if "expect_element_visible" in v else []
            if len(expects) != 1:
                raise ValueError(
                    "assert_page_state requires exactly one of 'expect_text_present', "
                    "'expect_url_contains', 'expect_element_visible'"
                )
            if "expect_text_present" in v and not isinstance(v["expect_text_present"], str):
                raise ValueError("assert_page_state 'expect_text_present' must be a string")
            if "expect_url_contains" in v and not isinstance(v["expect_url_contains"], str):
                raise ValueError("assert_page_state 'expect_url_contains' must be a string")

        elif tool == "set_variable":
            if "name" not in v or not isinstance(v["name"], str) or not v["name"].strip():
                raise ValueError("set_variable requires a non-empty string 'name'")
            if "value" not in v:
                raise ValueError("set_variable requires 'value' in args")

        elif tool == "get_variable":
            if "name" not in v or not isinstance(v["name"], str) or not v["name"].strip():
                raise ValueError("get_variable requires a non-empty string 'name'")

        elif tool == "store_context":
            reserved_fields = {"tool", "thought", "reasoning"}
            for key, value in v.items():
                if key in reserved_fields:
                    continue
                if isinstance(value, str) and len(value) > MAX_STORE_VALUE_LENGTH:
                    raise ValueError(
                        f"store_context value for '{key}' exceeds max length "
                        f"({len(value)} > {MAX_STORE_VALUE_LENGTH} chars). "
                        "Store a shorter summary instead of raw page content."
                    )

        return v

    model_config = ConfigDict(
        # FIX (4.2): reject unexpected top-level fields (e.g. "tool_name"
        # typos from the LLM) instead of silently dropping them.
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "thought": "The user wants to search, so I'll type in the search box",
                    "tool": "type_text",
                    "args": {"element_id": 5, "text": "Python tutorial", "press_enter": True},
                },
                {
                    "thought": "Task is complete, all jobs have been saved",
                    "tool": "done",
                    "args": {"summary": "Successfully found and saved 5 job listings"},
                },
            ]
        },
    )


class ActionResult(BaseModel):
    """
    Result of executing an action.

    Why structured results?
    - Consistent interface for all tools
    - Easy to check success without parsing strings
    - Can attach rich metadata (timing, screenshots, etc.)
    """

    success: bool = Field(..., description="Whether action succeeded")

    message: str = Field(..., description="Human-readable result message")

    data: dict[str, Any] | None = Field(
        default=None, description="Additional result data (e.g., DOM snapshot, screenshot path)"
    )

    error: str | None = Field(default=None, description="Error message if success=False")

    warning: str | None = Field(
        default=None,
        description="Non-fatal warning (e.g. '.first' fallback used on a non-unique selector)",
    )

    execution_time_ms: int | None = Field(
        default=None, description="Action execution time in milliseconds"
    )


# ===== Agent State =====


class ObservationState(BaseModel):
    """
    Current page state observation.

    Why separate model?
    - Observations are appended to conversation history
    - Structured observations enable better compression/truncation
    - Can implement intelligent caching based on state hash
    """

    url: str = Field(..., description="Current page URL")

    title: str = Field(default="", description="Page title")

    dom_elements: list[dict[str, Any]] = Field(
        default_factory=list, description="Simplified DOM representation with element IDs"
    )

    viewport_size: dict[str, int] | None = Field(
        default=None, description="Viewport dimensions {width, height}"
    )

    screenshot_path: str | None = Field(default=None, description="Path to screenshot if taken")

    @computed_field
    @property
    def state_hash(self) -> str:
        """
        Compute hash of observation for loop detection.

        Why computed field?
        - Automatically available on every observation
        - Used for detecting identical states in loop protection
        - Excludes timestamp to focus on actual page state
        """
        import hashlib
        import json

        # Hash based on URL + DOM structure
        state_data = {"url": self.url, "title": self.title, "dom_count": len(self.dom_elements)}
        state_str = json.dumps(state_data, sort_keys=True)
        return hashlib.md5(state_str.encode()).hexdigest()


class AgentState(BaseModel):
    """
    Complete agent state for a single step.

    Why track full state?
    - Enables step-by-step replay for debugging
    - Can checkpoint and resume from any point
    - Provides audit trail for compliance
    """

    step: int = Field(..., ge=0, description="Current step number")

    task: str = Field(..., description="Original task description")

    observation: ObservationState = Field(..., description="Current page observation")

    action: AgentAction | None = Field(default=None, description="Action taken this step")

    result: ActionResult | None = Field(default=None, description="Result of action execution")

    timestamp: datetime = Field(
        default_factory=datetime.now, description="When this state was recorded"
    )

    context_data: dict[str, Any] = Field(
        default_factory=dict, description="Stored context from previous actions"
    )


class ConversationMessage(BaseModel):
    """
    Single message in agent conversation history.

    Why model messages?
    - Ensures conversation format matches LLM API expectations
    - Easy to serialize for debugging or caching
    - Can implement message compression strategies
    """

    role: Literal["system", "user", "assistant"] = Field(..., description="Message role")

    content: str = Field(..., description="Message content")

    timestamp: datetime = Field(
        default_factory=datetime.now, description="When message was created"
    )

    tokens: int | None = Field(
        default=None, description="Estimated token count (for budget tracking)"
    )


class TaskResult(BaseModel):
    """
    Final result of task execution.

    Why structured?
    - Clear success/failure indication
    - Can attach execution metadata
    - Easy to serialize for logging or reporting
    """

    success: bool = Field(..., description="Whether task completed successfully")

    summary: str = Field(..., description="Summary of task execution")

    steps_taken: int = Field(..., ge=0, description="Number of reasoning steps executed")

    total_duration_seconds: float = Field(..., ge=0.0, description="Total execution time")

    final_url: str | None = Field(default=None, description="Final page URL")

    error: str | None = Field(default=None, description="Error message if failed")

    tokens_used: int | None = Field(
        default=None,
        description="Total LLM tokens (prompt + completion) consumed by the "
        "run, as reported by the provider's usage blocks. None when the "
        "provider/mock did not report usage.",
    )

    context_data: dict[str, Any] = Field(
        default_factory=dict, description="Final stored context data"
    )
