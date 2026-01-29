#!/usr/bin/env python3
"""
Autonomous Browser Agent v2.0 - PRODUCTION GRADE (OPTIMIZED)
Designed for harsh real-world conditions:
- Slow proxy connections
- Anti-bot systems (Gmail, Google, etc.)
- Broken JSON responses
- Network timeouts
- Limited DOM visibility
"""

import os
import json
import time
import re
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from pathlib import Path

# Load environment variables FIRST
from dotenv import load_dotenv
load_dotenv()

# Third-party imports
import httpx
from openai import OpenAI
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, BrowserContext, Error as PlaywrightError
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
    """Configuration optimized for slow/unstable connections."""
    
    # API Configuration
    api_key: str
    api_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4"
    
    # Proxy Configuration
    proxy_url: str = "http://10.0.2.2:7897"
    
    # Browser Configuration
    user_data_dir: str = "./browser_data"
    headless: bool = False
    
    # Agent Configuration
    max_steps: int = 25  # Увеличено для медленных условий
    page_load_timeout: int = 90000  # 90 секунд (было 60)
    action_timeout: int = 60000     # 30 секунд (было 20)
    
    # LLM Configuration
    max_tokens: int = 2000  # Увеличено для более подробных ответов
    temperature: float = 0.1  # Ниже для более детерминированного поведения
    
    # HTTP Timeouts (для очень медленных соединений)
    http_timeout: float = 120.0  # 2 минуты
    
    # Human-like delays (анти-бот)
    min_action_delay: float = 5.0  # Минимальная пауза между действиями
    max_action_delay: float = 20.0  # Максимальная пауза
    typing_delay: int = 150  # Задержка между нажатиями клавиш (мс)
    
    # Retry settings
    max_json_retries: int = 5  # Максимум попыток парсинга JSON
    max_empty_response_retries: int = 3  # Попытки при пустом ответе
    
    @classmethod
    def from_env(cls) -> 'Config':
        """Load configuration from environment variables."""
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
        if not api_key:
            raise ValueError(
                "API key must be set in OPENAI_API_KEY or LLM_API_KEY environment variable."
            )
        
        return cls(
            api_key=api_key,
            api_base_url=os.getenv("API_BASE_URL", cls.api_base_url),
            model_name=os.getenv("MODEL_NAME", cls.model_name),
            proxy_url=os.getenv("PROXY_URL", cls.proxy_url),
            user_data_dir=os.getenv("USER_DATA_DIR", cls.user_data_dir),
            headless=os.getenv("HEADLESS", "false").lower() == "true",
            http_timeout=float(os.getenv("HTTP_TIMEOUT", "120.0")),
        )


# ============================================================================
# Super-Robust JSON Parser
# ============================================================================

class RobustJSONParser:
    """
    Парсер JSON, который справляется с любым мусором от LLM.
    Обрабатывает:
    - Markdown code blocks (```json ... ```)
    - Пустые ответы
    - Текст до/после JSON
    - Битый JSON с восстановлением
    """
    
    @staticmethod
    def clean_response(response: str) -> str:
        """Очистить ответ от Markdown и лишнего текста."""
        response = response.strip()
        
        # Удаляем markdown code blocks
        # Варианты: ```json ... ```, ```JSON ... ```, ``` ... ```
        patterns = [
            r'```json\s*(.*?)\s*```',
            r'```JSON\s*(.*?)\s*```',
            r'```\s*(.*?)\s*```',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, response, re.DOTALL | re.IGNORECASE)
            if match:
                response = match.group(1).strip()
                break
        
        # Удаляем возможный текст до первой {
        if '{' in response:
            start = response.index('{')
            response = response[start:]
        
        # Удаляем возможный текст после последней }
        if '}' in response:
            end = response.rindex('}') + 1
            response = response[:end]
        
        return response
    
    @staticmethod
    def attempt_fix_json(json_str: str) -> str:
        """Попытаться исправить битый JSON."""
        # Исправление 1: Добавить недостающие кавычки
        # "thought: "..." → "thought": "..."
        json_str = re.sub(r'(\w+):\s*(["\'])', r'"\1": \2', json_str)
        
        # Исправление 2: Убрать trailing commas
        json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)
        
        # Исправление 3: Исправить одинарные кавычки на двойные
        # Но только вокруг ключей и строковых значений
        json_str = json_str.replace("'", '"')
        
        return json_str
    
    @classmethod
    def parse(cls, response: str, max_attempts: int = 3) -> Optional[Dict[str, Any]]:
        """
        Парсинг JSON с несколькими попытками исправления.
        
        Returns:
            Словарь или None если не удалось распарсить
        """
        if not response or not response.strip():
            logger.error("Empty response from LLM")
            return None
        
        # Попытка 1: Очистить от Markdown
        cleaned = cls.clean_response(response)
        
        # Попытка 2: Распарсить как есть
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse failed (attempt 1): {e}")
        
        # Попытка 3: Исправить и распарсить
        try:
            fixed = cls.attempt_fix_json(cleaned)
            return json.loads(fixed)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse failed (attempt 2): {e}")
        
        # Попытка 4: Регулярками вытащить поля
        try:
            decision = {}
            
            # Извлечь thought
            thought_match = re.search(r'"thought"\s*:\s*"([^"]*)"', response, re.DOTALL)
            if thought_match:
                decision['thought'] = thought_match.group(1)
            
            # Извлечь action_type
            action_match = re.search(r'"action_type"\s*:\s*"([^"]*)"', response)
            if action_match:
                decision['action_type'] = action_match.group(1)
            
            # Извлечь element_id
            element_match = re.search(r'"element_id"\s*:\s*(\d+)', response)
            if element_match:
                decision['element_id'] = int(element_match.group(1))
            
            # Извлечь args
            args_match = re.search(r'"args"\s*:\s*\{([^}]*)\}', response, re.DOTALL)
            if args_match:
                args_str = args_match.group(1)
                decision['args'] = {}
                
                # Извлечь каждый параметр в args
                for param_match in re.finditer(r'"(\w+)"\s*:\s*"([^"]*)"', args_str):
                    decision['args'][param_match.group(1)] = param_match.group(2)
            else:
                decision['args'] = {}
            
            if 'action_type' in decision:
                logger.info("✓ Regex extraction successful")
                return decision
        except Exception as e:
            logger.error(f"Regex extraction failed: {e}")
        
        return None


# ============================================================================
# LLM Client with Proxy Support
# ============================================================================

class LLMClient:
    """
    LLM client with proxy support and retry logic.
    Optimized for slow connections and rate limits.
    """
    
    def __init__(self, config: Config):
        self.config = config
        
        # HTTP client with proxy
        if config.proxy_url:
            http_client = httpx.Client(
                proxy=config.proxy_url,
                timeout=config.http_timeout,
                verify=False  # Для development, в продакшене убрать!
            )
        else:
            http_client = httpx.Client(
                timeout=config.http_timeout
            )
        
        # OpenAI client
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.api_base_url,
            http_client=http_client,
        )
    
    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def chat(self, messages: List[Dict[str, str]]) -> str:
        """
        Send chat request with retry logic.
        
        Args:
            messages: Chat history
            
        Returns:
            LLM response text
        """
        try:
            response = self.client.chat.completions.create(
                model=self.config.model_name,
                messages=messages,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
            )
            
            content = response.choices[0].message.content
            
            if not content:
                logger.warning("Empty response from LLM")
                return ""
            
            return content.strip()
        
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning("Rate limit (429), retrying...")
                raise  # Tenacity will retry
            logger.error(f"HTTP error {e.response.status_code}: {e}")
            raise
        
        except Exception as e:
            logger.error(f"LLM request failed: {e}")
            raise


# ============================================================================
# UPDATED SYSTEM PROMPT (OPTIMIZED)
# ============================================================================

SYSTEM_PROMPT = """You are an autonomous browser agent. You see web pages as simplified DOM snapshots.

AVAILABLE ACTIONS:
1. click - Click element (use element_id)
2. type - Type text into input (requires element_id and "text" in args)
3. select - Select dropdown option (requires element_id and "value" in args)
4. scroll - Scroll page ("direction": "down" or "up")
5. navigate - Go to URL directly (requires "url" in args)
6. wait - Wait N seconds (requires "seconds" in args)
7. Maps - Navigate to URL fragment or internal route (requires "url" in args)
8. done - Task completed (requires "summary" in args)

CRITICAL NAVIGATION RULES:
- If clicking navigation elements (tabs, menu items) does NOT change URL or DOM after 2 attempts, use Maps action with direct URL.
- Example: In Gmail, if clicking "Promotions" tab fails twice, use Maps with url="#promotions" or url="https://mail.google.com/mail/u/0/#promotions"
- Maps action bypasses DOM interaction and navigates directly to internal routes

ELEMENT SELECTION PRIORITY:
- Interactive elements (buttons, inputs, checkboxes) are prioritized in the element list
- Use ARIA labels (aria-label) and placeholders to identify Gmail-specific elements
- Text content and visible labels are the most reliable identifiers

RESPONSE FORMAT (strict JSON):
{
  "thought": "reasoning about next step",
  "action_type": "click|type|select|scroll|navigate|wait|Maps|done",
  "element_id": 123,  // optional, for click/type/select
  "args": {
    "text": "...",      // for type
    "value": "...",     // for select
    "url": "...",       // for navigate/Maps
    "direction": "...", // for scroll
    "seconds": 3,       // for wait
    "summary": "..."    // for done
  }
}

BEST PRACTICES:
- When elements are hard to click, try scrolling first to bring them into view
- If click fails, try wait action then retry
- Use Maps for direct navigation when normal clicks fail repeatedly
- Always prefer elements with clear aria-label or placeholder attributes
"""


# ============================================================================
# OPTIMIZED DOM Processor
# ============================================================================

class PageProcessor:
    """
    РАДИКАЛЬНО ОПТИМИЗИРОВАННЫЙ процессор DOM-дерева.
    
    Улучшения:
    1. Полная очистка HTML от script, style, svg, path, link, meta, noscript
    2. Приоритетная очередь: сначала интерактивные элементы, потом ссылки
    3. Поддержка ARIA атрибутов
    4. Стабильные селекторы (id > aria-label > placeholder > text)
    """
    
    def __init__(self, browser_manager=None):
        self.browser = browser_manager
    
    def process_page(self, page: Page) -> Dict[str, Any]:
        """
        Обрабатывает страницу с РАДИКАЛЬНОЙ экономией токенов.
        
        Returns:
            Dict with keys: url, title, summary, element_count, element_map
        """
        try:
            # Базовая информация
            url = page.url
            title = page.title() or "No title"
            
            # Получаем HTML с радикальной очисткой
            html = page.content()
            
            # КРИТИЧНО: Удаляем все токено-жрущие теги ДО парсинга
            soup = BeautifulSoup(html, 'html.parser')
            
            # Удаляем мусорные теги полностью
            for tag in soup.find_all(['script', 'style', 'svg', 'path', 'link', 'meta', 'noscript']):
                tag.decompose()
            
            # ПРИОРИТЕТНАЯ ОЧЕРЕДЬ ЭЛЕМЕНТОВ
            priority_elements = []  # Buttons, inputs, checkboxes
            secondary_elements = []  # Links
            
            # Собираем ПРИОРИТЕТНЫЕ элементы (интерактивные)
            interactive_tags = ['button', 'input', 'select', 'textarea']
            for tag_name in interactive_tags:
                for elem in soup.find_all(tag_name):
                    if not self._is_visible_element(elem):
                        continue
                    priority_elements.append(elem)
            
            # Собираем элементы с ARIA ролями (Gmail!)
            aria_roles = ['button', 'checkbox', 'menuitem', 'tab', 'option']
            for role in aria_roles:
                for elem in soup.find_all(attrs={'role': role}):
                    if not self._is_visible_element(elem):
                        continue
                    if elem not in priority_elements:  # Избегаем дубликатов
                        priority_elements.append(elem)
            
            # Собираем ВТОРОСТЕПЕННЫЕ элементы (ссылки)
            for a_tag in soup.find_all('a', href=True):
                if self._is_visible_element(a_tag):
                    secondary_elements.append(a_tag)
            
            # Формируем финальный список: сначала приоритетные, потом дополняем ссылками
            MAX_ELEMENTS = 500
            final_elements = priority_elements[:MAX_ELEMENTS]
            
            if len(final_elements) < MAX_ELEMENTS:
                remaining_slots = MAX_ELEMENTS - len(final_elements)
                final_elements.extend(secondary_elements[:remaining_slots])
            
            # Создаем описания элементов
            element_map = {}
            element_descriptions = []
            
            for idx, elem in enumerate(final_elements):
                elem_id = idx
                
                # Строим СТАБИЛЬНЫЙ селектор
                selector = self._build_selector(elem)
                
                # Получаем расширенное описание (с ARIA и placeholder)
                desc = self._get_element_description(elem, elem_id)
                
                element_map[elem_id] = {
                    'element': elem,
                    'selector': selector,
                    'type': elem.name,
                    'text': desc
                }
                
                element_descriptions.append(desc)
            
            # Сохраняем карту элементов в браузере для последующего использования
            if self.browser:
                self.browser._current_element_map = element_map
            
            # Формируем компактное представление страницы
            page_summary = f"""URL: {url}
Title: {title}
Interactive Elements: {len(priority_elements)}
Links: {len(secondary_elements)}
Total visible: {len(final_elements)}

Elements:
""" + "\n".join(element_descriptions[:MAX_ELEMENTS])
            
            logger.info(f"Page processed: {len(final_elements)} elements (priority: {len(priority_elements)}, links: {len(secondary_elements)})")
            
            return {
                'url': url,
                'title': title,
                'summary': page_summary,
                'element_count': len(final_elements),
                'element_map': element_map
            }
        
        except Exception as e:
            logger.error(f"Page processing error: {e}", exc_info=True)
            return {
                'url': page.url if page else "unknown",
                'title': "Error",
                'summary': f"Failed to process page: {e}",
                'element_count': 0,
                'element_map': {}
            }
    
    def _get_element_description(self, elem, elem_id: int) -> str:
        """
        Создает расширенное описание элемента с поддержкой ARIA.
        
        Приоритеты описания:
        1. aria-label (самое надежное для Gmail)
        2. placeholder (для input полей)
        3. value (для button/input)
        4. text_content (видимый текст)
        5. title (подсказка)
        6. type (для input)
        """
        parts = [f"[{elem_id}]", elem.name.upper()]
        
        # ARIA атрибуты (приоритет!)
        if elem.get('aria-label'):
            parts.append(f'aria="{elem["aria-label"][:50]}"')
        
        if elem.get('role'):
            parts.append(f'role={elem["role"]}')
        
        # Placeholder (для input)
        if elem.get('placeholder'):
            parts.append(f'placeholder="{elem["placeholder"][:50]}"')
        
        # Value (для button/input)
        if elem.get('value'):
            parts.append(f'value="{elem["value"][:50]}"')
        
        # Текстовое содержимое
        text = elem.get_text(strip=True)
        if text:
            parts.append(f'text="{text[:50]}"')
        
        # Title attribute
        if elem.get('title'):
            parts.append(f'title="{elem["title"][:50]}"')
        
        # Тип (для input)
        if elem.name == 'input' and elem.get('type'):
            parts.append(f'type={elem["type"]}')
        
        # Href (для ссылок)
        if elem.name == 'a' and elem.get('href'):
            href = elem['href'][:50]
            parts.append(f'href={href}')
        
        return " ".join(parts)
    
    def _build_selector(self, elem) -> str:
        """
        Построить СТАБИЛЬНЫЙ селектор для элемента.
        
        Приоритеты:
        1. ID (самый стабильный)
        2. aria-label (для Gmail)
        3. placeholder (для input)
        4. name attribute
        5. Комбинация tag + text (fallback)
        """
        # Приоритет 1: ID
        if elem.get('id'):
            return f"#{elem['id']}"
        
        # Приоритет 2: ARIA label
        if elem.get('aria-label'):
            aria = elem['aria-label'].replace('"', '\\"')
            return f'[aria-label="{aria}"]'
        
        # Приоритет 3: Placeholder
        if elem.get('placeholder'):
            placeholder = elem['placeholder'].replace('"', '\\"')
            return f'[placeholder="{placeholder}"]'
        
        # Приоритет 4: Name
        if elem.get('name'):
            return f'[name="{elem["name"]}"]'
        
        # Приоритет 5: Role
        if elem.get('role'):
            role = elem['role']
            text = elem.get_text(strip=True)[:30]
            if text:
                text_escaped = text.replace('"', '\\"')
                return f'[role="{role}"]:has-text("{text_escaped}")'
            return f'[role="{role}"]'
        
        # Fallback: Tag + текст (может быть нестабильным!)
        text = elem.get_text(strip=True)[:30]
        if text:
            text_escaped = text.replace('"', '\\"')
            return f'{elem.name}:has-text("{text_escaped}")'
        
        # Последний вариант: просто тег (очень нестабильно)
        return elem.name
    
    def _is_visible_element(self, elem) -> bool:
        """
        Проверяет, является ли элемент видимым.
        
        Фильтрует:
        - Скрытые элементы (display: none, visibility: hidden)
        - Элементы без текста и без интерактивных атрибутов
        - Элементы с нулевыми размерами
        """
        # Проверка стиля
        style = elem.get('style', '')
        if 'display:none' in style.replace(' ', '') or 'visibility:hidden' in style.replace(' ', ''):
            return False
        
        # Скрытые по aria-hidden
        if elem.get('aria-hidden') == 'true':
            return False
        
        # Для интерактивных элементов - всегда видимы
        if elem.name in ['button', 'input', 'select', 'textarea']:
            return True
        
        # Для элементов с role - всегда видимы
        if elem.get('role'):
            return True
        
        # Для остальных - должен быть текст или href
        text = elem.get_text(strip=True)
        if not text and not elem.get('href'):
            return False
        
        return True


# ============================================================================
# Browser Manager (Playwright)
# ============================================================================

class BrowserManager:
    """
    Manages Playwright browser with persistent context.
    Optimized for slow connections and anti-bot protection.
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._current_element_map = {}
    
    def start(self):
        """Start browser with persistent context."""
        try:
            self.playwright = sync_playwright().start()
            
            # Browser arguments for stealth
            browser_args = [
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
            ]
            
            # Proxy settings
            proxy_settings = None
            if self.config.proxy_url:
                proxy_settings = {"server": self.config.proxy_url}
            
            # Launch persistent context (сохраняет cookies между запусками)
            self.context = self.playwright.chromium.launch_persistent_context(
                
                user_data_dir = "/home/vboxuser/projects/CogniWeb_Agent/agent_profile",
                headless=self.config.headless,
                args=browser_args,
                proxy=proxy_settings,
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0',
            )
            
            # Set default timeouts
            self.context.set_default_timeout(self.config.page_load_timeout)
            self.context.set_default_navigation_timeout(self.config.page_load_timeout)
            
            # Get/create page
            if len(self.context.pages) > 0:
                self.page = self.context.pages[0]
            else:
                self.page = self.context.new_page()
            
            logger.info("✓ Browser started")
            
        except Exception as e:
            logger.error(f"Failed to start browser: {e}")
            raise
    
    def stop(self):
        """Stop browser gracefully."""
        try:
            if self.context:
                self.context.close()
            if self.playwright:
                self.playwright.stop()
            logger.info("✓ Browser stopped")
        except Exception as e:
            logger.warning(f"Browser stop error: {e}")
    
    def navigate(self, url: str) -> bool:
        """Navigate to URL with error handling."""
        try:
            logger.info(f"Navigating to: {url}")
            
            # Navigate with increased timeout
            response = self.page.goto(
                url,
                wait_until='domcontentloaded',
                timeout=self.config.page_load_timeout
            )
            
            if response and response.ok:
                logger.info(f"✓ Navigation successful: {self.page.url}")
                return True
            else:
                status = response.status if response else "unknown"
                logger.warning(f"Navigation returned status: {status}")
                return False
        
        except PlaywrightError as e:
            logger.error(f"Navigation failed: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected navigation error: {e}")
            return False


# ============================================================================
# Agent (Main Orchestrator)
# ============================================================================

class Agent:
    """
    Main agent orchestrating the Observe → Think → Act loop.
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.llm_client = LLMClient(config)
        self.browser = None
        self.page_processor = None
    
    def run(self, task: str) -> bool:
        """
        Run agent with exponential backoff for rate limits.
        
        Добавлена обработка 429 ошибок с экспоненциальным бэкоффом.
        """
        logger.info(f"\n{'='*70}")
        logger.info(f"STARTING TASK: {task}")
        logger.info(f"{'='*70}\n")
        
        self.browser = BrowserManager(self.config)
        
        try:
            self.browser.start()
            
            # Инициализация PageProcessor с ссылкой на browser
            self.page_processor = PageProcessor(browser_manager=self.browser)
            
            logger.info("✓ Browser started")
            
            # Начинаем с homepage (если нужно)
            if not self.browser.page.url.startswith('http'):
                self.browser.navigate("about:blank")
            
            conversation_history = []
            retry_delay = 1.0  # Начальная задержка для бэкоффа
            max_retry_delay = 40.0  # Максимальная задержка
            
            for step in range(self.config.max_steps):
                logger.info(f"\n--- Step {step + 1}/{self.config.max_steps} ---")
                
                try:
                    # 1. OBSERVE: Process page
                    page_data = self.page_processor.process_page(self.browser.page)
                    
                    # 2. THINK: Get LLM decision
                    decision = self._get_llm_decision(task, page_data, conversation_history)
                    
                    if not decision:
                        logger.error("No valid decision from LLM")
                        continue
                    
                    # Reset retry delay on success
                    retry_delay = 1.0
                    
                    # 3. ACT: Execute action
                    result = self.execute_action(decision)
                    
                    # Update history
                    conversation_history.append({
                        'step': step + 1,
                        'thought': decision.get('thought', ''),
                        'action': decision.get('action_type', ''),
                        'result': result
                    })
                    
                    # Check if done
                    if result.get('done'):
                        logger.info("✓ Task completed!")
                        return True
                    
                    # Проверяем успешность действия
                    if not result.get('success'):
                        logger.warning(f"Action failed: {result.get('message')}")
                
                except Exception as e:
                    error_message = str(e)
                    
                    # КРИТИЧНО: Обработка Rate Limit (429)
                    if '429' in error_message or 'rate limit' in error_message.lower():
                        logger.warning(f"Rate limit hit! Backing off for {retry_delay}s")
                        time.sleep(retry_delay)
                        
                        # Экспоненциальный бэкофф
                        retry_delay = min(retry_delay * 2, max_retry_delay)
                        continue
                    
                    # Обработка других ошибок
                    logger.error(f"Step error: {e}", exc_info=True)
                    
                    # Добавляем информацию об ошибке в историю
                    conversation_history.append({
                        'step': step + 1,
                        'error': str(e)
                    })
            
            logger.warning(f"Max steps ({self.config.max_steps}) reached")
            return False
        
        finally:
            if self.browser:
                self.browser.stop()
                logger.info("✓ Browser stopped")
    
    def _get_llm_decision(
        self,
        task: str,
        page_data: Dict[str, Any],
        history: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Get decision from LLM with robust parsing."""
        
        # Build prompt with page summary
        page_summary = page_data.get('summary', 'No page data')
        
        user_prompt = f"""
TASK: {task}

CURRENT PAGE:
{page_summary}

HISTORY (last 3 steps):
{self._format_history(history[-3:])}

What is your next action? Respond ONLY with JSON.
"""
        
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ]
        
        # Try multiple times with different strategies
        for attempt in range(self.config.max_json_retries):
            try:
                response = self.llm_client.chat(messages)
                
                if not response:
                    logger.warning(f"Empty response (attempt {attempt + 1})")
                    continue
                
                logger.info(f"LLM response (attempt {attempt + 1}):\n{response[:200]}...")
                
                # Parse with robust parser
                decision = RobustJSONParser.parse(response)
                
                if decision and 'action_type' in decision:
                    # Validate element_id if present
                    element_id = decision.get('element_id')
                    if element_id is not None:
                        element_map = page_data.get('element_map', {})
                        if element_id not in element_map:
                            logger.warning(f"Element {element_id} not in map, retrying...")
                            messages.append({"role": "assistant", "content": response})
                            messages.append({
                                "role": "user",
                                "content": f"ERROR: Element {element_id} does not exist. Available elements: 0-{len(element_map)-1}. Try again."
                            })
                            continue
                    
                    logger.info(f"✓ Valid decision: {decision.get('action_type')}")
                    return decision
                else:
                    logger.warning("Invalid decision format")
            
            except Exception as e:
                logger.error(f"Decision error (attempt {attempt + 1}): {e}")
        
        logger.error("Failed to get valid decision after max retries")
        return None
    
    def _format_history(self, history: List[Dict[str, Any]]) -> str:
        """Format conversation history."""
        if not history:
            return "No previous actions"
        
        lines = []
        for item in history:
            step = item.get('step', '?')
            action = item.get('action', 'unknown')
            result = item.get('result', {})
            success = "✓" if result.get('success') else "✗"
            message = result.get('message', '')
            
            lines.append(f"Step {step}: {action} {success} - {message}")
        
        return "\n".join(lines)
    
    def execute_action(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Execute agent decision with Maps support."""
        try:
            action_type = decision.get('action_type')
            element_id = decision.get('element_id')
            args = decision.get('args', {})
            
            logger.info(f"Action: {action_type}, Element: {element_id}, Args: {args}")
            
            if action_type == 'done':
                return {'success': True, 'message': 'Task completed', 'done': True}
            elif action_type == 'click':
                return self._action_click(element_id)
            elif action_type == 'type':
                return self._action_type(element_id, args.get('text', ''))
            elif action_type == 'select':
                return self._action_select(element_id, args.get('value', ''))
            elif action_type == 'scroll':
                return self._action_scroll(args.get('direction', 'down'))
            elif action_type == 'navigate':
                return self._action_navigate(args.get('url', ''))
            elif action_type == 'Maps':  # НОВОЕ ДЕЙСТВИЕ
                return self._action_navigate(args.get('url', ''))
            elif action_type == 'wait':
                return self._action_wait(args.get('seconds', 3))
            else:
                return {'success': False, 'message': f"Unknown action: {action_type}"}
        except Exception as e:
            return {'success': False, 'message': f"Action error: {e}"}
    
    def _action_click(self, element_id: int) -> Dict[str, Any]:
        """Click with IMPROVED selector stability and JS injection."""
        try:
            element_id = int(element_id)
        except (ValueError, TypeError):
            return {'success': False, 'message': f"Invalid element_id: {element_id}"}
        
        if not hasattr(self.browser, '_current_element_map'):
            return {'success': False, 'message': "No element map"}
        
        element_map = self.browser._current_element_map
        
        if element_id not in element_map:
            return {'success': False, 'message': f"Element {element_id} not found"}
        
        element = element_map[element_id]
        selector = element['selector']
        
        logger.info(f"Clicking: {element['text'][:50]}")
        
        try:
            # ОПТИМИЗАЦИЯ: Прокрутка к элементу перед кликом
            try:
                self.browser.page.locator(selector).scroll_into_view_if_needed(
                    timeout=5000
                )
            except Exception as scroll_e:
                logger.warning(f"Scroll to element failed: {scroll_e}")
            
            # Human-like: mouse move + pause before click
            import random
            time.sleep(random.uniform(0.3, 0.7))
            
            # ОПТИМИЗАЦИЯ: Попытка клика с JS, если обычный клик не работает
            try:
                self.browser.page.click(selector, timeout=self.config.action_timeout)
            except Exception as click_error:
                logger.warning(f"Regular click failed, trying JS click: {click_error}")
                
                # JS клик как fallback
                self.browser.page.evaluate(f"""
                    const elem = document.querySelector('{selector.replace("'", "\\'")}');
                    if (elem) elem.click();
                """)
            
            # Wait for response
            time.sleep(random.uniform(1.5, 2.5))
            
            return {
                'success': True,
                'message': f"Clicked {element['type']}: {element['text'][:50]}"
            }
        except Exception as e:
            return {'success': False, 'message': f"Click failed: {e}"}
    
    def _action_type(self, element_id: int, text: str) -> Dict[str, Any]:
        """Type with human-like speed."""
        try:
            element_id = int(element_id)
        except (ValueError, TypeError):
            return {'success': False, 'message': f"Invalid element_id: {element_id}"}
        
        if not hasattr(self.browser, '_current_element_map'):
            return {'success': False, 'message': "No element map"}
        
        element_map = self.browser._current_element_map
        
        if element_id not in element_map:
            return {'success': False, 'message': f"Element {element_id} not found"}
        
        element = element_map[element_id]
        selector = element['selector']
        
        logger.info(f"Typing: '{text[:30]}...'")
        
        try:
            # Clear existing text
            self.browser.page.fill(selector, '', timeout=self.config.action_timeout)
            
            # Type with human-like delay
            self.browser.page.type(
                selector, 
                text, 
                timeout=self.config.action_timeout,
                delay=self.config.typing_delay  # 150ms между символами
            )
            
            import random
            time.sleep(random.uniform(0.5, 1.0))
            
            return {
                'success': True,
                'message': f"Typed '{text[:30]}...' into {element['type']}"
            }
        except Exception as e:
            return {'success': False, 'message': f"Type failed: {e}"}
    
    def _action_select(self, element_id: int, value: str) -> Dict[str, Any]:
        """Select option from dropdown."""
        try:
            element_id = int(element_id)
        except (ValueError, TypeError):
            return {'success': False, 'message': f"Invalid element_id: {element_id}"}
        
        if not hasattr(self.browser, '_current_element_map'):
            return {'success': False, 'message': "No element map"}
        
        element_map = self.browser._current_element_map
        
        if element_id not in element_map:
            return {'success': False, 'message': f"Element {element_id} not found"}
        
        element = element_map[element_id]
        selector = element['selector']
        
        logger.info(f"Selecting: '{value}'")
        
        try:
            self.browser.page.select_option(selector, value, timeout=self.config.action_timeout)
            
            import random
            time.sleep(random.uniform(0.5, 1.0))
            
            return {'success': True, 'message': f"Selected '{value}'"}
        except Exception as e:
            return {'success': False, 'message': f"Select failed: {e}"}
    
    def _action_scroll(self, direction: str) -> Dict[str, Any]:
        """Scroll page with human-like behavior."""
        logger.info(f"Scrolling {direction}")
        
        try:
            import random
            
            if direction == 'down':
                # Scroll down by viewport height
                self.browser.page.evaluate("window.scrollBy(0, window.innerHeight)")
            else:
                # Scroll up
                self.browser.page.evaluate("window.scrollBy(0, -window.innerHeight)")
            
            # Wait for content to load
            time.sleep(random.uniform(1.0, 2.0))
            
            return {'success': True, 'message': f"Scrolled {direction}"}
        except Exception as e:
            return {'success': False, 'message': f"Scroll failed: {e}"}
    
    def _action_navigate(self, url: str) -> Dict[str, Any]:
        """
        Navigate to URL with ENHANCED support for internal routes.
        
        Supports:
        1. Full URLs (https://...)
        2. URL fragments (#inbox, #promotions)
        3. Relative paths (/mail/u/0/#inbox)
        """
        logger.info(f"Navigating to: {url}")
        
        try:
            # Определяем тип URL
            if url.startswith('#'):
                # Fragment: добавляем к текущему URL
                current_url = self.browser.page.url
                base_url = current_url.split('#')[0]
                full_url = base_url + url
                logger.info(f"Fragment navigation: {full_url}")
                url = full_url
            elif url.startswith('/') and not url.startswith('//'):
                # Relative path: строим полный URL
                from urllib.parse import urlparse
                parsed = urlparse(self.browser.page.url)
                full_url = f"{parsed.scheme}://{parsed.netloc}{url}"
                logger.info(f"Relative navigation: {full_url}")
                url = full_url
            
            # Навигация
            success = self.browser.navigate(url)
            
            if success:
                return {'success': True, 'message': f"Navigated to {url}"}
            else:
                return {'success': False, 'message': f"Navigation failed for {url}"}
        
        except Exception as e:
            return {'success': False, 'message': f"Navigation error: {e}"}
    
    def _action_wait(self, seconds: int) -> Dict[str, Any]:
        """Wait for specified seconds."""
        logger.info(f"Waiting {seconds} seconds")
        
        try:
            seconds = int(seconds)
            time.sleep(seconds)
            return {'success': True, 'message': f"Waited {seconds} seconds"}
        except Exception as e:
            return {'success': False, 'message': f"Wait failed: {e}"}


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Main entry point."""
    print("\n" + "="*70)
    print("   AUTONOMOUS BROWSER AGENT v2.0 - PRODUCTION GRADE (OPTIMIZED)")
    print("="*70 + "\n")
    
    try:
        config = Config.from_env()
        logger.info("✓ Configuration loaded")
        logger.info(f"  API: {config.api_base_url}")
        logger.info(f"  Model: {config.model_name}")
        logger.info(f"  Proxy: {config.proxy_url}")
        logger.info(f"  Max steps: {config.max_steps}")
        logger.info(f"  Human-like delays: {config.min_action_delay}-{config.max_action_delay}s")
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        print("\n❌ Please set environment variables:")
        print("  - OPENAI_API_KEY or LLM_API_KEY (required)")
        return 1
    
    print("\n" + "-"*70)
    task = input("📝 Task: ").strip()
    
    if not task:
        task = "Go to google.com and search for 'Playwright Python tutorial'"
        print(f"Using demo task: {task}")
    
    print("-"*70 + "\n")
    
    try:
        agent = Agent(config)
        success = agent.run(task)
        
        if success:
            print("\n" + "="*70)
            print("✓✓✓ TASK COMPLETED! ✓✓✓")
            print("="*70 + "\n")
            return 0
        else:
            print("\n" + "="*70)
            print("✗✗✗ TASK FAILED ✗✗✗")
            print("="*70 + "\n")
            return 1
    except KeyboardInterrupt:
        print("\n\n⚠ Interrupted")
        return 130
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    exit(main())