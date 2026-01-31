# Быстрый старт

Подробное руководство по установке и запуску автономного браузер-агента.

## Требования

- Python 3.10 или выше
- 4GB RAM минимум (8GB рекомендуется)
- Интернет для установки зависимостей и LLM API
- Linux/macOS/Windows (WSL2 рекомендуется для Windows)

## Шаг 1: Установка

### 1.1 Клонирование репозитория

```bash
git clone <https://github.com/Ilyat9/CogniWeb_Agent>
cd CogniWeb_Agent
```

### 1.2 Создание виртуального окружения

**Linux/macOS**:
```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows**:
```bash
python -m venv venv
venv\Scripts\activate
```
### 1.3 Создаём папку для скриншотов

```bash
mkdir -p screenshots
```

### 1.4 Установка зависимостей

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

**Если возникают ошибки**:
- Убедитесь, что используется Python 3.10+: `python --version`
- Обновите pip: `pip install --upgrade pip`
- Для Windows: установите Visual C++ Build Tools

### 1.5 Установка браузера Chromium

```bash
playwright install chromium
```

Это скачает ~400MB браузер. Если нужны другие браузеры:

```bash
playwright install firefox webkit  # опционально
```

## Шаг 2: Конфигурация

### 2.1 Создать файл .env

```bash
cp .env.example .env
```

### 2.2 Получить API ключ

1. Перейти на https://openrouter.ai
2. Зарегистрироваться (можно через Google/GitHub)
3. Перейти в https://openrouter.ai/keys
4. Создать новый API ключ
5. Скопировать ключ

### 2.3 Настроить .env

Открыть `.env` в текстовом редакторе и заменить:

```env
OPENAI_API_KEY=your_openrouter_api_key_here
```

на:

```env
OPENAI_API_KEY=sk-or-v1-abcd1234...  # ваш реальный ключ
```

### 2.4 Выбрать модель

**Бесплатные модели** (рекомендуется для начала):
```env
MODEL_NAME=upstage/solar-pro-3:free
# или
MODEL_NAME=meta-llama/llama-3.2-3b-instruct:free
```

**Платные модели** (лучшее качество):
```env
MODEL_NAME=anthropic/claude-3.5-sonnet
# или
MODEL_NAME=openai/gpt-4o
```

### 2.5 Остальные параметры

**Для локального дебаггинга**:
```env
HEADLESS=false          # видно окно браузера
DEBUG_MODE=true         # детальные логи
SLOW_MO=100             # замедление для наблюдения
```

**Для продакшена/автоматизации**:
```env
HEADLESS=true           # браузер без GUI
DEBUG_MODE=false        # минимум логов
SLOW_MO=50              # быстрее
```

## Шаг 3: Первый запуск

### 3.1 Запустить агента

```bash
python main.py
```

Должен появиться вывод:

```
======================================================================
   BATTLE-READY BROWSER AGENT v4.2
   Modular Monolith Architecture
======================================================================

✅ Configuration loaded
   Model: upstage/solar-pro-3:free
   Max Steps: 50
   Stealth: Enabled

----------------------------------------------------------------------
📝 Enter task: 
```

### 3.2 Ввести задачу

**Простая задача для теста**:
```
Find the main heading on this page
```

**Starting URL**:
```
https://example.com
```

### 3.3 Наблюдать выполнение

Агент начнёт выполнение:

```
======================================================================
STEP 1/50
======================================================================
🤔 Agent reasoning...
💭 Thought: I need to navigate to the starting URL first
🔧 Tool: navigate
📝 Args: {'url': 'https://example.com'}
✅ Result: Successfully navigated to https://example.com
```

## Шаг 4: Проверка установки

### 4.1 Проверить структуру директорий

После первого запуска должны появиться:

```
refactored_agent/  # или название вашей директории проекта
├── browser_data/          # Persistent browser session
│   └── ...
├── screenshots/           # Error snapshots (если были ошибки)
└── agent.log              # Файл логов
```

### 4.2 Проверить логи

```bash
tail -f agent.log
```

Должны быть записи вида:
```
2026-01-31 12:00:00 - __main__ - INFO - Configuration loaded
2026-01-31 12:00:01 - __main__ - INFO - Browser and LLM services initialized
```

## Распространённые проблемы

### Проблема 1: API Key Invalid

**Ошибка**:
```
❌ Configuration Error: Invalid API key detected.
```

**Решение**:
1. Проверить, что ключ скопирован полностью
2. Убедиться, что нет пробелов до/после ключа
3. Проверить, что `.env` файл в корне проекта
4. Попробовать заново создать ключ на openrouter.ai

### Проблема 2: playwright-stealth not found

**Ошибка**:
```
⚠️ WARNING: playwright-stealth not installed
```

**Решение**:
```bash
pip install playwright-stealth
```

Если не помогает:
```bash
pip uninstall playwright-stealth
pip install playwright-stealth --no-cache-dir
```

### Проблема 3: Chromium не установлен

**Ошибка**:
```
playwright._impl._api_types.Error: Executable doesn't exist
```

**Решение**:
```bash
playwright install chromium
```

Если проблема сохраняется:
```bash
playwright install-deps  # установить системные зависимости
playwright install chromium
```

### Проблема 4: Rate Limiting

**Симптомы**:
```
⏳ Rate limiting: waiting 15.0s before next LLM request...
```

**Это нормально**. Агент защищается от превышения лимитов API.

**Настроить**:
В `src/agent/orchestrator.py`, строка 84:
```python
RATE_LIMIT_SECONDS = 15  # уменьшить/увеличить
```

### Проблема 5: Timeout Error

**Ошибка**:
```
TimeoutError: Page load timeout exceeded
```

**Решение**:
В `.env` увеличить таймауты:
```env
PAGE_LOAD_TIMEOUT=120000      # было 60000
ACTION_TIMEOUT=30000          # было 20000
HTTP_TIMEOUT=180.0            # было 120.0
```

### Проблема 6: Permission Denied (Linux)

**Ошибка**:
```
PermissionError: [Errno 13] Permission denied: './browser_data'
```

**Решение**:
```bash
chmod +w browser_data screenshots
# или
sudo chown -R $USER:$USER .
```

## Примеры задач для тестирования

### Пример 1: Простая навигация

```
Task: Navigate to wikipedia.org and tell me the main heading
URL(optional): https://wikipedia.org
```

### Пример 2: Поиск и клик

```
Task: Go to hacker news, click on the first article link
URL(optional): https://news.ycombinator.com
```

### Пример 3: Извлечение данных

```
Task: Find all links on this page and save them to context
URL(optional): https://example.com
```

### Пример 4: Многошаговый сценарий

```
Task: 1) Navigate to github.com, 2) Search for "playwright", 3) Click first result, 4) Save repository name
URL(optional): https://github.com
```

### Пример 5: Проверка контента

```
Task: Find the word "python" on the page and tell me how many times it appears
URL(optional): https://python.org
```

## Следующие шаги

После успешного запуска:

1. **Изучить архитектуру**: [ARCHITECTURE.md](ARCHITECTURE.md)
2. **Настроить под свои задачи**: отредактировать `.env`
3. **Добавить custom tools**: см. раздел в README
4. **Запустить в headless**: `HEADLESS=true` для автоматизации

## Дополнительная настройка

### Использование прокси

В `.env`:
```env
PROXY_URL=http://user:pass@proxy.example.com:8080
```

### Debug режим

В `.env`:
```env
DEBUG_MODE=true
```

Это включает:
- Подробные логи всех действий
- Скриншоты после каждого шага
- Сохранение HTML дампов

### Persistent browser session

Браузер сохраняет cookies и localStorage в `./browser_data`.

**Очистить сессию**:
```bash
rm -rf browser_data
```

### Кастомизация DOM processing

Если агент "не видит" элементы:

```env
TEXT_BLOCK_MAX_LENGTH=500      # увеличить лимит текста
DOM_MAX_TOKENS_ESTIMATE=15000  # увеличить лимит токенов
```

Если слишком много токенов:

```env
TEXT_BLOCK_MAX_LENGTH=100      # уменьшить
DOM_MAX_TOKENS_ESTIMATE=5000   # уменьшить
```

## Поддержка

При возникновении проблем:

1. Проверить `agent.log`
2. Включить `DEBUG_MODE=true`
3. Посмотреть screenshots в `./screenshots/`
4. Проверить версии: `pip list | grep playwright`

## Минимальный .env для работы

```env
OPENAI_API_KEY=sk-or-v1-ваш-ключ
API_BASE_URL=https://openrouter.ai/api/v1
MODEL_NAME=upstage/solar-pro-3:free
```

Всё остальное — опционально, есть дефолтные значения.
