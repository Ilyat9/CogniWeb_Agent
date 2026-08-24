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

---

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

---

## Инфраструктура и CI/CD

### Docker контейнеризация

**Философия**: Воспроизводимость окружения + безопасность

#### Многоэтапная сборка (Multi-stage build)

```dockerfile
# Stage 1: Builder (as root)
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy
RUN apt-get update && apt-get install -y ca-certificates fonts-liberation
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

# Stage 2: Runtime (non-root user)
RUN groupadd agentuser && useradd -m -g agentuser agentuser
COPY --chown=agentuser:agentuser . /app/
USER agentuser
CMD ["python", "main.py"]
```

**Почему два stage**:

1. **Builder stage** (root):
   - Установка системных зависимостей (fonts, ca-certificates)
   - Установка Python пакетов (требует write в `/usr/local`)
   - Установка Playwright браузеров (требует `/ms-playwright`)

2. **Runtime stage** (non-root):
   - Минимизация attack surface (не-привилегированный пользователь)
   - Соответствие best practices для production образов
   - Защита от container breakout атак

**Почему базовый образ Playwright**:
- ✅ Предустановлены системные зависимости (libglib, libgtk, etc)
- ✅ Совместимость с Ubuntu 22.04 (Jammy)
- ✅ Оптимизирован для headless браузеров
- ✅ Официально поддерживается Microsoft

**Безопасность**:
```dockerfile
# Non-root пользователь (UID не фиксирован для совместимости)
RUN groupadd agentuser && \
    useradd -m -g agentuser agentuser

# Explicit ownership для всех файлов приложения
COPY --chown=agentuser:agentuser . /app/

# Переключение на non-root
USER agentuser
```

### CI/CD пайплайн (GitHub Actions)

**Философия**: Shift-left testing + автоматизация качества

#### Workflow `.github/workflows/ci.yml`

```yaml
name: CI Pipeline

on:
  push:
    branches: [ main, develop ]
  pull_request:
    branches: [ main, develop ]
```

**5 параллельных jobs**:

1. **Lint** (Code Quality)
   - Ruff: fast Python linter (замена Flake8 + Pylint)
   - Black: code formatting проверка
   - isort: import sorting проверка

2. **Test** (Matrix: Python 3.10, 3.11, 3.12)
   - Pytest с async поддержкой
   - Coverage отчёты (Codecov integration)
   - Playwright браузеры с системными deps

3. **Security** (Vulnerability Scanning)
   - Safety: проверка известных CVE в зависимостях
   - Bandit: SAST анализ кода (SQL injection, hardcoded secrets)

4. **Docker** (Build verification, только для main/develop)
   - Multi-stage build с кэшированием слоёв
   - Smoke test образа (version check)

5. **Notify** (Results aggregation)
   - Агрегация результатов всех jobs
   - Fail-fast на критичных ошибках

#### Кэширование в CI

**pip dependencies**:
```yaml
- uses: actions/cache@v4
  with:
    path: ~/.cache/pip
    key: ${{ runner.os }}-pip-${{ hashFiles('requirements.txt') }}
```

**Docker layers**:
```yaml
- uses: actions/cache@v4
  with:
    path: /tmp/.buildx-cache
    key: ${{ runner.os }}-buildx-${{ github.sha }}
```

**Результат**: ускорение CI на 60-70% (2min → 45sec для тестов)

### Makefile автоматизация

**Философия**: Developer Experience + CI/CD переиспользование

#### Ключевые команды

| Команда | CI эквивалент | Применение |
|---------|---------------|-----------|
| `make lint` | CI job: lint | Pre-commit hook |
| `make test-coverage` | CI job: test | Локальная проверка |
| `make security-check` | CI job: security | Pre-release audit |
| `make docker-build` | CI job: docker | Staging/Production deploy |
| `make ci` | Full CI pipeline | Pre-push validation |

**Пример: CI эмуляция локально**
```bash
make ci
# Запускает: lint → test-coverage → security-check
```

#### Dependency management

**Production vs Development**:
```makefile
install: ## Production dependencies
    pip install -r requirements.txt
    playwright install chromium

install-dev: install ## Add dev dependencies
    pip install -r requirements-dev.txt
    playwright install --with-deps chromium
```

**Почему разделение**:
- Production образ: 450MB (без dev зависимостей)
- Development окружение: 650MB (с pytest, ruff, black, mypy)
- CI кэш-эффективность (меньше инвалидаций)

### Конфигурация инструментов (pyproject.toml)

**Централизованная конфигурация**:

```toml
[tool.black]
line-length = 100
target-version = ['py311']

[tool.isort]
profile = "black"
line_length = 100

[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-ra --strict-markers"
```

**Почему pyproject.toml**:
- ✅ PEP 518 стандарт
- ✅ Единый источник правды для всех инструментов
- ✅ Совместимость с Poetry, PDM, Hatch
- ✅ Уменьшает clutter в корне проекта

---

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

---

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

---

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

---

## Тестирование

Проект покрыт Unit-тестами с использованием `pytest` и `pytest-asyncio`. Все внешние зависимости (LLM, Browser) изолированы через моки для обеспечения быстрого и надёжного тестирования без реальных API вызовов.

### Стратегия тестирования

**Принципы**:
- **Изоляция**: Все I/O операции (LLM API, браузер) мокируются через `AsyncMock`
- **Dependency Injection**: Позволяет легко подменять реальные сервисы на моки
- **Fast execution**: Тесты выполняются за <1 секунду без внешних зависимостей
- **High coverage**: Покрытие критичных компонентов (валидация, оркестрация, loop detection)

### Тестируемые компоненты

**1. Pydantic Validation (`models.py`)**:
```python
def test_valid_action():
    """Valid action should parse without errors."""
    action = AgentAction(
        thought="Navigate to example",
        tool="navigate",
        args={"url": "https://example.com"}
    )
    assert action.tool == "navigate"

def test_invalid_tool_name():
    """Invalid tool name should raise ValidationError."""
    with pytest.raises(ValidationError):
        AgentAction(
            thought="Invalid tool",
            tool="invalid_tool_name",
            args={}
        )
```

**2. Orchestrator Logic (`orchestrator.py`)**:
```python
@pytest.mark.asyncio
async def test_get_trimmed_history_preserves_system_prompt():
    """System prompt (index 0) must always be preserved."""
    orchestrator = AgentOrchestrator(mock_settings, mock_browser, mock_llm)
    
    # Simulate conversation with 15 messages
    orchestrator.conversation_history = [
        {"role": "system", "content": "SYSTEM_PROMPT"},
        {"role": "user", "content": "msg1"},
        # ... more messages
    ]
    
    trimmed = orchestrator.get_trimmed_history(window_size=5)
    
    # Should have: system prompt + last 5 messages = 6 total
    assert len(trimmed) == 6
    assert trimmed[0]["role"] == "system"
```

**3. LLM JSON Parsing (`llm.py`)**:
```python
def test_extract_json_from_code_block():
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
```

**4. Smart Loop Detection (`orchestrator.py`)** — критически важная защита от зацикливания:

**Проблема**: Агент может попасть в бесконечный цикл, повторяя одно и то же неуспешное действие.

**Решение**: Детекция последовательных идентичных неудачных действий с анализом триплета `(tool, target, success)`.

**Тестирование защиты от зацикливания**:
```python
@pytest.mark.asyncio
async def test_loop_detected_on_identical_failures(
    mock_settings, mock_browser, mock_llm
):
    """Should raise LoopDetectedError on 3 identical failures.
    
    Critical test: Validates that agent detects when it's stuck
    trying the same action on the same target repeatedly.
    """
    orchestrator = AgentOrchestrator(mock_settings, mock_browser, mock_llm)
    
    # Create identical action (same tool + same target)
    action = AgentAction(
        thought="Click button",
        tool="click_element",
        args={"element_id": 42}  # Target = element #42
    )
    
    # Failed result
    result = ActionResult(success=False, message="Element not found")
    
    # First attempt - no error expected
    orchestrator._check_for_loops(action, result)
    
    # Second attempt - still no error
    orchestrator._check_for_loops(action, result)
    
    # Third identical failure - should raise LoopDetectedError
    with pytest.raises(LoopDetectedError) as exc_info:
        orchestrator._check_for_loops(action, result)
    
    # Verify error message indicates being stuck
    assert "stuck" in str(exc_info.value).lower()
```

### Запуск тестов

```bash
# Все тесты с подробным выводом
pytest tests/test_agent_core.py -v

# Только тесты loop detection
pytest tests/test_agent_core.py::TestSmartLoopDetection -v

# С coverage report
pytest tests/test_agent_core.py --cov=src --cov-report=term-missing

# Через Makefile
make test-coverage
```

### CI/CD интеграция

**GitHub Actions workflow**:
```yaml
- name: Run pytest with coverage
  env:
    OPENAI_API_KEY: "sk-test-key-for-ci-not-real"
  run: |
    pytest tests/ \
      -v \
      --cov=src \
      --cov-report=xml \
      --asyncio-mode=auto
      
- name: Upload coverage to Codecov
  uses: codecov/codecov-action@v4
  with:
    files: ./coverage.xml
```

---

## Новые возможности (Task 1–4)

Четыре расширения поверх исходной архитектуры. Ни одно из них не меняет поведение по умолчанию — каждое включается явно через `Settings` (см. `src/config/settings.py`), а деградация всегда безопасна: неудачная попытка новой фичи откатывается на прежнее поведение, а не роняет задачу.

### Task 1 — Local Provider Mode (`src/config/settings.py`)

`Settings.llm_provider_mode` ("cloud" | "local") объявлен раньше `api_key`/`api_base_url`/`model_name`, чтобы их `field_validator`-ы могли прочитать его через `info.data` (Pydantic v2 валидирует поля по порядку объявления). В режиме `cloud` работают все прежние проверки (формат ключа, HTTPS, блокировка Ollama-паттернов). В режиме `local` эти же валидаторы переключаются на минимальные проверки (непустой ключ, корректный `http(s)://` URL, непустое имя модели без требования формата `provider/model`).

`AgentOrchestrator._wait_for_rate_limit()` читает `settings.llm_provider_mode` и выбирает между `rate_limit_seconds` (облако, 15с по умолчанию) и `local_rate_limit_seconds` (локально, 1с по умолчанию, можно 0). Сам HTTP-клиент (`LLMService`) не меняется — и OpenRouter, и локальный сервер идут через один и тот же `AsyncOpenAI`-клиент, так как оба OpenAI-совместимы.

### Task 2 — Обобщённая деградация клика/DOM (`src/infrastructure/browser.py`, `src/utils/dom.py`)

`BrowserService.click_element_safe()` — не одна попытка с одним фолбэком, а лестница уровней, от наименее к наиболее инвазивному, с остановкой на первом сработавшем:
1. обычный клик по селектору;
2. при strict-mode violation (селектор не уникален) — `.first`;
3. при признаках перекрытия/нестабильности (`intercept`, `obscures`, `not stable`, `outside of the viewport`) — попытка закрыть типичный cookie/modal-оверлей (`_try_dismiss_overlay()`) и повтор с начала;
4. если это не помогло — forced click (`force=True`, в обход проверок актуальности состояния Playwright);
5. последний резерв — прямой `dispatch_event('click')` через JS.

`DOMProcessor.get_interactive_elements()` (`src/utils/dom.py`) расширен набором CSS/ARIA-селекторов (`menuitem`, `tab`, `checkbox`, `radio`, `option`, `contenteditable`, `summary`) и постобработкой `_annotate_duplicate_text()`, которая добавляет порядковый суффикс (`"Apply (#2 of 5 similar)"`) элементам с одинаковым видимым текстом — сигнал для LLM в ситуациях, где несколько визуально разных элементов текстуально неотличимы.

### Task 3 — Context Compaction (`src/agent/orchestrator.py`)

`AgentOrchestrator._maybe_compact_history()` вызывается в основном цикле (`run()`) после добавления очередного наблюдения, но до вызова LLM за действием — так, чтобы решение "думать" уже видело потенциально сжатую историю. Триггер — эвристика (число сообщений ИЛИ оценка размера в токенах через `len(str(content)) // 4`, без реального токенайзера, в русле уже принятого в проекте подхода — см. SELF_REVIEW.md). При срабатывании делается отдельный запрос через новый метод `LLMService.generate_text()` (в отличие от `generate_action()`, не требует JSON/`AgentAction`-схему — просто произвольный текст), и `conversation_history` заменяется на `[system_prompt, compact_summary]`. Оригинальный текст задачи (`self.task`) и уже накопленный `context_data` не входят в `conversation_history` и потому переживают компакцию без изменений; они также явно передаются в промпт суммаризации, чтобы отчёт не терял ключевые факты. Существующий `get_trimmed_history()` (жёсткое обрезание по числу сообщений) продолжает работать поверх результата компакции как отдельный, более дешёвый уровень защиты — эти два механизма не заменяют, а дополняют друг друга.

### Task 4 — Vision Fallback с grounding (`src/agent/orchestrator.py`, `src/infrastructure/browser.py`)

`_get_observation()` сохраняет результат последнего извлечения DOM (`self._last_elements`, `self._last_extraction_error`) как атрибуты оркестратора, не меняя сигнатуру самого метода. `_should_use_vision_fallback()` использует их сразу после наблюдения, чтобы решить, нужен ли визуальный режим (сбой извлечения, пустой список, либо много элементов почти без текста). Grounding обеспечивается на уровне браузера: `BrowserService.capture_annotated_screenshot()` рисует рамки поверх живого DOM через `page.evaluate()` (используя тот же `data-agent-id`, что и текстовый режим, — единая система идентификаторов, без отдельного сопоставления координат) и снимает их сразу после скриншота. Модель получает мультимодальное сообщение (текст + `image_url` с base64 PNG) и инструкцию отвечать только номером элемента; `AgentAction` разбирается тем же путём, что и в текстовом режиме, — грounding сводится к тому, что ответ модели изначально ограничен теми же `element_id`, что уже понимает остальная система. `LLMService.generate_action()`/`generate_text()` типизируют `content` как `Any` именно ради этого мультимодального формата, не меняя остальную обработку ответа.

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

**Kubernetes deployment**:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cogniweb-agent
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: agent
        image: cogniweb-agent:latest
        resources:
          limits:
            memory: "2Gi"
            cpu: "1000m"
```

---

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
**Версия документа**: 2.0
---

## Фаза 4: Web UI, новые инструменты, stealth-режим

Четыре расширения (Задачи 1–4 фазы 4). Как и прежде: всё через флаги в `Settings`, дефолты не ломают существующее поведение (единственное намеренное исключение — stealth-режим, включён по умолчанию, т.к. не меняет функционального поведения, только надёжность сессии), деградация опциональных зависимостей безопасна (ленивый импорт + один warning за run, паттерн tiktoken).

### Задача 1 — Web UI поверх API (`src/api/app.py`, `src/api/static/`)

**Backend.** Шаги агента публикуются через in-memory pub/sub, а не парсинг `agent.log`: `AgentOrchestrator` принимает опциональный `event_sink` (sync-callback, получает dict-события `started`/`step`/`final`/`captcha`). `create_app(task_runner, settings=None)` строит на этом:

- `GET /tasks` — история задач (id, state, task, summary);
- `GET /task/{id}/steps` — все события шагов задачи (fallback-канал для клиентов без WebSocket);
- `WS /ws/task/{id}` — live-стрим: при подключении реплей уже записанных событий, затем новые до `final`;
- `GET /task/{id}/screenshot` — последний скриншот run'а (из `take_screenshot`-шагов) как файл;
- `POST /task/{id}/stop` — per-task graceful stop: тот же паттерн `shutdown_check`, что у глобального SIGTERM, но на конкретную задачу (оркестратор выходит на границе следующего шага, сохранив `context_data`); остановка queued-задачи превращает её в `StoppedByUser` без запуска;
- `GET /config` — текущие настройки с маскированием секретов (`mask_settings`: любое поле с `key/token/secret/password/credential` в имени → `***masked`);
- `GET /reports`, `GET /reports/{run_id}` — список и содержимое `reports/run_*.json` (run_id валидируется строгим паттерном + resolve-проверка внутри `REPORTS_DIR` — защита от traversal).

TaskRunner остаётся 2-аргументным контрактом: воркер интроспектирует сигнатуру и передаёт `emit=`/`stop_check=` только если раннер их объявил — все существующие тесты и CLI-раннеры работают без изменений.

**Frontend.** Single-file vanilla-JS SPA в `src/api/static/index.html`, отдаётся FastAPI через `StaticFiles(html=True)`, монтируется последним (API-маршруты приоритетнее). Выбор vanilla JS вместо React+Vite/HTMX обоснован: нулевой build-пайплайн, нулевые JS-зависимости, ноль новых backend-зависимостей для статики; WebSocket с polling-fallback на `/steps` покрывает live-прогресс. UI: форма запуска, пошаговый лог (thought/tool/args/status/duration), текущий скриншот + URL, история с детальным просмотром (`tokens_used`, `context_data`), отчёты в человекочитаемом виде (бейджи/таблицы), read-only конфиг, кнопка «Остановить», явный баннер капчи и captcha circuit breaker. Запуск: `make install-ui && make run-ui` (uvicorn на :8000).

### Задача 2 — Расширение набора инструментов (10 новых tools)

Каждый: `Literal` + `valid_tools` + валидация args в `models.py` → метод `BrowserService` → ветка диспетчера в `orchestrator.py` → system prompt → тесты (happy + error). Выбраны по ценности:

| Инструмент | Зачем |
|---|---|
| `wait_for_element` | условное ожидание вместо «слепого» `wait(seconds)` — убирает гонки с рендерингом/AJAX (главный источник ложных SelectorError) |
| `find_element_by_text` | семантический поиск по живому DOM (не только по бюджетно-обрезанному снапшоту) + регистрация свежих `element_id` |
| `extract_page_content` | очищенный Markdown страницы вместо сырого DOM — экономия 60–80% токенов на задачах чтения (см. Задачу 3) |
| `extract_structured_data` | таблицы страницы сразу в `context_data[key]` — закрывает сценарий «распарсить список X» одним вызовом |
| `hover_element` | hover-only контролы (меню, тултипы) |
| `press_key` | клавиатурные события/комбинации без element_id (Enter/Escape/Tab/Ctrl+A) |
| `list_tabs` / `switch_tab` | работа с вкладками, открывшимися по target=_blank клику (дополняет `new_page()`/`run_parallel_agents`) |
| `download_file` | явная обработка Playwright download-события; сохранение в `DOWNLOAD_ALLOWED_DIR`, filename приводится к basename (защита от traversal, аналог `upload_file`) |
| `go_forward` | симметрия `go_back` |

`request_human_input` из рекомендованного списка сознательно не реализован: human-in-the-loop канал уже существует (captcha-checkpoint: сохранение состояния + ожидание человека + circuit breaker), второй канал дублировал бы его с теми же свойствами.

Произвольный JS-инструмент не добавлялся (расширение поверхности атаки без явной необходимости).

### Задача 3 — Идеи Browser-Use и Crawl4AI (принятые решения реализованы как описано)

**Browser-Use (только визуальное распознавание, без пакета-зависимости, без captcha-solving сервисов).** Существующий vision fallback расширен вторым триггером: после `VISUAL_FALLBACK_ERROR_STREAK` (дефолт 2) подряд идущих шагов с ошибкой `InvalidElementId` следующий шаг переключается на аннотированный скриншот (set-of-marks: рамки с номерами поверх живого DOM через `page.evaluate`-оверлей, номер = тот же `element_id`, что в текстовом режиме; без Pillow/cairosvg). Стрик сбрасывается любым успешным шагом и после успешного vision-шага. Флаг: `ENABLE_VISUAL_FALLBACK` — алиас существовавшего до задачи `ENABLE_VISION_FALLBACK`; его дефолт `true` сохранён без изменений (правило проекта: дефолты существующих фичей не меняются), эффективное поведение по умолчанию всё равно выключено гейтом `MODEL_SUPPORTS_VISION` (дефолт `false`). Пакет `browser-use` НЕ устанавливается (свой оркестратор + своя версия Playwright = конфликт без выгоды). Сторонние платные captcha-сервисы (CapSolver/2captcha/…) не интегрируются: основной путь для показанной капчи не изменился — `CaptchaDetectedError` → checkpoint → ручное решение → circuit breaker (UX ожидания улучшен в Web UI: явный баннер капчи и статус breaker с предложением перезапуска).

**Crawl4AI (подход, обёртка с fallback, без второго браузерного движка).** `extract_page_content` берёт HTML уже открытой Playwright-страницы (`page.content()`) и конвертирует: сначала ленивый импорт `crawl4ai.markdown_generation_strategy.DefaultMarkdownGenerator` (используется ТОЛЬКО как HTML→Markdown конвертер, собственный браузер crawl4ai никогда не запускается), при любой ошибке/отсутствии пакета — встроенный беззависимый эвристический очиститель (`src/utils/extract.py`: снос `<script>/<style>/<nav>/<footer>/<aside>/…`, заголовки → `#`, ссылки → `[text](abs-url)`, списки → `- `, ячейки таблиц → pipe-строки). Флаг `ENABLE_MARKDOWN_EXTRACTION` (дефолт `false`); при выключенном флаге инструмент отвечает явной ошибкой `MarkdownExtractionDisabled`. Опциональная зависимость — `requirements-tools.txt`.

### Задача 4 — Stealth-режим браузера

Цель — снизить ложные срабатывания анти-бот детекции для ЛЕГИТИМНОЙ сессии (меньше лишних капч/блокировок). Это отдельная задача от captcha circuit breaker: здесь про снижение числа ложных срабатываний детекции, не про решение показанной капчи. Никакой обход уже показанного challenge не реализуется.

Реализация в `BrowserService`:
1. **Init-скрипты** (`context.add_init_script`, применяются ко всем будущим страницам): `navigator.webdriver` → `undefined`, `window.chrome` заглушка, `navigator.languages` согласован с локалью, непустые `navigator.plugins`/`mimeTypes` (Chrome PDF Viewer), WebGL vendor/renderer вместо заглушки headless-рендерера (`ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.1)`).
2. **Согласованный профиль контекста**: `STEALTH_USER_AGENT` / `STEALTH_LOCALE` / `STEALTH_TIMEZONE` / `STEALTH_VIEWPORT_*` применяются вместе; `Accept-Language` и `sec-ch-ua-platform` (в `captcha_avoidance_mode`) выводятся из тех же полей. Принцип: рассинхрон фингерпринта — более сильный сигнал детекции, чем «неидеальный, но цельный» профиль; поэтому «рандом ради рандома» не используется.
3. **playwright-stealth** — опциональная надстройка (ленивый импорт, один warning за run при отсутствии, при ошибке применения — continue): перенесён из базового `requirements.txt` в `requirements-tools.txt`. Встроенные init-скрипты работают и без него.
4. **Человекоподобное взаимодействие**: клику предшествует малая случайная пауза + многоточечная траектория `page.mouse.move()` с джиттером (не мгновенный прыжок в точные координаты); `type_text` уже вводит посимвольно со случайной задержкой (`TYPING_SPEED_MIN/MAX`). Источник рандома — уже одобренный в `.bandit` (`B311`).
5. **`ENABLE_STEALTH_MODE`** — единственный новый флаг с дефолтом `true` (старое имя `ENABLE_STEALTH` работает как алиас): функциональное поведение агента не меняется, только надёжность сессии, поэтому включение по умолчанию безопасно; выключение возвращает точное до-stealth поведение запуска (в т.ч. legacy UA в non-persistent ветке) — регрессионный тест это фиксирует.

Тестируется применение патчей на уровне вызовов Playwright API (моки), а не прохождение детекции на живых сайтах (недетерминировано, не для CI).

### Hardening-дополнение (доступ, live-статус, обёртка контента)

**Доступ к API.** `API_BIND_HOST` (дефолт `127.0.0.1`): `make run-ui` читает настройку через `uvicorn.run(..., host=load_settings().api_bind_host)`; Docker `MODE=api` биндит `0.0.0.0` сознательно (иначе порт недостижим с хоста) — задокументировано в Dockerfile. `API_AUTH_TOKEN` (опционально): FastAPI-зависимость `_require_token` вешается на все `/task*`-эндпоинты + `/config`/`/reports`; `/health` свободен (liveness-проби). Токен принимается **только** в заголовке `Authorization: Bearer` — никогда в query-string (URL утекают в access-логи сервера/прокси, историю браузера, Referer). Для WebSocket (браузер не может выставить WS-заголовки) токен НЕ подставляется в URL: UI сначала обменивает его на **одноразовый короткоживущий тикет** через `POST /ws/ticket` (Bearer-защищён, TTL 60с, single-use — `pop()` из store при проверке), и в WS-хендшейк идёт только тикет `?ticket=...`; украденный/реплейнутый тикет бесполезен. UI хранит токен в переменной JS (память вкладки), подставляет в fetch-запросы; скриншоты грузятся blob'ом через fetch, чтобы проходил Bearer.

**on_step-хук.** `AgentOrchestrator(..., on_step=Callable[[int, AgentAction, ActionResult], None])` — по образцу `shutdown_check`; вызывается после каждого исполненного шага (основной путь и JSON-retry-путь), неблокирующий, исключения глотаются. API-воркер передаёт его раннеру (интроспекция сигнатуры, как у emit/stop_check) и пишет `current_step`/`last_tool` в запись задачи — `GET /task/{id}` отдаёт их во время выполнения.

**Path traversal.** `GET /task/{id}/screenshot`: путь из записи резолвится и обязан лежать внутри `SCREENSHOT_DIR` (task_id — только ключ uuid-словаря, но путь всё равно валидируется от подмены). `GET /reports/{run_id}`: строгий `^[A-Za-z0-9_-]{1,64}$` + resolve-проверка внутри `REPORTS_DIR`. Тесты гоняют `../../etc/passwd`-подобные id и путь-вне-директории.

**Санитизация входящих задач.** `POST /task` прогоняет текст через `src/infrastructure/task_policy.py::TaskPolicy.validate()` ДО постановки в очередь: длина (`TASK_MAX_LENGTH`), пустота, управляющие символы, отсутствие алфавитно-цифровых символов; отклонение — HTTP 400 `{error, rule}` + запись в отдельный JSONL-аудит (`TASK_AUDIT_LOG_PATH`: ts/rule/tenant_id/превью 200). Опциональный контент-фильтр (`ENABLE_TASK_CONTENT_FILTER` + `TASK_FORBIDDEN_PATTERNS`, выключен по дефолту) — построчные case-insensitive regex, ничего не захардкожено, битые паттерны пропускаются с warning. Это защита от очевидных злоупотреблений, не модерация: regex не понимает намерение и обходится перефразированием; для публичного сервиса нужен классификатор/человеческий ревью (см. SELF_REVIEW.md §6).

**Новые инструменты.** `assert_page_state` — дешёвая no-LLM проверка (`expect_text_present` / `expect_url_contains` / `expect_element_visible`), провал = обычный `ActionResult(error="AssertionFailed")`, не исключение. `set_variable`/`get_variable` — `scratch_memory` оркестратора, отдельная от `context_data` (финальный результат): промежуточные вычисления не засоряют `TaskResult`; system prompt явно описывает разницу (store_context = финальный результат, set_variable = промежуточное).

**Untrusted-обёртка.** `UNTRUSTED_CONTENT_TOOLS` + `_format_action_result()`: результат любого инструмента, возвращающего текст страницы, попадает в conversation history только внутри `<untrusted_page_content>` — тот же механизм, что у `_get_observation()`; класс закрытой ранее уязвимости не открывается новыми инструментами. Регрессионный тест: malicious-текст через `extract_page_content` остаётся внутри разделителей.

**Regression-guard'ы.** Тест «каждый tool из valid_tools присутствует в system prompt» (защита от «инструмент есть в коде, модель о нём не знает»). `make check-no-captcha-solvers` (в `ci`): grep-гарант отсутствия `2captcha|anti-captcha|capmonster|capsolver|gatesolve` в `src/` и requirements (дублируется pytest-тестом).

**Backlog (зафиксировано, вне скоупа):** ротация `DOWNLOAD_ALLOWED_DIR`; rate-limit уровня API (нужен при `API_BIND_HOST` ≠ 127.0.0.1).

---

**Дата обновления (Фаза 4 + hardening-дополнение)**: 2026-08-22
**Версия документа**: 2.2
