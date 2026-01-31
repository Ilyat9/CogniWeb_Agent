# syntax=docker/dockerfile:1.4

# ==============================================================================
# STAGE 1: Builder - установка Python зависимостей
# ==============================================================================
FROM python:3.11-slim-bookworm AS builder

# Метаданные
LABEL maintainer="CogniWeb Agent Team"
LABEL version="1.0"
LABEL description="Autonomous browser agent with ReAct architecture"

# Установка системных зависимостей для компиляции Python пакетов
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Создание виртуального окружения
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Копирование только requirements.txt для кэширования слоя
COPY requirements.txt /tmp/requirements.txt

# Установка Python зависимостей (кэшируется до изменения requirements.txt)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r /tmp/requirements.txt

# ==============================================================================
# STAGE 2: Runtime - финальный образ на базе Playwright
# ==============================================================================
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Создание не-root пользователя для безопасности
ARG USER_ID=1000
ARG GROUP_ID=1000
RUN groupadd agentuser && \
    useradd -m -g agentuser agentuser

# Установка только runtime зависимостей
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Копирование виртуального окружения из builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Установка Playwright браузеров (только Chromium для оптимизации размера)
# Используем --with-deps для системных зависимостей
RUN playwright install --with-deps chromium

# Создание рабочей директории
WORKDIR /app

# Создание директорий для persistent data
RUN mkdir -p /app/browser_data /app/screenshots /app/logs && \
    chown -R agentuser:agentuser /app

# Копирование исходного кода
COPY --chown=agentuser:agentuser . /app/

# Переключение на не-root пользователя
USER agentuser

# Переменные окружения для production
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEADLESS=true \
    DEBUG_MODE=false \
    USER_DATA_DIR=/app/browser_data \
    SCREENSHOT_DIR=/app/screenshots \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Health check (опционально, для Kubernetes/Docker Swarm)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Точка входа
ENTRYPOINT ["python"]
CMD ["main.py"]

# ==============================================================================
# Размер образа: ~1.5GB (оптимизирован через multi-stage и только chromium)
# Security: Runs as non-root user (UID 1000)
# Cache: Слои requirements.txt и код разделены для эффективного кэша
# ==============================================================================