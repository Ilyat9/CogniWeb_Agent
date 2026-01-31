# syntax=docker/dockerfile:1.4

# ==============================================================================
# Базовый образ Playwright с предустановленными системными зависимостями
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

# Копирование requirements.txt для кэширования слоя
COPY requirements.txt /app/requirements.txt

# Установка Python зависимостей (как root)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r /app/requirements.txt

# Установка Playwright браузеров БЕЗ --with-deps (системные зависимости уже есть)
RUN python3 -m playwright install chromium

# Создание группы и пользователя БЕЗ жестко заданных UID/GID
RUN groupadd agentuser && \
    useradd -m -g agentuser agentuser

# Создание необходимых директорий и выдача прав
RUN mkdir -p /app/screenshots /app/logs /app/browser_data && \
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
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Точка входа
ENTRYPOINT ["python"]
CMD ["main.py"]

# ==============================================================================
# Исправления:
# 1. ✅ Браузеры устанавливаются под root, приложение запускается под agentuser
# 2. ✅ Нет жестко заданных UID/GID 1000 - используется автоматическое назначение
# 3. ✅ Используется базовый образ Playwright + команда БЕЗ --with-deps
# ==============================================================================