# 🚀 Быстрый старт

Подробное руководство по установке и запуску автономного браузер-агента.

## 📋 Требования

- **Python 3.10 или выше**
- **4GB RAM минимум** (8GB рекомендуется)
- **Интернет** для установки зависимостей и LLM API
- **Linux/macOS/Windows** (WSL2 рекомендуется для Windows)

---

## 🎯 Два способа установки

### 🔧 Способ 1: Локальная установка (для разработки)

Рекомендуется для:
- Разработки и отладки кода
- Экспериментов с конфигурацией
- Пошаговой отладки агента

### 🐳 Способ 2: Docker (для продакшена)

Рекомендуется для:
- Развёртывания на серверах
- Воспроизводимости окружения
- Изоляции зависимостей

---

## 🔧 Способ 1: Локальная установка

### Шаг 1: Клонирование репозитория

```bash
git clone https://github.com/Ilyat9/CogniWeb_Agent
cd CogniWeb_Agent
```

### Шаг 2: Установка через Makefile

**Для разработки (включает тесты и линтеры)**:

```bash
make install-dev
```

Эта команда автоматически:
1. ✅ Обновит pip, setuptools, wheel
2. ✅ Установит production зависимости из `requirements.txt`
3. ✅ Установит development зависимости из `requirements-dev.txt` (pytest, ruff, black, etc)
4. ✅ Скачает браузер Chromium (playwright install chromium)
5. ✅ Установит системные зависимости для Playwright

**Для production (только runtime зависимости)**:

```bash
make install
```

Эта команда устанавливает только `requirements.txt` и браузер Chromium.

### Шаг 3: Настройка окружения

```bash
# Создать .env из шаблона
make setup-env
```

Откройте `.env` в текстовом редакторе:

```env
OPENAI_API_KEY=your_openrouter_api_key_here  # ← ИЗМЕНИТЬ
API_BASE_URL=https://openrouter.ai/api/v1
MODEL_NAME=upstage/solar-pro
```

**Получение API ключа**:

1. Перейдите на https://openrouter.ai
2. Зарегистрируйтесь (через Google/GitHub)
3. Откройте https://openrouter.ai/keys
4. Создайте новый API ключ
5. Скопируйте и вставьте в `.env`

**Рекомендуемые модели**:

| Модель | Цена | Качество | Применение |
|--------|------|----------|-----------|
| `upstage/solar-pro:free` | Бесплатно | Хорошее | Тестирование |
| `meta-llama/llama-3.2-3b-instruct:free` | Бесплатно | Среднее | Лёгкие задачи |
| `anthropic/claude-3.5-sonnet` | Платно | Отличное | Продакшен |
| `openai/gpt-4o` | Платно | Отличное | Продакшен |

### Шаг 4: Запуск агента

```bash
make run
```

Вы увидите:

```
======================================================================
   BATTLE-READY BROWSER AGENT v4.2
   Modular Monolith Architecture
======================================================================

✅ Configuration loaded
   Model: upstage/solar-pro
   Max Steps: 50
   Stealth: Enabled

----------------------------------------------------------------------
📝 Enter task: 
```

**Пример первой задачи**:

```
Задача: Find the main heading on this page
URL: https://example.com
```

---

## 🐳 Способ 2: Docker

### Шаг 1: Сборка образа

```bash
make docker-build
```

Или напрямую:

```bash
docker build -t cogniweb-agent .
```

**Что происходит при сборке**:

1. **Stage 1 (builder)**: Установка зависимостей как root
   - Системные пакеты (fonts, ca-certificates)
   - Python зависимости из `requirements.txt`
   - Playwright браузеры (chromium)

2. **Stage 2 (runtime)**: Настройка окружения
   - Создание не-привилегированного пользователя `agentuser`
   - Копирование исходного кода с правильными правами
   - Настройка переменных окружения

### Шаг 2: Настройка .env

```bash
# Создать .env (если ещё не создан)
cp .env.example .env

# Отредактировать OPENAI_API_KEY
nano .env
```

### Шаг 3: Запуск контейнера

**Через Makefile**:

```bash
make docker-run
```

**Или напрямую через Docker**:

```bash
docker run --rm \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/browser_data:/app/browser_data \
  -v $(pwd)/screenshots:/app/screenshots \
  -v $(pwd)/logs:/app/logs \
  cogniweb-agent
```

**Пояснение volume mount**:

- `/app/.env:ro` — конфигурация (read-only)
- `/app/browser_data` — сессия браузера (cookies, localStorage)
- `/app/screenshots` — скриншоты при ошибках
- `/app/logs` — лог-файлы

### Дополнительные Docker команды

| Команда | Описание |
|---------|----------|
| `make docker-shell` | Запустить bash в контейнере для отладки |
| `make docker-test` | Запустить тесты внутри контейнера |
| `make docker-clean` | Удалить образ и очистить кэш |

---

## 📚 Основные команды Makefile

### Установка и настройка

| Команда | Описание |
|---------|----------|
| `make install` | Установить production зависимости и браузеры |
| `make install-dev` | Установить dev зависимости (тесты, линтеры) |
| `make setup-env` | Создать .env из .env.example |

### Запуск

| Команда | Описание |
|---------|----------|
| `make run` | Запустить агента (main.py) |
| `make dev` | Запустить в dev режиме (DEBUG_MODE=true, HEADLESS=false) |

### Тестирование

| Команда | Описание |
|---------|----------|
| `make test` | Запустить unit-тесты |
| `make test-verbose` | Тесты с подробным выводом |
| `make test-coverage` | Тесты с coverage отчётом (→ htmlcov/index.html) |
| `make test-watch` | Тесты в watch режиме (требует pytest-watch) |

### Качество кода

| Команда | Описание |
|---------|----------|
| `make lint` | Проверить код (ruff, black, isort) |
| `make format` | Автоформатирование (black, isort) |
| `make type-check` | Проверка типов (mypy) |
| `make security-check` | Проверка безопасности (safety, bandit) |

### Docker

| Команда | Описание |
|---------|----------|
| `make docker-build` | Собрать Docker образ |
| `make docker-run` | Запустить контейнер |
| `make docker-shell` | Интерактивная оболочка в контейнере |
| `make docker-test` | Запустить тесты в Docker |
| `make docker-clean` | Удалить образы и контейнеры |

### Очистка

| Команда | Описание |
|---------|----------|
| `make clean` | Удалить кэш, логи, временные файлы |
| `make clean-all` | Полная очистка (включая Docker) |

### Утилиты

| Команда | Описание |
|---------|----------|
| `make check-deps` | Проверить устаревшие зависимости |
| `make update-deps` | Обновить requirements.txt |
| `make ci` | Эмулировать CI пайплайн локально |
| `make info` | Показать информацию о проекте |

---

## ✅ Проверка установки

### После локальной установки

```bash
# Проверить версию Python
python --version  # Должно быть 3.10+

# Проверить установленные зависимости
pip list | grep playwright

# Проверить браузеры
playwright --version

# Запустить тесты
make test
```

Должны появиться директории:

```
CogniWeb_Agent/
├── browser_data/          # Persistent browser session
├── screenshots/           # Error snapshots (если были ошибки)
└── agent.log              # Файл логов
```

### Проверка логов

```bash
# Просмотр последних логов
tail -f agent.log
```

Должны быть записи вида:

```
2026-01-31 12:00:00 - __main__ - INFO - Configuration loaded
2026-01-31 12:00:01 - __main__ - INFO - Browser and LLM services initialized
```

---

## 🐛 Распространённые проблемы

### Проблема 1: API Key Invalid

**Ошибка**:
```
❌ Configuration Error: Invalid API key detected.
```

**Решение**:
1. Убедитесь, что ключ скопирован полностью (должен начинаться с `sk-or-v1-`)
2. Проверьте отсутствие пробелов до/после ключа
3. Убедитесь, что `.env` файл находится в корне проекта
4. Попробуйте пересоздать ключ на openrouter.ai

### Проблема 2: playwright-stealth not found

**Ошибка**:
```
⚠️ WARNING: playwright-stealth not installed
```

**Решение**:
```bash
pip install playwright-stealth --no-cache-dir

# Или через Makefile
make install-dev
```

### Проблема 3: Chromium не установлен

**Ошибка**:
```
playwright._impl._api_types.Error: Executable doesn't exist
```

**Решение**:
```bash
# Установить только Chromium
playwright install chromium

# Или с системными зависимостями
playwright install --with-deps chromium

# Или через Makefile
make install-dev
```

### Проблема 4: Rate Limiting (это нормально)

**Симптомы**:
```
⏳ Rate limiting: waiting 15.0s before next LLM request...
```

**Это защита от превышения лимитов API.**

Если хотите изменить задержку:

В `src/agent/orchestrator.py`, строка 84:

```python
RATE_LIMIT_SECONDS = 15  # уменьшить/увеличить
```

### Проблема 5: Timeout Error

**Ошибка**:
```
TimeoutError: Page load timeout exceeded
```

**Решение**: Увеличить таймауты в `.env`:

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
# Дать права на запись
chmod +w browser_data screenshots

# Или сменить владельца
sudo chown -R $USER:$USER .
```

### Проблема 7: Docker образ не собирается

**Ошибка**:
```
ERROR: failed to solve: process "/bin/sh -c playwright install chromium" did not complete successfully
```

**Решение**:

1. Очистить Docker кэш:
   ```bash
   make docker-clean
   docker system prune -a
   ```

2. Пересобрать образ:
   ```bash
   make docker-build
   ```

3. Если не помогает, проверьте Docker версию:
   ```bash
   docker --version  # Должно быть 20.10+
   ```

---

## 🎯 Примеры задач для тестирования

### Пример 1: Простая навигация

```
Задача: Navigate to wikipedia.org and tell me the main heading
URL: https://wikipedia.org
```

**Ожидаемый результат**: Агент перейдёт на сайт и извлечёт главный заголовок.

### Пример 2: Поиск и клик

```
Задача: Go to hacker news, click on the first article link
URL: https://news.ycombinator.com
```

**Ожидаемый результат**: Агент найдёт первую статью и кликнет на неё.

### Пример 3: Извлечение данных

```
Задача: Find all links on this page and save them to context
URL: https://example.com
```

**Ожидаемый результат**: Агент сохранит список ссылок в `context_data`.

### Пример 4: Многошаговый сценарий

```
Задача: 1) Navigate to github.com, 2) Search for "playwright", 3) Click first result, 4) Save repository name
URL: https://github.com
```

**Ожидаемый результат**: Агент выполнит последовательность действий и сохранит название репозитория.

### Пример 5: Проверка контента

```
Задача: Find the word "python" on the page and tell me how many times it appears
URL: https://python.org
```

**Ожидаемый результат**: Агент подсчитает вхождения слова "python".

---

## ⚙️ Дополнительная настройка

### Использование прокси

В `.env`:

```env
PROXY_URL=http://user:pass@proxy.example.com:8080
```

### Debug режим

В `.env`:

```env
DEBUG_MODE=true
HEADLESS=false  # Видно окно браузера
```

Это включает:
- ✅ Подробные логи всех действий
- ✅ Скриншоты после каждого шага
- ✅ Сохранение HTML дампов

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

---

## 🧪 Тестирование установки

### Запуск тестов

```bash
# Все тесты
make test

# С coverage
make test-coverage

# Открыть coverage отчёт в браузере
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

### Ожидаемый результат

```
======================== test session starts =========================
tests/test_agent_core.py::TestAgentActionValidation::test_valid_action PASSED
tests/test_agent_core.py::TestAgentActionValidation::test_invalid_tool_name PASSED
tests/test_agent_core.py::TestSmartLoopDetection::test_loop_detected_on_identical_failures PASSED
...
======================== 15 passed in 2.34s ==========================
```

---

## 🚀 Следующие шаги

После успешного запуска:

1. **Изучить архитектуру**: [ARCHITECTURE.md](ARCHITECTURE.md)
2. **Настроить под свои задачи**: отредактировать `.env`
3. **Добавить custom tools**: см. раздел в [ARCHITECTURE.md](ARCHITECTURE.md)
4. **Запустить в headless**: `HEADLESS=true` для автоматизации
5. **Интегрировать в CI/CD**: см. `.github/workflows/ci.yml`

---

## 🆘 Поддержка

При возникновении проблем:

1. ✅ Проверьте `agent.log`
2. ✅ Включите `DEBUG_MODE=true`
3. ✅ Посмотрите screenshots в `./screenshots/`
4. ✅ Проверьте версии: `make info`
5. ✅ Запустите `make ci` для эмуляции CI локально

---

## 📝 Минимальная конфигурация .env

```env
OPENAI_API_KEY=sk-or-v1-ваш-ключ
API_BASE_URL=https://openrouter.ai/api/v1
MODEL_NAME=upstage/solar-pro:free
```

Всё остальное — опционально, есть дефолтные значения в `settings.py`.

---

**Удачи в автоматизации! 🚀**
---

## 🖥️ Вариант 3: Web UI (Фаза 4)

Работа с агентом через браузер вместо терминала/curl:

```bash
# 1. Зависимости API + UI (fastapi, uvicorn, websockets)
make install-ui

# 2. Запуск (UI + API на http://localhost:8000)
make run-ui
```

Что умеет UI:
- запуск задачи (текст + опциональный starting_url);
- живой пошаговый прогресс (WebSocket, с автоматическим fallback на polling);
- текущий скриншот и URL страницы;
- история задач с детальным просмотром результата (`tokens_used`, `context_data`);
- отчёты по запускам в человекочитаемом виде;
- кнопка «Остановить» (graceful, per-task);
- явный баннер капчи и captcha circuit breaker с предложением перезапуска;
- read-only просмотр активной конфигурации (секреты маскированы).

Доступ (см. .env.example, секция «Web UI / API access control»): по умолчанию
сервер слушает только `127.0.0.1` (`API_BIND_HOST`). Для доступа с других машин
задайте `API_BIND_HOST` и обязательно `API_AUTH_TOKEN` (Bearer; поле для токена —
справа вверху в UI, хранится только в памяти вкладки). `/health` всегда открыт.

Опциональные улучшения (не обязательны для UI):
```bash
make install-tools   # playwright-stealth (полный набор патчей) + crawl4ai (качественнее Markdown)
```

Docker: `docker build --build-arg MODE=api --build-arg TOOLS=true -t cogniweb-agent:ui .`

> ⚠️ Контейнер с `MODE=api` биндит `0.0.0.0` (иначе `-p 8000:8000` не работал бы). При публикации порта наружу обязательно задайте `API_AUTH_TOKEN` — `API_BIND_HOST` тут не защита (см. README → Docker).
