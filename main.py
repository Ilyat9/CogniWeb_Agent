#!/usr/bin/env python3
"""
CogniWeb Agent v3.5 - Senior/Staff Engineer Edition
====================================================

НОВЫЕ ВОЗМОЖНОСТИ:
✓ Human-like Interactions - Эмуляция поведения реального пользователя
✓ Breadcrumbs System - Система хлебных крошек для предотвращения зацикливания
✓ Graceful Degeneracy - Умное завершение с полным отчетом
✓ Improved CAPTCHA Detection - Устранение ложных срабатываний
✓ Zero-Touch UX - Запуск с about:blank, агент сам выбирает начальный URL

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
import random
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from collections import deque
from enum import Enum
from datetime import datetime

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
    max_dom_elements: int = 150  # Limit elements per page
    max_text_length: int = 300  # Max chars for text content
    max_history_turns: int = 10  # Keep only last N turns
    
    # Popup/Captcha Detection
    popup_detection_enabled: bool = True
    max_popup_close_attempts: int = 3
    
    # Human-like Behavior
    min_action_delay: float = 1.0
    max_action_delay: float = 3.0
    typing_delay_min: int = 50  # ms between keystrokes
    typing_delay_max: int = 150  # ms between keystrokes
    mouse_move_steps: int = 10  # steps for mouse movement
    
    @classmethod
    def from_env(cls) -> 'Config':
        """Загрузить конфигурацию из переменных окружения."""
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
# Breadcrumbs Tracker - Защита от зацикливания
# ============================================================================

@dataclass
class ActionRecord:
    """Запись о выполненном действии."""
    action_type: str
    target: str
    timestamp: float


class BreadcrumbsTracker:
    """
    Система хлебных крошек для отслеживания действий и предотвращения зацикливания.
    
    Логика:
    - Хранит последние N действий (deque с maxlen)
    - Проверяет повторение одинаковых действий подряд
    - Если действие повторяется 3+ раз, возвращает предупреждение
    """
    
    def __init__(self, max_history: int = 10, max_repeats: int = 3):
        """
        Args:
            max_history: Максимальное количество действий в истории
            max_repeats: Максимальное количество повторов одного действия
        """
        self.history: deque = deque(maxlen=max_history)
        self.max_repeats = max_repeats
    
    def record_action(self, action_type: str, target: str):
        """
        Записать выполненное действие.
        
        Args:
            action_type: Тип действия (click, type, navigate и т.д.)
            target: Цель действия (element_id, URL, текст и т.д.)
        """
        record = ActionRecord(
            action_type=action_type,
            target=str(target),
            timestamp=time.time()
        )
        self.history.append(record)
        logger.debug(f"Breadcrumb: {action_type} -> {target}")
    
    def check_loop(self) -> Tuple[bool, Optional[str]]:
        """
        Проверить, не зациклился ли агент.
        
        Returns:
            (is_looping, warning_message)
        """
        if len(self.history) < self.max_repeats:
            return False, None
        
        # Взять последние max_repeats действий
        recent = list(self.history)[-self.max_repeats:]
        
        # Проверить, все ли они одинаковые
        first = recent[0]
        all_same = all(
            r.action_type == first.action_type and r.target == first.target
            for r in recent
        )
        
        if all_same:
            warning = (
                f"⚠️ LOOP DETECTED: Action '{first.action_type}' on '{first.target}' "
                f"repeated {self.max_repeats} times. Agent should try a different approach."
            )
            return True, warning
        
        return False, None
    
    def get_summary(self) -> str:
        """Получить краткую сводку последних действий."""
        if not self.history:
            return "No actions yet"
        
        summary = []
        for i, record in enumerate(list(self.history)[-5:], 1):
            summary.append(f"{i}. {record.action_type} -> {record.target}")
        
        return "\n".join(summary)


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
            options = [opt.get_text(strip=True) for opt in select.find_all('option')]
            element_data = self._extract_element_data(select, 'SELECT', page)
            element_data['options'] = options[:10]  # Limit options
            elements.append(element_data)
        
        return elements
    
    def _extract_element_data(self, element, elem_type: str, page: Page) -> Dict:
        """Извлечь данные элемента."""
        # Build selector
        selector = self._build_selector(element)
        
        # Get text content
        text = element.get_text(strip=True)
        if len(text) > self.max_text_length:
            text = text[:self.max_text_length] + "..."
        
        # Get attributes
        attrs = {}
        for attr in ['id', 'class', 'name', 'placeholder', 'type', 'href', 'value']:
            val = element.get(attr)
            if val:
                if isinstance(val, list):
                    attrs[attr] = ' '.join(val)
                else:
                    attrs[attr] = str(val)
        
        return {
            'type': elem_type,
            'selector': selector,
            'text': text,
            'attrs': attrs
        }
    
    def _build_selector(self, element) -> str:
        """Построить CSS селектор для элемента."""
        # Priority 1: ID
        if element.get('id'):
            return f"#{element['id']}"
        
        # Priority 2: Name attribute
        if element.get('name'):
            return f"[name='{element['name']}']"
        
        # Priority 3: Unique combination
        tag = element.name
        classes = element.get('class', [])
        
        if classes:
            class_str = '.'.join(classes[:2])  # Use first 2 classes
            return f"{tag}.{class_str}"
        
        # Priority 4: Tag + position (least reliable)
        return tag
    
    def _prioritize_elements(self, elements: List[Dict]) -> List[Dict]:
        """
        Приоритизировать элементы по важности.
        
        Приоритет:
        1. Inputs и buttons (основные действия)
        2. Links с видимым текстом
        3. Остальные элементы
        """
        priority_order = {
            'INPUT(submit)': 1,
            'BUTTON': 1,
            'INPUT(text)': 2,
            'INPUT(search)': 2,
            'TEXTAREA': 2,
            'LINK': 3,
            'SELECT': 3,
        }
        
        def get_priority(elem):
            elem_type = elem['type']
            base_priority = priority_order.get(elem_type, 999)
            
            # Boost elements with visible text
            if elem.get('text') and len(elem['text']) > 2:
                base_priority -= 0.5
            
            return base_priority
        
        return sorted(elements, key=get_priority)
    
    def _format_element(self, idx: int, elem: Dict) -> str:
        """Форматировать элемент для вывода в LLM."""
        parts = [f"[{idx}] {elem['type']}"]
        
        if elem.get('text'):
            parts.append(f"'{elem['text']}'")
        
        # Add key attributes
        attrs_str = []
        for key in ['id', 'name', 'placeholder', 'href']:
            if key in elem.get('attrs', {}):
                attrs_str.append(f"{key}={elem['attrs'][key]}")
        
        if attrs_str:
            parts.append(f"({', '.join(attrs_str)})")
        
        # Add options for select
        if 'options' in elem:
            opts = ', '.join(elem['options'][:5])
            parts.append(f"[options: {opts}]")
        
        return " ".join(parts)


# ============================================================================
# Browser Manager - Playwright wrapper
# ============================================================================

class BrowserManager:
    """
    Управление браузером через Playwright.
    
    Функции:
    - Запуск/остановка браузера
    - Навигация
    - Закрытие попапов
    - Скриншоты
    - Anti-CAPTCHA логика
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._current_element_map = {}
    
    def start(self):
        """Запустить браузер."""
        logger.info("Starting browser...")
        
        self.playwright = sync_playwright().start()
        
        # Launch browser with proxy
        self.browser = self.playwright.chromium.launch(
            headless=self.config.headless,
            proxy={"server": self.config.proxy_url} if self.config.proxy_url else None
        )
        
        # Create persistent context
        user_data_path = Path(self.config.user_data_dir)
        user_data_path.mkdir(parents=True, exist_ok=True)
        
        self.context = self.browser.new_context(
            viewport={
                'width': self.config.viewport_width,
                'height': self.config.viewport_height
            },
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        
        self.page = self.context.new_page()
        self.page.set_default_timeout(self.config.action_timeout)
        
        logger.info("✓ Browser started")
    
    def stop(self):
        """Остановить браузер."""
        if self.page:
            self.page.close()
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
        
        logger.info("Browser stopped")
    
    def navigate(self, url: str) -> bool:
        """
        Перейти по URL.
        
        Returns:
            True если успешно, False если ошибка
        """
        try:
            logger.info(f"Navigating to: {url}")
            
            response = self.page.goto(
                url, 
                wait_until='domcontentloaded',
                timeout=self.config.page_load_timeout
            )
            
            # Wait a bit for JS to load
            self.page.wait_for_timeout(2000)
            
            # Check if navigation was successful
            if response and response.ok:
                logger.info(f"✓ Loaded: {self.page.url}")
                return True
            else:
                logger.warning(f"Navigation returned non-OK status")
                return False
        
        except PlaywrightTimeout:
            logger.warning(f"Navigation timeout (may still be usable)")
            return True  # Page might be partially loaded
        
        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            return False
    
    def close_popups(self) -> int:
        """
        Закрыть всплывающие окна.
        
        Returns:
            Количество закрытых попапов
        """
        closed_count = 0
        
        try:
            # Common popup close patterns
            close_selectors = [
                '[aria-label*="close" i]',
                '[aria-label*="dismiss" i]',
                'button:has-text("Close")',
                'button:has-text("×")',
                '.modal-close',
                '.popup-close',
                '[class*="close"]'
            ]
            
            for selector in close_selectors:
                try:
                    elements = self.page.locator(selector).all()
                    for elem in elements[:3]:  # Max 3 per selector
                        try:
                            if elem.is_visible(timeout=500):
                                elem.click(timeout=1000)
                                closed_count += 1
                                logger.debug(f"Closed popup: {selector}")
                                time.sleep(0.5)
                        except:
                            continue
                except:
                    continue
            
            if closed_count > 0:
                logger.info(f"✓ Closed {closed_count} popup(s)")
        
        except Exception as e:
            logger.debug(f"Popup closing error: {e}")
        
        return closed_count
    
    def take_screenshot(self, name: str = "screenshot") -> str:
        """
        Сделать скриншот.
        
        Returns:
            Путь к файлу скриншота
        """
        screenshots_dir = Path("./screenshots")
        screenshots_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = screenshots_dir / f"{name}_{timestamp}.png"
        
        self.page.screenshot(path=str(filepath), full_page=True)
        logger.info(f"Screenshot saved: {filepath}")
        
        return str(filepath)
    
    def has_interactive_elements(self) -> bool:
        """
        ANTI-PARANOIA: Проверить, есть ли на странице интерактивные элементы.
        Используется для уменьшения false positive CAPTCHA detection.
        
        Returns:
            True если найдены интерактивные элементы (ссылки, кнопки, инпуты)
        """
        try:
            # Проверить наличие базовых интерактивных элементов
            interactive_selectors = [
                'a[href]',
                'button',
                'input',
                'textarea',
                'select'
            ]
            
            for selector in interactive_selectors:
                elements = self.page.locator(selector).all()
                # Проверить, есть ли хотя бы один видимый элемент
                for elem in elements[:5]:  # Проверяем первые 5
                    try:
                        if elem.is_visible(timeout=500):
                            logger.debug(f"Found interactive element: {selector}")
                            return True
                    except:
                        continue
            
            return False
        
        except Exception as e:
            logger.debug(f"Error checking interactive elements: {e}")
            return False
    
    def detect_captcha(self) -> bool:
        """
        УЛУЧШЕННАЯ ЛОГИКА: Определить, заблокирована ли страница капчей.
        
        Логика:
        1. Ищем признаки CAPTCHA
        2. Если признаки найдены, проверяем наличие интерактивных элементов
        3. Если элементы есть - это НЕ блокировка, продолжаем
        4. Если элементов нет - это действительно CAPTCHA
        
        Returns:
            True если страница заблокирована капчей
        """
        try:
            html = self.page.content()
            title = self.page.title().lower()
            url = self.page.url.lower()
            
            # Признаки CAPTCHA
            captcha_indicators = [
                'captcha' in html.lower(),
                'recaptcha' in html.lower(),
                'challenge' in title,
                'robot' in title,
                'verify' in title,
                'cloudflare' in url and 'challenge' in html.lower(),
            ]
            
            # Если найдены признаки CAPTCHA
            if any(captcha_indicators):
                logger.warning("⚠️ CAPTCHA indicators detected, checking for interactive elements...")
                
                # ANTI-PARANOIA: Проверить, есть ли интерактивные элементы
                if self.has_interactive_elements():
                    logger.info("✓ Interactive elements found, page is usable despite CAPTCHA indicators")
                    return False  # Страница работает, можно продолжать
                else:
                    logger.error("✗ No interactive elements, page is truly blocked by CAPTCHA")
                    return True  # Действительно заблокировано
            
            return False
        
        except Exception as e:
            logger.debug(f"CAPTCHA detection error: {e}")
            return False


# ============================================================================
# LLM Client - OpenAI API wrapper
# ============================================================================

class LLMClient:
    """
    Клиент для работы с LLM (OpenAI API).
    
    Функции:
    - Отправка запросов к модели
    - Управление историей
    - Token economy
    """
    
    def __init__(self, config: Config):
        self.config = config
        
        # Configure HTTP client with proxy
        http_client = httpx.Client(
            proxy=config.proxy_url if config.proxy_url else None,
            timeout=httpx.Timeout(
                timeout=config.http_timeout,
                connect=config.http_connect_timeout
            )
        )
        
        # Initialize OpenAI client
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.api_base_url,
            http_client=http_client
        )
        
        # Conversation history
        self.messages = []
        self.max_history = config.max_history_turns
    
    def add_system_message(self, content: str):
        """Добавить системное сообщение."""
        self.messages.append({
            "role": "system",
            "content": content
        })
    
    def add_user_message(self, content: str):
        """Добавить сообщение пользователя."""
        self.messages.append({
            "role": "user",
            "content": content
        })
        
        # Trim history to save tokens
        self._trim_history()
    
    def add_assistant_message(self, content: str):
        """Добавить сообщение ассистента."""
        self.messages.append({
            "role": "assistant",
            "content": content
        })
    
    def _trim_history(self):
        """Обрезать историю для экономии токенов."""
        # Always keep system message
        if len(self.messages) > 1:
            system_msg = self.messages[0]
            recent_messages = self.messages[-(self.max_history * 2):]
            self.messages = [system_msg] + recent_messages
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError))
    )
    def get_completion(self) -> str:
        """
        Получить ответ от LLM.
        
        Returns:
            Текст ответа модели
        """
        try:
            response = self.client.chat.completions.create(
                model=self.config.model_name,
                messages=self.messages,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature
            )
            
            content = response.choices[0].message.content
            
            # Add to history
            self.add_assistant_message(content)
            
            return content
        
        except Exception as e:
            logger.error(f"LLM API error: {e}")
            raise


# ============================================================================
# Autonomous Agent - Main orchestrator
# ============================================================================

class AutonomousAgent:
    """
    Автономный агент для решения задач в браузере.
    
    Архитектура:
    1. Observe - получить состояние страницы
    2. Think - запросить решение у LLM
    3. Act - выполнить действие
    4. Repeat
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.browser = BrowserManager(config)
        self.llm = LLMClient(config)
        self.dom_processor = SmartDOMDistiller(
            max_elements=config.max_dom_elements,
            max_text_length=config.max_text_length
        )
        self.breadcrumbs = BreadcrumbsTracker()
        
        # State
        self.task = ""
        self.step_count = 0
        self.task_completed = False
        self.task_report = []
    
    def run(self, task: str, starting_url: Optional[str] = None) -> bool:
        """
        Выполнить задачу.
        
        Args:
            task: Описание задачи
            starting_url: Начальный URL (опционально, если None - начинаем с about:blank)
        
        Returns:
            True если задача выполнена успешно
        """
        self.task = task
        logger.info(f"\n{'='*70}")
        logger.info(f"TASK: {task}")
        logger.info(f"{'='*70}\n")
        
        try:
            # Start browser
            self.browser.start()
            
            # ZERO-TOUCH UX: Если URL не указан, начинаем с about:blank
            if starting_url is None:
                logger.info("No starting URL provided, agent will choose the first URL")
                self.browser.navigate("about:blank")
            else:
                self.browser.navigate(starting_url)
            
            # Initialize LLM with system prompt
            self._init_system_prompt()
            
            # Main agent loop
            while self.step_count < self.config.max_steps:
                self.step_count += 1
                logger.info(f"\n--- STEP {self.step_count}/{self.config.max_steps} ---")
                
                # 1. OBSERVE
                observation = self._observe()
                
                # Check for CAPTCHA
                if observation.get('captcha_detected'):
                    logger.error("CAPTCHA detected, cannot proceed")
                    self.task_report.append(f"Step {self.step_count}: CAPTCHA detected")
                    break
                
                # 2. THINK
                decision = self._think(observation)
                
                if not decision:
                    logger.error("Failed to get valid decision from LLM")
                    self.task_report.append(f"Step {self.step_count}: LLM decision failed")
                    break
                
                # Log thought process
                if 'thought' in decision:
                    logger.info(f"Thinking: {decision['thought']}")
                    self.task_report.append(f"Step {self.step_count} Thinking: {decision['thought']}")
                
                # Check for task completion
                if decision.get('action_type') == 'done':
                    logger.info("✓ Task marked as complete by agent")
                    self.task_completed = True
                    self.task_report.append(f"Step {self.step_count}: Task completed")
                    break
                
                # 3. ACT
                result = self._act(decision)
                
                # Log result
                logger.info(f"Action result: {result.get('message', 'N/A')}")
                self.task_report.append(
                    f"Step {self.step_count} Action: {decision.get('action_type')} - {result.get('message')}"
                )
                
                if not result.get('success'):
                    logger.warning(f"Action failed: {result.get('message')}")
                
                # Check for loops
                is_looping, warning = self.breadcrumbs.check_loop()
                if is_looping:
                    logger.error(warning)
                    self.task_report.append(f"Step {self.step_count}: {warning}")
                    # Add warning to LLM context
                    self.llm.add_user_message(
                        f"WARNING: {warning}\n"
                        "You are stuck in a loop. Please try a completely different approach."
                    )
            
            # Check if max steps reached
            if self.step_count >= self.config.max_steps:
                logger.warning("Max steps reached without completion")
                self.task_report.append("Max steps reached")
            
            return self.task_completed
        
        except KeyboardInterrupt:
            logger.info("\nTask interrupted by user")
            self.task_report.append("Interrupted by user")
            raise
        
        except Exception as e:
            logger.error(f"Fatal error during task execution: {e}", exc_info=True)
            self.task_report.append(f"Fatal error: {str(e)}")
            return False
        
        finally:
            # GRACEFUL DEGENERACY: Финальный отчет
            self._save_final_report()
            
            # Final screenshot
            try:
                screenshot_path = self.browser.take_screenshot("final")
                logger.info(f"Final screenshot: {screenshot_path}")
            except:
                pass
            
            # Cleanup
            self.browser.stop()
    
    def _init_system_prompt(self):
        """Инициализировать системный промпт для LLM."""
        system_prompt = f"""You are an autonomous web navigation agent. Your task is to help users accomplish goals on the web.

TASK: {self.task}

You work in a loop:
1. OBSERVE - You receive the current page state (URL, title, interactive elements)
2. THINK - You reason about what to do next (ALWAYS include your reasoning in "thought" field)
3. ACT - You choose an action to perform

Available actions:
- navigate: Go to a URL (use this if starting from about:blank)
- click: Click an element by ID
- type: Type text into an input field
- select: Select an option from a dropdown
- scroll: Scroll the page (up/down)
- wait: Wait for N seconds
- done: Mark task as complete

Response format (JSON):
{{
    "thought": "Your reasoning about what to do next and why",
    "action_type": "navigate|click|type|select|scroll|wait|done",
    "element_id": 123,  // Required for click/type/select
    "args": {{  // Action-specific arguments
        "url": "https://example.com",  // For navigate
        "text": "search query",  // For type
        "value": "option1",  // For select
        "direction": "down",  // For scroll
        "seconds": 2  // For wait
    }}
}}

IMPORTANT RULES:
1. ALWAYS start with "thought" - explain your reasoning
2. Be methodical and careful
3. If you make a mistake, try a different approach
4. When task is complete, use action_type: "done"
5. Prefer simple, direct paths to the goal
6. If you encounter obstacles, adapt your strategy

SPECIAL INSTRUCTIONS:
- If starting from about:blank, your FIRST action should be to navigate to a relevant URL
- You can use search engines (google.com, bing.com) to find information
- Be patient with slow pages - wait if needed
- If an element is not found, try scrolling or navigating differently
"""
        
        self.llm.add_system_message(system_prompt)
    
    def _observe(self) -> Dict:
        """
        OBSERVE: Получить текущее состояние страницы.
        
        Returns:
            Dict с информацией о странице
        """
        logger.info("Observing page state...")
        
        # Close any popups
        if self.config.popup_detection_enabled:
            self.browser.close_popups()
        
        # Check for CAPTCHA
        captcha_detected = self.browser.detect_captcha()
        
        # Get page HTML
        html = self.browser.page.content()
        
        # Process DOM
        dom_text, element_map = self.dom_processor.process_page(html, self.browser.page)
        
        # Store element map for later use
        self.browser._current_element_map = element_map
        
        return {
            'dom': dom_text,
            'element_map': element_map,
            'captcha_detected': captcha_detected,
            'url': self.browser.page.url,
            'title': self.browser.page.title()
        }
    
    def _think(self, observation: Dict) -> Optional[Dict]:
        """
        THINK: Запросить решение у LLM.
        
        Args:
            observation: Результат наблюдения
        
        Returns:
            Dict с решением или None при ошибке
        """
        logger.info("Consulting LLM for next action...")
        
        # Build observation message
        obs_message = f"""Current page state:

{observation['dom']}

Recent actions:
{self.breadcrumbs.get_summary()}

What should I do next to accomplish the task?
Remember to include your "thought" explaining your reasoning!
"""
        
        self.llm.add_user_message(obs_message)
        
        # Get LLM response
        try:
            response = self.llm.get_completion()
            logger.debug(f"LLM response: {response}")
            
            # Parse JSON decision
            decision = self._parse_json(response)
            
            if not decision:
                logger.error("Failed to parse LLM response as JSON")
                return None
            
            # Validate decision
            if not self._validate_decision(decision):
                logger.error("Invalid decision from LLM")
                return None
            
            return decision
        
        except Exception as e:
            logger.error(f"Error getting LLM decision: {e}")
            return None
    
    def _act(self, decision: Dict) -> Dict:
        """
        ACT: Выполнить действие.
        
        Args:
            decision: Решение от LLM
        
        Returns:
            Dict с результатом выполнения
        """
        action_type = decision['action_type']
        logger.info(f"Executing action: {action_type}")
        
        # Record action in breadcrumbs
        target = decision.get('element_id', decision.get('args', {}).get('url', 'N/A'))
        self.breadcrumbs.record_action(action_type, target)
        
        # Human-like delay before action
        delay = random.uniform(self.config.min_action_delay, self.config.max_action_delay)
        time.sleep(delay)
        
        # Execute action
        if action_type == 'navigate':
            url = decision.get('args', {}).get('url')
            return self._action_navigate(url)
        
        elif action_type == 'click':
            element_id = decision.get('element_id')
            return self._action_click(element_id)
        
        elif action_type == 'type':
            element_id = decision.get('element_id')
            text = decision.get('args', {}).get('text', '')
            return self._action_type(element_id, text)
        
        elif action_type == 'select':
            element_id = decision.get('element_id')
            value = decision.get('args', {}).get('value', '')
            return self._action_select(element_id, value)
        
        elif action_type == 'scroll':
            direction = decision.get('args', {}).get('direction', 'down')
            return self._action_scroll(direction)
        
        elif action_type == 'wait':
            seconds = decision.get('args', {}).get('seconds', 2)
            return self._action_wait(seconds)
        
        else:
            return {'success': False, 'message': f"Unknown action: {action_type}"}
    
    # Action implementations with human-like behavior
    
    def _action_click(self, element_id: int) -> Dict:
        """
        Кликнуть на элемент с человекоподобным поведением.
        
        Реализация:
        1. Найти элемент
        2. Получить его центр
        3. Переместить мышь к центру с random jitter
        4. Кликнуть
        """
        try:
            element_map = self.browser._current_element_map
            
            if element_id not in element_map:
                return {'success': False, 'message': f"Element {element_id} not found"}
            
            element_data = element_map[element_id]
            selector = element_data['selector']
            
            logger.debug(f"Clicking element: {selector}")
            
            # Find element
            locator = self.browser.page.locator(selector).first
            
            # Get element's bounding box
            box = locator.bounding_box()
            if not box:
                return {'success': False, 'message': "Element not visible"}
            
            # Calculate center with random jitter
            center_x = box['x'] + box['width'] / 2
            center_y = box['y'] + box['height'] / 2
            
            # Add small random offset (±5 pixels)
            jitter_x = random.uniform(-5, 5)
            jitter_y = random.uniform(-5, 5)
            
            target_x = center_x + jitter_x
            target_y = center_y + jitter_y
            
            # HUMAN-LIKE: Move mouse gradually
            self.browser.page.mouse.move(
                target_x, 
                target_y, 
                steps=self.config.mouse_move_steps
            )
            
            # Small pause before click
            time.sleep(random.uniform(0.1, 0.3))
            
            # Click
            self.browser.page.mouse.click(target_x, target_y)
            
            # Wait for potential navigation/changes
            time.sleep(1)
            
            return {'success': True, 'message': f"Clicked element {element_id}"}
        
        except Exception as e:
            logger.error(f"Click failed: {e}")
            return {'success': False, 'message': f"Click error: {e}"}
    
    def _action_type(self, element_id: int, text: str) -> Dict:
        """
        Ввести текст с человекоподобной скоростью.
        
        Реализация:
        1. Найти элемент
        2. Кликнуть на него (фокус)
        3. Ввести текст посимвольно с задержкой
        """
        try:
            element_map = self.browser._current_element_map
            
            if element_id not in element_map:
                return {'success': False, 'message': f"Element {element_id} not found"}
            
            element_data = element_map[element_id]
            selector = element_data['selector']
            
            logger.debug(f"Typing into element: {selector}")
            
            # Find and click element (to focus)
            locator = self.browser.page.locator(selector).first
            locator.click()
            
            # Clear existing text
            locator.fill('')
            
            # HUMAN-LIKE: Type with random delay between characters
            for char in text:
                self.browser.page.keyboard.type(char)
                delay = random.uniform(
                    self.config.typing_delay_min, 
                    self.config.typing_delay_max
                )
                time.sleep(delay / 1000)  # Convert ms to seconds
            
            # Small pause after typing
            time.sleep(0.5)
            
            return {'success': True, 'message': f"Typed '{text}' into element {element_id}"}
        
        except Exception as e:
            logger.error(f"Type failed: {e}")
            return {'success': False, 'message': f"Type error: {e}"}
    
    def _action_select(self, element_id: int, value: str) -> Dict:
        """Выбрать опцию в select."""
        try:
            element_map = self.browser._current_element_map
            
            if element_id not in element_map:
                return {'success': False, 'message': f"Element {element_id} not found"}
            
            element_data = element_map[element_id]
            selector = element_data['selector']
            
            logger.debug(f"Selecting '{value}' in: {selector}")
            
            # Select option
            locator = self.browser.page.locator(selector).first
            locator.select_option(value)
            
            time.sleep(0.5)
            
            return {'success': True, 'message': f"Selected '{value}'"}
        
        except Exception as e:
            return {'success': False, 'message': f"Select failed: {e}"}
    
    def _action_scroll(self, direction: str) -> Dict:
        """Прокрутить страницу."""
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
        """Перейти по URL."""
        try:
            success = self.browser.navigate(url)
            
            if success:
                return {'success': True, 'message': f"Navigated to {url}"}
            else:
                return {'success': False, 'message': f"Navigation failed"}
        
        except Exception as e:
            return {'success': False, 'message': f"Navigate error: {e}"}
    
    def _action_wait(self, seconds: int) -> Dict:
        """Подождать указанное количество секунд."""
        try:
            time.sleep(int(seconds))
            return {'success': True, 'message': f"Waited {seconds}s"}
        
        except Exception as e:
            return {'success': False, 'message': f"Wait failed: {e}"}
    
    # Utility methods
    
    def _parse_json(self, response: str) -> Optional[Dict]:
        """Распарсить JSON из ответа LLM (с устойчивостью к ошибкам)."""
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
        """Валидировать решение от LLM."""
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
    
    def _save_final_report(self):
        """
        GRACEFUL DEGENERACY: Сохранить финальный отчет о выполнении задачи.
        """
        try:
            reports_dir = Path("./reports")
            reports_dir.mkdir(exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = reports_dir / f"task_report_{timestamp}.md"
            
            # Build report content
            report_lines = [
                f"# Task Execution Report",
                f"",
                f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"**Task:** {self.task}",
                f"**Status:** {'✓ COMPLETED' if self.task_completed else '✗ FAILED'}",
                f"**Steps Taken:** {self.step_count}/{self.config.max_steps}",
                f"",
                f"## Execution Log",
                f""
            ]
            
            for i, entry in enumerate(self.task_report, 1):
                report_lines.append(f"{i}. {entry}")
            
            report_lines.append(f"")
            report_lines.append(f"## Final State")
            report_lines.append(f"- URL: {self.browser.page.url if self.browser.page else 'N/A'}")
            report_lines.append(f"- Title: {self.browser.page.title() if self.browser.page else 'N/A'}")
            
            # Write report
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(report_lines))
            
            logger.info(f"✓ Task report saved: {report_path}")
        
        except Exception as e:
            logger.error(f"Failed to save report: {e}")


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Главная точка входа."""
    print("\n" + "="*70)
    print("   COGNIWEB AGENT v3.5 - FULLY AUTONOMOUS")
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
    
    # ZERO-TOUCH UX: Убрать запрос URL, агент начнет с about:blank
    starting_url = None
    
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