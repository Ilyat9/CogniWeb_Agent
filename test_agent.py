#!/usr/bin/env python3
"""
Unit tests for Autonomous Browser Agent components.
Run with: pytest test_agent.py -v
"""

import pytest
import json
from unittest.mock import Mock, patch, MagicMock
from bs4 import BeautifulSoup

# Import components to test
import sys
sys.path.insert(0, '.')
from main import (
    Config, EnhancedDOMProcessor, LLMClient, Agent,
    SYSTEM_PROMPT
)


# ============================================================================
# Config Tests
# ============================================================================

class TestConfig:
    """Test configuration management."""
    
    def test_config_from_env_with_api_key(self, monkeypatch):
        """Test loading config from environment."""
        monkeypatch.setenv("OPENAI_API_KEY", "test_key_123")
        monkeypatch.setenv("PROXY_URL", "http://test-proxy:8080")
        
        config = Config.from_env()
        
        assert config.api_key == "test_key_123"
        assert config.proxy_url == "http://test-proxy:8080"
    
    def test_config_requires_api_key(self, monkeypatch):
        """Test that config raises error without API key."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        
        with pytest.raises(ValueError, match="API key must be set"):
            Config.from_env()
    
    def test_config_defaults(self, monkeypatch):
        """Test default configuration values."""
        monkeypatch.setenv("OPENAI_API_KEY", "test_key")
        
        config = Config.from_env()
        
        assert config.max_steps == 20
        assert config.headless == False
        assert config.temperature == 0.1


# ============================================================================
# DOM Processor Tests
# ============================================================================

class TestDOMProcessor:
    """Test DOM processing and simplification."""
    
    @pytest.fixture
    def processor(self):
        """Create a DOM processor instance."""
        return EnhancedDOMProcessor()
    
    @pytest.fixture
    def mock_page(self):
        """Create a mock Playwright page."""
        page = Mock()
        page.query_selector.return_value = Mock()
        return page
    
    def test_process_simple_form(self, processor, mock_page):
        """Test processing a simple HTML form."""
        html = """
        <html>
            <body>
                <form>
                    <input type="text" name="username" placeholder="Username">
                    <input type="password" name="password" placeholder="Password">
                    <button type="submit">Login</button>
                </form>
            </body>
        </html>
        """
        
        dom_text, element_map = processor.process_page(html, mock_page)
        
        # Should find 3 elements
        assert len(element_map) == 3
        
        # Should contain element descriptions
        assert "INPUT" in dom_text
        assert "BUTTON" in dom_text
        assert "Login" in dom_text
    
    def test_process_removes_scripts(self, processor, mock_page):
        """Test that script tags are removed."""
        html = """
        <html>
            <body>
                <script>alert('test')</script>
                <button>Click Me</button>
            </body>
        </html>
        """
        
        dom_text, element_map = processor.process_page(html, mock_page)
        
        # Should not contain script content
        assert "alert" not in dom_text
        assert "BUTTON" in dom_text
    
    def test_element_id_assignment(self, processor, mock_page):
        """Test that unique IDs are assigned sequentially."""
        html = """
        <html>
            <body>
                <button>Button 1</button>
                <button>Button 2</button>
                <button>Button 3</button>
            </body>
        </html>
        """
        
        dom_text, element_map = processor.process_page(html, mock_page)
        
        # Should have IDs 0, 1, 2
        assert 0 in element_map
        assert 1 in element_map
        assert 2 in element_map
        assert len(element_map) == 3
    
    def test_build_selector_with_id(self, processor):
        """Test selector building for element with ID."""
        html = '<button id="submit-btn">Submit</button>'
        soup = BeautifulSoup(html, 'html.parser')
        button = soup.find('button')
        
        selector = processor._build_selector(button)
        
        assert selector == "#submit-btn"
    
    def test_build_selector_with_name(self, processor):
        """Test selector building for element with name."""
        html = '<input name="email" type="text">'
        soup = BeautifulSoup(html, 'html.parser')
        input_elem = soup.find('input')
        
        selector = processor._build_selector(input_elem)
        
        assert selector == "input[name='email']"


# ============================================================================
# Agent Tests
# ============================================================================

class TestAgent:
    """Test agent decision-making logic."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock configuration."""
        config = Mock(spec=Config)
        config.max_steps = 5
        config.action_timeout = 10000
        config.api_key = "test_key"
        config.proxy_url = "http://test:8080"
        return config
    
    @pytest.fixture
    def agent(self, mock_config):
        """Create agent with mocked dependencies."""
        with patch('main.LLMClient'):
            agent = Agent(mock_config)
            agent.llm_client = Mock()
            agent.dom_processor = Mock()
            return agent
    
    def test_parse_valid_json(self, agent):
        """Test parsing valid LLM JSON response."""
        response = json.dumps({
            "thought": "I should click the button",
            "action_type": "click",
            "element_id": 5,
            "args": {}
        })
        
        decision = agent._parse_action(response)
        
        assert decision["action_type"] == "click"
        assert decision["element_id"] == 5
        assert decision["thought"] == "I should click the button"
    
    def test_parse_json_with_markdown(self, agent):
        """Test parsing JSON wrapped in markdown code blocks."""
        response = """```json
{
  "thought": "Test thought",
  "action_type": "type",
  "element_id": 3,
  "args": {"text": "hello"}
}
```"""
        
        decision = agent._parse_action(response)
        
        assert decision["action_type"] == "type"
        assert decision["args"]["text"] == "hello"
    
    def test_parse_invalid_json_raises_error(self, agent):
        """Test that invalid JSON raises appropriate error."""
        response = "This is not JSON at all"
        
        with pytest.raises(ValueError, match="Invalid JSON"):
            agent._parse_action(response)
    
    def test_validate_decision_checks_action_type(self, agent):
        """Test that validation requires action_type field."""
        decision = {"element_id": 5}
        
        with pytest.raises(ValueError, match="action_type"):
            agent._validate_decision(decision)
    
    def test_validate_decision_checks_element_exists(self, agent):
        """Test that validation checks element ID exists."""
        agent.dom_processor.element_map = {1: {}, 2: {}, 3: {}}
        
        decision = {
            "action_type": "click",
            "element_id": 99  # Doesn't exist
        }
        
        with pytest.raises(ValueError, match="does not exist"):
            agent._validate_decision(decision)
    
    def test_validate_decision_accepts_valid_element(self, agent):
        """Test that validation accepts valid element ID."""
        agent.dom_processor.element_map = {1: {}, 2: {}, 3: {}}
        
        decision = {
            "action_type": "click",
            "element_id": 2,
            "thought": "Clicking element 2"
        }
        
        # Should not raise
        agent._validate_decision(decision)


# ============================================================================
# System Prompt Tests
# ============================================================================

class TestSystemPrompt:
    """Test system prompt content."""
    
    def test_system_prompt_contains_actions(self):
        """Test that system prompt documents all actions."""
        required_actions = [
            "click", "type", "select", "scroll", 
            "navigate", "done", "fail"
        ]
        
        for action in required_actions:
            assert action in SYSTEM_PROMPT.lower()
    
    def test_system_prompt_mentions_json(self):
        """Test that system prompt requires JSON output."""
        assert "json" in SYSTEM_PROMPT.lower()
        assert "action_type" in SYSTEM_PROMPT
        assert "element_id" in SYSTEM_PROMPT
    
    def test_system_prompt_mentions_thought(self):
        """Test that system prompt requires thought field."""
        assert "thought" in SYSTEM_PROMPT.lower()


# ============================================================================
# Integration Tests
# ============================================================================

class TestIntegration:
    """Integration tests for complete workflows."""
    
    @pytest.fixture
    def mock_config(self, monkeypatch):
        """Create test configuration."""
        monkeypatch.setenv("OPENAI_API_KEY", "test_key_123")
        return Config.from_env()
    
    def test_agent_observes_page(self, mock_config):
        """Test that agent can observe a page state."""
        with patch('main.BrowserManager') as MockBrowser:
            # Mock browser
            mock_browser = Mock()
            mock_browser.get_html.return_value = "<html><button>Click</button></html>"
            mock_browser.get_url.return_value = "https://example.com"
            mock_browser.page = Mock()
            
            MockBrowser.return_value.__enter__.return_value = mock_browser
            
            agent = Agent(mock_config)
            agent.browser = mock_browser
            
            observation = agent.self.browser.get_page_state()
            
            assert "example.com" in observation
            assert "BUTTON" in observation or "button" in observation.lower()


# ============================================================================
# Run Tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
