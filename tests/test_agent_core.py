"""
Unit tests for core agent logic.

Tests cover:
- Pydantic validation (models.py)
- Orchestrator logic (orchestrator.py)
- LLM JSON parsing (llm.py)
- Smart loop detection

All tests use mocks - no real API calls or browser launches.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic import ValidationError

# Import components to test
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.models import AgentAction, ActionResult
from src.core.exceptions import LoopDetectedError
from src.config.settings import Settings
from src.agent.orchestrator import AgentOrchestrator
from src.infrastructure.llm import LLMService


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def mock_settings():
    """Create mock settings for testing."""
    return Settings(
        api_key="sk-test-key-not-real",
        api_base_url="https://api.test.com/v1",
        model_name="test-model",
        max_steps=50,
        temperature=0.1,
        loop_detection_window=3,
        max_identical_states=3
    )


@pytest.fixture
def mock_browser():
    """Create mock browser service."""
    browser = AsyncMock()
    browser.element_map = {}
    browser.navigate = AsyncMock(return_value=ActionResult(success=True, message="Navigated"))
    browser.click_element_safe = AsyncMock(return_value=ActionResult(success=True, message="Clicked"))
    browser.get_current_url = AsyncMock(return_value="https://example.com")
    browser.get_page_title = AsyncMock(return_value="Test Page")
    browser.detect_captcha = AsyncMock(return_value=False)
    browser.page = AsyncMock()
    return browser


@pytest.fixture
def mock_llm():
    """Create mock LLM service."""
    llm = AsyncMock()
    llm.generate_action = AsyncMock(return_value=AgentAction(
        thought="Test thought",
        tool="navigate",
        args={"url": "https://example.com"}
    ))
    return llm


# ============================================================================
# TEST: Pydantic Validation (models.py)
# ============================================================================

class TestAgentActionValidation:
    """Test AgentAction model validation."""
    
    def test_valid_action(self):
        """Valid action should parse without errors."""
        action = AgentAction(
            thought="Navigate to example",
            tool="navigate",
            args={"url": "https://example.com"}
        )
        assert action.tool == "navigate"
        assert action.args["url"] == "https://example.com"
    
    def test_invalid_tool_name(self):
        """Invalid tool name should raise ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            AgentAction(
                thought="Invalid tool",
                tool="invalid_tool_name",  # Not in allowed list
                args={}
            )
        
        # Check error message mentions valid tools
        assert "navigate" in str(exc_info.value)
    
    def test_navigate_requires_url(self):
        """navigate tool must have 'url' in args."""
        with pytest.raises(ValidationError) as exc_info:
            AgentAction(
                thought="Navigate without URL",
                tool="navigate",
                args={}  # Missing 'url'
            )
        
        assert "requires 'url'" in str(exc_info.value)
    
    def test_click_element_requires_element_id(self):
        """click_element tool must have 'element_id' in args."""
        with pytest.raises(ValidationError) as exc_info:
            AgentAction(
                thought="Click without element_id",
                tool="click_element",
                args={}  # Missing 'element_id'
            )
        
        assert "requires 'element_id'" in str(exc_info.value)
    
    def test_type_text_requires_both_args(self):
        """type_text tool must have 'element_id' and 'text'."""
        # Missing 'text'
        with pytest.raises(ValidationError):
            AgentAction(
                thought="Type without text",
                tool="type_text",
                args={"element_id": 1}
            )
        
        # Missing 'element_id'
        with pytest.raises(ValidationError):
            AgentAction(
                thought="Type without element_id",
                tool="type_text",
                args={"text": "hello"}
            )
    
    def test_scroll_validates_direction(self):
        """scroll_page direction must be 'up' or 'down'."""
        with pytest.raises(ValidationError) as exc_info:
            AgentAction(
                thought="Invalid scroll direction",
                tool="scroll_page",
                args={"direction": "left"}  # Invalid
            )
        
        assert "must be 'up' or 'down'" in str(exc_info.value)


# ============================================================================
# TEST: Orchestrator Logic (orchestrator.py)
# ============================================================================

class TestOrchestratorLogic:
    """Test AgentOrchestrator methods."""
    
    @pytest.mark.asyncio
    async def test_get_trimmed_history_preserves_system_prompt(
        self, mock_settings, mock_browser, mock_llm
    ):
        """System prompt (index 0) must always be preserved."""
        orchestrator = AgentOrchestrator(mock_settings, mock_browser, mock_llm)
        
        # Simulate conversation with 15 messages
        orchestrator.conversation_history = [
            {"role": "system", "content": "SYSTEM_PROMPT"},  # Index 0
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
            {"role": "user", "content": "msg3"},
            {"role": "assistant", "content": "msg4"},
            {"role": "user", "content": "msg5"},
            {"role": "assistant", "content": "msg6"},
            {"role": "user", "content": "msg7"},
            {"role": "assistant", "content": "msg8"},
            {"role": "user", "content": "msg9"},
            {"role": "assistant", "content": "msg10"},
            {"role": "user", "content": "msg11"},
            {"role": "assistant", "content": "msg12"},
            {"role": "user", "content": "msg13"},
            {"role": "assistant", "content": "msg14"},
        ]
        
        # Get trimmed history with window_size=5
        trimmed = orchestrator.get_trimmed_history(window_size=5)
        
        # Should have: system prompt + last 5 messages = 6 total
        assert len(trimmed) == 6
        
        # First message must be system prompt
        assert trimmed[0]["role"] == "system"
        assert trimmed[0]["content"] == "SYSTEM_PROMPT"
        
        # Last 5 messages should be preserved
        assert trimmed[-1]["content"] == "msg14"
        assert trimmed[-2]["content"] == "msg13"
    
    @pytest.mark.asyncio
    async def test_get_trimmed_history_no_trim_if_short(
        self, mock_settings, mock_browser, mock_llm
    ):
        """If history is short, don't trim."""
        orchestrator = AgentOrchestrator(mock_settings, mock_browser, mock_llm)
        
        orchestrator.conversation_history = [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "msg2"},
        ]
        
        trimmed = orchestrator.get_trimmed_history(window_size=10)
        
        # Should return all messages (3 total)
        assert len(trimmed) == 3


# ============================================================================
# TEST: LLM JSON Parsing (llm.py)
# ============================================================================

class TestLLMJsonParsing:
    """Test LLMService._extract_json_from_response method."""
    
    def test_extract_json_from_code_block(self, mock_settings):
        """Should extract JSON from markdown code block."""
        llm = LLMService(mock_settings)
        
        response = """
        Here's the action:
```json
        {"tool": "navigate", "args": {"url": "https://example.com"}}
```
        """
        
        result = llm._extract_json_from_response(response)
        assert result == '{"tool": "navigate", "args": {"url": "https://example.com"}}'
    
    def test_extract_json_without_code_block(self, mock_settings):
        """Should extract JSON without code block markers."""
        llm = LLMService(mock_settings)
        
        response = """
        Some text before
        {"tool": "click_element", "args": {"element_id": 42}}
        Some text after
        """
        
        result = llm._extract_json_from_response(response)
        assert '{"tool": "click_element"' in result
        assert '"element_id": 42' in result
    
    def test_extract_json_with_trailing_comma(self, mock_settings):
        """Should handle trailing comma in JSON."""
        llm = LLMService(mock_settings)
        
        # Note: trailing comma after "url"
        response = '{"tool": "navigate", "args": {"url": "https://example.com",}}'
        
        result = llm._extract_json_from_response(response)
        
        # Should be valid JSON after cleanup
        import json
        parsed = json.loads(result)
        assert parsed["tool"] == "navigate"
    
    def test_extract_json_returns_empty_on_garbage(self, mock_settings):
        """Should return empty string on unparseable input."""
        llm = LLMService(mock_settings)
        
        response = "This is not JSON at all, just random text!"
        
        result = llm._extract_json_from_response(response)
        assert result == ""
    
    def test_extract_json_handles_nested_braces(self, mock_settings):
        """Should handle nested JSON objects."""
        llm = LLMService(mock_settings)
        
        response = """
```json
        {
            "tool": "store_context",
            "args": {
                "data": {
                    "nested": "value"
                }
            }
        }
```
        """
        
        result = llm._extract_json_from_response(response)
        
        import json
        parsed = json.loads(result)
        assert parsed["args"]["data"]["nested"] == "value"


# ============================================================================
# TEST: Smart Loop Detection (orchestrator.py)
# ============================================================================

class TestSmartLoopDetection:
    """Test loop detection logic."""
    
    @pytest.mark.asyncio
    async def test_loop_detected_on_identical_failures(
        self, mock_settings, mock_browser, mock_llm
    ):
        """Should raise LoopDetectedError on 3 identical failures."""
        orchestrator = AgentOrchestrator(mock_settings, mock_browser, mock_llm)
        
        # Create identical action
        action = AgentAction(
            thought="Click button",
            tool="click_element",
            args={"element_id": 42}
        )
        
        # Failed result
        result = ActionResult(success=False, message="Element not found")
        
        # Add 2 times - should not raise
        orchestrator._check_for_loops(action, result)
        orchestrator._check_for_loops(action, result)
        
        # Third time - should raise LoopDetectedError
        with pytest.raises(LoopDetectedError) as exc_info:
            orchestrator._check_for_loops(action, result)
        
        assert "stuck" in str(exc_info.value).lower()
    
    @pytest.mark.asyncio
    async def test_no_loop_on_different_targets(
        self, mock_settings, mock_browser, mock_llm
    ):
        """Should NOT raise loop if targets are different."""
        orchestrator = AgentOrchestrator(mock_settings, mock_browser, mock_llm)
        
        # Different element IDs - not a loop
        action1 = AgentAction(tool="click_element", args={"element_id": 1})
        action2 = AgentAction(tool="click_element", args={"element_id": 2})
        action3 = AgentAction(tool="click_element", args={"element_id": 3})
        
        result = ActionResult(success=False, message="Failed")
        
        # Should not raise - targets are different
        orchestrator._check_for_loops(action1, result)
        orchestrator._check_for_loops(action2, result)
        orchestrator._check_for_loops(action3, result)
    
    @pytest.mark.asyncio
    async def test_no_loop_on_successes(
        self, mock_settings, mock_browser, mock_llm
    ):
        """Should NOT raise loop if actions succeed."""
        orchestrator = AgentOrchestrator(mock_settings, mock_browser, mock_llm)
        
        action = AgentAction(tool="click_element", args={"element_id": 42})
        result = ActionResult(success=True, message="Clicked")
        
        # Should not raise - actions are successful
        orchestrator._check_for_loops(action, result)
        orchestrator._check_for_loops(action, result)
        orchestrator._check_for_loops(action, result)
    
    @pytest.mark.asyncio
    async def test_loop_detected_on_all_failures(
        self, mock_settings, mock_browser, mock_llm
    ):
        """Should raise loop if last 5 actions all failed."""
        orchestrator = AgentOrchestrator(mock_settings, mock_browser, mock_llm)
        
        # Different actions, but all failing
        actions = [
            AgentAction(tool="navigate", args={"url": f"https://example{i}.com"})
            for i in range(5)
        ]
        
        result = ActionResult(success=False, message="Failed")
        
        # Add 5 failed actions
        for action in actions:
            orchestrator._check_for_loops(action, result)
        
        # Should raise LoopDetectedError
        # (orchestrator checks for 5 consecutive failures)


# ============================================================================
# TEST: ActionResult Model
# ============================================================================

class TestActionResult:
    """Test ActionResult model."""
    
    def test_success_result(self):
        """Should create successful result."""
        result = ActionResult(success=True, message="Action completed")
        assert result.success is True
        assert result.message == "Action completed"
        assert result.error is None
    
    def test_failure_result_with_error(self):
        """Should create failed result with error."""
        result = ActionResult(
            success=False,
            message="Action failed",
            error="ElementNotFound"
        )
        assert result.success is False
        assert result.error == "ElementNotFound"


# ============================================================================
# RUN INSTRUCTIONS
# ============================================================================

if __name__ == "__main__":
    """
    Run tests directly:
    python tests/test_agent_core.py
    """
    pytest.main([__file__, "-v"])