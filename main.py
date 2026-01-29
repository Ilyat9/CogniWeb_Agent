#!/usr/bin/env python3
"""
CogniWeb Agent v3.0 - Fully Autonomous Browser Agent
=======================================================

Критические возможности:
- Абсолютная автономность (NO hardcoded selectors)
- Работа в условиях "грязного интернета" (медленные прокси, капчи, попапы)
- Экономия токенов (умный DOM Distiller)
- Явный шаг Thinking перед каждым действием
- Обобщенный цикл Observe → Think → Act

Архитектура основана на статье Anthropic "Building effective agents":
- Augmented LLM с Tool Use
- Clear Tool Definitions
- Context Window Management
"""

import os
import json
import time
import re
import logging
import hashlib
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from collections import deque
from enum import Enum

# Load environment variables FIRST
from dotenv import load_dotenv
load_dotenv()

# Third-party imports
import httpx
from openai import OpenAI
from bs4 import BeautifulSoup, Comment
from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeout
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# Configuration Management
# ============================================================================

@dataclass
class Config:
    """Production-grade configuration with all optimizations."""
    
    # API Configuration
    api_key: str
    api_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4o-mini"
    
    # Proxy Configuration
    proxy_url: str = "http://10.0.2.2:7897"
    
    # Browser Configuration
    user_data_dir: str = "./browser_data"
    headless: bool = False
    viewport_width: int = 1920
    viewport_height: int = 1080
    
    # Agent Configuration
    max_steps: int = 20
    page_load_timeout: int = 60000  # 60 seconds
    action_timeout: int = 30000     # 30 seconds
    
    # LLM Configuration
    max_tokens: int = 2000
    temperature: float = 0.1
    
    # HTTP Configuration
    http_timeout: float = 90.0
    http_connect_timeout: float = 30.0
    
    # Token Economy Configuration
    max_dom_elements: int = 50  # Limit elements per page
    max_text_length: int = 500  # Max chars for text content
    max_history_turns: int = 10  # Keep only last N turns
    
    # Popup/Captcha Detection
    popup_detection_enabled: bool = True
    max_popup_close_attempts: int = 3
    
    # Human-like Behavior
    min_action_delay: float = 1.0
    max_action_delay: float = 3.0
    typing_delay: int = 100  # ms between keystrokes
    
    @classmethod
    def from_env(cls) -> 'Config':
        """Load configuration from environment variables."""
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
        if not api_key:
            raise ValueError(
                "API key must be set in OPENAI_API_KEY or LLM_API_KEY environment variable"
            )
        
        return cls(
            api_key=api_key,
            api_base_url=os.getenv("API_BASE_URL", "https://api.openai.com/v1"),
            model_name=os.getenv("MODEL_NAME", "gpt-4o-mini"),
            proxy_url=os.getenv("PROXY_URL", "http://10.0.2.2:7897"),
            user_data_dir=os.getenv("USER_DATA_DIR", "./browser_data"),
            headless=os.getenv("HEADLESS", "false").lower() == "true",
        )


# ============================================================================
# Enhanced DOM Processor - Token-Efficient
# ============================================================================

class SmartDOMDistiller:
    """
    Интеллектуальный процессор DOM, который:
    1. Удаляет весь шум (scripts, styles, SVG, comments)
    2. Извлекает только интерактивные элементы
    3. Присваивает уникальные ID
    4. Создает компактное представление для LLM
    """
    
    # Элементы, которые нас интересуют
    INTERACTIVE_TAGS = {
        'a', 'button', 'input', 'textarea', 'select', 'option',
        'label', 'form', '[role="button"]', '[onclick]'
    }
    
    # Элементы-шум, которые нужно удалить
    NOISE_TAGS = {
        'script', 'style', 'svg', 'path', 'noscript', 'meta', 'link'
    }
    
    def __init__(self, max_elements: int = 50, max_text_length: int = 500):
        self.max_elements = max_elements
        self.max_text_length = max_text_length
        self.element_map: Dict[int, Dict[str, Any]] = {}
    
    def process_page(self, html: str, page: Page) -> Tuple[str, Dict[int, Dict]]:
        """
        Обработать HTML страницы и вернуть компактное представление.
        
        Returns:
            (dom_text, element_map) - текстовое представление и карта элементов
        """
        soup = BeautifulSoup(html, 'html.parser')
        
        # Шаг 1: Удалить весь шум
        self._remove_noise(soup)
        
        # Шаг 2: Найти все интерактивные элементы
        interactive_elements = self._find_interactive_elements(soup, page)
        
        # Шаг 3: Ограничить количество элементов (token economy)
        interactive_elements = self._prioritize_elements(interactive_elements)
        
        # Шаг 4: Присвоить ID и построить селекторы
        self.element_map = {}
        dom_lines = []
        
        for idx, element_data in enumerate(interactive_elements[:self.max_elements]):
            self.element_map[idx] = element_data
            dom_lines.append(self._format_element(idx, element_data))
        
        # Шаг 5: Добавить контекстную информацию
        page_title = soup.title.string if soup.title else "No title"
        url = page.url
        
        dom_text = f"URL: {url}\nTitle: {page_title}\n\nInteractive Elements:\n"
        dom_text += "\n".join(dom_lines)
        
        return dom_text, self.element_map
    
    def _remove_noise(self, soup: BeautifulSoup):
        """Удалить все нешумовые элементы."""
        # Удалить теги
        for tag in self.NOISE_TAGS:
            for element in soup.find_all(tag):
                element.decompose()
        
        # Удалить комментарии
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()
    
    def _find_interactive_elements(self, soup: BeautifulSoup, page: Page) -> List[Dict]:
        """Найти все интерактивные элементы."""
        elements = []
        
        # Links
        for link in soup.find_all('a', href=True):
            elements.append(self._extract_element_data(link, 'LINK', page))
        
        # Buttons
        for button in soup.find_all('button'):
            elements.append(self._extract_element_data(button, 'BUTTON', page))
        
        # Inputs
        for input_elem in soup.find_all('input'):
            input_type = input_elem.get('type', 'text')
            elements.append(self._extract_element_data(
                input_elem, 
                f'INPUT({input_type})', 
                page
            ))
        
        # Textareas
        for textarea in soup.find_all('textarea'):
            elements.append(self._extract_element_data(textarea, 'TEXTAREA', page))
        
        # Selects
        for select in soup.find_all('select'):
            elements.append(self._extract_element_data(select, 'SELECT', page))
        
        # Elements with role="button"
        for elem in soup.find_all(attrs={'role': 'button'}):
            if elem.name not in ['button', 'a']:  # Avoid duplicates
                elements.append(self._extract_element_data(elem, 'CLICKABLE', page))
        
        # Filter out None values
        elements = [e for e in elements if e is not None]
        
        return elements
    
    def _extract_element_data(self, element, elem_type: str, page: Page) -> Optional[Dict]:
        """Извлечь данные из элемента."""
        try:
            # Build selector
            selector = self._build_selector(element)
            if not selector:
                return None
            
            # Check if element is visible (crucial for filtering)
            try:
                is_visible = page.is_visible(selector, timeout=1000)
            except:
                is_visible = False
            
            # Get text content
            text = element.get_text(strip=True)
            if not text:
                text = element.get('placeholder', '') or element.get('aria-label', '')
            
            # Truncate text
            if len(text) > self.max_text_length:
                text = text[:self.max_text_length] + "..."
            
            # Get attributes
            attrs = {}
            if element.get('href'):
                attrs['href'] = element['href']
            if element.get('value'):
                attrs['value'] = element['value']
            if element.get('name'):
                attrs['name'] = element['name']
            
            return {
                'type': elem_type,
                'text': text,
                'selector': selector,
                'attrs': attrs,
                'visible': is_visible
            }
        except Exception as e:
            logger.debug(f"Failed to extract element: {e}")
            return None
    
    def _build_selector(self, element) -> str:
        """Построить надежный CSS селектор для элемента."""
        # Priority 1: ID (most reliable)
        if element.get('id'):
            return f"#{element['id']}"
        
        # Priority 2: Name (for forms)
        if element.get('name'):
            tag = element.name
            return f"{tag}[name='{element['name']}']"
        
        # Priority 3: Unique attributes
        if element.get('data-testid'):
            return f"[data-testid='{element['data-testid']}']"
        
        # Priority 4: Class + tag combination
        if element.get('class'):
            classes = ' '.join(element['class'])
            return f"{element.name}.{element['class'][0]}"
        
        # Priority 5: XPath-like (last resort)
        # Build path from root
        path_parts = []
        current = element
        
        while current and current.name != '[document]':
            siblings = [s for s in current.parent.children if s.name == current.name] if current.parent else []
            index = siblings.index(current) + 1 if len(siblings) > 1 else 0
            
            if index > 0:
                path_parts.append(f"{current.name}:nth-of-type({index})")
            else:
                path_parts.append(current.name)
            
            current = current.parent
            if len(path_parts) >= 4:  # Limit depth
                break
        
        return " > ".join(reversed(path_parts))
    
    def _prioritize_elements(self, elements: List[Dict]) -> List[Dict]:
        """
        Приоритизировать элементы по важности.
        
        Критерии (в порядке важности):
        1. Visible элементы (видимые на экране)
        2. Интерактивные (buttons, inputs)
        3. С текстом (не пустые)
        """
        def element_score(elem: Dict) -> int:
            score = 0
            
            # Visible = +10
            if elem.get('visible'):
                score += 10
            
            # Interactive types = +5
            if elem['type'] in ['BUTTON', 'INPUT(submit)', 'LINK']:
                score += 5
            
            # Has text = +3
            if elem.get('text'):
                score += 3
            
            # Has href/action = +2
            if elem.get('attrs', {}).get('href'):
                score += 2
            
            return score
        
        # Sort by score (descending)
        elements.sort(key=element_score, reverse=True)
        
        return elements
    
    def _format_element(self, idx: int, element_data: Dict) -> str:
        """Форматировать элемент для LLM."""
        elem_type = element_data['type']
        text = element_data.get('text', '').strip()
        attrs = element_data.get('attrs', {})
        
        # Format: [ID] TYPE: text (attr1=value1, attr2=value2)
        parts = [f"[{idx}]", elem_type]
        
        if text:
            parts.append(f": {text}")
        
        if attrs:
            attr_str = ", ".join(f"{k}={v}" for k, v in attrs.items())
            parts.append(f"({attr_str})")
        
        return " ".join(parts)


# ============================================================================
# Popup and Captcha Detector
# ============================================================================

class PopupDetector:
    """
    Детектор и обработчик попапов/капч.
    
    Обнаруживает:
    - Cookie banners
    - Age verification
    - Newsletters
    - CAPTCHA (и корректно fail если непроходима)
    """
    
    POPUP_PATTERNS = [
        # Cookie banners
        "accept cookie", "accept all", "agree", "got it",
        # Age verification  
        "i am 18", "yes", "enter",
        # Newsletters
        "no thanks", "close", "dismiss", "skip",
        # Generic
        "×", "✕", "[x]", "close button"
    ]
    
    CAPTCHA_INDICATORS = [
        "captcha", "recaptcha", "cloudflare", "verify you are human",
        "security check", "i'm not a robot"
    ]
    
    def detect_overlay(self, page: Page) -> bool:
        """Проверить, есть ли оверлей на странице."""
        try:
            # Check for common overlay patterns
            overlay_selectors = [
                "[class*='modal']",
                "[class*='popup']",
                "[class*='overlay']",
                "[role='dialog']",
                "[class*='cookie']",
                "[class*='banner']"
            ]
            
            for selector in overlay_selectors:
                if page.is_visible(selector, timeout=1000):
                    return True
            
            return False
        except:
            return False
    
    def detect_captcha(self, page: Page) -> bool:
        """Проверить, есть ли CAPTCHA."""
        try:
            html = page.content().lower()
            
            for indicator in self.CAPTCHA_INDICATORS:
                if indicator in html:
                    return True
            
            return False
        except:
            return False
    
    def try_close_popup(self, page: Page) -> bool:
        """
        Попытаться закрыть попап.
        
        Returns:
            True если успешно закрыт, False otherwise
        """
        try:
            html = page.content().lower()
            
            # Try clicking common close patterns
            for pattern in self.POPUP_PATTERNS:
                try:
                    # Try to find by text
                    page.get_by_text(pattern, exact=False).first.click(timeout=2000)
                    logger.info(f"Closed popup using pattern: {pattern}")
                    time.sleep(1)
                    return True
                except:
                    continue
            
            # Try common close button selectors
            close_selectors = [
                "button[aria-label*='close']",
                "button[aria-label*='dismiss']",
                "[class*='close-button']",
                "[class*='dismiss']",
                "button:has-text('×')",
                "button:has-text('✕')"
            ]
            
            for selector in close_selectors:
                try:
                    page.click(selector, timeout=2000)
                    logger.info(f"Closed popup using selector: {selector}")
                    time.sleep(1)
                    return True
                except:
                    continue
            
            return False
        except Exception as e:
            logger.debug(f"Popup close failed: {e}")
            return False


# ============================================================================
# Context Window Manager
# ============================================================================

class ContextManager:
    """
    Управление историей диалога для экономии токенов.
    
    Стратегии:
    - Скользящее окно (последние N сообщений)
    - Сжатие старых сообщений
    - Удаление промежуточных ошибок
    """
    
    def __init__(self, max_turns: int = 10):
        self.max_turns = max_turns
        self.history = deque(maxlen=max_turns * 2)  # user + assistant = 2 messages
    
    def add_message(self, role: str, content: str):
        """Добавить сообщение в историю."""
        self.history.append({"role": role, "content": content})
    
    def get_messages(self) -> List[Dict[str, str]]:
        """Получить сообщения для отправки в LLM."""
        return list(self.history)
    
    def compress_if_needed(self):
        """Сжать историю если она слишком большая."""
        if len(self.history) > self.max_turns * 2:
            # Keep only last max_turns pairs
            self.history = deque(
                list(self.history)[-self.max_turns * 2:],
                maxlen=self.max_turns * 2
            )


# ============================================================================
# LLM Client with Retry Logic
# ============================================================================

class LLMClient:
    """OpenAI-compatible client с retry механизмами."""
    
    def __init__(self, config: Config):
        self.config = config
        
        # Create HTTP client with proxy
        http_client = httpx.Client(
            proxy=config.proxy_url,
            timeout=httpx.Timeout(
                timeout=config.http_timeout,
                connect=config.http_connect_timeout
            )
        )
        
        # Create OpenAI client
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.api_base_url,
            http_client=http_client
        )
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=4, max=30),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError))
    )
    def chat(self, messages: List[Dict[str, str]], system_prompt: str) -> str:
        """
        Отправить запрос в LLM.
        
        Args:
            messages: История диалога
            system_prompt: Системный промпт
        
        Returns:
            Ответ модели (строка)
        """
        all_messages = [{"role": "system", "content": system_prompt}] + messages
        
        logger.info(f"LLM request: {len(messages)} messages, {sum(len(m['content']) for m in messages)} chars")
        
        try:
            response = self.client.chat.completions.create(
                model=self.config.model_name,
                messages=all_messages,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature
            )
            
            reply = response.choices[0].message.content
            
            logger.info(f"LLM response: {len(reply)} chars")
            
            return reply
        
        except Exception as e:
            logger.error(f"LLM API error: {e}")
            raise


# ============================================================================
# Browser Manager with Anti-Detection
# ============================================================================

class BrowserManager:
    """Управление браузером с anti-bot мерами."""
    
    def __init__(self, config: Config):
        self.config = config
        self.playwright = None
        self.context = None
        self.page = None
    
    def __enter__(self):
        """Initialize browser."""
        self.playwright = sync_playwright().start()
        
        # Launch persistent context (saves cookies)
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.config.user_data_dir,
            headless=self.config.headless,
            proxy={"server": self.config.proxy_url},
            viewport={
                'width': self.config.viewport_width,
                'height': self.config.viewport_height
            },
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
            # Anti-detection
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox'
            ]
        )
        
        # Remove webdriver property
        self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        
        # Get first page
        if self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = self.context.new_page()
        
        # Set timeouts
        self.page.set_default_timeout(self.config.action_timeout)
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Cleanup."""
        try:
            if self.context:
                self.context.close()
            if self.playwright:
                self.playwright.stop()
        except:
            pass
    
    def navigate(self, url: str) -> bool:
        """Navigate to URL with retry."""
        try:
            logger.info(f"Navigating to: {url}")
            
            response = self.page.goto(
                url,
                timeout=self.config.page_load_timeout,
                wait_until='domcontentloaded'
            )
            
            # Wait a bit for JS to load
            time.sleep(2)
            
            return response.ok if response else True
        
        except PlaywrightTimeout:
            logger.warning(f"Navigation timeout for {url}")
            return False
        except Exception as e:
            logger.error(f"Navigation error: {e}")
            return False
    
    def get_html(self) -> str:
        """Get current page HTML."""
        return self.page.content()
    
    def get_url(self) -> str:
        """Get current URL."""
        return self.page.url


# ============================================================================
# System Prompt - Optimized for Autonomy
# ============================================================================

SYSTEM_PROMPT = """You are an autonomous web browser agent. Your goal is to accomplish user tasks by interacting with web pages.

## INPUT FORMAT
You receive the current page state as:
```
URL: https://example.com
Title: Page Title

Interactive Elements:
[0] BUTTON: Click Me (type=submit)
[1] INPUT(text): Email Address (name=email)
[2] LINK: Sign Up (href=/register)
...
```

## YOUR TASK
Analyze the page and decide the NEXT SINGLE ACTION to take.

## AVAILABLE ACTIONS

1. **click** - Click an element
   ```json
   {"thought": "...", "action_type": "click", "element_id": 5, "args": {}}
   ```

2. **type** - Type text into an input
   ```json
   {"thought": "...", "action_type": "type", "element_id": 3, "args": {"text": "hello@example.com"}}
   ```

3. **select** - Select dropdown option
   ```json
   {"thought": "...", "action_type": "select", "element_id": 7, "args": {"value": "option1"}}
   ```

4. **scroll** - Scroll page
   ```json
   {"thought": "...", "action_type": "scroll", "args": {"direction": "down"}}
   ```

5. **navigate** - Navigate to URL
   ```json
   {"thought": "...", "action_type": "navigate", "args": {"url": "https://example.com"}}
   ```

6. **wait** - Wait some seconds
   ```json
   {"thought": "...", "action_type": "wait", "args": {"seconds": 3}}
   ```

7. **done** - Task completed successfully
   ```json
   {"thought": "Task is complete because...", "action_type": "done", "args": {}}
   ```

8. **fail** - Cannot complete task
   ```json
   {"thought": "Cannot proceed because...", "action_type": "fail", "args": {"reason": "..."}}
   ```

## CRITICAL RULES

1. **ALWAYS output valid JSON** - No markdown, no extra text
2. **ALWAYS include "thought"** - Explain your reasoning BEFORE acting
3. **ONLY use element IDs from the current page** - Never hallucinate IDs
4. **Think step-by-step** - Don't rush, plan your approach
5. **Handle errors gracefully** - If action fails, try alternative approach
6. **Detect blockers** - If you see CAPTCHA or cannot proceed, use "fail"

## THINKING PROCESS

Before each action, explicitly state:
- What you observe on the page
- What the goal is
- Why this action will help
- What you expect to happen

Example:
```json
{
  "thought": "I see the page has loaded successfully. There's a search input field (element 2) and a search button (element 5). My task is to search for 'Python'. I'll first type into the search box, then click the button.",
  "action_type": "type",
  "element_id": 2,
  "args": {"text": "Python"}
}
```

## RESPONSE FORMAT

ALWAYS respond with ONLY a JSON object:
```json
{
  "thought": "Your detailed reasoning here",
  "action_type": "action_name",
  "element_id": <number> (if applicable),
  "args": {<key>: <value>}
}
```

NO markdown, NO explanations outside JSON, NO excuses. Just pure JSON.
"""


# ============================================================================
# Autonomous Agent - Main Class
# ============================================================================

class AutonomousAgent:
    """
    Полностью автономный агент.
    
    Цикл: Observe → Think → Act
    Никаких хардкод-сценариев, только обобщенная логика.
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.llm_client = LLMClient(config)
        self.dom_processor = SmartDOMDistiller(
            max_elements=config.max_dom_elements,
            max_text_length=config.max_text_length
        )
        self.popup_detector = PopupDetector()
        self.context_manager = ContextManager(max_turns=config.max_history_turns)
        self.browser = None
    
    def run(self, task: str, starting_url: Optional[str] = None) -> bool:
        """
        Выполнить задачу автономно.
        
        Args:
            task: Описание задачи на естественном языке
            starting_url: Начальный URL (опционально)
        
        Returns:
            True если задача выполнена, False otherwise
        """
        logger.info(f"Starting task: {task}")
        
        with BrowserManager(self.config) as browser:
            self.browser = browser
            
            # Navigate to starting URL if provided
            if starting_url:
                if not browser.navigate(starting_url):
                    logger.error("Failed to navigate to starting URL")
                    return False
            
            # Add task to context
            self.context_manager.add_message("user", f"TASK: {task}")
            
            # Main loop: Observe → Think → Act
            for step in range(1, self.config.max_steps + 1):
                logger.info(f"\n{'='*60}\nSTEP {step}/{self.config.max_steps}\n{'='*60}")
                
                # OBSERVE
                observation = self._observe()
                if not observation:
                    logger.error("Failed to observe page")
                    return False
                
                self.context_manager.add_message("user", observation)
                
                # THINK
                decision = self._think()
                if not decision:
                    logger.error("Failed to get decision from LLM")
                    return False
                
                # Log thought process
                logger.info(f"💭 Thought: {decision.get('thought', 'No thought provided')}")
                logger.info(f"🎯 Action: {decision['action_type']}")
                
                # Check for completion
                if decision['action_type'] == 'done':
                    logger.info("✓ Task completed successfully!")
                    return True
                
                if decision['action_type'] == 'fail':
                    reason = decision.get('args', {}).get('reason', 'Unknown')
                    logger.error(f"✗ Task failed: {reason}")
                    return False
                
                # ACT
                result = self._act(decision)
                
                # Report result back to LLM
                if result['success']:
                    logger.info(f"✓ Action successful: {result['message']}")
                    self.context_manager.add_message("user", f"Result: SUCCESS - {result['message']}")
                else:
                    logger.warning(f"✗ Action failed: {result['message']}")
                    self.context_manager.add_message("user", f"Result: FAILED - {result['message']}")
                
                # Human-like delay
                import random
                delay = random.uniform(self.config.min_action_delay, self.config.max_action_delay)
                time.sleep(delay)
                
                # Compress context if needed
                self.context_manager.compress_if_needed()
            
            logger.warning("Reached max steps without completion")
            return False
    
    def _observe(self) -> Optional[str]:
        """
        Наблюдение: Получить состояние страницы.
        
        Включает:
        - Детекцию и закрытие попапов
        - Детекцию CAPTCHA
        - Упрощение DOM
        """
        try:
            # Check for popups/overlays
            if self.config.popup_detection_enabled:
                if self.popup_detector.detect_overlay(self.browser.page):
                    logger.info("Popup detected, attempting to close...")
                    
                    for attempt in range(self.config.max_popup_close_attempts):
                        if self.popup_detector.try_close_popup(self.browser.page):
                            logger.info("Popup closed successfully")
                            time.sleep(1)
                            break
                    else:
                        logger.warning("Failed to close popup after all attempts")
            
            # Check for CAPTCHA
            if self.popup_detector.detect_captcha(self.browser.page):
                logger.error("CAPTCHA detected - cannot proceed automatically")
                return "OBSERVATION: CAPTCHA detected on page. Cannot proceed. Use 'fail' action."
            
            # Get HTML
            html = self.browser.get_html()
            
            # Process DOM
            dom_text, element_map = self.dom_processor.process_page(html, self.browser.page)
            
            # Store element map for action execution
            self.browser._current_element_map = element_map
            
            logger.info(f"Observed {len(element_map)} interactive elements")
            
            return f"OBSERVATION:\n{dom_text}"
        
        except Exception as e:
            logger.error(f"Observation error: {e}")
            return None
    
    def _think(self) -> Optional[Dict]:
        """
        Мышление: Получить решение от LLM.
        
        Returns:
            Decision dict или None если не удалось
        """
        try:
            # Get messages
            messages = self.context_manager.get_messages()
            
            # Call LLM
            response = self.llm_client.chat(messages, SYSTEM_PROMPT)
            
            # Parse response
            decision = self._parse_json(response)
            
            if not decision:
                logger.error("Failed to parse LLM response")
                logger.debug(f"Raw response: {response}")
                return None
            
            # Validate decision
            if not self._validate_decision(decision):
                logger.error("Invalid decision from LLM")
                return None
            
            # Add decision to context
            self.context_manager.add_message("assistant", json.dumps(decision))
            
            return decision
        
        except Exception as e:
            logger.error(f"Thinking error: {e}")
            return None
    
    def _act(self, decision: Dict) -> Dict:
        """
        Действие: Выполнить решение.
        
        Returns:
            {'success': bool, 'message': str}
        """
        action_type = decision['action_type']
        args = decision.get('args', {})
        element_id = decision.get('element_id')
        
        try:
            if action_type == 'click':
                return self._action_click(element_id)
            
            elif action_type == 'type':
                return self._action_type(element_id, args.get('text', ''))
            
            elif action_type == 'select':
                return self._action_select(element_id, args.get('value', ''))
            
            elif action_type == 'scroll':
                return self._action_scroll(args.get('direction', 'down'))
            
            elif action_type == 'navigate':
                return self._action_navigate(args.get('url', ''))
            
            elif action_type == 'wait':
                return self._action_wait(args.get('seconds', 2))
            
            else:
                return {'success': False, 'message': f"Unknown action: {action_type}"}
        
        except Exception as e:
            logger.error(f"Action execution error: {e}")
            return {'success': False, 'message': str(e)}
    
    # Action implementations
    
    def _action_click(self, element_id: int) -> Dict:
        """Click an element."""
        try:
            element_map = getattr(self.browser, '_current_element_map', {})
            
            if element_id not in element_map:
                return {'success': False, 'message': f"Element {element_id} not found"}
            
            element = element_map[element_id]
            selector = element['selector']
            
            # Try regular click
            try:
                self.browser.page.click(selector, timeout=self.config.action_timeout)
            except:
                # Fallback: JS click
                self.browser.page.evaluate(f"""
                    document.querySelector('{selector.replace("'", "\\'")}')?.click()
                """)
            
            time.sleep(1)
            
            return {'success': True, 'message': f"Clicked {element['type']}: {element['text'][:50]}"}
        
        except Exception as e:
            return {'success': False, 'message': f"Click failed: {e}"}
    
    def _action_type(self, element_id: int, text: str) -> Dict:
        """Type into an element."""
        try:
            element_map = getattr(self.browser, '_current_element_map', {})
            
            if element_id not in element_map:
                return {'success': False, 'message': f"Element {element_id} not found"}
            
            element = element_map[element_id]
            selector = element['selector']
            
            # Clear and type
            self.browser.page.fill(selector, '', timeout=self.config.action_timeout)
            self.browser.page.type(
                selector,
                text,
                timeout=self.config.action_timeout,
                delay=self.config.typing_delay
            )
            
            time.sleep(0.5)
            
            return {'success': True, 'message': f"Typed '{text[:30]}...' into {element['type']}"}
        
        except Exception as e:
            return {'success': False, 'message': f"Type failed: {e}"}
    
    def _action_select(self, element_id: int, value: str) -> Dict:
        """Select dropdown option."""
        try:
            element_map = getattr(self.browser, '_current_element_map', {})
            
            if element_id not in element_map:
                return {'success': False, 'message': f"Element {element_id} not found"}
            
            element = element_map[element_id]
            selector = element['selector']
            
            self.browser.page.select_option(selector, value, timeout=self.config.action_timeout)
            
            time.sleep(0.5)
            
            return {'success': True, 'message': f"Selected '{value}'"}
        
        except Exception as e:
            return {'success': False, 'message': f"Select failed: {e}"}
    
    def _action_scroll(self, direction: str) -> Dict:
        """Scroll the page."""
        try:
            if direction == 'down':
                self.browser.page.evaluate("window.scrollBy(0, window.innerHeight)")
            else:
                self.browser.page.evaluate("window.scrollBy(0, -window.innerHeight)")
            
            time.sleep(1)
            
            return {'success': True, 'message': f"Scrolled {direction}"}
        
        except Exception as e:
            return {'success': False, 'message': f"Scroll failed: {e}"}
    
    def _action_navigate(self, url: str) -> Dict:
        """Navigate to URL."""
        try:
            success = self.browser.navigate(url)
            
            if success:
                return {'success': True, 'message': f"Navigated to {url}"}
            else:
                return {'success': False, 'message': f"Navigation failed"}
        
        except Exception as e:
            return {'success': False, 'message': f"Navigate error: {e}"}
    
    def _action_wait(self, seconds: int) -> Dict:
        """Wait for specified seconds."""
        try:
            time.sleep(int(seconds))
            return {'success': True, 'message': f"Waited {seconds}s"}
        
        except Exception as e:
            return {'success': False, 'message': f"Wait failed: {e}"}
    
    # Utility methods
    
    def _parse_json(self, response: str) -> Optional[Dict]:
        """Parse JSON from LLM response (robust)."""
        try:
            # Clean markdown
            response = response.strip()
            
            # Remove markdown code blocks
            if '```json' in response:
                response = response.split('```json')[1].split('```')[0]
            elif '```' in response:
                response = response.split('```')[1].split('```')[0]
            
            # Extract JSON object
            if '{' in response:
                start = response.index('{')
                end = response.rindex('}') + 1
                response = response[start:end]
            
            # Parse
            return json.loads(response)
        
        except Exception as e:
            logger.debug(f"JSON parse error: {e}")
            
            # Fallback: regex extraction
            try:
                decision = {}
                
                # Extract thought
                thought_match = re.search(r'"thought"\s*:\s*"([^"]*)"', response, re.DOTALL)
                if thought_match:
                    decision['thought'] = thought_match.group(1)
                
                # Extract action_type
                action_match = re.search(r'"action_type"\s*:\s*"([^"]*)"', response)
                if action_match:
                    decision['action_type'] = action_match.group(1)
                
                # Extract element_id
                element_match = re.search(r'"element_id"\s*:\s*(\d+)', response)
                if element_match:
                    decision['element_id'] = int(element_match.group(1))
                
                # Extract args
                args_match = re.search(r'"args"\s*:\s*\{([^}]*)\}', response, re.DOTALL)
                if args_match:
                    decision['args'] = {}
                    for param in re.finditer(r'"(\w+)"\s*:\s*"([^"]*)"', args_match.group(1)):
                        decision['args'][param.group(1)] = param.group(2)
                else:
                    decision['args'] = {}
                
                if 'action_type' in decision:
                    return decision
                
            except:
                pass
            
            return None
    
    def _validate_decision(self, decision: Dict) -> bool:
        """Validate decision from LLM."""
        # Check required fields
        if 'action_type' not in decision:
            logger.error("Missing action_type")
            return False
        
        if 'thought' not in decision:
            logger.warning("Missing thought (should always reason first)")
        
        action_type = decision['action_type']
        
        # Validate element_id if needed
        if action_type in ['click', 'type', 'select']:
            element_id = decision.get('element_id')
            
            if element_id is None:
                logger.error(f"Action {action_type} requires element_id")
                return False
            
            element_map = getattr(self.browser, '_current_element_map', {})
            
            if element_id not in element_map:
                logger.error(f"Element {element_id} does not exist (available: {list(element_map.keys())})")
                return False
        
        return True


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Main entry point."""
    print("\n" + "="*70)
    print("   COGNIWEB AGENT v3.0 - FULLY AUTONOMOUS")
    print("="*70 + "\n")
    
    try:
        config = Config.from_env()
        logger.info("✓ Configuration loaded")
        logger.info(f"  API: {config.api_base_url}")
        logger.info(f"  Model: {config.model_name}")
        logger.info(f"  Proxy: {config.proxy_url}")
        logger.info(f"  Max steps: {config.max_steps}")
        logger.info(f"  Token optimization: {config.max_dom_elements} elements, {config.max_text_length} chars")
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        print("\n❌ Please set OPENAI_API_KEY or LLM_API_KEY environment variable")
        return 1
    
    print("\n" + "-"*70)
    task = input("📝 Enter task description: ").strip()
    
    if not task:
        print("No task provided. Using demo task...")
        task = "Go to google.com and search for 'autonomous web agents'"
    
    starting_url = input("🌐 Enter starting URL (optional): ").strip() or None
    
    print("-"*70 + "\n")
    
    try:
        agent = AutonomousAgent(config)
        success = agent.run(task, starting_url)
        
        if success:
            print("\n" + "="*70)
            print("✓✓✓ TASK COMPLETED SUCCESSFULLY! ✓✓✓")
            print("="*70 + "\n")
            return 0
        else:
            print("\n" + "="*70)
            print("✗✗✗ TASK FAILED ✗✗✗")
            print("="*70 + "\n")
            return 1
    
    except KeyboardInterrupt:
        print("\n\n⚠ Task interrupted by user")
        return 130
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    exit(main())