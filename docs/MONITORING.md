# Мониторинг: подключение внешних систем

Справочный материал по наблюдаемости API-режима (`MODE=api`). Сам Prometheus/Grafana/Sentry
**не разворачиваются** этим проектом — ожидается, что они уже есть в вашей инфраструктуре;
здесь только то, что нужно, чтобы начать их скармливать.

## Эндпоинты

| Эндпоинт | Формат | Назначение |
|---|---|---|
| `GET /metrics` | Prometheus exposition | Метрики для скрейпа (см. таблицу ниже). Без авторизации — скрейпер ходит внутри доверенного периметра. |
| `GET /health` | JSON `{status, components}` | `status`: `ok` / `degraded` / `down`; `components`: `api`, `llm`, `browser`, `store` — каждый `ok`/`degraded`/`down`/`unknown`. HTTP 503 при `down` и при drain (SIGTERM). |

Проверки `/health` кешируются на 30 секунд (`HEALTH_CACHE_SECONDS`) — частый опрос не превращается
в load-генератор против LLM-провайдера.

## Метрики

| Метрика | Тип | Лейблы | Смысл |
|---|---|---|---|
| `cogniweb_tasks_total` | counter | `tenant_id`, `state` | Переходы задач: `queued` → `running` → `finished` / `failed` |
| `cogniweb_task_duration_seconds` | histogram | `tenant_id` | Wall-clock время выполнения задачи |
| `cogniweb_llm_errors_total` | counter | `kind` | Ошибки LLM-провайдера: `timeout`, `connect`, `api_connection`, `rate_limit`, `api_error` |
| `cogniweb_browser_contexts_open` | gauge | — | Открытые персистентные браузерные контексты тенантов (≈ процессы Chromium) |

Лейбл `tenant_id` ограничен валидацией на входе (`^[A-Za-z0-9_-]{1,64}$`), так что кардинальность
контролируема числом реальных тенантов.

## Пример prometheus.yml (локальный Prometheus)

```yaml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: cogniweb-agent
    metrics_path: /metrics
    static_configs:
      - targets: ["localhost:8000"]   # host за nginx; внутри Docker-сети - имя контейнера
        # labels:
        #   env: production
```

Если API закрыт bearer-токеном (`API_AUTH_TOKEN`), помните: `/metrics` и `/health`
сознательно открыты без токена (как liveness-probi) — закройте их на уровне reverse proxy,
если порт доступен шире доверенного периметра:

```nginx
location /metrics { allow 10.0.0.0/8; deny all; proxy_pass http://127.0.0.1:8000; }
```

## Sentry

Активируется **только** двумя условиями одновременно:
1. Переменная окружения `SENTRY_DSN` задана.
2. Пакет `sentry-sdk` установлен (входит в `requirements/api.txt`).

Без DSN интеграция полностью инертна — поведение идентично версии без Sentry.
Трейсинг выключен (`traces_sample_rate=0.0`): отправляется только необработанные исключения.

## Что смотреть в первую очередь

- рост `cogniweb_llm_errors_total{kind="rate_limit"}` → ужесточить pacing (`RATE_LIMIT_SECONDS`);
- `cogniweb_tasks_total{state="failed"}` растёт быстрее `finished` → смотреть отчёты/логи конкретных задач;
- `cogniweb_browser_contexts_open` у потолка `MAX_CONCURRENT_TENANT_CONTEXTS` → память под давлением,
  проверить TTL-закрытие неактивных тенантов;
- `/health` = `degraded` с `llm: down` → провайдер недоступен; если настроен fallback — проверьте его статус в логах.
