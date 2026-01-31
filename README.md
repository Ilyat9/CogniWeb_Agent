# 🤖 CogniWeb Agent

[![CI Pipeline](https://github.com/Ilyat9/CogniWeb_Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Ilyat9/CogniWeb_Agent/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE.md)

Production-ready веб-агент с архитектурой **ReAct** (Reasoning + Acting), реализующий автономную навигацию и взаимодействие с веб-сайтами через LLM-управляемую автоматизацию браузера.

## 📋 О проекте

Агент использует **Playwright** для управления браузером и **LLM** (через OpenRouter API) для принятия решений. В отличие от простых скриптовых ботов, агент способен анализировать DOM страницы, планировать последовательность действий и адаптироваться к изменениям интерфейса без жёсткой привязки к селекторам.

Архитектура построена как **модульный монолит** с чётким разделением слоёв: конфигурация, бизнес-логика, инфраструктура и оркестрация. Все компоненты изолированы через Dependency Injection, что обеспечивает тестируемость и гибкость.

## ✨ Ключевые возможности

- **ReAct Pattern**: Цикл Observe → Think → Act с reasoning на каждом шаге
- **11 инструментов**: navigate, click_element, type_text, upload_file, scroll_page, take_screenshot, wait, go_back, query_dom, store_context, done
- **Stealth Mode**: playwright-stealth для обхода базовых антибот-систем
- **Smart Loop Detection**: Детекция зацикливаний с анализом (action + target + success)
- **Error Recovery**: Автоматические снимки (screenshot + HTML dump) при сбоях
- **Rate Limiting**: Настраиваемый контроль частоты запросов к LLM API (по умолчанию 15 сек между запросами)
- **Context Management**: Сохранение извлечённых данных между шагами
- **Graceful Shutdown**: Корректное закрытие браузера при SIGINT/SIGTERM

## 🛠 Технологический стек

### Основные технологии
- **Python 3.10+**: Async/await, Type hints, Pydantic v2
- **Playwright 1.40+**: Браузерная автоматизация с stealth режимом
- **OpenRouter API**: LLM интеграция (совместим с OpenAI SDK)
- **Pydantic Settings**: Type-safe конфигурация с валидацией

### Инфраструктура и автоматизация
- **Docker**: Многоэтапная сборка на базе `mcr.microsoft.com/playwright/python`
- **Makefile**: Автоматизация задач (установка, тестирование, линтинг, Docker)
- **GitHub Actions**: CI/CD пайплайн (тесты на Python 3.10-3.12, проверка безопасности)
- **Pytest**: Unit-тесты с моками и покрытием кода
- **Ruff + Black + isort**: Линтинг и форматирование кода
- **Bandit + Safety**: Сканирование безопасности кода и зависимостей

## 🚀 Быстрый старт

### Вариант 1: Локальная установка (рекомендуется для разработки)

```bash
# Клонировать репозиторий
git clone https://github.com/Ilyat9/CogniWeb_Agent
cd CogniWeb_Agent

# Установить зависимости и браузеры
make install-dev

# Настроить .env
make setup-env
# Отредактировать .env: добавить OPENAI_API_KEY

# Запустить агента
make run
```

### Вариант 2: Docker (рекомендуется для продакшена)

```bash
# Собрать образ
make docker-build

# Запустить контейнер
make docker-run

# Или через docker напрямую
docker build -t cogniweb-agent .
docker run --rm -v $(pwd)/.env:/app/.env:ro cogniweb-agent
```

**Подробная инструкция**: [QUICK_START.md](QUICK_START.md)

## 📁 Структура проекта

```
.
├── main.py                      # Entry point с signal handling
├── Dockerfile                   # Многоэтапная сборка (non-root user)
├── Makefile                     # Автоматизация команд
├── pyproject.toml               # Конфигурация инструментов (black, ruff, pytest)
├── requirements.txt             # Production зависимости
├── requirements-dev.txt         # Development зависимости (pytest, ruff, etc)
├── .env.example                 # Шаблон конфигурации
│
├── src/
│   ├── config/
│   │   ├── settings.py          # Pydantic Settings с валидацией
│   │   └── __init__.py
│   │
│   ├── core/
│   │   ├── models.py            # AgentAction, TaskResult, ObservationState
│   │   ├── exceptions.py        # Иерархия исключений
│   │   └── __init__.py
│   │
│   ├── infrastructure/
│   │   ├── browser.py           # BrowserService (Playwright)
│   │   ├── llm.py               # LLMService (OpenRouter API)
│   │   └── __init__.py
│   │
│   ├── agent/
│   │   ├── orchestrator.py      # ReAct цикл (observe/think/act)
│   │   └── __init__.py
│   │
│   └── utils/
│       ├── dom.py               # DOM tree shaking и оптимизация
│       └── __init__.py
│
├── tests/
│   ├── test_agent_core.py       # Unit-тесты (Pydantic, orchestrator, LLM)
│   └── __init__.py
│
├── .github/
│   └── workflows/
│       ├── ci.yml               # CI пайплайн (тесты, линтинг, безопасность)
│       └── release.yml          # Автоматизация релизов
│
├── browser_data/                # Persistent browser session (создаётся при запуске)
├── screenshots/                 # Error snapshots (создаётся при запуске)
├── agent.log                    # Лог файл (создаётся при запуске)
│
├── QUICK_START.md               # Подробная инструкция по установке
├── ARCHITECTURE.md              # Архитектурная документация
├── CLAUDE.md                    # Руководство для Claude Code
└── LICENSE.md                   # MIT License
```

## ⚙️ Конфигурация

Основные параметры в `.env`:

```env
# API (обязательно)
OPENAI_API_KEY=your_openrouter_api_key
API_BASE_URL=https://openrouter.ai/api/v1
MODEL_NAME=upstage/solar-pro

# Браузер
HEADLESS=false                    # true для продакшена
SLOW_MO=50                        # задержка между действиями (мс)
ENABLE_STEALTH=true               # playwright-stealth

# Агент
MAX_STEPS=50                      # лимит шагов
TEMPERATURE=0.1                   # детерминированность LLM
MAX_TOKENS=1000                   # размер ответа LLM

# DOM оптимизация
TEXT_BLOCK_MAX_LENGTH=500         # обрезка длинных текстов
DOM_MAX_TOKENS_ESTIMATE=10000     # лимит токенов для DOM

# Loop detection
LOOP_DETECTION_WINDOW=3           # окно для проверки
MAX_IDENTICAL_STATES=5            # терпимость к повторам
```

Полный список параметров в [.env.example](.env.example)

## 💡 Примеры задач

**Навигация и поиск**:
```
Задача: Найди статью про Python на википедии и скажи год создания языка
URL: https://wikipedia.org
```

**Заполнение форм**:
```
Задача: Найди форму регистрации, заполни поля (имя: Test, email: test@example.com)
```

**Извлечение данных**:
```
Задача: Найди на странице все цены товаров и сохрани их
```

**Многошаговые сценарии**:
```
Задача: Перейди на hacker news, открой первую статью, прочитай заголовок и первый абзац
URL: https://news.ycombinator.com
```

## 🧪 Тестирование

Проект покрыт Unit-тестами с использованием `pytest` и `pytest-asyncio`. Все внешние зависимости (LLM, Browser) изолированы через моки.

### Запуск тестов:

```bash
# Все тесты с подробным выводом
make test

# Тесты с coverage отчётом
make test-coverage

# Только тесты loop detection
pytest tests/test_agent_core.py::TestSmartLoopDetection -v
```

### CI/CD пайплайн

При каждом push в `main` или `develop` автоматически запускаются:

1. **Линтинг**: Ruff, Black, isort
2. **Тесты**: Pytest на Python 3.10, 3.11, 3.12
3. **Безопасность**: Bandit (код), Safety (зависимости)
4. **Docker**: Сборка образа (только на main/develop)

## 📦 Docker

### Многоэтапная сборка

Dockerfile использует два stage:
- **builder**: Установка зависимостей как root
- **runtime**: Запуск от не-привилегированного пользователя `agentuser`

### Основные команды

```bash
# Сборка образа
make docker-build

# Запуск контейнера
make docker-run

# Запуск тестов в Docker
make docker-test

# Интерактивная оболочка для отладки
make docker-shell

# Очистка
make docker-clean
```

## 🎯 Архитектура

Проект следует принципам **модульного монолита**:

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

**Подробнее**: [ARCHITECTURE.md](ARCHITECTURE.md)

## ⚠️ Ограничения

### Технические
- LLM может галлюцинировать несуществующие элементы на странице
- Не работает с CAPTCHA (требует ручного решения)
- Ограниченная поддержка iframe (нужен workaround)
- Динамические SPA могут вызывать проблемы с timing

### Безопасность
- Stealth mode не гарантирует обход всех антибот-систем
- Не предназначен для обхода защиты сайтов с активной защитой
- Rate limiting может быть недостаточен для некоторых API

### Производительность
- Каждый шаг = 1 LLM запрос (15-30 сек при rate limiting)
- DOM processing добавляет overhead на больших страницах
- Медленнее специализированных скраперов с хардкодом

## 🤝 Разработка

### Локальная разработка

```bash
# Установить dev зависимости
make install-dev

# Запустить в dev режиме (с DEBUG_MODE=true)
make dev

# Проверить код
make lint

# Автоформатирование
make format

# Проверка типов
make type-check

# Проверка безопасности
make security-check
```

### Эмуляция CI локально

```bash
# Запустить все CI проверки
make ci
```

## 📄 Лицензия

MIT License — см. [LICENSE.md](LICENSE.md)

---

**Python**: 3.10+  
**Зависимости**: Playwright 1.40+, OpenAI SDK 1.0+, Pydantic 2.0+  
**Поддержка**: Issues и Pull Requests приветствуются

**Документация**:
- [Быстрый старт](QUICK_START.md)
- [Архитектура](ARCHITECTURE.md)
- [Руководство для Claude Code](CLAUDE.md)