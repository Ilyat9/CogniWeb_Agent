"""
Agent Orchestrator - Main reasoning loop with tool execution.

FIXES:
1. Use get_interactive_elements() for live DOM extraction (no HTML parsing)
2. Single source of truth: browser.element_map
3. Smart loop detection: tracks action+target, not just observation
4. Context trimming preserves current element_map
"""

import asyncio
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
import random
import time

logger = logging.getLogger(__name__)



from ..config import Settings
from ..core.models import (
    AgentAction,
    ActionResult,
    TaskResult,
    ObservationState,
    AgentState,
    ConversationMessage
)
from ..core.exceptions import (
    LoopDetectedError,
    CaptchaDetectedError,
    ActionError,
    LLMError
)
from ..infrastructure import BrowserService, LLMService
from ..utils import DOMProcessor


class AgentOrchestrator:
    """
    Orchestrates the agent's reasoning-action loop.
    
    CRITICAL FIXES:
    - Uses live DOM extraction instead of HTML parsing
    - Single element_map maintained in browser service
    - Smart loop detection distinguishes errors from real loops
    """
    
    def __init__(
        self,
        settings: Settings,
        browser: BrowserService,
        llm: LLMService
    ):
        """
        Initialize orchestrator with dependencies.
        
        Args:
            settings: Application configuration
            browser: Browser automation service
            llm: LLM service for reasoning
        """
        self.settings = settings
        self.browser = browser
        self.llm = llm
        self.dom_processor = DOMProcessor(settings)
        
        # State management
        self.conversation_history: List[Dict[str, str]] = []
        self.action_history: List[Dict[str, Any]] = []  # NEW: Track actions for loop detection
        self.context_data: Dict[str, Any] = {}
        self.previous_observation: Optional[str] = None
        self.last_call_time = 0 

    async def _call_llm_with_rate_limit(self, messages: List[Dict[str, str]], temperature: float = 0.7):
        """
        Вызывает LLM с ограничением частоты запросов (rate limiting).
        Например, 1 запрос каждые 15 секунд.
        """
        RATE_LIMIT_SECONDS = 15  # можно менять
        
        current_time = time.time()
        time_since_last = current_time - self.last_call_time
        
        if time_since_last < RATE_LIMIT_SECONDS:
            delay = RATE_LIMIT_SECONDS - time_since_last
            print(f"⏳ Rate limiting: waiting {delay:.1f}s before next LLM request...")
            await asyncio.sleep(delay)
        
        # Сам вызов LLM
        action = await self.llm.generate_action(messages=messages, temperature=temperature)
        self.last_call_time = time.time()
        return action

        
    def get_trimmed_history(self, window_size=10):
        """
        Get trimmed conversation history while preserving system prompt.
        
        IMPORTANT: Always keep system prompt (index 0) + last N messages
        """
        if len(self.conversation_history) <= window_size + 1:
            return self.conversation_history
        return [self.conversation_history[0]] + self.conversation_history[-window_size:]
    
    async def run(
        self,
        task: str,
        starting_url: Optional[str] = None
    ) -> TaskResult:
        """
        Execute task using autonomous agent loop.
        
        Args:
            task: Natural language task description
            starting_url: Optional starting URL
            
        Returns:
            TaskResult with execution summary
        """
        start_time = datetime.now()
        
        # Initialize conversation with system prompt
        self._initialize_conversation(task)
        
        # Navigate to starting URL if provided
        if starting_url:
            print(f"🌐 Navigating to: {starting_url}")
            await self.browser.navigate(starting_url)
        
        # Main reasoning loop
        for step in range(1, self.settings.max_steps + 1):
            print(f"\n{'='*70}")
            print(f"STEP {step}/{self.settings.max_steps}")
            print(f"{'='*70}")
            
            try:
                # 1. Observe current state (FIXED: use live DOM extraction)
                observation = await self._get_observation()
                self.previous_observation = observation
                
                # 2. Add observation to conversation
                self.conversation_history.append({
                    "role": "user",
                    "content": f"Current page observation:\n{observation}"
                })
                
                # 3. Get next action from LLM
                print("🤔 Agent reasoning...")
                action = await self._call_llm_with_rate_limit(
                    messages=self.get_trimmed_history(window_size=5), 
                    temperature=self.settings.temperature
                )
                
                print(f"💭 Thought: {action.thought}")
                print(f"🔧 Tool: {action.tool}")
                print(f"📝 Args: {action.args}")
                
                # 4. Check for task completion
                if action.tool == "done":
                    elapsed = (datetime.now() - start_time).total_seconds()
                    return TaskResult(
                        success=True,
                        summary=action.args.get("summary", "Task completed"),
                        steps_taken=step,
                        total_duration_seconds=elapsed,
                        final_url=await self.browser.get_current_url(),
                        context_data=self.context_data.copy()
                    )
                
                # 5. Execute action
                result = await self._execute_action(action)
                
                # 6. Add action and result to conversation
                self.conversation_history.append({
                    "role": "assistant",
                    "content": f"Action: {action.tool}\nResult: {result.message}"
                })
                
                # 7. FIXED: Smart loop detection (action + target aware)
                self._check_for_loops(action, result)
                
                # 8. Print result
                status = "✅" if result.success else "❌"
                print(f"{status} Result: {result.message}")
                if self.settings.agent_step_delay > 0:
                    delay = random.uniform(
                        self.settings.agent_step_delay * 0.5,
                        self.settings.agent_step_delay * 1.5
                    )
                    await asyncio.sleep(delay)
            except CaptchaDetectedError as e:
                print(f"⚠️ Captcha detected: {str(e)}")
                print("🛑 Пожалуйста, решите капчу вручную. Агент будет ждать...")
                while await self.browser.detect_captcha():
                    await asyncio.sleep(3)
                print("✅ Капча решена, продолжаем выполнение задачи.")
                continue
            
            except LLMError as e:
                print(f"⚠️ LLM Error: {str(e)}")
                
                # JSON truncation is usually caused by context being too long
                # Strategy: Trim conversation history more aggressively and retry
                if "No valid JSON found" in str(e) or "truncated" in str(e).lower():
                    print("🔄 Detected JSON truncation - trimming conversation history and retrying...")
                    
                    # Remove the last observation that was added (it's too long)
                    if self.conversation_history and self.conversation_history[-1]["role"] == "user":
                        self.conversation_history.pop()
                    
                    # Add a much shorter observation summary instead
                    self.conversation_history.append({
                        "role": "user",
                        "content": f"Current URL: {await self.browser.get_current_url()}\n"
                                   f"Context so far: {len(self.context_data)} items stored\n"
                                   f"Please continue with a simple action (navigate, click, type_text, or done)."
                    })
                    
                    # Retry with more aggressive trimming
                    try:
                        print("🤔 Retrying with shorter context...")
                        action = await self.llm.generate_action(
                            messages=self.get_trimmed_history(window_size=2),  # Very short window
                            temperature=self.settings.temperature
                        )
                        
                        print(f"💭 Thought: {action.thought}")
                        print(f"🔧 Tool: {action.tool}")
                        print(f"📝 Args: {action.args}")
                        
                        # Execute the recovered action
                        result = await self._execute_action(action)
                        
                        self.conversation_history.append({
                            "role": "assistant",
                            "content": f"Action: {action.tool}\nResult: {result.message}"
                        })
                        
                        status = "✅" if result.success else "❌"
                        print(f"{status} Result: {result.message}")
                        
                    except LLMError as retry_error:
                        print(f"❌ Retry failed: {retry_error}")
                        # Add helpful message to conversation
                        self.conversation_history.append({
                            "role": "assistant",
                            "content": "Error: Unable to parse action. Continuing to next step."
                        })
                        continue
                else:
                    # Other LLM errors - log and continue
                    print(f"❌ LLM error: {str(e)}")
                    self.conversation_history.append({
                        "role": "assistant",
                        "content": f"Error: {str(e)}. Continuing to next step."
                    })
                    continue
            
            except LoopDetectedError as e:
                elapsed = (datetime.now() - start_time).total_seconds()
                return TaskResult(
                    success=False,
                    summary=f"Loop detected: {str(e)}",
                    steps_taken=step,
                    total_duration_seconds=elapsed,
                    final_url=await self.browser.get_current_url(),
                    error="LoopDetected"
                )
        # Max steps exceeded
        elapsed = (datetime.now() - start_time).total_seconds()
        return TaskResult(
            success=False,
            summary=f"Max steps ({self.settings.max_steps}) exceeded",
            steps_taken=self.settings.max_steps,
            total_duration_seconds=elapsed,
            final_url=await self.browser.get_current_url(),
            context_data=self.context_data.copy(),
            error="MaxStepsExceeded"
        )
        
        
    
    def _initialize_conversation(self, task: str) -> None:
        """Initialize conversation with system prompt."""
        system_prompt = f"""You are an autonomous web browser agent. Your task is:

{task} 

You can use these tools:
- navigate(url): Navigate to a URL
- click_element(element_id): Click an element
- type_text(element_id, text, press_enter=False): Type text into an element
- upload_file(element_id, file_path): Upload a file to an input element
- select_option(element_id, value): Select option from dropdown
- scroll_page(direction="down"): Scroll up or down
- take_screenshot(): Take a screenshot
- wait(seconds): Wait for page to update
- go_back(): Go to previous page
- query_dom(query): Search for text in current page
- store_context(key, value): Store single data point OR store_context(field1=value1, field2=value2, ...): Store multiple data points at once
- done(summary): Complete the task

CRITICAL RULES:
1. Element IDs are ONLY valid for the CURRENT observation
2. After ANY page change (navigate, click, scroll), you MUST re-observe to get fresh element IDs
3. If you get "Invalid element ID" error, it means the page changed - use fresh observation
4. DO NOT retry the same action with same element_id if it failed - the page likely changed
OUTPUT RULE: ONLY JSON. No explanations, no code blocks, no markdown.
Format: {{"tool": "<tool_name>", "args": {{<parameters>}}}}
Example output:
{{"tool": "store_context", "args": {{"vacancy_name": "Ai engineer", "company": "Tech Solutions Inc.", "salary": "от 150 000 ₽", "requirements": "3+ years of experience, FastAPI, PostgreSQL", "responsibilities": "Developing microservices and AI integration"}}}}




Always think step-by-step and explain your reasoning."""

        self.conversation_history.append({
            "role": "system",
            "content": system_prompt
        })
    
    async def _get_observation(self) -> str:
        """
        FIXED: Get current page state using LIVE DOM extraction.
        
        WHY THIS FIX MATTERS:
        - Old code: fetch HTML -> parse with BeautifulSoup -> generate IDs
        - Problem: IDs from BeautifulSoup don't match live page
        - New code: JavaScript injects data-agent-id into live DOM
        - Result: IDs are guaranteed valid for Playwright selectors
        
        Returns:
            Formatted observation with current element IDs
        """
        # Check for captcha
        if await self.browser.detect_captcha():
            raise CaptchaDetectedError("Captcha detected on page")
        
        # Get page metadata
        url = await self.browser.get_current_url()
        title = await self.browser.get_page_title()
        
        # CRITICAL FIX: Use live DOM extraction
        elements = await self.dom_processor.get_interactive_elements(self.browser.page)
        
        # CRITICAL FIX: Update browser's element_map as SINGLE SOURCE OF TRUTH
        self.browser.element_map.clear()
        for elem in elements:
            self.browser.element_map[elem['id']] = elem['selector']
        
        # Format observation
        lines = [
            f"URL: {url}",
            f"Title: {title}",
            f"\nInteractive Elements ({len(elements)} total):",
            ""
        ]
        
        # Limit to first 100 elements to avoid token bloat
        # On hh.ru, this ensures we see job listings, not just header
        for elem in elements[:50]:
            # Format: [ID] TAG text
            text_preview = elem['text'][:80] if elem['text'] else ""
            lines.append(f"[{elem['id']}] {elem['tag'].upper()} {text_preview}")
        
        if len(elements) > 100:
            lines.append(f"\n... and {len(elements) - 100} more elements (use scroll_page to see more)")
        
        return "\n".join(lines)
    
    async def _execute_action(self, action: AgentAction) -> ActionResult:
        """
        Execute agent action via browser service.
        
        Args:
            action: Action to execute
            
        Returns:
            ActionResult with execution status
        """
        tool = action.tool
        args = action.args
        result = ActionResult(success=False, message="Unknown tool")
        
        # Route to appropriate handler
        if tool == "navigate":
            url = args.get("url", "")
            if not url:
                result = ActionResult(
                    success=False,
                    message="navigate requires 'url' parameter",
                    error="MissingUrl"
                )
            else:
                result = await self.browser.navigate(url)
                print(f"🌐 Navigated to: {url}")
        
        elif tool == "click_element":
            element_id = args.get("element_id")
            
            if element_id is None:
                result = ActionResult(
                    success=False,
                    message="click_element requires 'element_id' parameter",
                    error="MissingElementId"
                )
            elif not isinstance(element_id, int):
                result = ActionResult(
                    success=False,
                    message=f"element_id must be integer, got {type(element_id).__name__}",
                    error="InvalidType"
                )
            else:
                # FIXED: Validate element_id exists in current map
                if element_id not in self.browser.element_map:
                    result = self._get_invalid_element_error(element_id)
                else:
                    result = await self.browser.click_element_safe(element_id)
                    print(f"🖱️  Clicked element {element_id}")

                    new_obs = await self._get_observation()
                    self.previous_observation = new_obs
        
        elif tool == "type_text":
            element_id = args.get("element_id")
            text = args.get("text", "")
            press_enter = args.get("press_enter", False)
            
            if element_id is None:
                result = ActionResult(
                    success=False,
                    message="type_text requires 'element_id' parameter",
                    error="MissingElementId"
                )
            elif not isinstance(element_id, int):
                result = ActionResult(
                    success=False,
                    message=f"element_id must be integer, got {type(element_id).__name__}",
                    error="InvalidType"
                )
            elif not text:
                result = ActionResult(
                    success=False,
                    message="type_text requires 'text' parameter",
                    error="MissingText"
                )
            else:
                # FIXED: Validate element_id exists in current map
                if element_id not in self.browser.element_map:
                    result = self._get_invalid_element_error(element_id)
                else:
                    result = await self.browser.type_text(element_id, text, press_enter)
                    print(f"⌨️  Typed into element {element_id}")
        
        elif tool == "select_option":
            element_id = args.get("element_id")
            value = args.get("value", "")
            
            if element_id is None:
                result = ActionResult(
                    success=False,
                    message="select_option requires 'element_id' parameter",
                    error="MissingElementId"
                )
            elif not isinstance(element_id, int):
                result = ActionResult(
                    success=False,
                    message=f"element_id must be integer, got {type(element_id).__name__}",
                    error="InvalidType"
                )
            else:
                # FIXED: Validate element_id exists in current map
                if element_id not in self.browser.element_map:
                    result = self._get_invalid_element_error(element_id)
                else:
                    result = await self.browser.select_option(element_id, value)
                    print(f"📋 Selected option in element {element_id}")
        
        elif tool == "scroll_page":
            direction = args.get("direction", "down")
            if direction not in ["up", "down"]:
                result = ActionResult(
                    success=False,
                    message="direction must be 'up' or 'down'",
                    error="InvalidDirection"
                )
            else:
                try:
                    if direction == "down":
                        await self.browser.page.evaluate("window.scrollBy(0, window.innerHeight)")
                    else:
                        direction = args.get("direction", "down")
                        result = await self.browser.scroll(direction)
                        print(f"📜 Scrolled {direction}")
                    
                    await asyncio.sleep(0.5)
                    
                    result = ActionResult(
                        success=True,
                        message=f"Scrolled {direction}"
                    )
                    print(f"📜 Scrolled {direction}")
                except Exception as e:
                    result = ActionResult(
                        success=False,
                        message=f"Scroll failed: {str(e)}",
                        error=str(e)
                    )
        
        elif tool == "take_screenshot":
            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_path = self.settings.screenshot_dir / f"screenshot_{timestamp}.png"
                await self.browser.page.screenshot(path=str(screenshot_path))
                result = ActionResult(
                    success=True,
                    message=f"Screenshot saved: {screenshot_path}",
                    data={"path": str(screenshot_path)}
                )
                print(f"📸 Screenshot saved: {screenshot_path}")
            except Exception as e:
                result = ActionResult(
                    success=False,
                    message=f"Screenshot failed: {str(e)}",
                    error=str(e)
                )
        
        elif tool == "wait":
            MAX_WAIT_SECONDS = 30
            requested_seconds = args.get("seconds", 1)
            
            if not isinstance(requested_seconds, (int, float)):
                result = ActionResult(
                    success=False,
                    message=f"wait requires numeric 'seconds' parameter, got: {type(requested_seconds).__name__}",
                    error="InvalidType"
                )
            else:
                seconds = max(0.5, min(float(requested_seconds), MAX_WAIT_SECONDS))
                
                if requested_seconds > MAX_WAIT_SECONDS:
                    print(f"⚠️  Wait time capped: requested {requested_seconds}s → using {MAX_WAIT_SECONDS}s")
                
                print(f"⏳ Waiting {seconds} seconds for page update...")
                
                await asyncio.sleep(seconds)
                
                try:
                    await self.browser.page.wait_for_load_state(
                        "networkidle",
                        timeout=5000
                    )
                    print("✅ Network idle detected")
                except Exception as e:
                    # Network idle is optional, log but don't fail
                    logger.debug(f"Network idle timeout (expected for some pages): {e}")
                    pass
                
                result = ActionResult(
                    success=True,
                    message=f"Waited {seconds} seconds"
                )
        
        elif tool == "go_back":
            try:
                await self.browser.page.go_back(timeout=self.settings.page_load_timeout)
                result = ActionResult(
                    success=True,
                    message="Went back to previous page"
                )
                print("⬅️  Went back")
            except Exception as e:
                result = ActionResult(
                    success=False,
                    message=f"Go back failed: {str(e)}",
                    error=str(e)
                )
        
        elif tool == "query_dom":
            query = args.get("query", "").strip()

            if not self.previous_observation:
                result = ActionResult(
                    success=False,
                    message="No page observation available yet. Please use 'navigate' or 'wait' first.",
                    error="NoObservation"
                )
            elif not query:
                result = ActionResult(
                    success=False,
                    message="query_dom requires 'query' parameter",
                    error="MissingQuery"
                )
            else:
                # Разбиваем длинный запрос на ключевые слова
                keywords = re.split(r'[\s,;]+', query)
                all_matches = []

                for keyword in keywords:
                    keyword_lower = keyword.lower()
                    matches = []

                    for line in self.previous_observation.split('\n'):
                        if keyword_lower in line.lower():
                            matches.append(line.strip())

                    if matches:
                        all_matches.append({
                            "keyword": keyword,
                            "matches": matches[:10],  # Ограничение первых 10 результатов
                            "total_count": len(matches)
                        })

                if all_matches:
                    messages = []
                    for m in all_matches:
                        messages.append(f"{m['keyword']} ({m['total_count']} match(es)):\n" +
                                        "\n".join(m['matches']))
                    result = ActionResult(
                        success=True,
                        message="\n\n".join(messages),
                        data={"matches": all_matches}
                    )
                    print(f"🔍 Query '{query}': {len(all_matches)} keyword(s) found matches")
                else:
                    result = ActionResult(
                        success=False,
                        message=f"None of the keywords from '{query}' found in current page.",
                        error="NotFound"
                    )
                    print(f"❌ Query '{query}' found nothing")

        
        elif tool == "store_context":
            # Support both single key-value and multiple key-value formats
            # Format 1 (legacy): {"key": "name", "value": "John"}
            # Format 2 (new): {"field1": "value1", "field2": "value2", ...}
            
            stored_items = {}
            
            # Check if using legacy single key-value format
            if "key" in args and "value" in args:
                key = args.get("key", "").strip()
                value = args.get("value", "")
                
                if not key:
                    result = ActionResult(
                        success=False,
                        message="store_context requires 'key' parameter",
                        error="MissingKey"
                    )
                else:
                    self.context_data[key] = value
                    stored_items[key] = value
                    result = ActionResult(
                        success=True,
                        message=f"Stored context: {key} = {value}",
                        data={"stored": stored_items}
                    )
                    print(f"💾 Stored context: {key}")
            
            # New format: multiple key-value pairs directly in args
            else:
                # Filter out non-data fields (like 'tool', 'thought', etc.)
                reserved_fields = {"tool", "thought", "reasoning"}
                
                for key, value in args.items():
                    if key not in reserved_fields and key.strip():
                        self.context_data[key] = value
                        stored_items[key] = value
                
                if not stored_items:
                    result = ActionResult(
                        success=False,
                        message="store_context requires at least one key-value pair",
                        error="NoDataProvided"
                    )
                else:
                    # Create summary message
                    items_summary = ", ".join([f"{k}" for k in stored_items.keys()])
                    result = ActionResult(
                        success=True,
                        message=f"Stored {len(stored_items)} context item(s): {items_summary}",
                        data={"stored": stored_items}
                    )
                    print(f"💾 Stored {len(stored_items)} context item(s): {items_summary}")
        
        else:
            result = ActionResult(
                success=False,
                message=f"Unknown tool: {tool}",
                error="UnknownTool"
            )
        
        return result
    
    def _get_invalid_element_error(self, element_id: int) -> ActionResult:
        """
        Return standardized error for invalid element ID.
        
        IMPORTANT: This message helps LLM understand it needs fresh observation.
        """
        return ActionResult(
            success=False,
            message=f"Invalid element ID: {element_id}. The page has changed - element IDs are no longer valid. Get a fresh observation to see current elements.",
            error="InvalidElementId"
        )
    
    def _check_for_loops(self, action: AgentAction, result: ActionResult) -> None:
        """
        FIXED: Smart loop detection that distinguishes errors from real loops.
        
        OLD BEHAVIOR:
        - Only looked at observation text
        - "Invalid element ID" counted as same state
        - Agent died after 3 validation errors
        
        NEW BEHAVIOR:
        - Track (action_type, target, success) tuples
        - Only count as loop if SAME ACTION on SAME TARGET fails repeatedly
        - Errors don't count as loops if agent is trying different things
        
        Args:
            action: Action that was just executed
            result: Result of the action
            
        Raises:
            LoopDetectedError: If real loop detected (not just errors)
        """
        # Build action signature: (tool, target_element_id, success)
        target = action.args.get("element_id", action.args.get("url", ""))
        action_signature = (action.tool, target, result.success)
        
        # Add to history
        self.action_history.append(action_signature)
        
        # Keep only recent history
        max_history = self.settings.loop_detection_window
        if len(self.action_history) > max_history:
            self.action_history.pop(0)
        
        # Check for loops: SAME action on SAME target failing repeatedly
        if len(self.action_history) >= 3:
            recent_3 = self.action_history[-3:]
            
            # If last 3 actions are identical (same tool + target + failure)
            if len(set(recent_3)) == 1:
                tool, target, success = recent_3[0]
                
                # Only raise error if it's the SAME action failing
                # (not just different invalid element IDs)
                if not success and tool in ["click_element", "type_text", "select_option"]:
                    raise LoopDetectedError(
                        f"Agent stuck: action '{tool}' on target '{target}' failed 3 times in a row. "
                        f"This suggests the element is not interactable or the selector is wrong.",
                        loop_count=3
                    )
        
        # Additional check: If last 5 actions are ALL failures (regardless of type)
        # This catches cases where agent is thrashing without making progress
        if len(self.action_history) >= 5:
            recent_5 = self.action_history[-5:]
            all_failures = all(not success for _, _, success in recent_5)
            
            if all_failures:
                raise LoopDetectedError(
                    f"Agent stuck: last 5 actions all failed. Actions: {[tool for tool, _, _ in recent_5]}",
                    loop_count=5
                )