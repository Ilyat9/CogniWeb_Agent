#!/usr/bin/env python3
"""Generate remaining files for the refactored agent."""

from pathlib import Path

base = Path("/home/claude/refactored_agent")

# Agent orchestrator
(base / "src/agent/orchestrator.py").write_text('''"""
Main agent orchestrator implementing ReAct loop.

The Orchestrator is the brain of the system:
- Observe: Get current page state
- Think: Call LLM to reason about next action
- Act: Execute the chosen action
- Loop Check: Detect infinite loops and intervention points

Why separate orchestrator?
- Clean separation of concerns (orchestration vs. tool execution)
- Easy to test reasoning loop without browser
- Can swap different reasoning strategies (ReAct, Chain-of-Thought, etc.)
"""

import asyncio
import hashlib
from typing import List, Dict, Any, Optional
from datetime import datetime

from ..config import Settings
from ..core.models import (
    AgentAction,
    ActionResult,
    ObservationState,
    AgentState,
    TaskResult
)
from ..core.exceptions import (
    LoopDetectedError,
    CaptchaDetectedError,
    AgentCriticalError
)
from ..infrastructure import BrowserService, LLMService
from ..utils import DOMProcessor


SYSTEM_PROMPT = """You are an autonomous browser agent using the ReAct (Reasoning + Acting) pattern.

Your goal: Complete the user's task by interacting with web pages.

AVAILABLE TOOLS:
1. navigate(url: str) - Go to a URL
2. click_element(element_id: int) - Click an element
3. type_text(element_id: int, text: str, press_enter: bool) - Type text
4. scroll_page(direction: "up"|"down") - Scroll page
5. take_screenshot(name: str) - Capture screenshot
6. wait(seconds: int) - Wait for time
7. go_back() - Navigate back
8. query_dom(query: str) - Ask about page content
9. done(summary: str, subtasks: list) - Mark task complete

RESPONSE FORMAT (strict JSON only):
{
  "thought": "Your reasoning about what to do next",
  "tool": "tool_name",
  "args": {"param": "value"}
}

CRITICAL RULES:
1. Always check for captchas - if detected, stop and ask for help
2. If action doesn't change state 2 times, analyze why and try different approach
3. Use scroll_page if you don't see needed elements
4. Never guess element IDs - verify they exist first
5. When stuck, try going to homepage or search instead

OBSERVATIONS FORMAT:
You'll receive: Current URL, page title, and interactive elements with IDs.
Each element shows: [ID] TAG_NAME attributes "text content"

Example:
[5] BUTTON type="submit" "Login"
[6] INPUT type="text" name="username" placeholder="Username"
"""


class AgentOrchestrator:
    """
    Main agent orchestrator implementing the ReAct loop.
    
    This is where all components come together:
    - BrowserService for web interactions
    - LLMService for reasoning
    - DOMProcessor for efficient observations
    - Loop detection for safety
    """
    
    def __init__(
        self,
        settings: Settings,
        browser: BrowserService,
        llm: LLMService
    ):
        """
        Initialize orchestrator with injected dependencies.
        
        Why Dependency Injection?
        - Makes testing trivial (mock browser and LLM)
        - Explicit dependencies visible in constructor
        - No hidden global state
        - Follows SOLID principles
        
        Args:
            settings: Application settings
            browser: Browser service instance
            llm: LLM service instance
        """
        self.settings = settings
        self.browser = browser
        self.llm = llm
        self.dom_processor = DOMProcessor(settings.text_block_max_length)
        
        # State tracking
        self.conversation_history: List[Dict[str, str]] = []
        self.state_history: List[str] = []
        self.identical_state_count = 0
        self.context_data: Dict[str, Any] = {}
    
    async def run(self, task: str, starting_url: Optional[str] = None) -> TaskResult:
        """
        Execute task with ReAct loop.
        
        Args:
            task: User's task description
            starting_url: Optional starting URL
            
        Returns:
            TaskResult with execution summary
        """
        start_time = datetime.now()
        
        # Initialize conversation
        self.conversation_history = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Task: {task}"}
        ]
        
        # Navigate to starting URL if provided
        if starting_url:
            await self.browser.navigate(starting_url)
        
        # Main ReAct loop
        for step in range(1, self.settings.max_steps + 1):
            print(f"\\n{'='*70}")
            print(f"STEP {step}/{self.settings.max_steps}")
            print(f"{'='*70}")
            
            try:
                # OBSERVE
                observation = await self._observe()
                
                # Check for captcha
                if await self.browser.detect_captcha():
                    raise CaptchaDetectedError(
                        "Captcha detected - requires human intervention",
                        page_url=await self.browser.get_current_url()
                    )
                
                # Check for loop
                if self._detect_loop(observation):
                    raise LoopDetectedError(
                        "Agent stuck in loop - same state repeated",
                        loop_count=self.identical_state_count
                    )
                
                # THINK
                action = await self._think()
                print(f"\\n🤖 Thought: {action.thought}")
                print(f"⚒️  Tool: {action.tool} {action.args}")
                
                # ACT
                result = await self._act(action)
                print(f"📝 Result: {result.message}")
                
                # Check if done
                if action.tool == "done":
                    duration = (datetime.now() - start_time).total_seconds()
                    return TaskResult(
                        success=True,
                        summary=result.message,
                        steps_taken=step,
                        total_duration_seconds=duration,
                        final_url=await self.browser.get_current_url(),
                        context_data=self.context_data
                    )
                
            except CaptchaDetectedError as e:
                print(f"\\n⚠️ CAPTCHA DETECTED")
                print("Please solve captcha in browser, then press Enter...")
                input()
                continue
            
            except LoopDetectedError as e:
                print(f"\\n⚠️ LOOP DETECTED")
                guidance = input("Provide guidance (or 'quit' to stop): ")
                if guidance.lower() == 'quit':
                    break
                self.conversation_history.append({
                    "role": "user",
                    "content": f"User guidance: {guidance}"
                })
                self.identical_state_count = 0
                continue
        
        # Max steps reached
        duration = (datetime.now() - start_time).total_seconds()
        return TaskResult(
            success=False,
            summary=f"Failed to complete task within {self.settings.max_steps} steps",
            steps_taken=self.settings.max_steps,
            total_duration_seconds=duration,
            final_url=await self.browser.get_current_url(),
            error="MaxStepsExceeded"
        )
    
    async def _observe(self) -> str:
        """Get current page observation."""
        html = await self.browser.page.content()
        dom_text, element_map = self.dom_processor.process_html(html)
        self.browser.element_map = element_map
        
        url = await self.browser.get_current_url()
        title = await self.browser.get_page_title()
        
        observation = f"URL: {url}\\nTitle: {title}\\n\\nInteractive Elements:\\n{dom_text}"
        
        # Add to conversation
        self.conversation_history.append({
            "role": "user",
            "content": f"OBSERVATION:\\n{observation}"
        })
        
        return observation
    
    async def _think(self) -> AgentAction:
        """Generate next action using LLM."""
        action = await self.llm.generate_action(
            self.conversation_history,
            self.settings.temperature
        )
        return action
    
    async def _act(self, action: AgentAction) -> ActionResult:
        """Execute action using browser."""
        tool = action.tool
        args = action.args
        
        if tool == "navigate":
            result = await self.browser.navigate(args["url"])
        elif tool == "click_element":
            result = await self.browser.click_element_safe(args["element_id"])
        elif tool == "type_text":
            result = await self.browser.type_humanly(
                args["element_id"],
                args["text"],
                args.get("press_enter", False)
            )
        elif tool == "scroll_page":
            result = await self.browser.scroll(args.get("direction", "down"))
        elif tool == "done":
            result = ActionResult(
                success=True,
                message=args.get("summary", "Task completed")
            )
        else:
            result = ActionResult(
                success=False,
                message=f"Unknown tool: {tool}",
                error="UnknownTool"
            )
        
        # Add result to conversation
        self.conversation_history.append({
            "role": "assistant",
            "content": f"Executed {tool}: {result.message}"
        })
        
        return result
    
    def _detect_loop(self, observation: str) -> bool:
        """Detect if agent is stuck in loop."""
        state_hash = hashlib.md5(observation.encode()).hexdigest()
        self.state_history.append(state_hash)
        
        # Check last N states
        window = self.settings.loop_detection_window
        if len(self.state_history) >= window:
            recent_states = self.state_history[-window:]
            if len(set(recent_states)) == 1:
                self.identical_state_count += 1
                if self.identical_state_count >= self.settings.max_identical_states:
                    return True
            else:
                self.identical_state_count = 0
        
        return False
''')

print("Created orchestrator.py")

# Agent __init__
(base / "src/agent/__init__.py").write_text('''"""Agent orchestration logic."""

from .orchestrator import AgentOrchestrator

__all__ = ["AgentOrchestrator"]
''')

# Main entry point
(base / "main.py").write_text('''#!/usr/bin/env python3
"""
Battle-Ready Browser Agent - Main Entry Point

Production-grade autonomous browser agent with:
- Async/await architecture
- Dependency injection
- Graceful shutdown handling
- Comprehensive error handling
- Signal management for clean termination
"""

import asyncio
import signal
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.config import load_settings
from src.core.exceptions import ConfigurationError, AgentCriticalError
from src.infrastructure import BrowserService, LLMService
from src.agent import AgentOrchestrator


class GracefulShutdown:
    """
    Handle shutdown signals gracefully.
    
    Why needed?
    - SIGINT (Ctrl+C) and SIGTERM must trigger clean browser shutdown
    - Prevents zombie browser processes
    - Ensures resources are released properly
    """
    
    def __init__(self):
        self.shutdown_requested = False
    
    def request_shutdown(self, signum, frame):
        """Signal handler."""
        print("\\n⚠️  Shutdown requested... cleaning up")
        self.shutdown_requested = True


async def main() -> int:
    """
    Main async entry point.
    
    Returns:
        Exit code (0 = success, 1 = failure)
    """
    print("\\n" + "="*70)
    print("   BATTLE-READY BROWSER AGENT v2.0")
    print("   Modular Monolith Architecture")
    print("="*70 + "\\n")
    
    # Setup signal handling
    shutdown = GracefulShutdown()
    signal.signal(signal.SIGINT, shutdown.request_shutdown)
    signal.signal(signal.SIGTERM, shutdown.request_shutdown)
    
    try:
        # Load and validate configuration
        settings = load_settings()
        print("✅ Configuration loaded")
        print(f"   Model: {settings.model_name}")
        print(f"   Max Steps: {settings.max_steps}")
        print(f"   Stealth: {'Enabled' if settings.enable_stealth else 'Disabled'}")
        
    except ConfigurationError as e:
        print(f"❌ Configuration Error: {e}")
        return 1
    
    # Get task from user
    print("\\n" + "-"*70)
    task = input("📝 Enter task: ").strip()
    if not task:
        print("No task provided")
        return 1
    
    starting_url = input("🌐 Starting URL (optional): ").strip() or None
    print("-"*70 + "\\n")
    
    # Create services with dependency injection
    browser = BrowserService(settings)
    llm = LLMService(settings)
    
    try:
        # Use context manager for guaranteed cleanup
        async with browser:
            print("✅ Browser launched\\n")
            
            # Create orchestrator
            orchestrator = AgentOrchestrator(settings, browser, llm)
            
            # Run task
            result = await orchestrator.run(task, starting_url)
            
            # Display result
            print("\\n" + "="*70)
            if result.success:
                print("✅ TASK COMPLETED SUCCESSFULLY!")
            else:
                print("❌ TASK FAILED")
            print("="*70)
            print(f"Summary: {result.summary}")
            print(f"Steps: {result.steps_taken}")
            print(f"Duration: {result.total_duration_seconds:.1f}s")
            if result.final_url:
                print(f"Final URL: {result.final_url}")
            
            return 0 if result.success else 1
    
    except AgentCriticalError as e:
        print(f"\\n❌ CRITICAL ERROR: {e}")
        if e.context.get("screenshot_path"):
            print(f"Screenshot saved: {e.context['screenshot_path']}")
        if e.context.get("html_dump_path"):
            print(f"HTML dump saved: {e.context['html_dump_path']}")
        return 1
    
    except Exception as e:
        print(f"\\n❌ UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    finally:
        print("\\n👋 Cleanup complete")


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
''')

print("Created main.py")

# Updated requirements.txt
(base / "requirements.txt").write_text('''# ============================================================================
# Battle-Ready Browser Agent v2.0 - Requirements
# ============================================================================
# Production-grade dependencies with version pinning for stability
# ============================================================================

# Core Dependencies
python-dotenv>=1.0.0
pydantic>=2.0.0
pydantic-settings>=2.0.0

# LLM API
openai>=1.0.0
httpx>=0.25.0

# Browser Automation
playwright>=1.40.0
playwright-stealth>=1.0.6

# HTML Processing
beautifulsoup4>=4.12.0
lxml>=4.9.0

# Retry Logic
tenacity>=8.2.0

# Async Support
asyncio-extras>=1.3.2

# ============================================================================
# Post-Installation
# ============================================================================
# After installing, run:
#   playwright install chromium
# ============================================================================
''')

print("Created requirements.txt")

# .env.example
(base / ".env.example").write_text('''# ============================================================================
# Browser Agent Configuration
# ============================================================================

# REQUIRED: LLM API Configuration
OPENAI_API_KEY=your_api_key_here
API_BASE_URL=https://api.openai.com/v1
MODEL_NAME=gpt-4o-mini

# OPTIONAL: Network
PROXY_URL=
HTTP_TIMEOUT=60.0

# Browser Settings
USER_DATA_DIR=./browser_data
HEADLESS=false
SLOW_MO=50
PAGE_LOAD_TIMEOUT=60000
ACTION_TIMEOUT=20000

# Agent Behavior
MAX_STEPS=50
MAX_RETRY_ATTEMPTS=3
TEMPERATURE=0.1
MAX_TOKENS=2000

# DOM Processing & Token Optimization
TEXT_BLOCK_MAX_LENGTH=200
DOM_MAX_TOKENS_ESTIMATE=10000

# Loop Protection
LOOP_DETECTION_WINDOW=3
MAX_IDENTICAL_STATES=5

# Stealth Mode
ENABLE_STEALTH=true
TYPING_SPEED_MIN=50
TYPING_SPEED_MAX=150

# Debugging
DEBUG_MODE=false
SCREENSHOT_DIR=./screenshots
''')

print("Created .env.example")

# .gitignore
(base / ".gitignore").write_text('''# Environment
.env
.env.local

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
*.egg-info/
dist/
build/

# Browser Data
browser_data/
screenshots/
*.png
*.html

# IDE
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# Logs
*.log
''')

print("Created .gitignore")

print("\\n✅ All files created successfully!")
print("\\nFile structure:")
import subprocess
subprocess.run(["tree", "-L", "3", str(base)], check=False)

