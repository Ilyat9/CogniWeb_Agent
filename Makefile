# ==============================================================================
# CogniWeb Agent - Makefile
# ==============================================================================

.PHONY: help install install-dev test test-verbose test-coverage lint format clean docker-build docker-run docker-clean run dev

# Переменные
PYTHON := python3
PIP := $(PYTHON) -m pip
PYTEST := pytest
DOCKER_IMAGE := cogniweb-agent
DOCKER_TAG := latest

# Цвета для вывода (опционально)
CYAN := \033[0;36m
GREEN := \033[0;32m
RED := \033[0;31m
RESET := \033[0m

# ==============================================================================
# Справка (по умолчанию)
# ==============================================================================
help: ## Показать это сообщение помощи
	@echo "$(CYAN)CogniWeb Agent - Available Commands:$(RESET)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# ==============================================================================
# Установка зависимостей
# ==============================================================================
install: ## Установить production зависимости и браузеры
	@echo "$(CYAN)Installing production dependencies...$(RESET)"
	$(PIP) install --upgrade pip setuptools wheel
	$(PIP) install -r requirements.txt
	@echo "$(CYAN)Installing Playwright browsers (chromium only)...$(RESET)"
	playwright install chromium
	@echo "$(GREEN)✓ Installation complete!$(RESET)"

install-dev: install ## Установить dev зависимости (тесты, линтеры)
	@echo "$(CYAN)Installing development dependencies...$(RESET)"
	$(PIP) install pytest pytest-asyncio pytest-cov
	$(PIP) install ruff black isort mypy bandit safety
	playwright install --with-deps chromium
	@echo "$(GREEN)✓ Development environment ready!$(RESET)"

# ==============================================================================
# Запуск приложения
# ==============================================================================
run: ## Запустить агента (main.py)
	@echo "$(CYAN)Starting CogniWeb Agent...$(RESET)"
	$(PYTHON) main.py

dev: ## Запустить в dev режиме (с DEBUG_MODE=true)
	@echo "$(CYAN)Starting agent in development mode...$(RESET)"
	DEBUG_MODE=true HEADLESS=false $(PYTHON) main.py

# ==============================================================================
# Тестирование
# ==============================================================================
test: ## Запустить unit-тесты
	@echo "$(CYAN)Running unit tests...$(RESET)"
	$(PYTEST) tests/ -v --tb=short --maxfail=3 --asyncio-mode=auto

test-verbose: ## Запустить тесты с подробным выводом
	@echo "$(CYAN)Running tests with verbose output...$(RESET)"
	$(PYTEST) tests/ -vv --tb=long --asyncio-mode=auto

test-coverage: ## Запустить тесты с coverage отчетом
	@echo "$(CYAN)Running tests with coverage...$(RESET)"
	$(PYTEST) tests/ \
		-v \
		--cov=src \
		--cov-report=html \
		--cov-report=term-missing \
		--cov-report=xml \
		--asyncio-mode=auto
	@echo "$(GREEN)✓ Coverage report generated in htmlcov/index.html$(RESET)"

test-watch: ## Запустить тесты в watch режиме (требует pytest-watch)
	@echo "$(CYAN)Starting test watcher...$(RESET)"
	$(PIP) install pytest-watch
	ptw tests/ -- -v --asyncio-mode=auto

# ==============================================================================
# Качество кода
# ==============================================================================
lint: ## Проверить код линтерами (ruff, black, isort)
	@echo "$(CYAN)Running code quality checks...$(RESET)"
	ruff check src/ tests/
	black --check src/ tests/
	isort --check-only src/ tests/
	@echo "$(GREEN)✓ Linting passed!$(RESET)"

format: ## Автоформатирование кода (black, isort)
	@echo "$(CYAN)Formatting code...$(RESET)"
	black src/ tests/
	isort src/ tests/
	@echo "$(GREEN)✓ Code formatted!$(RESET)"

type-check: ## Проверка типов (mypy)
	@echo "$(CYAN)Running type checker...$(RESET)"
	mypy src/ --ignore-missing-imports

security-check: ## Проверка безопасности зависимостей
	@echo "$(CYAN)Checking for security vulnerabilities...$(RESET)"
	safety check
	bandit -r src/ -f screen

# ==============================================================================
# Docker
# ==============================================================================
docker-build: ## Собрать Docker образ
	@echo "$(CYAN)Building Docker image...$(RESET)"
	docker build -t $(DOCKER_IMAGE):$(DOCKER_TAG) .
	@echo "$(GREEN)✓ Docker image built: $(DOCKER_IMAGE):$(DOCKER_TAG)$(RESET)"

docker-run: ## Запустить в Docker контейнере
	@echo "$(CYAN)Running agent in Docker...$(RESET)"
	docker run --rm \
		-v $(PWD)/.env:/app/.env:ro \
		-v $(PWD)/browser_data:/app/browser_data \
		-v $(PWD)/screenshots:/app/screenshots \
		-v $(PWD)/logs:/app/logs \
		$(DOCKER_IMAGE):$(DOCKER_TAG)

docker-shell: ## Запустить bash в контейнере (для отладки)
	@echo "$(CYAN)Starting interactive shell in Docker...$(RESET)"
	docker run --rm -it \
		-v $(PWD)/.env:/app/.env:ro \
		--entrypoint /bin/bash \
		$(DOCKER_IMAGE):$(DOCKER_TAG)

docker-test: ## Запустить тесты в Docker
	@echo "$(CYAN)Running tests in Docker...$(RESET)"
	docker run --rm \
		-e OPENAI_API_KEY=sk-test-key-not-real \
		-e API_BASE_URL=https://api.test.com/v1 \
		-e MODEL_NAME=test-model \
		$(DOCKER_IMAGE):$(DOCKER_TAG) \
		-m pytest tests/ -v --asyncio-mode=auto

docker-clean: ## Удалить Docker образы и контейнеры
	@echo "$(CYAN)Cleaning Docker artifacts...$(RESET)"
	docker rmi $(DOCKER_IMAGE):$(DOCKER_TAG) 2>/dev/null || true
	docker system prune -f
	@echo "$(GREEN)✓ Docker cleanup complete!$(RESET)"

# ==============================================================================
# Очистка
# ==============================================================================
clean: ## Удалить кэш, логи и временные файлы
	@echo "$(CYAN)Cleaning temporary files...$(RESET)"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.log" -delete
	rm -rf htmlcov/ .coverage coverage.xml
	rm -rf browser_data/* screenshots/* logs/*
	@echo "$(GREEN)✓ Cleanup complete!$(RESET)"

clean-all: clean docker-clean ## Полная очистка (включая Docker)
	@echo "$(GREEN)✓ Deep cleanup complete!$(RESET)"

# ==============================================================================
# Утилиты
# ==============================================================================
setup-env: ## Создать .env из .env.example
	@if [ ! -f .env ]; then \
		echo "$(CYAN)Creating .env from .env.example...$(RESET)"; \
		cp .env.example .env; \
		echo "$(GREEN)✓ .env created! Edit it with your API keys.$(RESET)"; \
	else \
		echo "$(RED)✗ .env already exists. Skipping.$(RESET)"; \
	fi

check-deps: ## Проверить установленные зависимости
	@echo "$(CYAN)Checking Python dependencies...$(RESET)"
	$(PIP) list --outdated
	@echo ""
	@echo "$(CYAN)Checking Playwright browsers...$(RESET)"
	playwright --version
	playwright install --dry-run chromium

update-deps: ## Обновить requirements.txt до последних версий
	@echo "$(CYAN)Updating requirements.txt...$(RESET)"
	$(PIP) install --upgrade pip-tools
	pip-compile --upgrade requirements.in -o requirements.txt
	@echo "$(GREEN)✓ Dependencies updated!$(RESET)"

# ==============================================================================
# CI/CD эмуляция
# ==============================================================================
ci: lint test-coverage security-check ## Эмуляция CI пайплайна локально
	@echo ""
	@echo "$(GREEN)═══════════════════════════════════════$(RESET)"
	@echo "$(GREEN)✓ All CI checks passed!$(RESET)"
	@echo "$(GREEN)═══════════════════════════════════════$(RESET)"

# ==============================================================================
# Информация
# ==============================================================================
info: ## Показать информацию о проекте
	@echo "$(CYAN)Project Information:$(RESET)"
	@echo "  Python: $(shell $(PYTHON) --version)"
	@echo "  Pip: $(shell $(PIP) --version | cut -d' ' -f2)"
	@echo "  Playwright: $(shell playwright --version 2>/dev/null || echo 'Not installed')"
	@echo "  Docker: $(shell docker --version 2>/dev/null || echo 'Not installed')"
	@echo ""
	@echo "$(CYAN)Project Stats:$(RESET)"
	@find src -name '*.py' | xargs wc -l | tail -1 | awk '{printf "  Lines of code: %s\n", $$1}'
	@find tests -name '*.py' 2>/dev/null | xargs wc -l 2>/dev/null | tail -1 | awk '{printf "  Test lines: %s\n", $$1}' || echo "  Test lines: 0"