#!/usr/bin/env python3
"""
Autonomous Browser Agent v2.0 - PRODUCTION GRADE
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
    max_steps: int = 20  # Увеличено для медленных условий
    page_load_timeout: int = 90000  # 90 секунд (было 60)
    action_timeout: int = 60000     # 30 секунд (было 20)
    
    # LLM Configuration
    max_tokens: int = 2000  # Увеличено для более подробных ответов
    temperature: float = 0.1  # Ниже для более детерминированного поведения
    
    # HTTP Timeouts (для очень медленных соединений)
    http_timeout: float = 120.0  # 2 минуты
    
    # Human-like delays (анти-бот)
    min_action_delay: float = 1.5  # Минимальная пауза между действиями
    max_action_delay: float = 3.0  # Максимальная пауза
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
                logger.info(f"Recovered JSON using regex extraction")
                return decision
            
        except Exception as e:
            logger.error(f"Regex extraction failed: {e}")
        
        # Все попытки провалились
        logger.error(f"Failed to parse JSON after {max_attempts} attempts")
        logger.error(f"Raw response: {response[:500]}")
        return None


# ============================================================================
# LLM Client with Enhanced Retry Logic
# ============================================================================

class LLMClient:
    """LLM client with robust error handling and retries."""
    
    def __init__(self, config: Config):
        self.config = config
        self.parser = RobustJSONParser()
        
        # Создаем httpx.Client с прокси
        http_client = httpx.Client(
            proxy=config.proxy_url,
            timeout=httpx.Timeout(config.http_timeout, connect=60.0)
        )
        
        # Инициализируем OpenAI client
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.api_base_url,
            http_client=http_client
        )
        
        logger.info(f"LLM Client v2.0 initialized")
        logger.info(f"  Model: {config.model_name}")
        logger.info(f"  Proxy: {config.proxy_url}")
        logger.info(f"  Timeout: {config.http_timeout}s")
    
    @retry(
        stop=stop_after_attempt(5),  # Больше попыток
        wait=wait_exponential(multiplier=3, min=6, max=60),  # Длиннее ожидание
        retry=retry_if_exception_type((Exception,)),
    )
    def chat(
        self,
        system_prompt: str,
        messages: List[Dict[str, str]],
    ) -> str:
        """
        Send chat completion with enhanced error handling.
        
        Returns:
            Response text (может быть пустым при фатальной ошибке)
        """
        try:
            full_messages = [{"role": "system", "content": system_prompt}] + messages
            
            logger.debug(f"Sending request to LLM...")
            
            response = self.client.chat.completions.create(
                model=self.config.model_name,
                messages=full_messages,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
            )
            
            content = response.choices[0].message.content
            
            if not content or not content.strip():
                logger.warning("LLM returned empty response")
                raise ValueError("Empty response from LLM")
            
            logger.debug(f"LLM Response received: {len(content)} chars")
            return content
            
        except Exception as e:
            logger.error(f"LLM API error: {type(e).__name__}: {e}")
            raise
    
    def __del__(self):
        """Cleanup."""
        try:
            if hasattr(self, 'client') and hasattr(self.client, '_client'):
                self.client._client.close()
        except:
            pass


# ============================================================================
# Enhanced DOM Processing - Priority Elements
# ============================================================================

class EnhancedDOMProcessor:
    """
    Улучшенный процессор DOM с приоритизацией важных элементов.
    
    Изменения v2.0:
    - Приоритет: текстовые заголовки, кнопки, input
    - Извлечение aria-label для accessibility
    - Ограничение по количеству элементов (топ 50)
    - Лучшая обработка динамического контента
    """
    
    REMOVE_TAGS = {'script', 'style', 'noscript', 'svg', 'path', 'iframe', 'img'}
    
    # Приоритеты элементов (чем выше число, тем важнее)
    ELEMENT_PRIORITY = {
        'button': 10,
        'input': 9,
        'textarea': 9,
        'a': 7,
        'select': 8,
        'h1': 6,
        'h2': 6,
        'h3': 5,
        'label': 4,
    }
    
    def __init__(self):
        self.element_map: Dict[int, Dict[str, Any]] = {}
        self.current_id = 0
    
    def process_page(self, html: str, page: Page) -> Tuple[str, Dict[int, Dict]]:
        """
        Process HTML with priority-based element extraction.
        
        Returns:
            (simplified_dom_text, element_map)
        """
        self.element_map = {}
        self.current_id = 0
        
        soup = BeautifulSoup(html, 'html.parser')

        for cb in soup.find_all(attrs={"role": "checkbox"}):
            all_elements.append(('input', cb, self.ELEMENT_PRIORITY['input']))
            
        # Remove unwanted tags
        for tag in self.REMOVE_TAGS:
            for element in soup.find_all(tag):
                element.decompose()
        
        # Собираем все интерактивные элементы с приоритетами
        all_elements = []
        
        # Buttons (высокий приоритет)
        for button in soup.find_all('button'):
            all_elements.append(('button', button, self.ELEMENT_PRIORITY['button']))
        
        # Inputs (высокий приоритет)
        for input_elem in soup.find_all('input'):
            input_type = input_elem.get('type', 'text')
            # Пропускаем hidden inputs
            if input_type != 'hidden':
                all_elements.append(('input', input_elem, self.ELEMENT_PRIORITY['input']))
        
        # Textareas (высокий приоритет)
        for textarea in soup.find_all('textarea'):
            all_elements.append(('textarea', textarea, self.ELEMENT_PRIORITY['textarea']))
        
        # Selects (высокий приоритет)
        for select in soup.find_all('select'):
            all_elements.append(('select', select, self.ELEMENT_PRIORITY['select']))
        
        # Links (средний приоритет)
        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            # Пропускаем javascript: и пустые ссылки
            if not href.startswith('javascript:') and href.strip():
                all_elements.append(('link', link, self.ELEMENT_PRIORITY['a']))
        
        # Headers (для контекста)
        for i, tag in enumerate(['h1', 'h2', 'h3']):
            for header in soup.find_all(tag):
                all_elements.append((tag, header, self.ELEMENT_PRIORITY.get(tag, 5)))
        
        # Сортируем по приоритету (важные элементы первыми)
        all_elements.sort(key=lambda x: x[2], reverse=True)
        
        # Ограничиваем количество элементов (топ 50 самых важных)
        all_elements = all_elements[:150]
        
        # Формируем текстовое представление
        elements_text = []
        elements_text.append("=== INTERACTIVE ELEMENTS (Priority Sorted) ===\n")
        
        # Add page metadata
        title = soup.find('title')
        if title:
            elements_text.insert(1, f"Page Title: {title.get_text(strip=True)}\n\n")
        
        # Process each element
        for element_type, element, priority in all_elements:
            element_id = self._register_element(element_type, element, page)
            if element_id is not None:
                desc = self._get_element_description(element_type, element)
                elements_text.append(f"[{element_id}] {desc}\n")
        
        # Add visible text summary (ограничен)
        text_content = self._extract_visible_text(soup)
        if text_content:
            elements_text.append(f"\n=== PAGE TEXT (Sample) ===\n{text_content[:800]}...\n")
        
        dom_representation = "".join(elements_text)
        logger.debug(f"Processed DOM: {len(self.element_map)} priority elements")
        
        return dom_representation, self.element_map
    
    def _get_element_description(self, element_type: str, element) -> str:
        """Получить описание элемента с aria-label."""
        text = element.get_text(strip=True)
        
        # Извлекаем aria-label (важно для accessibility)
        aria_label = element.get('aria-label', '')
        
        # Извлекаем title
        title = element.get('title', '')
        
        # Извлекаем placeholder (для inputs)
        placeholder = element.get('placeholder', '')
        
        # Извлекаем name
        name = element.get('name', '')
        
        # Собираем описание
        parts = []
        
        if element_type == 'button':
            parts.append(f"BUTTON: {text or aria_label or title or '[no text]'}")
            button_type = element.get('type', 'button')
            if button_type != 'button':
                parts.append(f"(type: {button_type})")
        
        elif element_type == 'input':
            input_type = element.get('type', 'text')
            label = aria_label or placeholder or name or f"[{input_type}]"
            parts.append(f"INPUT ({input_type}): {label}")
            value = element.get('value', '')
            if value:
                parts.append(f"(current: {value[:30]})")
        
        elif element_type == 'textarea':
            label = aria_label or placeholder or name or "Text Area"
            parts.append(f"TEXTAREA: {label}")
        
        elif element_type == 'select':
            label = aria_label or name or "Dropdown"
            options = [opt.get_text(strip=True) for opt in element.find_all('option')]
            parts.append(f"SELECT: {label}")
            if options:
                options_text = ", ".join(options[:3])
                if len(options) > 3:
                    options_text += f"... ({len(options)} total)"
                parts.append(f"(options: {options_text})")
        
        elif element_type == 'link':
            href = element.get('href', '')
            parts.append(f"LINK: {text or aria_label or '[link]'}")
            if href:
                parts.append(f"(href: {href[:50]})")
        
        elif element_type in ['h1', 'h2', 'h3']:
            parts.append(f"{element_type.upper()}: {text[:60]}")
        
        return " ".join(parts)
    
    def _register_element(self, element_type: str, element, page: Page) -> Optional[int]:
        """Register element with ID."""
        try:
            selector = self._build_selector(element)
            
            if not selector:
                return None
            
            element_id = self.current_id
            self.element_map[element_id] = {
                'type': element_type,
                'selector': selector,
                'tag': element.name,
                'text': element.get_text(strip=True)[:100],
                'aria_label': element.get('aria-label', ''),
            }
            
            self.current_id += 1
            return element_id
            
        except Exception as e:
            logger.debug(f"Failed to register element: {e}")
            return None
    
    def _build_selector(self, element) -> str:
        """Build CSS selector for element."""
        # Try ID first
        if element.get('id'):
            elem_id = element['id']
            # Escape special characters in ID
            elem_id = re.sub(r'([:.[\],])', r'\\\1', elem_id)
            return f"#{elem_id}"
        
        # Try name
        if element.get('name'):
            return f"{element.name}[name='{element['name']}']"
        
        # Try aria-label
        if element.get('aria-label'):
            aria = element['aria-label'].replace("'", "\\'")
            return f"{element.name}[aria-label='{aria}']"
        
        # Build path-based selector
        path = []
        current = element
        
        for _ in range(5):
            if current.name in ['html', 'body', '[document]']:
                break
                
            siblings = [s for s in current.parent.children if hasattr(s, 'name') and s.name == current.name]
            index = siblings.index(current) + 1
            
            if len(siblings) > 1:
                path.insert(0, f"{current.name}:nth-of-type({index})")
            else:
                path.insert(0, current.name)
            
            current = current.parent
            if not hasattr(current, 'name'):
                break
        
        return " > ".join(path)
    
    def _extract_visible_text(self, soup: BeautifulSoup) -> str:
        """Extract visible text content from page."""
        # Remove interactive elements
        for tag in ['a', 'button', 'input', 'select', 'textarea', 'script', 'style']:
            for elem in soup.find_all(tag):
                elem.decompose()
        
        text = soup.get_text(separator=' ', strip=True)
        text = ' '.join(text.split())
        return text


# ============================================================================
# Browser Manager with Anti-Bot Enhancements
# ============================================================================

class BrowserManager:
    """Browser manager optimized for anti-bot evasion and slow connections."""
    
    def __init__(self, config: Config):
        self.config = config
        self.playwright = None
        self.browser = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._current_element_map: Dict[int, Dict[str, Any]] = {}
        self._last_url: str = ""
        self._last_html_hash: int = 0
    
    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        try:
            self.close()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
    
    def start(self):
        """Start browser with enhanced stealth mode."""
        logger.info("Starting browser with enhanced anti-bot measures...")
        
        Path(self.config.user_data_dir).mkdir(parents=True, exist_ok=True)
        
        try:
            self.playwright = sync_playwright().start()
            
            # Launch with aggressive anti-detection
            self.context = self.playwright.chromium.launch_persistent_context(
                user_data_dir=self.config.user_data_dir,
                headless=self.config.headless,
                proxy={"server": self.config.proxy_url},
                viewport={'width': 1920, 'height': 1080},
                user_agent=(
                    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
                ),
                locale='en-US',
                timezone_id='America/New_York',
                ignore_https_errors=True,
                java_script_enabled=True,
                bypass_csp=True,
                # Анти-бот аргументы
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-web-security',
                    '--disable-features=IsolateOrigins,site-per-process',
                    '--allow-running-insecure-content',
                ]
            )
            
            # Усиленный анти-детект скрипт
            self.context.add_init_script("""
                // Удаляем navigator.webdriver
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                
                // Подделываем chrome объект
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };
                
                // Подделываем permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
                
                // Подделываем plugins (больше плагинов = более реалистично)
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                
                // Подделываем languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en', 'ru']
                });
                
                // Подделываем platform
                Object.defineProperty(navigator, 'platform', {
                    get: () => 'Linux x86_64'
                });
                
                // Подделываем connection
                Object.defineProperty(navigator, 'connection', {
                    get: () => ({
                        effectiveType: '4g',
                        rtt: 100,
                        downlink: 10,
                        saveData: false
                    })
                });
            """)
            
            # Set timeouts
            self.context.set_default_timeout(self.config.page_load_timeout)
            self.context.set_default_navigation_timeout(self.config.page_load_timeout)
            
            # Create page
            if len(self.context.pages) > 0:
                self.page = self.context.pages[0]
            else:
                self.page = self.context.new_page()
            
            logger.info("✓ Browser started with enhanced stealth mode")
            
        except Exception as e:
            logger.error(f"Failed to start browser: {e}")
            raise RuntimeError(f"Browser initialization failed: {e}")
    
    def close(self):
        """Close browser."""
        logger.info("Closing browser...")
        try:
            if self.context:
                self.context.close()
            if self.playwright:
                self.playwright.stop()
            logger.info("✓ Browser closed")
        except Exception as e:
            logger.error(f"Error closing browser: {e}")
    
    def navigate(self, url: str, wait_until: str = 'domcontentloaded') -> bool:
        """Navigate with retry and extended wait."""
        try:
            logger.info(f"Navigating to: {url}")
            
            # Human-like delay before navigation
            import random
            delay = random.uniform(0.5, 1.5)
            time.sleep(delay)
            
            response = self.page.goto(url, wait_until=wait_until, timeout=self.config.page_load_timeout)
            time.sleep(10) 
            if response and response.status >= 400:
                logger.warning(f"Page returned status {response.status}")
                return False
            
            # Extended wait for dynamic content + anti-bot
            time.sleep(random.uniform(3.0, 5.0))
            
            # Update tracking
            self._last_url = url
            self._last_html_hash = hash(self.page.content())
            
            logger.info(f"✓ Navigation successful")
            return True
            
        except PlaywrightError as e:
            logger.error(f"Navigation error: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected navigation error: {e}")
            return False
    
    def get_html(self) -> str:
        """Get current page HTML."""
        try:
            return self.page.content()
        except Exception as e:
            logger.error(f"Failed to get HTML: {e}")
            return ""
    
    def get_url(self) -> str:
        """Get current page URL."""
        try:
            return self.page.url
        except Exception as e:
            logger.error(f"Failed to get URL: {e}")
            return ""
    
    def get_page_state(self) -> str:
        """
        Get simplified page state for LLM.
        
        Returns:
            String with page state
        """
        try:
            processor = EnhancedDOMProcessor()
            
            html = self.get_html()
            
            if not html or len(html) < 100:
                return ""
            
            dom_text, element_map = processor.process_page(html, self.page)
            
            # Save element map
            self._current_element_map = element_map
            
            return dom_text
            
        except Exception as e:
            logger.error(f"Error getting page state: {e}")
            return ""
    
    def check_page_changed(self) -> bool:
        """
        Проверить, изменилась ли страница после последнего действия.
        
        Returns:
            True если страница изменилась
        """
        try:
            current_url = self.get_url()
            current_html = self.page.content()
            current_hash = hash(current_html)
            
            changed = (current_url != self._last_url) or (current_hash != self._last_html_hash)
            
            if changed:
                self._last_url = current_url
                self._last_html_hash = current_hash
            
            return changed
            
        except Exception as e:
            logger.error(f"Error checking page change: {e}")
            return False


# ============================================================================
# System Prompt - Enhanced
# ============================================================================

SYSTEM_PROMPT = """You are an autonomous web browser agent. Your job is to help the user complete tasks on websites.

## INPUT FORMAT
You receive a simplified representation of the web page's interactive elements. Each element has a unique numeric ID:

[12] BUTTON: Submit Form (type: submit)
[13] INPUT (text): Email Address
[14] LINK: Sign Out (href: /logout)

## AVAILABLE ACTIONS

1. **click** - Click an element
   Example: {"action_type": "click", "element_id": 12, "args": {}}

2. **type** - Type text into an input field
   Example: {"action_type": "type", "element_id": 13, "args": {"text": "hello@example.com"}}

3. **select** - Select dropdown option
   Example: {"action_type": "select", "element_id": 15, "args": {"value": "option1"}}

4. **scroll** - Scroll the page
   Example: {"action_type": "scroll", "args": {"direction": "down"}}

5. **navigate** - Go to specific URL
   Example: {"action_type": "navigate", "args": {"url": "https://example.com"}}

6. **wait** - Wait for page to load/update
   Example: {"action_type": "wait", "args": {"seconds": 3}}

7. **done** - Mark task as complete
   Example: {"action_type": "done", "args": {}}

8. **fail** - Report task cannot be completed
   Example: {"action_type": "fail", "args": {"reason": "Login required but no credentials provided"}}

## CRITICAL RULES

1. **ALWAYS output valid JSON** in this exact format:
   {
     "thought": "Your reasoning about what you see and what to do next",
     "action_type": "click",
     "element_id": 12,
     "args": {}
   }

2. **NEVER use element IDs that don't exist** in the current page. Check the available IDs carefully.

3. **Think step-by-step**. Always explain your reasoning in the "thought" field BEFORE deciding the action.

4. **Be patient**. Some pages load slowly. If action fails, analyze error and try different approach.

5. **Anti-bot awareness**: If you see login pages, CAPTCHAs, or "verify you're human" - report this in thought.

6. **If page doesn't change** after action - try different element or scroll to find more content.

7. **Self-Correction & Strategy**: If an action fails, times out, or the page state remains unchanged, your previous strategy is likely blocked or inefficient. Evaluate all available actions. Consider if navigating to a state-specific URL, scrolling to refresh the DOM, or using a different sequence of actions is more robust than repeating a failing one.

8. **Visual Confirmation**: If the page seems empty but the URL is correct, use scroll or wait to ensure dynamic content has loaded before declaring failure.

## RESPONSE FORMAT
Respond with ONLY valid JSON. No text before or after. Must have:
- "thought": string (your reasoning)
- "action_type": string (one of the actions above)
- "element_id": integer (only for click, type, select)
- "args": object (additional parameters)

Example:
{
  "thought": "I see the login button with ID 8. I'll click it to proceed.",
  "action_type": "click",
  "element_id": 8,
  "args": {}
}
If a click action fails or times out twice, try to navigate directly to the target section using a URL instead of clicking.
"""


# ============================================================================
# Agent v2.0 with Production Reliability
# ============================================================================

class Agent:
    """Production-grade autonomous agent."""
    
    def __init__(self, config: Config):
        self.config = config
        self.llm_client = LLMClient(config)
        self.browser: Optional[BrowserManager] = None
        self.history: List[Dict[str, str]] = []
        self.step_count = 0
        self.consecutive_failures = 0
        self.max_consecutive_failures = 3
    
    def run(self, task: str) -> bool:
        """Execute task with enhanced error handling."""
        logger.info(f"\n{'='*70}")
        logger.info(f"🎯 Task: {task}")
        logger.info(f"{'='*70}\n")
        
        # Initialize history
        self.history = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            {
                "role": "user",
                "content": (
                    f"TASK: {task}\n\n"
                    f"You start with an empty browser (about:blank).\n"
                    f"First action: analyze task and use 'navigate' to go to appropriate website.\n"
                    f"Then complete the task step by step.\n"
                    f"When done, use 'done' action."
                )
            }
        ]
        
        self.step_count = 0
        browser_initialized = False
        
        try:
            with BrowserManager(self.config) as browser:
                self.browser = browser
                browser_initialized = True
                logger.info("✓ Browser initialized\n")
                
                # Main loop
                while self.step_count < self.config.max_steps:
                    self.step_count += 1
                    
                    logger.info(f"\n{'='*60}")
                    logger.info(f"📍 STEP {self.step_count}/{self.config.max_steps}")
                    logger.info(f"{'='*60}\n")
                    
                    # Check if too many consecutive failures
                    if self.consecutive_failures >= self.max_consecutive_failures:
                        logger.error(f"Too many consecutive failures ({self.consecutive_failures})")
                        logger.error("Aborting task")
                        return False
                    
                    # OBSERVE
                    observation = self._observe()
                    
                    if not observation:
                        logger.warning("Failed to observe page")
                        time.sleep(2)
                        continue
                    
                    self.history.append({
                        "role": "user",
                        "content": f"[STEP {self.step_count}] PAGE STATE:\n{observation}"
                    })
                    
                    # THINK
                    decision = self._think_with_retry()
                    
                    if not decision:
                        logger.error("Failed to get decision from LLM after retries")
                        self.consecutive_failures += 1
                        continue
                    
                    # Reset failure counter on successful decision
                    self.consecutive_failures = 0
                    
                    thought = decision.get('thought', 'N/A')
                    action_type = decision.get('action_type', 'unknown')
                    element_id = decision.get('element_id')
                    args = decision.get('args', {})
                    
                    logger.info(f"💭 Thought: {thought[:150]}{'...' if len(thought) > 150 else ''}")
                    logger.info(f"🎬 Action: {action_type}")
                    if element_id is not None:
                        logger.info(f"🎯 Element: {element_id}")
                    if args:
                        logger.info(f"📋 Args: {args}")
                    
                    self.history.append({
                        "role": "assistant",
                        "content": json.dumps(decision, ensure_ascii=False, indent=2)
                    })
                    
                    # Check for completion
                    if action_type == 'done':
                        logger.info("\n" + "="*70)
                        logger.info("✅ ✅ ✅  TASK COMPLETED!  ✅ ✅ ✅")
                        logger.info("="*70 + "\n")
                        return True
                    
                    if action_type == 'fail':
                        reason = args.get('reason', 'Unknown')
                        logger.error("\n" + "="*70)
                        logger.error(f"❌  TASK FAILED: {reason}")
                        logger.error("="*70 + "\n")
                        return False
                    
                    # ACT
                    logger.info(f"⚡ Executing action '{action_type}'...")
                    
                    action_dict = self._act(decision)
                    action_success = action_dict.get('success', False)
                    action_result = action_dict.get('message', 'Unknown result')
                    
                    if action_success:
                        logger.info(f"✅ {action_result}")
                        result_prefix = "✅ SUCCESS"
                        self.consecutive_failures = 0
                    else:
                        logger.warning(f"⚠️  {action_result}")
                        result_prefix = "⚠️ FAILED"
                        self.consecutive_failures += 1
                    
                    # Check if page changed (для detect stuck)
                    if action_type in ['click', 'navigate']:
                        time.sleep(2)  # Wait for page update
                        page_changed = self.browser.check_page_changed()
                        if not page_changed:
                            result_prefix += " (⚠️ Page didn't change - may need different approach)"
                    
                    feedback = f"{result_prefix}: {action_result}"
                    
                    self.history.append({
                        "role": "user",
                        "content": f"[STEP {self.step_count}] RESULT:\n{feedback}"
                    })
                    
                    # Human-like delay
                    import random
                    delay = random.uniform(self.config.min_action_delay, self.config.max_action_delay)
                    time.sleep(delay)
                
                # Max steps reached
                logger.warning(f"\n⏱️  Max steps ({self.config.max_steps}) reached")
                return False
        
        except KeyboardInterrupt:
            logger.info("\n⚠️  Interrupted by user")
            return False
        except Exception as e:
            logger.error(f"\n💥 Fatal error: {e}", exc_info=True)
            return False
        finally:
            logger.info("\n🧹 Cleaning up...")
            if browser_initialized:
                logger.info("✓ Browser will close automatically")
    
    def _observe(self) -> str:
        """Observe page state with error handling."""
        try:
            page_state = self.browser.get_page_state()
            current_url = self.browser.get_url()
        except Exception as e:
            logger.error(f"Error observing page: {e}")
            return ""
        
        if not page_state or not page_state.strip():
            # Empty page or about:blank
            return (
                f"⚠️ STATUS: Browser empty or on about:blank\n"
                f"URL: {current_url}\n\n"
                f"ACTION: Use 'navigate' to go to website\n"
                f"IMPORTANT: You MUST start with navigate!"
            )
        else:
            return f"URL: {current_url}\n\n{page_state}"
    
    def _think_with_retry(self) -> Optional[Dict[str, Any]]:
        """
        Get decision from LLM with multiple retry attempts.
        
        Returns:
            Decision dict or None if all attempts failed
        """
        parser = RobustJSONParser()
        
        for attempt in range(self.config.max_json_retries):
            try:
                logger.debug(f"LLM request attempt {attempt + 1}/{self.config.max_json_retries}")
                
                # Get response from LLM
                response = self.llm_client.chat(
                    system_prompt=SYSTEM_PROMPT,
                    messages=self.history
                )
                
                # Parse JSON
                decision = parser.parse(response)
                
                if decision:
                    # Validate decision
                    try:
                        self._validate_decision(decision)
                        return decision
                    except ValueError as e:
                        logger.warning(f"Decision validation failed: {e}")
                        # Add error to history and retry
                        self.history.append({
                            "role": "user",
                            "content": f"ERROR: {e}\nPlease try again with valid element IDs."
                        })
                        continue
                else:
                    logger.warning(f"JSON parsing failed (attempt {attempt + 1})")
                    
            except Exception as e:
                logger.error(f"Error in think (attempt {attempt + 1}): {e}")
            
            # Wait before retry
            if attempt < self.config.max_json_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
        
        return None
    
    def _validate_decision(self, decision: Dict[str, Any]):
        """Validate decision with smart element_id checking."""
        action_type = decision.get('action_type')
        
        if not action_type:
            raise ValueError("Missing 'action_type'")
        
        # Actions that don't need element_id
        NO_ELEMENT_ACTIONS = {'navigate', 'scroll', 'done', 'fail', 'wait'}
        
        if action_type in NO_ELEMENT_ACTIONS:
            return
        
        # Actions that need element_id
        element_id = decision.get('element_id')
        
        if element_id is None:
            raise ValueError(f"Action '{action_type}' requires 'element_id'")
        
        if not hasattr(self.browser, '_current_element_map'):
            raise ValueError("No element map available - navigate to page first")
        
        element_map = self.browser._current_element_map
        
        if not element_map:
            raise ValueError("Element map empty - page may not be loaded")
        
        try:
            element_id_int = int(element_id)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid element_id: {element_id!r}")
        
        if element_id_int not in element_map:
            available = list(element_map.keys())
            raise ValueError(
                f"Element ID {element_id_int} not found. "
                f"Available: {available[:20]}"
            )
    
    def _act(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Execute action with error handling."""
        action_type = decision.get('action_type')
        element_id = decision.get('element_id')
        args = decision.get('args', {})
        
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
                return self._action_wait(args.get('seconds', 3))
            else:
                return {'success': False, 'message': f"Unknown action: {action_type}"}
        except Exception as e:
            return {'success': False, 'message': f"Action error: {e}"}
    
    def _action_click(self, element_id: int) -> Dict[str, Any]:
        """Click with human-like behavior."""
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
            # Human-like: mouse move + pause before click
            import random
            time.sleep(random.uniform(0.3, 0.7))
            
            self.browser.page.click(selector, timeout=self.config.action_timeout)
            
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
        """Navigate to URL."""
        logger.info(f"Navigating to: {url}")
        
        try:
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
    print("   AUTONOMOUS BROWSER AGENT v2.0 - PRODUCTION GRADE")
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
