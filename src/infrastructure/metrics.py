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

Cardinality note: tenant_id labels are bounded in practice (a handful of
known tenants per deployment); unbounded label values would be a metric
cardinality bug, which is why tenant_id is regex-validated at intake.
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


def observe_task_finished(tenant_id: str, success: bool, duration_seconds: float) -> None:
    observe_task(tenant_id, "finished" if success else "failed", duration_seconds)


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
