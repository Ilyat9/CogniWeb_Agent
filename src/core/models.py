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

from typing import Optional, Any, Dict, List, Literal
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, computed_field


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
        min_length=0
    )
    
    tool: Literal[
        "navigate",
        "click_element",
        "type_text",
        "upload_file",
        "scroll_page",
        "take_screenshot",
        "wait",
        "go_back",
        "query_dom",
        "store_context",
        "done"
    ] = Field(
        default="wait",
        description="Tool name to execute"
    )
    
    args: Dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments as key-value pairs"
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
            "navigate", "click_element", "type_text", "upload_file",
            "scroll_page", "take_screenshot", "wait", "go_back",
            "query_dom", "store_context", "done"
        ]
        if v not in valid_tools:
            raise ValueError(f"Invalid tool '{v}'. Valid tools: {valid_tools}")
        return v
    
    @field_validator("args")
    @classmethod
    def validate_args(cls, v: Dict[str, Any], info) -> Dict[str, Any]:
        """
        Validate arguments match tool signature.
        
        Why runtime validation?
        - LLM might pass wrong arg names or types
        - Catches errors before browser execution
        - Provides actionable error messages for debugging
        """
        # Get tool name from context
        tool = info.data.get("tool")
        
        # Tool-specific validation
        if tool == "click_element":
            if "element_id" not in v:
                raise ValueError("click_element requires 'element_id' in args")
        
        elif tool == "type_text":
            if "element_id" not in v or "text" not in v:
                raise ValueError("type_text requires 'element_id' and 'text' in args")
        
        elif tool == "navigate":
            if "url" not in v:
                raise ValueError("navigate requires 'url' in args")
        
        elif tool == "scroll_page":
            if "direction" in v and v["direction"] not in ["up", "down"]:
                raise ValueError("scroll_page direction must be 'up' or 'down'")
        
        return v
    
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "thought": "The user wants to search, so I'll type in the search box",
                    "tool": "type_text",
                    "args": {"element_id": 5, "text": "Python tutorial", "press_enter": True}
                },
                {
                    "thought": "Task is complete, all jobs have been saved",
                    "tool": "done",
                    "args": {"summary": "Successfully found and saved 5 job listings"}
                }
            ]
        }
    }


class ActionResult(BaseModel):
    """
    Result of executing an action.
    
    Why structured results?
    - Consistent interface for all tools
    - Easy to check success without parsing strings
    - Can attach rich metadata (timing, screenshots, etc.)
    """
    
    success: bool = Field(
        ...,
        description="Whether action succeeded"
    )
    
    message: str = Field(
        ...,
        description="Human-readable result message"
    )
    
    data: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional result data (e.g., DOM snapshot, screenshot path)"
    )
    
    error: Optional[str] = Field(
        default=None,
        description="Error message if success=False"
    )
    
    execution_time_ms: Optional[int] = Field(
        default=None,
        description="Action execution time in milliseconds"
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
    
    url: str = Field(
        ...,
        description="Current page URL"
    )
    
    title: str = Field(
        default="",
        description="Page title"
    )
    
    dom_elements: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Simplified DOM representation with element IDs"
    )
    
    viewport_size: Optional[Dict[str, int]] = Field(
        default=None,
        description="Viewport dimensions {width, height}"
    )
    
    screenshot_path: Optional[str] = Field(
        default=None,
        description="Path to screenshot if taken"
    )
    
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
        state_data = {
            "url": self.url,
            "title": self.title,
            "dom_count": len(self.dom_elements)
        }
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
    
    step: int = Field(
        ...,
        ge=0,
        description="Current step number"
    )
    
    task: str = Field(
        ...,
        description="Original task description"
    )
    
    observation: ObservationState = Field(
        ...,
        description="Current page observation"
    )
    
    action: Optional[AgentAction] = Field(
        default=None,
        description="Action taken this step"
    )
    
    result: Optional[ActionResult] = Field(
        default=None,
        description="Result of action execution"
    )
    
    timestamp: datetime = Field(
        default_factory=datetime.now,
        description="When this state was recorded"
    )
    
    context_data: Dict[str, Any] = Field(
        default_factory=dict,
        description="Stored context from previous actions"
    )


class ConversationMessage(BaseModel):
    """
    Single message in agent conversation history.
    
    Why model messages?
    - Ensures conversation format matches LLM API expectations
    - Easy to serialize for debugging or caching
    - Can implement message compression strategies
    """
    
    role: Literal["system", "user", "assistant"] = Field(
        ...,
        description="Message role"
    )
    
    content: str = Field(
        ...,
        description="Message content"
    )
    
    timestamp: datetime = Field(
        default_factory=datetime.now,
        description="When message was created"
    )
    
    tokens: Optional[int] = Field(
        default=None,
        description="Estimated token count (for budget tracking)"
    )


class TaskResult(BaseModel):
    """
    Final result of task execution.
    
    Why structured?
    - Clear success/failure indication
    - Can attach execution metadata
    - Easy to serialize for logging or reporting
    """
    
    success: bool = Field(
        ...,
        description="Whether task completed successfully"
    )
    
    summary: str = Field(
        ...,
        description="Summary of task execution"
    )
    
    steps_taken: int = Field(
        ...,
        ge=0,
        description="Number of reasoning steps executed"
    )
    
    total_duration_seconds: float = Field(
        ...,
        ge=0.0,
        description="Total execution time"
    )
    
    final_url: Optional[str] = Field(
        default=None,
        description="Final page URL"
    )
    
    error: Optional[str] = Field(
        default=None,
        description="Error message if failed"
    )
    
    context_data: Dict[str, Any] = Field(
        default_factory=dict,
        description="Final stored context data"
    )
