"""
Prometheus metrics export for the API service mode.

OPTIONAL dependency by design: when prometheus_client is not installed,
every observe_* call below is a no-op and GET /metrics answers 503 -
the rest of the system is completely unaffected. This follows the same
"optional extras" pattern as playwright-stealth / crawl4ai / aiosqlite.

Metric set (minimum per the public-service review):
- cogniweb_tasks_total{tenant_id,state} - Counter, transitions
  queued -> running -> finished|failed
- cogniweb_task_duration_seconds{tenant_id} - Histogram, wall-clock run time
- cogniweb_llm_errors_total{kind} - Counter, provider error classes
  (timeout | connect | api_connection | rate_limit | api_error)
- cogniweb_browser_contexts_open - Gauge, open tenant browser contexts

Runtime extension (tools / HTTP / limits / evaluator):
- cogniweb_tool_duration_seconds{tool} - Histogram, per-tool dispatch latency
- cogniweb_tool_calls_total{tool,outcome} - Counter, outcome=success|failure
- cogniweb_http_requests_total{method,path_template,status} - Counter
- cogniweb_http_request_duration_seconds{method,path_template} - Histogram
- cogniweb_rate_limit_wait_seconds - Histogram, ACTUAL delay spent pacing
  LLM calls (LLM request-rate throttling, NOT per-tenant quota)
- cogniweb_usage_rejections_total{tenant_id,reason} - Counter, per-tenant
  usage/quota admission refusals (concurrent_limit|hourly_limit|
  quota_exceeded) - a different mechanism from the LLM pacing above
- cogniweb_tenant_tokens_used_total{tenant_id} - Counter, mirrored from
  UsageTracker.record_completion
- cogniweb_evaluator_verdicts_total{verdict} - Counter (pass|fail|error);
  NOTE: true ECE is impossible without a numeric confidence score - the
  evaluator emits a binary verdict only, so this is the measurable proxy
- cogniweb_evaluator_verdict_duration_seconds - Histogram
- cogniweb_browser_action_errors_total{tool,error_type} - Counter, browser
  layer failures classified into a CLOSED error-type set (timeout|other);
  raw exception text NEVER becomes a label (cardinality)
- cogniweb_task_steps_total{outcome} - Histogram of steps per finished task
  (outcome=success|failure) - a degradation signal (agent walking in circles)
- cogniweb_llm_retries_total{provider} - Counter, transport-level tenacity
  retries (provider = active_provider_mode: cloud|local)
- cogniweb_llm_failover_total - Counter, switches to the fallback provider

Cardinality note: tenant_id labels are bounded in practice (a handful of
known tenants per deployment); unbounded label values would be a metric
cardinality bug, which is why tenant_id is regex-validated at intake.
tool / provider / verdict / outcome / reason / path_template are all
closed sets by construction (Pydantic enums, reason codes, route
templates); free text (element_id, task_id, error messages) is never
used as a label value.
"""

import logging

logger = logging.getLogger(__name__)

try:  # optional extra - never a hard requirement
    from prometheus_client import (
        REGISTRY,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the dep
    AVAILABLE = False

if AVAILABLE:
    TASKS_TOTAL = Counter(
        "cogniweb_tasks_total",
        "Task lifecycle transitions by tenant and state.",
        ["tenant_id", "state"],
    )
    TASK_DURATION = Histogram(
        "cogniweb_task_duration_seconds",
        "Wall-clock duration of completed task runs.",
        ["tenant_id"],
        buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
    )
    LLM_ERRORS = Counter(
        "cogniweb_llm_errors_total",
        "LLM provider errors by class.",
        ["kind"],
    )
    BROWSER_CONTEXTS = Gauge(
        "cogniweb_browser_contexts_open",
        "Currently open persistent tenant browser contexts.",
    )
    # ---- runtime extension ----------------------------------------------
    TOOL_DURATION = Histogram(
        "cogniweb_tool_duration_seconds",
        "Wall-clock time of one AgentOrchestrator tool dispatch.",
        ["tool"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    )
    TOOL_CALLS = Counter(
        "cogniweb_tool_calls_total",
        "Tool dispatches by tool and outcome (success|failure).",
        ["tool", "outcome"],
    )
    HTTP_REQUESTS = Counter(
        "cogniweb_http_requests_total",
        "HTTP requests by method, route path template and status code.",
        ["method", "path_template", "status"],
    )
    HTTP_DURATION = Histogram(
        "cogniweb_http_request_duration_seconds",
        "HTTP request latency by method and route path template.",
        ["method", "path_template"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    )
    RATE_LIMIT_WAIT = Histogram(
        "cogniweb_rate_limit_wait_seconds",
        "Actual delay spent in LLM rate-limit pacing before a call "
        "(request-RATE throttling; per-tenant quota is a separate metric).",
        buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30, 60),
    )
    USAGE_REJECTIONS = Counter(
        "cogniweb_usage_rejections_total",
        "Per-tenant usage/quota admission refusals (concurrent_limit | "
        "hourly_limit | quota_exceeded) - token budget / task quota, "
        "NOT LLM request pacing.",
        ["tenant_id", "reason"],
    )
    TENANT_TOKENS = Counter(
        "cogniweb_tenant_tokens_used_total",
        "Cumulative LLM tokens per tenant, mirrored from UsageTracker.",
        ["tenant_id"],
    )
    EVALUATOR_VERDICTS = Counter(
        "cogniweb_evaluator_verdicts_total",
        "Self-critique evaluator verdicts (pass | fail | error); binary "
        "verdict proxy - true ECE needs a numeric confidence score.",
        ["verdict"],
    )
    EVALUATOR_DURATION = Histogram(
        "cogniweb_evaluator_verdict_duration_seconds",
        "Latency of the evaluator LLM call.",
        buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    )
    BROWSER_ACTION_ERRORS = Counter(
        "cogniweb_browser_action_errors_total",
        "Browser-layer action failures by tool and classified error type "
        "(closed set: timeout | other) - explains WHY the tool latency "
        "tail is long. Raw exception text is never a label.",
        ["tool", "error_type"],
    )
    TASK_STEPS = Histogram(
        "cogniweb_task_steps_total",
        "Steps taken per finished task by outcome - growth signals the "
        "agent walking in circles before latency/errors rise.",
        ["outcome"],
        buckets=(1, 2, 3, 5, 8, 10, 15, 20, 30, 50, 75, 100),
    )
    LLM_RETRIES = Counter(
        "cogniweb_llm_retries_total",
        "Transport-level (tenacity) LLM retries by active provider mode.",
        ["provider"],
    )
    LLM_FAILOVERS = Counter(
        "cogniweb_llm_failover_total",
        "Switches to the fallback LLM provider after connection failures.",
    )


def observe_task(tenant_id: str, state: str, duration_seconds: float | None = None) -> None:
    """Record one task transition (+ duration on terminal states). Never
    raises - metrics must not be able to break task execution."""
    if not AVAILABLE:
        return
    try:
        TASKS_TOTAL.labels(tenant_id=tenant_id, state=state).inc()
        if duration_seconds is not None:
            TASK_DURATION.labels(tenant_id=tenant_id).observe(duration_seconds)
    except Exception:  # noqa: BLE001 - observability must never crash the app
        logger.debug("metrics.observe_task failed", exc_info=True)


def observe_task_queued(tenant_id: str) -> None:
    observe_task(tenant_id, "queued")


def observe_task_running(tenant_id: str) -> None:
    observe_task(tenant_id, "running")


def observe_llm_error(kind: str) -> None:
    if not AVAILABLE:
        return
    try:
        LLM_ERRORS.labels(kind=kind).inc()
    except Exception:  # noqa: BLE001
        logger.debug("metrics.observe_llm_error failed", exc_info=True)


def set_browser_contexts(open_count: int) -> None:
    if not AVAILABLE:
        return
    try:
        BROWSER_CONTEXTS.set(open_count)
    except Exception:  # noqa: BLE001
        logger.debug("metrics.set_browser_contexts failed", exc_info=True)


def observe_tool_call(tool: str, success: bool, duration_seconds: float) -> None:
    """One AgentOrchestrator tool dispatch: latency + success/failure.
    `tool` is the closed Literal from AgentAction, never free text."""
    if not AVAILABLE:
        return
    try:
        outcome = "success" if success else "failure"
        TOOL_CALLS.labels(tool=tool, outcome=outcome).inc()
        TOOL_DURATION.labels(tool=tool).observe(duration_seconds)
    except Exception:  # noqa: BLE001 - observability must never crash the app
        logger.debug("metrics.observe_tool_call failed", exc_info=True)


def observe_http_request(
    method: str, path_template: str, status: int, duration_seconds: float
) -> None:
    """One HTTP request: status counter + latency histogram. Called from the
    app middleware with request.scope["route"].path (template like
    /task/{task_id}) - never the concrete path, so cardinality stays flat."""
    if not AVAILABLE:
        return
    try:
        HTTP_REQUESTS.labels(
            method=method, path_template=path_template, status=str(status)
        ).inc()
        HTTP_DURATION.labels(method=method, path_template=path_template).observe(
            duration_seconds
        )
    except Exception:  # noqa: BLE001
        logger.debug("metrics.observe_http_request failed", exc_info=True)


def observe_rate_limit_wait(waited_seconds: float) -> None:
    """Actual delay spent pacing an LLM call (request-RATE throttling).
    Distinct from per-tenant usage/quota limits - see USAGE_REJECTIONS."""
    if not AVAILABLE:
        return
    try:
        RATE_LIMIT_WAIT.observe(waited_seconds)
    except Exception:  # noqa: BLE001
        logger.debug("metrics.observe_rate_limit_wait failed", exc_info=True)


def observe_usage_rejection(tenant_id: str, reason: str) -> None:
    """Per-tenant usage/quota refusal (concurrent_limit | hourly_limit |
    quota_exceeded) - token budget / task quota, NOT LLM pacing."""
    if not AVAILABLE:
        return
    try:
        USAGE_REJECTIONS.labels(tenant_id=tenant_id, reason=reason).inc()
    except Exception:  # noqa: BLE001
        logger.debug("metrics.observe_usage_rejection failed", exc_info=True)


def observe_tenant_tokens(tenant_id: str, tokens_used: int) -> None:
    """Mirror one record_completion() token increment per tenant."""
    if not AVAILABLE:
        return
    try:
        TENANT_TOKENS.labels(tenant_id=tenant_id).inc(int(tokens_used))
    except Exception:  # noqa: BLE001
        logger.debug("metrics.observe_tenant_tokens failed", exc_info=True)


def observe_evaluator_verdict(verdict: str, duration_seconds: float) -> None:
    """Evaluator verdict (pass|fail|error) + call latency. The evaluator
    emits a binary verdict only, so pass/fail rate is the honest proxy for
    quality-of-self-assessment (true ECE needs numeric confidence)."""
    if not AVAILABLE:
        return
    try:
        EVALUATOR_VERDICTS.labels(verdict=verdict).inc()
        EVALUATOR_DURATION.observe(duration_seconds)
    except Exception:  # noqa: BLE001
        logger.debug("metrics.observe_evaluator_verdict failed", exc_info=True)


def observe_browser_action_error(tool: str, error_type: str) -> None:
    """Browser-layer action failure. `error_type` must come from the closed
    classifier (timeout | other); raw exception text is never passed here."""
    if not AVAILABLE:
        return
    try:
        BROWSER_ACTION_ERRORS.labels(tool=tool, error_type=error_type).inc()
    except Exception:  # noqa: BLE001
        logger.debug("metrics.observe_browser_action_error failed", exc_info=True)


def observe_task_finished(
    tenant_id: str, success: bool, duration_seconds: float, steps_taken: int | None = None
) -> None:
    observe_task(tenant_id, "finished" if success else "failed", duration_seconds)
    if steps_taken is not None:
        if not AVAILABLE:
            return
        try:
            TASK_STEPS.labels(outcome="success" if success else "failure").observe(
                steps_taken
            )
        except Exception:  # noqa: BLE001
            logger.debug("metrics.observe_task_finished(steps) failed", exc_info=True)


def observe_llm_retry(provider: str) -> None:
    """One transport-level (tenacity) retry; provider = active provider mode
    (cloud | local) - a closed setting value, not a model string."""
    if not AVAILABLE:
        return
    try:
        LLM_RETRIES.labels(provider=provider).inc()
    except Exception:  # noqa: BLE001
        logger.debug("metrics.observe_llm_retry failed", exc_info=True)


def observe_llm_failover() -> None:
    """One actual switch to the fallback LLM provider."""
    if not AVAILABLE:
        return
    try:
        LLM_FAILOVERS.inc()
    except Exception:  # noqa: BLE001
        logger.debug("metrics.observe_llm_failover failed", exc_info=True)


def render() -> str | None:
    """Full exposition payload for GET /metrics; None = library missing."""
    if not AVAILABLE:
        return None
    try:
        return generate_latest(REGISTRY).decode("utf-8")
    except Exception:  # noqa: BLE001
        logger.exception("metrics render failed")
        return None


CONTENT_TYPE_LATEST = (
    "text/plain; version=0.0.4; charset=utf-8" if AVAILABLE else "text/plain; charset=utf-8"
)
