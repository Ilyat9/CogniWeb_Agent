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
| `cogniweb_task_steps_total` | histogram | `outcome` | Число шагов на завершённую задачу (`success`/`failure`). Рост медианы — ранний сигнал деградации: агент начал ходить кругами ещё до роста latency/ошибок |
| `cogniweb_llm_errors_total` | counter | `kind` | Ошибки LLM-провайдера: `timeout`, `connect`, `api_connection`, `rate_limit`, `api_error` |
| `cogniweb_llm_retries_total` | counter | `provider` | Транспортные ретраи (tenacity) перед вызовом LLM; `provider` = активный режим (`cloud`\|`local`) |
| `cogniweb_llm_failover_total` | counter | — | Фактические переключения на fallback-провайдер после connection-ошибок |
| `cogniweb_rate_limit_wait_seconds` | histogram | — | **Rate limiting** — фактическая задержка pacing'а вызовов LLM (см. различие ниже) |
| `cogniweb_usage_rejections_total` | counter | `tenant_id`, `reason` | **Usage/quota limiting** — отказы в приёме задачи: `concurrent_limit`\|`hourly_limit`\|`quota_exceeded` |
| `cogniweb_tenant_tokens_used_total` | counter | `tenant_id` | Суммарные LLM-токены по тенанту (зеркало `UsageTracker.record_completion`) |
| `cogniweb_tool_duration_seconds` | histogram | `tool` | Латентность диспетчеризации инструмента (весь if/elif-блок `_execute_action`); `tool` — закрытый enum из `AgentAction` |
| `cogniweb_tool_calls_total` | counter | `tool`, `outcome` | Вызовы инструментов: `success`\|`failure` |
| `cogniweb_browser_contexts_open` | gauge | — | Открытые персистентные браузерные контексты тенантов (≈ процессы Chromium) |
| `cogniweb_browser_action_errors_total` | counter | `tool`, `error_type` | Ошибки браузерного слоя — объясняют, ПОЧЕМУ хвост латентности тулов длинный; `error_type` — закрытое множество (`timeout`\|`other`), сырой текст исключения НИКОГДА не попадает в лейбл |
| `cogniweb_http_requests_total` | counter | `method`, `path_template`, `status` | HTTP-запросы; `path_template` — шаблон маршрута (`/task/{task_id}`), не конкретный id; `/metrics` и `/health` не инструментируются (self-scrape шум) |
| `cogniweb_http_request_duration_seconds` | histogram | `method`, `path_template` | Латентность HTTP-запросов |
| `cogniweb_evaluator_verdicts_total` | counter | `verdict` | Вердикты эвалюатора: `pass`\|`fail`\|`error` (`error` = вызов упал или ответ не распарсился) |
| `cogniweb_evaluator_verdict_duration_seconds` | histogram | — | Латентность вызова эвалюатора |

Лейбл `tenant_id` ограничен валидацией на входе (`^[A-Za-z0-9_-]{1,64}$`), так что кардинальность
контролируема числом реальных тенантов. Остальные лейблы (`tool`, `provider`, `verdict`,
`outcome`, `reason`, `path_template`, `error_type`) — закрытые множества по построению;
`element_id`, `task_id` и произвольный текст ошибок в лейблы не попадают никогда.

### Rate limiting vs usage/quota limiting — два разных механизма

В коде это два независимых механизма, не смешивайте их в дашбордах:

1. **Rate limiting (троттлинг скорости вызовов LLM)** — `RATE_LIMIT_SECONDS` /
   `LOCAL_RATE_LIMIT_SECONDS` + `LLMRateLimiter` в `llm.py`: держит паузу между
   вызовами провайдера, чтобы не упереться в его rate limit. Метрика —
   `cogniweb_rate_limit_wait_seconds` (фактически потраченное ожидание).
2. **Usage/quota limiting (бюджет и квоты тенанта)** — `UsageTracker` в `usage.py`:
   лимит одновременных задач, скользящее окно подач и жёсткий токен-бюджет per tenant.
   Отказ = HTTP 429 на POST /task. Метрики — `cogniweb_usage_rejections_total{reason}`
   и `cogniweb_tenant_tokens_used_total`.

### Эвалюатор и «ECE» (важно)

True Expected Calibration Error **недоступен** в этой системе: он требует пар
(числовой confidence, ground truth correctness), а эвалюатор
(`AgentOrchestrator._evaluate_completion`) возвращает только бинарный
`VERDICT:PASS|FAIL` без числовой уверенности. Поэтому здесь экспортируется честный
прокси качества самооценки агента: **pass/fail rate эвалюатора и её тренд**
(`cogniweb_evaluator_verdicts_total` — рост доли `fail`/`error` = агент перестал
адекватно оценивать своё завершение) плюс латентность вызова. Полноценный ECE
потребовал бы изменения промпта эвалюатора (возврат `CONFIDENCE:0-100`) и
суррогатного ground truth (например, финальный `TaskResult.success`) со скользящим
окном наблюдений — осознанно не делается в этом PR.

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

## Grafana: готовые PromQL-примеры

```promql
# p95 латентности по тулу (5m окно)
histogram_quantile(0.95, sum(rate(cogniweb_tool_duration_seconds_bucket[5m])) by (le, tool))

# Error rate по тулам
sum(rate(cogniweb_tool_calls_total{outcome="failure"}[5m])) by (tool)
  / sum(rate(cogniweb_tool_calls_total[5m])) by (tool)

# RPS по эндпоинтам (шаблон пути, без конкретных id)
sum(rate(cogniweb_http_requests_total[1m])) by (path_template)

# p95 латентности HTTP
histogram_quantile(0.95, sum(rate(cogniweb_http_request_duration_seconds_bucket[5m])) by (le, path_template))

# 5xx rate по API
sum(rate(cogniweb_http_requests_total{status=~"5.."}[5m]))
  / sum(rate(cogniweb_http_requests_total[5m]))

# Медиана/95перц. числа шагов на задачу — деградация «агент ходит кругами»
histogram_quantile(0.5, sum(rate(cogniweb_task_steps_total_bucket[30m])) by (le, outcome))
histogram_quantile(0.95, sum(rate(cogniweb_task_steps_total_bucket[30m])) by (le, outcome))

# Сколько времени агент проводит в pacing'е LLM (доля от времени выполнения)
sum(rate(cogniweb_rate_limit_wait_seconds_sum[5m]))
  / sum(rate(cogniweb_task_duration_seconds_sum[5m]))

# Здоровье LLM-провайдера: ретраи и фактические failover'ы
sum(rate(cogniweb_llm_retries_total[5m])) by (provider)
increase(cogniweb_llm_failover_total[1h])

# Fail rate эвалюатора (прокси качества самооценки агента)
sum(rate(cogniweb_evaluator_verdicts_total{verdict!="pass"}[30m]))
  / sum(rate(cogniweb_evaluator_verdicts_total[30m]))

# Отказы по квотам тенантов (ёмкость/лимиты подобраны плохо)
sum(increase(cogniweb_usage_rejections_total[1h])) by (tenant_id, reason)

# Расход токенов по тенантам (rate за час)
sum(increase(cogniweb_tenant_tokens_used_total[1h])) by (tenant_id)
```

## Что смотреть в первую очередь

- рост `cogniweb_llm_errors_total{kind="rate_limit"}` → ужесточить pacing (`RATE_LIMIT_SECONDS`);
- `cogniweb_tasks_total{state="failed"}` растёт быстрее `finished` → смотреть отчёты/логи конкретных задач;
- `cogniweb_browser_contexts_open` у потолка `MAX_CONCURRENT_TENANT_CONTEXTS` → память под давлением,
  проверить TTL-закрытие неактивных тенантов;
- `/health` = `degraded` с `llm: down` → провайдер недоступен; если настроен fallback — проверьте его статус в логах;
- длинный хвост `cogniweb_tool_duration_seconds` у конкретного `tool` → открыть
  `cogniweb_browser_action_errors_total{tool=...}`: таймауты/сбои браузерного слоя
  объясняют латентность лучше, чем сам график;
- медиана `cogniweb_task_steps_total` ползёт вверх при стабильной succeeded-доле →
  агент начал ходить кругами (ранний сигнал деградации);
- `cogniweb_llm_failover_total` растёт → провайдер мигает; помните, что failover
  sticky (автоматического возврата нет);
- доля `{verdict="fail"}` в `cogniweb_evaluator_verdicts_total` растёт → агент
  чаще «недооценивает» своё завершение; `{verdict="error"}` — сам эвалюатор деградирует.
