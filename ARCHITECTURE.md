# Архитектура проекта

Техническая документация архитектурных решений автономного браузер-агента.

## Обзор

Проект построен как **модульный монолит** — компромисс между монолитной и микросервисной архитектурой. Код организован в модули с чёткими границами, но работает в едином процессе.

### Почему модульный монолит?

**Преимущества**:
- Простота деплоя (один процесс, один Docker-образ)
- Отсутствие network overhead между модулями
- Чёткие границы ответственности
- Возможность выделения модулей в сервисы позже

**Недостатки**:
- Невозможность масштабировать модули независимо
- Один язык программирования для всего стека
- Риск размытия границ при недисциплине

## Слои архитектуры

```
┌─────────────────────────────────────┐
│      main.py (Entry Point)          │
│  Signal Handling, Orchestration     │
└──────────────┬──────────────────────┘
               │
┌──────────────▼──────────────────────┐
│     Agent Layer (Orchestration)     │
│   orchestrator.py - ReAct Loop      │
└──────────────┬──────────────────────┘
               │
       ┌───────┴────────┐
       │                │
┌──────▼─────┐   ┌─────▼──────┐
│Infrastructure│  │   Core     │
│  browser.py  │  │ models.py  │
│   llm.py     │  │exceptions │
└──────────────┘  └────────────┘
       │                │
┌──────▼────────────────▼──────────┐
│     Config Layer (Settings)      │
│   settings.py - Pydantic Config  │
└──────────────────────────────────┘
```

### 1. Entry Point Layer (`main.py`)

**Ответственность**:
- Инициализация приложения
- Signal handling (SIGINT/SIGTERM)
- Dependency injection setup
- Error handling верхнего уровня

**Ключевые компоненты**:
```python
class GracefulShutdown:
    """Обработка shutdown сигналов"""
    
async def main() -> int:
    """Main async entry point"""
    # 1. Load settings
    # 2. Setup signal handlers
    # 3. Initialize services (DI)
    # 4. Run orchestrator
    # 5. Cleanup
```

**Почему async main**:
- Playwright требует asyncio
- Позволяет concurrent операции в будущем
- Современный Python стандарт

### 2. Config Layer (`src/config/`)

**Ответственность**:
- Загрузка environment variables
- Валидация конфигурации
- Создание директорий

**Файлы**:
- `settings.py` — Pydantic Settings с валидаторами
- `__init__.py` — Экспорт `load_settings()`

**Пример валидации**:
```python
class Settings(BaseSettings):
    api_key: str = Field(..., alias="OPENAI_API_KEY")
    
    @field_validator("api_key")
    def validate_api_key(cls, v: str) -> str:
        if v in ["your_api_key_here", "test"]:
            raise ValueError("Invalid API key")
        return v
```

**Почему Pydantic Settings**:
- Type-safe конфигурация
- Валидация при загрузке (fail-fast)
- Автодокументация через Field descriptions
- Лёгкое тестирование через overrides

### 3. Core Layer (`src/core/`)

**Ответственность**:
- Доменные модели (data structures)
- Бизнес-логика (без I/O)
- Иерархия исключений

**Файлы**:
- `models.py` — AgentAction, TaskResult, ObservationState
- `exceptions.py` — Custom exceptions
- `__init__.py` — Экспорт публичного API

**Ключевые модели**:

```python
class AgentAction(BaseModel):
    """Действие агента"""
    thought: str              # Reasoning
    tool: Literal[            # Название tool
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
    ]
    args: Dict[str, Any]      # Аргументы

class TaskResult(BaseModel):
    """Результат выполнения задачи"""
    success: bool
    summary: str
    steps_taken: int
    total_duration_seconds: float
    final_url: Optional[str]
    context_data: Dict[str, Any]

class ActionResult(BaseModel):
    """Результат одного действия"""
    success: bool
    message: str
    data: Optional[Dict[str, Any]]
    error: Optional[str]
```

**Иерархия исключений**:
```python
AgentBaseException
├── ConfigurationError
├── NetworkError
├── BrowserError
├── SelectorError
├── ActionError
├── ValidationError
├── LLMError
├── LoopDetectedError
├── CaptchaDetectedError
├── TimeoutError
└── AgentCriticalError
```

**Почему Pydantic Models**:
- Runtime validation (критично для LLM outputs)
- Автодокументация структуры данных
- Сериализация/десериализация из коробки
- IDE autocomplete

### 4. Infrastructure Layer (`src/infrastructure/`)

**Ответственность**:
- Взаимодействие с внешними системами
- I/O операции (network, browser)
- Retry логика с backoff

**Файлы**:
- `browser.py` — BrowserService (Playwright)
- `llm.py` — LLMService (OpenAI SDK)
- `__init__.py` — Экспорт сервисов

#### BrowserService

**Ключевые возможности**:
```python
class BrowserService:
    async def navigate(self, url: str) -> ActionResult
    async def click_element(self, element_id: int) -> ActionResult
    async def type_text(self, element_id: int, text: str) -> ActionResult
    async def upload_file(self, element_id: int, file_path: str) -> ActionResult
    async def scroll_page(self, direction: str, amount: int) -> ActionResult
    async def take_screenshot(self, path: str) -> str
    async def go_back(self) -> ActionResult
    async def get_interactive_elements(self) -> List[Dict]
    async def detect_captcha(self) -> bool
```

**Паттерны**:
- Context manager для guaranteed cleanup
- Retry с exponential backoff
- Human-like typing с jitter
- Auto-snapshots при ошибках
- Stealth mode (playwright-stealth)

**Реализация retry**:
```python
async def _retry_action(self, action_fn, max_attempts: int):
    for attempt in range(max_attempts):
        try:
            return await action_fn()
        except PlaywrightTimeoutError:
            if attempt == max_attempts - 1:
                raise
            await asyncio.sleep(2 ** attempt)  # exponential backoff
```

#### LLMService

**Ключевые возможности**:
```python
class LLMService:
    async def generate_action(
        self, 
        messages: List[Dict[str, str]],
        temperature: float
    ) -> AgentAction
```

**Паттерны**:
- Валидация responses через Pydantic
- Retry с tenacity
- Token tracking
- Прокси support

**Обработка ошибок**:
```python
try:
    response = await self.client.chat.completions.create(...)
    action = AgentAction.model_validate_json(response.choices[0].message.content)
except ValidationError as e:
    raise LLMError(f"Invalid response format: {e}")
```

### 5. Agent Layer (`src/agent/`)

**Ответственность**:
- Оркестрация ReAct цикла
- State management
- Loop detection
- Context trimming

**Файлы**:
- `orchestrator.py` — AgentOrchestrator
- `__init__.py` — Экспорт

#### ReAct Loop

```python
async def run(self, task: str, starting_url: Optional[str]) -> TaskResult:
    for step in range(max_steps):
        # 1. OBSERVE
        observation = await self._get_observation()
        
        # 2. THINK
        action = await self.llm.generate_action(
            messages=self.conversation_history
        )
        
        # 3. ACT
        result = await self._execute_action(action)
        
        # 4. CHECK COMPLETION
        if action.tool == "done":
            return TaskResult(...)
        
        # 5. DETECT LOOPS
        self._check_for_loops(action, result)
```

**State management**:
```python
self.conversation_history: List[Dict]  # История диалога с LLM
self.action_history: List[Tuple]       # История действий для loop detection
self.context_data: Dict                # Данные, сохранённые агентом
self.previous_observation: str         # Кэш последнего observation
```

#### Smart Loop Detection

**Проблема старого подхода**:
- Считал "Invalid element ID" за зацикливание
- Не различал ошибки валидации и реальные циклы

**Новый подход**:
```python
def _check_for_loops(self, action: AgentAction, result: ActionResult):
    # Track (tool, target, success)
    signature = (action.tool, action.args.get("element_id"), result.success)
    self.action_history.append(signature)
    
    # Цикл = SAME action on SAME target failing repeatedly
    if len(set(recent_3_actions)) == 1 and not success:
        raise LoopDetectedError()
```

### 6. Utils Layer (`src/utils/`)

**Ответственность**:
- Вспомогательные pure functions
- DOM processing
- Утилиты без side effects

**Файлы**:
- `dom.py` — DOMProcessor для tree shaking
- `__init__.py` — Экспорт

**DOMProcessor**:
```python
class DOMProcessor:
    def simplify_dom(self, html: str) -> str:
        """
        Сжимает DOM на 70% через:
        1. Удаление non-interactive элементов
        2. Усечение длинных текстов
        3. Удаление атрибутов (кроме id, class)
        """
```

## Потоки данных

### 1. User Task → Task Result

```
User Input
    ↓
main.py (validate input)
    ↓
orchestrator.run(task)
    ↓
┌─────── ReAct Loop ───────┐
│                          │
│  observe() → LLM → act() │
│       ↑           ↓      │
│       └───────────┘      │
└──────────────────────────┘
    ↓
TaskResult (success/failure)
```

### 2. LLM Request Flow

```
orchestrator.run()
    ↓
conversation_history (system + observations)
    ↓
llm.generate_action(messages)
    ↓
OpenRouter API (with retry)
    ↓
JSON response → Pydantic validation
    ↓
AgentAction (thought, tool, args)
```

### 3. Browser Action Flow

```
AgentAction (tool="click_element", args={"element_id": 42})
    ↓
orchestrator._execute_action()
    ↓
browser.click_element(element_id)
    ↓
element_map[42] → CSS selector
    ↓
Playwright page.click(selector) with retry
    ↓
ActionResult (success, message, data)
```

## Ключевые паттерны

### Dependency Injection

**Почему**:
- Тестируемость (легко мокировать)
- Явные зависимости
- Гибкость (можно подменять реализации)

**Пример**:
```python
# Bad: скрытые зависимости
class Agent:
    def __init__(self):
        self.browser = BrowserService()  # создаёт внутри

# Good: explicit dependencies
class Agent:
    def __init__(self, browser: BrowserService):
        self.browser = browser  # получает извне

# Usage (в main.py)
browser = BrowserService(settings)
agent = Agent(browser)  # инъекция
```

### Context Managers

**Почему**:
- Гарантированный cleanup
- Защита от resource leaks
- Pythonic resource management

**Пример**:
```python
class BrowserService:
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await asyncio.shield(self.close())  # cleanup даже при cancel
        return False

# Usage
async with BrowserService(settings) as browser:
    await browser.navigate("https://example.com")
    # browser автоматически закроется даже при exception
```

### Async/Await

**Почему**:
- Playwright требует async
- Better resource utilization
- Concurrent operations
- Future-proof

**Пример**:
```python
# Sync (блокирует)
page.goto("https://example.com")  # CPU простаивает 2 секунды

# Async (можно делать другую работу)
await page.goto("https://example.com")  # event loop переключается
```

### Retry с Exponential Backoff

**Почему**:
- Временные сетевые ошибки
- API rate limits
- Lazy-loaded элементы

**Пример**:
```python
for attempt in range(max_attempts):
    try:
        return await action()
    except TransientError:
        if attempt == max_attempts - 1:
            raise
        delay = 2 ** attempt  # 1s, 2s, 4s, 8s
        await asyncio.sleep(delay)
```

## Решения безопасности

### Anti-Ban Protection

**Stealth Mode**:
- playwright-stealth патчит WebDriver признаки
- Скрывает `navigator.webdriver`
- Защищает от canvas/WebGL fingerprinting

**Human-like Typing**:
```python
async def type_humanly(self, text: str):
    for char in text:
        await page.keyboard.type(char)
        delay = random.randint(50, 150)  # jitter
        await asyncio.sleep(delay / 1000)
```

**Slow Motion**:
```python
browser = playwright.chromium.launch(slow_mo=50)  # 50ms между действиями
```

### Error Recovery

**Auto-Snapshots**:
```python
async def _capture_error_snapshot(self, error_type: str):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    screenshot_path = f"./screenshots/error_{error_type}_{timestamp}.png"
    await page.screenshot(path=screenshot_path)
    
    html_path = f"./screenshots/error_{error_type}_{timestamp}.html"
    html = await page.content()
    Path(html_path).write_text(html)
    
    return screenshot_path, html_path
```

**Graceful Shutdown**:
```python
signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

async def __aexit__(self, ...):
    await asyncio.shield(self.close())  # не прерывается
```

## Масштабируемость

### Текущие ограничения

- Один агент на процесс
- Один браузер на агента
- Синхронный ReAct loop (одно действие за раз)

### Возможные улучшения

**Multi-page agents**:
```python
async def run_parallel_agents(tasks: List[str]):
    async with BrowserService(settings) as browser:
        pages = [await browser.new_page() for _ in tasks]
        results = await asyncio.gather(*[
            orchestrator.run(task, page) 
            for task, page in zip(tasks, pages)
        ])
    return results
```

**Distributed architecture**:
```
┌─────────────┐
│   API GW    │
└──────┬──────┘
       │
   ┌───┴───┐
   │ Queue │ (RabbitMQ)
   └───┬───┘
       │
   ┌───▼───┐
   │Workers│ (N agents)
   └───┬───┘
       │
   ┌───▼────┐
   │Results │ (Redis)
   └────────┘
```

## Тестирование

### Unit Tests

**Мокирование зависимостей**:
```python
@pytest.mark.asyncio
async def test_orchestrator():
    browser = AsyncMock(BrowserService)
    llm = AsyncMock(LLMService)
    settings = Settings(api_key="test")
    
    orchestrator = AgentOrchestrator(settings, browser, llm)
    
    # Test loop detection
    for _ in range(6):
        orchestrator._check_for_loops(
            action=AgentAction(...),
            result=ActionResult(success=False)
        )
    # Should raise LoopDetectedError
```

### Integration Tests

**С реальным браузером**:
```python
@pytest.mark.asyncio
async def test_browser_navigation():
    settings = Settings(api_key="test")
    async with BrowserService(settings) as browser:
        result = await browser.navigate("https://example.com")
        assert result.success
        assert await browser.get_current_url() == "https://example.com/"
```

## Метрики и мониторинг

### Рекомендуемые метрики

- **Task success rate**: % успешных задач
- **Average steps per task**: среднее кол-во шагов
- **LLM token usage**: потребление токенов
- **Browser resource usage**: CPU/RAM
- **Error rate by type**: частота каждого типа ошибок
- **Loop detection triggers**: частота детекции циклов

### Логирование

**Текущая реализация**:
```python
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('agent.log'),
        logging.StreamHandler()
    ]
)
```

**Рекомендуемое улучшение**:
- Structured logging (structlog/loguru)
- JSON logs для парсинга
- Log levels по модулям
- Correlation IDs для трейсинга

---

**Дата обновления**: 2026-01-31
