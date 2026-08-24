# Продакшен-деплой (VPS с Ubuntu)

Следующий шаг после [QUICK_START.md](QUICK_START.md): превращение локально работающего
API-сервиса в сервис на сервере — за reverse proxy, с TLS, автозапуском и бэкапами.
Инструкция провайдер-агностична: подойдёт любой VPS с Ubuntu 22.04/24.04 и открытыми
портами 80/443. Установка зависимостей и локальный запуск здесь НЕ дублируются — см.
QUICK_START.md.

> ⚠️ Прежде чем открывать сервис наружу: агент управляет реальным браузером с персистентными
> cookies. Обязательный минимум — сильный `API_AUTH_TOKEN` и понимание, что tenant_id без
> аутентификации — это идентификатор бакета ресурсов, а не личность (см. SELF_REVIEW.md §7).

## 1. Reverse proxy (nginx) + WebSocket

WebSocket в проекте используется (`WS /ws/task/{id}` для живого стрима шагов), поэтому
конфиг обязан проксировать Upgrade-заголовки. `/etc/nginx/sites-available/cogniweb`:

```nginx
server {
    listen 80;
    server_name agent.example.com;          # ваш домен, A-запись -> IP сервера

    # Увеличенные лимиты: скриншоты и step-стрим бывают заметно больше дефолтных 1M
    client_max_body_size 10m;
    proxy_read_timeout 300s;                # длинные задачи: не рвать стрим по таймауту

    location / {
        proxy_pass http://127.0.0.1:8000;   # uvicorn слушает ТОЛЬКО localhost
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # --- WebSocket upgrade (/ws/task/{id}) ---
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/cogniweb /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Важно: приложение при этом должно слушать `127.0.0.1` (дефолт `API_BIND_HOST`) — наружу
смотрит только nginx. Если запускаете через Docker (`MODE=api`, контейнер биндит `0.0.0.0`
внутри), опубликуйте порт только на loopback: `-p 127.0.0.1:8000:8000`.

## 2. TLS (Let's Encrypt / certbot)

После того как HTTP-вариант выше отвечает на домене:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d agent.example.com
```

certbot сам допишет в nginx-конфиг redirect с 80 на 443 и блок `listen 443 ssl`.
Продление автоматическое (systemd timer `certbot.timer`; проверить: `sudo certbot renew --dry-run`).

## 3. Автозапуск (systemd)

Пример юнита `/etc/systemd/system/cogniweb.service` (запуск из репозитория,
venv — как в QUICK_START.md):

```ini
[Unit]
Description=CogniWeb Agent API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=cogniweb                          # выделенный непривилегированный пользователь
Group=cogniweb
WorkingDirectory=/opt/cogniweb/CogniWeb_Agent
EnvironmentFile=/opt/cogniweb/cogniweb.env
ExecStart=/opt/cogniweb/CogniWeb_Agent/.venv/bin/uvicorn src.api.app:build_default_app --factory --host 127.0.0.1 --port 8000
Restart=on-failure                     # автоперезапуск при падении
RestartSec=5
KillSignal=SIGTERM                     # корректный drain: текущая задача доходит до шага
TimeoutStopSec=120                     # время на graceful-завершение

# Жёсткая гигиена прав: сервису нужны только его каталоги
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=/opt/cogniweb

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -r -s /usr/sbin/nologin cogniweb
sudo chown -R cogniweb:cogniweb /opt/cogniweb
sudo systemctl daemon-reload
sudo systemctl enable --now cogniweb
journalctl -u cogniweb -f        # логи
```

SIGTERM обрабатывается как drain: новые задачи отклоняются (503), текущая корректно
останавливается на границе шага — поэтому `KillSignal=SIGTERM` + щедрый `TimeoutStopSec`.

## 4. Переменные окружения в продакшене

Файл `/opt/cogniweb/cogniweb.env` (формат KEY=VALUE, права `chmod 600`). Обязательные к
переопределению и безопасные дефолты:

| Переменная | Продакшен | Почему |
|---|---|---|
| `OPENAI_API_KEY` | реальный ключ | обязателен всегда |
| `API_AUTH_TOKEN` | **обязателен**, случайная строка ≥ 16 символов (`openssl rand -hex 32`) | сервис за пределами localhost без токена не должен жить |
| `TASK_DB_PATH` | `/opt/cogniweb/data/tasks.db` | история задач переживает рестарты; каталог под volume/бэкапы |
| `USER_DATA_DIR` | `/opt/cogniweb/browser_data` | персистентные профили; подпапки `tenants/{tenant_id}` создаются автоматически |
| `SCREENSHOT_DIR`, `REPORTS_DIR` | пути внутри `/opt/cogniweb` | не писать в рабочий каталог репозитория |
| `MAX_CONCURRENT_TENANT_CONTEXTS` | по числу активных тенантов (дефолт 1) | каждый открытый контекст ≈ отдельный Chromium, 100–300МБ RAM |
| `TENANT_CONTEXT_IDLE_TTL_SECONDS` | 600 (дефолт разумен) | освобождает память неактивных тенантов |
| `RATE_LIMIT_TASKS_PER_HOUR` | по договорённости с клиентами | защита от выжигания LLM-бюджета |
| `RATE_LIMIT_CONCURRENT_PER_TENANT` | 2 (дефолт разумен) | справедливость между тенантами |
| `TASK_MAX_LENGTH` | 10000 (дефолт разумен) | санитизация входа |
| `ENABLE_TASK_CONTENT_FILTER` + `TASK_FORBIDDEN_PATTERNS` | включить для публичного сервиса | базовая антиабьюз-защита (НЕ модерация — см. README) |
| `SENTRY_DSN` | DSN проекта или пусто | трекинг исключений; без пакета sentry-sdk игнорируется |
| `API_BIND_HOST` | `127.0.0.1` (не менять!) | наружу — только nginx |

Безопасные дефолты (можно не трогать): `HEADLESS=true`, `ENABLE_STEALTH_MODE`,
pacing LLM (`RATE_LIMIT_SECONDS`), TTL истории задач (`TASK_TTL_HOURS`).

## 5. Резервное копирование (SQLite)

Персистентное хранилище задач реализовано (`TASK_DB_PATH`, SQLite через aiosqlite) — бэкапить
один файл БД плюс, при желании, профили браузера:

```bash
#!/bin/bash
# /opt/cogniweb/backup.sh - cron: */30 * * * * /opt/cogniweb/backup.sh
set -euo pipefail
DEST=/var/backups/cogniweb
DB=/opt/cogniweb/data/tasks.db
mkdir -p "$DEST"
# Безопасный онлайн-бэкап SQLite: консистентная копия при живом писателе
sqlite3 "$DB" ".backup '$DEST/tasks-$(date +%F-%H%M).db'"
find "$DEST" -name 'tasks-*.db' -mtime +7 -delete   # ретеншн неделя
```

(`apt install sqlite3`. Альтернатива без установки sqlite3 — короткая остановка сервиса
перед обычным `cp`; для этого сервиса обычно избыточна.)

Профили браузера (`USER_DATA_DIR`) бэкапировать обычным rsync/tar вне окна активных задач:
это сессии сайтов, их утечка равносильна утечке аккаунтов — храните бэкапы с теми же
ограничениями доступа, что и секреты.

## Чеклист после деплоя

- [ ] `https://agent.example.com/health` → `{"status": "ok", ...}`
- [ ] `POST /task` без токена → 401; с токеном → 202
- [ ] WebSocket-стрим в UI обновляется живьём (значит, upgrade проксируется)
- [ ] `systemctl restart cogniweb` → история задач доступна (`GET /tasks`)
- [ ] бэкап-скрипт отработал и файл появился в `/var/backups/cogniweb`


