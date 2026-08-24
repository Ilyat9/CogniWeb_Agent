# syntax=docker/dockerfile:1.4

# ==============================================================================
# Базовый образ Playwright с предустановленными системными зависимостями.
# Python-версия: этот тег построен на Ubuntu 22.04 (jammy) и несёт
# системный Python 3.10 - нижнюю границу CI-матрицы (3.10/3.11/3.12).
# НЕ переходите на базовый тег с другой мажорной версией Python, не
# расширив CI-матрицу соответствующим образом.
# ==============================================================================
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Метаданные
LABEL maintainer="CogniWeb Agent Team"
LABEL version="2.0"
LABEL description="Autonomous browser agent with ReAct architecture"

# Установка дополнительных runtime зависимостей
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Установка рабочей директории
WORKDIR /app

# Копирование requirements для кэширования слоя
# requirements/lock.txt используется как CONSTRAINTS-файл (-c): версии
# устанавливаемых пакетов фиксируются на проверенном тестами наборе
# (см. его заголовок), при этом резолвер всё ещё может доставить то, чего
# в lock нет (например crawl4ai из tools-extra). Это закрывает разрыв
# "CI/Docker ставят диапазонные зависимости": образ собирается ровно на
# тех версиях, на которых прогнан тестовый набор.
COPY requirements/ /app/requirements/

# MODE=api ставит fastapi/uvicorn/websockets (extra [ui], включает [api]);
# MODE=cli (по умолчанию) ограничивается базовым requirements/base.txt.
# TOOLS=true дополнительно ставит опциональные tools-зависимости
# (playwright-stealth, crawl4ai) - лениво импортируемые улучшения.
ARG MODE=cli
ARG TOOLS=false

# Установка Python зависимостей (как root)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    if [ "$MODE" = "api" ]; then \
        pip install --no-cache-dir -c /app/requirements/lock.txt -r /app/requirements/ui.txt; \
    else \
        pip install --no-cache-dir -c /app/requirements/lock.txt -r /app/requirements/base.txt; \
    fi && \
    if [ "$TOOLS" = "true" ]; then \
        pip install --no-cache-dir -c /app/requirements/lock.txt -r /app/requirements/tools.txt; \
    fi

# Установка Playwright браузеров БЕЗ --with-deps (системные зависимости уже есть)
RUN python3 -m playwright install chromium

# Создание группы и пользователя БЕЗ жестко заданных UID/GID
RUN groupadd agentuser && \
    useradd -m -g agentuser agentuser

# Создание необходимых директорий и выдача прав
# /app/data: SQLite-файл истории задач (TASK_DB_PATH). Монтируйте его как
# volume (-v ./data:/app/data), иначе история задач будет стираться при
# пересоздании контейнера - в этом весь смысл персистентности.
RUN mkdir -p /app/screenshots /app/logs /app/browser_data /app/data && \
    chown -R agentuser:agentuser /app

# Копирование исходного кода с правильными правами
COPY --chown=agentuser:agentuser . /app/

# Переключение на не-привилегированного пользователя
USER agentuser

# Переменные окружения
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEADLESS=true \
    DEBUG_MODE=false \
    USER_DATA_DIR=/app/browser_data \
    SCREENSHOT_DIR=/app/screenshots \
    HEARTBEAT_FILE=/app/logs/heartbeat \
    TASK_DB_PATH=/app/data/tasks.db \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# MODE (cli по умолчанию / api) пробрасывается в ENV, чтобы точка входа
# могла выбрать режим во время запуска контейнера.
ARG MODE=cli
ENV MODE=${MODE}

# HEALTHCHECK: в MODE=api действительно проверяет /health (drain после
# SIGTERM корректно даёт 503 -> unhealthy). В MODE=cli скрипт сразу
# выходит 0 - batch-агент выполняет задачу и завершается, осмысленной
# проверки "жив/готов" для него нет, а декоративный always-true healthcheck
# хуже отсутствия (оркестратор считает зависший процесс здоровым).
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s \
    CMD ["python", "docker-healthcheck.py"]

# Точка входа: MODE=api поднимает uvicorn-сервис, иначе обычный CLI main.py.
# NOTE (hardening): --host 0.0.0.0 здесь - СОЗНАТЕЛЬНОЕ решение оператора
# при публикации портов контейнера (-p 8000:8000); localhost-биндинг
# внутри контейнера был бы недостижим с хоста. Локальный запуск БЕЗ Docker
# использует безопасный дефолт API_BIND_HOST=127.0.0.1 (см. make run-ui).
# При публикации порта наружу ОБЯЗАТЕЛЬНО задайте API_AUTH_TOKEN (>=16
# символов): src/api/app.py refuses to start на 0.0.0.0 без токена.
# Осознанный отказ от токена (только для изолированной доверенной сети) -
# ALLOW_UNAUTHENTICATED_PUBLIC_BIND=true.
ENTRYPOINT ["sh", "-c"]
CMD ["if [ \"$MODE\" = \"api\" ]; then exec python -m uvicorn src.api.app:build_default_app --factory --host 0.0.0.0 --port 8000; else exec python main.py; fi"]
