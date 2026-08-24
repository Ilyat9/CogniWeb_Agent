"""
3.3 (optional [api] extra): a small FastAPI service wrapper around the
orchestrator.

NOT part of base requirements - install via requirements/api.txt
(fastapi + uvicorn) or requirements/ui.txt (fastapi + uvicorn + websockets,
adds the WebSocket live-progress channel) and run with MODE=api in Docker
(see Dockerfile) or:

    uvicorn src.api.app:build_default_app --factory

Design:
- POST /task        -> 202 {"task_id"}; 503 once draining (SIGTERM)
- GET  /task/{id}   -> queued | running | finished (with TaskResult)
- GET  /tasks       -> task history (id, state, summaries)
- GET  /task/{id}/steps -> full step-event list for a task
- WS   /ws/task/{id}    -> live step/final event stream (UI polls
                       GET /task/{id}/steps as a fallback)
- GET  /task/{id}/screenshot -> last screenshot taken during the run
- POST /task/{id}/stop     -> per-task graceful stop request
- GET  /config       -> current Settings (secrets masked)
- GET  /reports, /reports/{run_id} -> per-run JSON reports
- GET  /health       -> 200 (503 while draining)
- GET  /             -> static web UI (src/api/static/index.html)

Task 1 (web UI): step events flow through an in-memory pub/sub, not the
agent.log file - the orchestrator gets an `event_sink` callback (see
AgentOrchestrator.__init__) which appends to the task record and fans out
to per-subscriber asyncio.Queues (one per open WebSocket). This is cleanly
testable with an injected fake task_runner and does not couple the
orchestrator to files or to the API layer.

Task 1 (persistence): `app.state.tasks` remains the hot in-memory working
set (the worker and pub/sub mutate records inline, synchronously), but it
is now a WRITE-THROUGH CACHE over a SQLite store (`src/api/task_store.py`,
aiosqlite). Every mutation is mirrored to disk; at startup the store is
hydrated back into memory, so task history survives restarts/redeploys.
When no settings are available (injected-runner tests) or aiosqlite is
not installed, the store stays None and behavior degrades gracefully to
the old pure in-memory mode - documented trade-off, see SELF_REVIEW.md.

Multi-tenant dispatcher (replaces the single queue + single worker): a FIFO
PER TENANT, drained round-robin so one tenant's backlog cannot starve
others; up to MAX_CONCURRENT_TENANT_CONTEXTS tasks run in parallel (one per
tenant - each tenant's persistent browser context is exclusive). Default
limit 1 = the historical strictly-one-at-a-time behavior.
SIGTERM = drain: new submissions are refused with 503, currently running
tasks are allowed to finish (the in-process graceful shutdown
flag additionally tells the orchestrator loop to stop at the next step
boundary, so "finish" means: stop cleanly at the earliest safe point).
"""

import asyncio
import inspect
import json
import logging
import re
import sys
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

from ..core.exceptions import ConfigurationError
from ..core.models import ActionResult, AgentAction, TaskResult
from ..infrastructure import metrics as _metrics
from ..infrastructure.task_policy import TaskPolicy
from ..infrastructure.usage import UsageTracker
from .models import (  # noqa: F401 - re-export
    _SAFE_TENANT_ID,
    DEFAULT_TENANT_ID,
    TaskStatus,
    TaskSubmission,
)
from .security import mask_settings  # noqa: F401 - re-export

logger = logging.getLogger(__name__)

TaskRunner = Callable[..., Awaitable[TaskResult]]
LifecycleHook = Callable[[], Awaitable[None]]
# A task runner may optionally accept extra keyword arguments injected by
# the worker (both are always passed by keyword, so 2-positional-arg
# runners - like every existing test fixture - keep working):
#   emit:       sync callable(dict) - live step-event publisher
#   stop_check: zero-arg callable -> bool - per-task graceful stop flag
#   on_step:    sync callable(step, action, result) - hardening supplement
#               live-status hook; the worker updates the task record's
#               current_step/last_tool from it (in-memory writes only)
#   tenant_id:  str - multi-tenancy: which tenant's browser context to run
#               this task on (runners that ignore it never see it)
_RUNNER_OPTIONAL_KWARGS = ("emit", "stop_check", "on_step", "tenant_id")

# Multi-tenancy: tenant_id is an IDENTIFIER, not an identity claim; its
# validation regex lives in src/api/models.py (_SAFE_TENANT_ID).
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Hardening (WS auth): browsers cannot set Authorization headers on a
# WebSocket, so the browser UI exchanges its Bearer token for a
# SHORT-LIVED, SINGLE-USE ticket via POST /ws/ticket and passes only that
# ticket in the WS query string. A long-lived static token must never
# appear in a URL: URLs leak into server/proxy access logs, browser
# history and Referer headers.
WS_TICKET_TTL_SECONDS = 60

# Finished-task bookkeeping: every task record carries its full steps
# buffer, so an unbounded dict is an unbounded memory leak in a
# long-lived API process. Finished records older than TASK_TTL_HOURS are
# dropped, then only the newest MAX_FINISHED_TASKS finished records are
# kept - pruned on every submit AND by a periodic background loop (a
# quiet API that receives no submits would otherwise never prune).
# FIX (persistence): these are now real Settings fields
# (task_ttl_hours / max_finished_tasks / task_prune_interval_seconds);
# the constants below remain only as defaults for settings-less test
# wiring where getattr() falls back to them.
TASK_TTL_HOURS = 24
MAX_FINISHED_TASKS = 200

# Pending-task backpressure: the worker runs strictly one task at a time,
# so without a cap a burst of submissions grows the queue (and its task
# records) without limit while each task waits hours to even start.
# Beyond MAX_PENDING_TASKS queued-or-running tasks new submissions are
# rejected with 429 instead of being silently buffered forever.
MAX_PENDING_TASKS = 50

# How often the background pruner sweeps finished tasks (seconds).
PRUNE_INTERVAL_SECONDS = 600

# /health component-check cache: probes (LLM provider ping, browser engine)
# run at most once per this interval - a monitoring system polling every
# 5s must not turn into a load generator against the LLM provider.
HEALTH_CACHE_SECONDS = 30.0

# Sentry init is process-global and idempotent; guard against re-init from
# multiple create_app() calls (tests build many apps).
_sentry_initialized = False


def _init_sentry(settings: Any | None) -> None:
    """Optional error tracking: activates ONLY when SENTRY_DSN is set AND
    sentry-sdk is importable. Unset DSN or missing package = behavior
    completely unchanged (documented trade-off, see SELF_REVIEW.md)."""
    global _sentry_initialized
    dsn = getattr(settings, "sentry_dsn", "") if settings is not None else ""
    if not dsn or _sentry_initialized:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        # traces_sample_rate=0.0 on purpose: error tracking only. Turning
        # on tracing silently ships every request's metadata to a third
        # party - that must be an explicit operator decision in code.
        sentry_sdk.init(
            dsn=dsn,
            integrations=[StarletteIntegration(), FastApiIntegration()],
            traces_sample_rate=0.0,
        )
        _sentry_initialized = True
        logger.info("Sentry initialized (SENTRY_DSN set)")
    except ImportError as e:
        logger.warning(f"SENTRY_DSN is set but sentry-sdk is not installed ({e}); skipped")
    except Exception as e:  # noqa: BLE001 - monitoring must never block startup
        logger.warning(f"Sentry initialization failed (continuing without it): {e}")

# Default sweep interval for idle tenant browser contexts (seconds) when no
# settings object is available (settings-less test wiring).
CONTEXT_SWEEP_INTERVAL_SECONDS = 60


def create_app(
    task_runner: TaskRunner,
    settings: Any | None = None,
    on_startup: LifecycleHook | None = None,
    on_shutdown: LifecycleHook | None = None,
    context_pool: Any | None = None,
    health_providers: dict[str, Callable[[], Awaitable[bool]]] | None = None,
) -> FastAPI:
    """Build the API app with an injected task runner (async callable
    (task, starting_url) -> TaskResult; may additionally accept keyword
    args `emit`, `stop_check` and `on_step`, see _RUNNER_OPTIONAL_KWARGS).
    Injection keeps this module testable without launching a browser or an
    LLM client. `settings` is optional: when given, GET /config reflects it
    and API_AUTH_TOKEN (if set) enables bearer auth; otherwise settings
    are lazily loaded on first request.

    on_startup / on_shutdown (optional): async no-arg hooks run inside the
    app's startup/shutdown events. They let production wiring (see
    build_default_app) own heavy shared resources - the browser and the
    LLM client - for the app's WHOLE lifetime instead of per task, while
    injected-runner tests keep constructing nothing."""

    app = FastAPI(title="CogniWeb Agent API", version="1.1")
    # Internal records: plain dicts (not TaskStatus) so we can carry the
    # raw task text / starting_url alongside the visible status fields,
    # plus the UI-facing step-event buffer and pub/sub state.
    app.state.tasks: dict[str, dict[str, Any]] = {}
    # Task 1 (persistence): durable backing store, set up at startup when
    # settings carry a task_db_path and aiosqlite is importable. None =
    # legacy pure in-memory mode (tests / missing optional dependency).
    app.state.task_store: Any | None = None
    # Multi-tenancy dispatch state (replaces the single asyncio.Queue):
    # - tenant_queues: FIFO of task_ids PER TENANT
    # - queue_order: tenants that currently have queued work, kept in
    #   round-robin rotation so one tenant's backlog cannot starve others
    # - running_count vs max_parallel caps globally open contexts; a
    #   tenant's own context is additionally EXCLUSIVE inside the pool.
    app.state.tenant_queues: dict[str, Any] = {}
    app.state.queue_order: Any = deque()
    app.state.dispatch_cond = asyncio.Condition()
    app.state.running_count = 0
    # Optional TenantContextPool, injected by build_default_app. When None
    # (injected-runner tests) no idle-context sweeper runs and no browser
    # resources are owned by the app.
    app.state.context_pool = context_pool
    # Observability (/health): component liveness probes injected by the
    # production wiring (build_default_app). Without wiring, components
    # report "unknown" and overall status stays "ok" - an injected-runner
    # test app cannot prove or disprove provider health.
    app.state.health_providers: dict[str, Callable[[], Awaitable[bool]]] = (
        health_providers or {}
    )
    app.state._health_cache: tuple[float, dict] | None = None
    _init_sentry(settings)
    app.state.draining = False
    app.state.settings = settings
    app.state._settings_loaded = settings is not None

    def _setting(name: str, default: Any) -> Any:
        """Settings value when available, else the module default - keeps
        settings-less test wiring on the historical constants."""
        if settings is None:
            return default
        return getattr(settings, name, default)

    task_ttl_hours = float(_setting("task_ttl_hours", TASK_TTL_HOURS))
    max_finished_tasks = int(_setting("max_finished_tasks", MAX_FINISHED_TASKS))
    prune_interval_seconds = float(
        _setting("task_prune_interval_seconds", PRUNE_INTERVAL_SECONDS)
    )
    # Multi-tenancy: how many tasks may run in parallel (= open contexts).
    # Default 1 reproduces the legacy strictly-sequential worker exactly.
    sweep_interval_seconds = float(
        _setting("tenant_context_sweep_interval_seconds", CONTEXT_SWEEP_INTERVAL_SECONDS)
    )
    max_parallel = int(_setting("max_concurrent_tenant_contexts", 1))
    # Hardening supplement (access control): optional bearer token. None
    # (default) keeps every endpoint open - backwards compatible with
    # already-deployed installations. Read from settings when they carry
    # the field (build_default_app path), else None until lazy load.
    app.state.auth_token = getattr(settings, "api_auth_token", None)
    # one-time WS tickets: ticket -> expiry (time.monotonic)
    app.state.ws_tickets: dict[str, float] = {}
    # Intake policy (sanitization): shared validator; reads its knobs from
    # settings when present, module defaults otherwise.
    app.state.task_policy = TaskPolicy(settings)
    # Rate limiting + per-tenant usage accounting (in-memory by design).
    app.state.usage = UsageTracker(
        max_concurrent_per_tenant=int(_setting("rate_limit_concurrent_per_tenant", 2)),
        tasks_per_hour=int(_setting("rate_limit_tasks_per_hour", 60)),
        token_budget=int(_setting("tenant_token_budget", 0)),
        cost_per_1k_tokens=float(_setting("token_cost_per_1k_usd", 0.0)),
    )

    # ---- auth dependency (hardening supplement) ------------------------
    # Protects every /task* endpoint plus /config, /reports and the WS
    # channel when API_AUTH_TOKEN is configured. /health deliberately
    # stays open: container/orchestrator liveness probes need it without
    # credentials.
    async def _require_token(request: Request) -> None:
        token = app.state.auth_token
        if token is None:
            return
        # Header only - a token must never travel in a URL/query string
        # (server/proxy access logs, browser history, Referer leakage).
        if request.headers.get("authorization", "") == f"Bearer {token}":
            return
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    protected = [Depends(_require_token)]

    def _runner_kwargs(record: dict[str, Any]) -> dict[str, Any]:
        """Inject emit/stop_check/on_step into the runner call only when
        the runner actually declares them (keeps 2-arg runners fully
        compatible)."""
        params = inspect.signature(task_runner).parameters
        kwargs: dict[str, Any] = {}
        if "emit" in params:
            kwargs["emit"] = record["emit"]
        if "stop_check" in params:
            kwargs["stop_check"] = lambda: record["stop_requested"]
        if "on_step" in params:
            kwargs["on_step"] = record["on_step"]
        if "tenant_id" in params:
            kwargs["tenant_id"] = record.get("tenant_id", DEFAULT_TENANT_ID)
        return kwargs

    def _publish(record: dict[str, Any], event: dict[str, Any]) -> None:
        """Append an event to the task's history and fan it out to every
        live subscriber queue. Called inline from the worker (same event
        loop), so plain put_nowait is safe."""
        record["steps"].append(event)
        path = event.get("screenshot_path")
        if path:
            record["latest_screenshot"] = path
        for queue in list(record["subscribers"]):
            queue.put_nowait(event)
        # Write-through: keep the durable copy current (steps buffer +
        # latest screenshot are exactly what a restarted process should
        # still be able to serve).
        _persist(record)

    def _persist(record: dict[str, Any]) -> None:
        """Mirror one record mutation to SQLite (write-through), fire-and-
        forget. Used on the HOT path (every step event): persistence must
        never slow or break streaming, and losing the very last step-event
        append on a crash costs nothing (the terminal state is saved with
        _persist_now, see below). Safe from sync contexts (_publish /
        emit / on_step) because they only ever run inside the live event
        loop."""
        store = app.state.task_store
        if store is None:
            return
        try:
            asyncio.get_running_loop().create_task(store.save(record))
        except RuntimeError:
            logger.debug("No running loop for task persistence; skipped")

    async def _persist_now(record: dict[str, Any]) -> None:
        """Awaited write-through for STATE TRANSITIONS (submit, running,
        finished, stop-requested): these must be durable even if the
        process dies immediately afterwards - a fire-and-forget task could
        still be sitting in the queue when the loop shuts down."""
        store = app.state.task_store
        if store is None:
            return
        try:
            await store.save(record)
        except Exception:
            logger.exception(f"Failed to persist task {record.get('task_id')}")

    async def _prune_finished_tasks() -> None:
        """Bound app.state.tasks memory AND the SQLite table (fix:
        unbounded task store). Drop finished records older than
        task_ttl_hours, then keep only the newest max_finished_tasks
        finished records - each record carries its full steps buffer, so
        without pruning a long-lived API process grows without limit.
        Called on every submit and by the background pruner; running/
        queued tasks are never touched."""
        finished = sorted(
            (
                (record["submitted_at"], task_id)
                for task_id, record in app.state.tasks.items()
                if record["state"] == "finished"
            ),
        )
        now = datetime.now()
        expired: list[str] = []
        if task_ttl_hours > 0:
            for submitted_at, task_id in finished:
                try:
                    age_seconds = (now - datetime.fromisoformat(submitted_at)).total_seconds()
                except ValueError:
                    age_seconds = float("inf")
                if age_seconds > task_ttl_hours * 3600:
                    expired.append(task_id)

        remaining = sorted(
            (record["submitted_at"], task_id)
            for task_id, record in app.state.tasks.items()
            if record["state"] == "finished" and task_id not in expired
        )
        excess = len(remaining) - max_finished_tasks
        if excess > 0:
            expired.extend(task_id for _, task_id in remaining[:excess])

        for task_id in expired:
            del app.state.tasks[task_id]
            store = app.state.task_store
            if store is not None:
                try:
                    await store.delete(task_id)
                except Exception:
                    logger.exception(f"Failed to delete task {task_id} from store")

    # ---- multi-tenant dispatcher ---------------------------------------
    #
    # Replaces the single global asyncio.Queue worker. Why not one shared
    # queue: with FIFO-over-tasks a single tenant submitting 50 tasks would
    # push every other tenant's task behind hours of work. The dispatcher
    # keeps a FIFO PER TENANT and picks tenants ROUND-ROBIN, so fairness is
    # per-tenant, not per-task. Parallelism (tasks running simultaneously)
    # is capped globally at max_parallel = MAX_CONCURRENT_TENANT_CONTEXTS;
    # within one tenant, execution stays strictly sequential because that
    # tenant's persistent browser context is exclusive.

    def _enqueue(task_id: str, tenant_id: str) -> None:
        queue = app.state.tenant_queues.setdefault(tenant_id, deque())
        if not queue:
            app.state.queue_order.append(tenant_id)
        queue.append(task_id)

    def _pop_next() -> tuple[str, str] | None:
        """Round-robin over tenants with queued work. Returns
        (task_id, tenant_id) or None."""
        order = app.state.queue_order
        while order:
            tenant_id = order[0]
            queue = app.state.tenant_queues.get(tenant_id)
            if not queue:  # stale entry (task vanished) - skip
                order.popleft()
                continue
            task_id = queue.popleft()
            if queue:
                order.rotate(-1)  # this tenant goes to the back of the line
            else:
                order.popleft()
                app.state.tenant_queues.pop(tenant_id, None)
            return task_id, tenant_id
        return None

    async def _execute_task(task_id: str) -> None:
        record = app.state.tasks.get(task_id)

        if record is None or record["stop_requested"]:
            if record is not None:
                # Stop requested while still queued: finish without running.
                record["result"] = TaskResult(
                    success=False,
                    summary="Task stopped by user before it started",
                    steps_taken=0,
                    total_duration_seconds=0.0,
                    error="StoppedByUser",
                ).model_dump()
                record["state"] = "finished"
                await _persist_now(record)
                _publish(
                    record,
                    {"type": "final", "task_id": task_id, "result": record["result"]},
                )
            async with app.state.dispatch_cond:
                app.state.running_count -= 1
                app.state.dispatch_cond.notify_all()
            return

        record["state"] = "running"
        await _persist_now(record)
        _metrics.observe_task_running(record.get("tenant_id", DEFAULT_TENANT_ID))
        runner: TaskRunner = app.state.task_runner
        try:
            result = await runner(
                record["task"], record["starting_url"], **_runner_kwargs(record)
            )
            record["result"] = result.model_dump()
        except Exception as e:
            logger.exception("Task %s crashed", task_id)
            record["result"] = TaskResult(
                success=False,
                summary=f"Task crashed: {e}",
                steps_taken=0,
                total_duration_seconds=0.0,
                error=type(e).__name__,
            ).model_dump()
        finally:
            # Usage accounting FIRST: it must be visible by the time the
            # record reads "finished" (clients poll on exactly that state).
            # None = provider reported no usage block.
            app.state.usage.record_completion(
                record.get("tenant_id", DEFAULT_TENANT_ID),
                (record.get("result") or {}).get("tokens_used"),
            )
            # Terminal metric: success/fail + wall-clock duration (from the
            # runner's own accounting; 0.0 for pre-start stops is honest).
            _metrics.observe_task_finished(
                record.get("tenant_id", DEFAULT_TENANT_ID),
                bool((record.get("result") or {}).get("success")),
                float((record.get("result") or {}).get("total_duration_seconds") or 0.0),
            )
            record["state"] = "finished"
            # Durable BEFORE publishing: a subscriber that sees the
            # final event must be able to rely on the persisted copy.
            await _persist_now(record)
            _publish(
                record,
                {
                    "type": "final",
                    "task_id": task_id,
                    "state": "finished",
                    "result": record["result"],
                },
            )
            async with app.state.dispatch_cond:
                app.state.running_count -= 1
                app.state.dispatch_cond.notify_all()

    async def _dispatcher() -> None:
        while True:
            async with app.state.dispatch_cond:
                await app.state.dispatch_cond.wait_for(
                    lambda: (
                        app.state.running_count < max_parallel
                        and any(app.state.tenant_queues.values())
                    )
                )
                picked = _pop_next()
            if picked is None:
                continue  # stale entries only; loop back to waiting
            task_id, _tenant_id = picked
            async with app.state.dispatch_cond:
                app.state.running_count += 1
            asyncio.create_task(_execute_task(task_id))

    async def _context_sweeper() -> None:
        """Multi-tenancy: periodically close idle tenants' browser contexts
        (no task ran for TENANT_CONTEXT_IDLE_TTL_SECONDS) to free the
        Chromium processes; profiles on disk survive, so sessions do too.
        Only runs when a pool was injected (build_default_app)."""
        while True:
            await asyncio.sleep(sweep_interval_seconds)
            try:
                pool = app.state.context_pool
                if pool is not None:
                    await pool.close_idle()
            except Exception:
                logger.exception("Tenant context sweep failed")

    async def _pruner() -> None:
        """Periodically sweep finished task records so memory stays bounded
        even when the API is idle (no submits -> no submit-time pruning)."""
        while True:
            await asyncio.sleep(prune_interval_seconds)
            try:
                await _prune_finished_tasks()
            except Exception:
                logger.exception("Task pruning sweep failed")

    async def _init_task_store() -> None:
        """Task 1 (persistence): open the SQLite store, hydrate previously
        persisted records into the in-memory working set, and mark tasks
        that were queued/running when the previous process died as
        finished (InterruptedByRestart) - their worker loop is gone, so
        leaving them 'running' forever would be a lie. Degrades to the
        legacy in-memory mode when there is no configured path or the
        optional aiosqlite dependency is missing."""
        db_path = getattr(settings, "task_db_path", None) if settings is not None else None
        if not db_path:
            return
        try:
            from .task_store import TaskStore  # noqa: PLC0415 - lazy optional dep
        except ImportError as e:
            logger.warning(f"aiosqlite unavailable ({e}); task history stays in-memory only")
            return
        store = TaskStore(Path(db_path))
        try:
            await store.initialize()
            records = await store.load_all()
        except Exception:
            logger.exception("Task store initialization failed; continuing in-memory only")
            return
        for record in records:
            if record["state"] in ("queued", "running"):
                record["state"] = "finished"
                record["result"] = TaskResult(
                    success=False,
                    summary="Task interrupted by process restart before completion",
                    steps_taken=record.get("current_step") or 0,
                    total_duration_seconds=0.0,
                    error="InterruptedByRestart",
                ).model_dump()
            app.state.tasks[record["task_id"]] = record
        app.state.task_store = store
        hydrated = len(records)
        if hydrated:
            logger.info(f"Hydrated {hydrated} task record(s) from {db_path}")
        # Bound memory right after hydration: a long-idle DB could hold
        # more finished records than the retention policy allows.
        try:
            await _prune_finished_tasks()
        except Exception:
            logger.exception("Post-hydration prune failed")

    @app.on_event("startup")
    async def _start_worker() -> None:
        app.state.task_runner = task_runner
        if on_startup is not None:
            await on_startup()
        await _init_task_store()
        app.state.dispatcher = asyncio.create_task(_dispatcher())
        app.state.pruner = asyncio.create_task(_pruner())
        if app.state.context_pool is not None:
            app.state.context_sweeper = asyncio.create_task(_context_sweeper())

    @app.on_event("shutdown")
    async def _stop_worker() -> None:
        app.state.draining = True
        for name in ("dispatcher", "pruner", "context_sweeper"):
            bg = getattr(app.state, name, None)
            if bg is not None:
                bg.cancel()
                try:
                    await bg
                except asyncio.CancelledError:
                    pass
        pool = app.state.context_pool
        if pool is not None:
            try:
                await asyncio.shield(pool.close_all())
            except Exception:  # noqa: BLE001 - shield may re-raise on cancel
                logger.exception("Tenant context pool shutdown failed")
        store = app.state.task_store
        if store is not None:
            try:
                await store.close()
            except Exception:
                logger.exception("Task store close failed")
            app.state.task_store = None
        if on_shutdown is not None:
            await on_shutdown()

    @app.get("/health")
    async def health() -> dict:
        if app.state.draining:
            raise HTTPException(status_code=503, detail="draining")
        # Structured component status (observability): ok / degraded / down
        # per component, overall = worst of the checked ones. Checks are
        # cached for HEALTH_CACHE_SECONDS so a 5s-interval scraper does not
        # become a load generator against the LLM provider. Without
        # production wiring (injected-runner tests) providers are absent ->
        # "unknown" components and overall "ok": the API itself is alive,
        # nothing is proven broken.
        now = time.monotonic()
        cached = app.state._health_cache
        if cached is not None and now - cached[0] < HEALTH_CACHE_SECONDS:
            return cached[1]

        providers = app.state.health_providers

        async def _probe(name: str, coro_factory: Callable[[], Awaitable[bool]]) -> str:
            try:
                return "ok" if await coro_factory() else "down"
            except Exception as e:  # noqa: BLE001 - probes never break health
                logger.debug(f"Health probe {name} raised: {e}")
                return "degraded"

        # Fixed component set: a monitoring dashboard can rely on these
        # keys always being present. Without production wiring the probes
        # honestly read "unknown" instead of faking "ok".
        components: dict[str, str] = {
            "api": "ok",
            "llm": "unknown",
            "browser": "unknown",
            "store": "unknown",
        }
        store = app.state.task_store
        if store is not None:
            components["store"] = "ok"
        pool = app.state.context_pool
        if pool is not None:
            # Browser engine: contexts may legitimately be all closed
            # (idle TTL) - that's healthy, not down.
            stats = pool.stats
            components["browser"] = (
                "ok"
                if stats["busy"] == 0 or stats["open"] > 0
                else "degraded"
            )

        for name in providers:
            components.setdefault(name, "unknown")
        for name, provider in providers.items():
            components[name] = await _probe(name, provider)

        statuses = set(components.values())
        if "down" in statuses:
            overall = "down"
        elif "degraded" in statuses:
            overall = "degraded"
        else:
            overall = "ok"

        body = {"status": overall, "components": components}
        app.state._health_cache = (now, body)
        if overall == "down":
            raise HTTPException(status_code=503, detail=body)
        return body

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        """Prometheus exposition format (observability). Deliberately OPEN
        like /health: scrapers run inside the trust boundary; tenant_id
        labels are client-chosen identifiers anyway. If the optional
        prometheus_client package is missing - explicit 503 instead of an
        empty page pretending everything is measured."""
        payload = _metrics.render()
        if payload is None:
            raise HTTPException(
                status_code=503,
                detail="prometheus_client is not installed (pip install prometheus-client)",
            )
        return Response(content=payload, media_type=_metrics.CONTENT_TYPE_LATEST)

    @app.post("/task", status_code=202, dependencies=protected)
    async def submit_task(submission: TaskSubmission) -> dict:
        if app.state.draining:
            raise HTTPException(status_code=503, detail="draining: no new tasks accepted")
        # Intake policy (sanitization): reject empty/garbage/oversized (and,
        # when the opt-in filter is on, blocklisted) task text BEFORE it can
        # occupy queue slots or burn LLM tokens. 400 with a machine-readable
        # rule name; every rejection is audited to the dedicated JSONL file.
        rejection = app.state.task_policy.validate(submission.task)
        if rejection is not None:
            raise HTTPException(
                status_code=400,
                detail={"error": "task_rejected", "rule": rejection},
            )

        # Backpressure (fix: unbounded queue growth): the single worker
        # drains one task at a time; past MAX_PENDING_TASKS queued-or-
        # running tasks, reject instead of buffering indefinitely.
        pending = sum(1 for r in app.state.tasks.values() if r["state"] in ("queued", "running"))
        if pending >= MAX_PENDING_TASKS:
            raise HTTPException(
                status_code=429,
                detail=f"too many pending tasks ({pending}); retry later",
            )

        # Per-tenant rate limiting (multi-tenancy): concurrent cap + sliding
        # window + optional hard token budget. 429 with a machine-readable
        # reason and Retry-After - never a silent drop.
        tenant = submission.tenant_id
        usage = app.state.usage
        running_for_tenant = sum(
            1
            for r in app.state.tasks.values()
            if r["state"] == "running"
            and r.get("tenant_id", DEFAULT_TENANT_ID) == tenant
        )
        allowed, reason, retry_after = usage.check_submission(tenant, running_for_tenant)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail={"error": "rate_limited", "reason": reason, "retry_after_seconds": retry_after},
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )

        task_id = uuid.uuid4().hex
        record: dict[str, Any] = {
            "task_id": task_id,
            "state": "queued",
            "submitted_at": datetime.now().isoformat(),
            "task": submission.task,
            "starting_url": submission.starting_url,
            "tenant_id": submission.tenant_id,
            "result": None,
            "steps": [],
            "subscribers": [],
            "stop_requested": False,
            "latest_screenshot": None,
            # Hardening supplement (on_step): live status served by
            # GET /task/{id} while the run is in flight.
            "current_step": None,
            "last_tool": None,
        }
        record["emit"] = lambda event: _publish(record, dict(event, task_id=task_id))

        def _on_step(step: int, action: AgentAction, result: ActionResult) -> None:
            # Fast, non-blocking: plain in-memory record writes only.
            record["current_step"] = step
            record["last_tool"] = action.tool
            record["last_success"] = result.success

        record["on_step"] = _on_step
        app.state.tasks[task_id] = record
        # Accepted: only NOW the submission consumes quota (rejected
        # requests must not).
        app.state.usage.record_submission(submission.tenant_id)
        _metrics.observe_task_queued(submission.tenant_id)
        await _persist_now(record)
        await _prune_finished_tasks()
        _enqueue(task_id, submission.tenant_id)
        async with app.state.dispatch_cond:
            app.state.dispatch_cond.notify_all()
        return {"task_id": task_id, "tenant_id": submission.tenant_id}

    def _resolve_tenant_record(task_id: str, tenant_id: str) -> dict[str, Any]:
        """Multi-tenancy access helper WITHOUT auth (documented scope): the
        requester names its tenant via query param (default 'default') and
        only sees records from that bucket. Mismatch = 404, not 403 - do
        not leak other tenants' task ids' existence."""
        record = app.state.tasks.get(task_id)
        if record is None or record.get("tenant_id", DEFAULT_TENANT_ID) != tenant_id:
            raise HTTPException(status_code=404, detail="unknown task_id")
        return record

    @app.get("/task/{task_id}", dependencies=protected)
    async def get_task(task_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> TaskStatus:
        record = _resolve_tenant_record(task_id, tenant_id)
        return TaskStatus(
            **{
                k: record[k]
                for k in ("task_id", "state", "submitted_at", "result")
            },
            current_step=record.get("current_step"),
            last_tool=record.get("last_tool"),
            tenant_id=record.get("tenant_id", DEFAULT_TENANT_ID),
        )

    @app.get("/tasks", dependencies=protected)
    async def list_tasks(tenant_id: str = DEFAULT_TENANT_ID) -> dict:
        """Task 1 (web UI): task history - newest first. Multi-tenancy:
        filtered to the requesting tenant's bucket by default; pass
        tenant_id=all for the unfiltered operator view."""
        items = []
        for record in app.state.tasks.values():
            record_tenant = record.get("tenant_id", DEFAULT_TENANT_ID)
            if tenant_id != "all" and record_tenant != tenant_id:
                continue
            result = record.get("result") or {}
            items.append(
                {
                    "task_id": record["task_id"],
                    "state": record["state"],
                    "tenant_id": record_tenant,
                    "submitted_at": record["submitted_at"],
                    "task": record["task"],
                    "starting_url": record["starting_url"],
                    "success": result.get("success"),
                    "summary": result.get("summary"),
                    "steps_taken": result.get("steps_taken"),
                    "error": result.get("error"),
                }
            )
        items.sort(key=lambda item: item["submitted_at"], reverse=True)
        return {"tasks": items}

    @app.get("/task/{task_id}/steps", dependencies=protected)
    async def get_task_steps(
        task_id: str, tenant_id: str = DEFAULT_TENANT_ID
    ) -> dict:
        """Task 1 (web UI): full step-event history for a task (the polling
        fallback for clients that cannot hold a WebSocket)."""
        record = _resolve_tenant_record(task_id, tenant_id)
        return {
            "task_id": task_id,
            "state": record["state"],
            "steps": record["steps"],
        }

    @app.get("/task/{task_id}/screenshot", dependencies=protected)
    async def get_task_screenshot(
        task_id: str, tenant_id: str = DEFAULT_TENANT_ID
    ):
        """Task 1 (web UI): the most recent screenshot taken during the run
        (from take_screenshot steps). Served as a file response.

        Hardening supplement (path traversal): the task_id is only a dict
        key (uuid hex), but the stored path itself is still resolved and
        MUST stay inside settings.screenshot_dir - a tampered/absolute/
        escaping path is rejected instead of being served."""
        record = _resolve_tenant_record(task_id, tenant_id)
        path = record.get("latest_screenshot")
        if not path:
            raise HTTPException(status_code=404, detail="no screenshot available for this task")
        file_path = Path(path).resolve()
        base_dir = Path(getattr(_settings(app), "screenshot_dir", Path("./screenshots"))).resolve()
        if base_dir not in file_path.parents:
            raise HTTPException(status_code=400, detail="screenshot path outside allowed directory")
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="screenshot file no longer exists")
        return FileResponse(str(file_path), media_type="image/png")

    @app.post("/task/{task_id}/stop", dependencies=protected)
    async def stop_task(task_id: str, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
        """Task 1 (web UI): per-task graceful stop. Sets the same kind of
        flag the global SIGTERM handler sets, but scoped to one task's
        orchestrator (shutdown_check) - the loop exits at the next step
        boundary with whatever progress is in context_data."""
        record = _resolve_tenant_record(task_id, tenant_id)
        if record["state"] == "finished":
            raise HTTPException(status_code=409, detail="task already finished")
        record["stop_requested"] = True
        await _persist_now(record)
        return {"task_id": task_id, "state": record["state"], "stop_requested": True}

    @app.post("/ws/ticket", dependencies=protected)
    async def issue_ws_ticket() -> dict:
        """Hardening (WS auth): exchange the Bearer token for a one-time,
        short-lived ticket usable as ?ticket= on the WebSocket handshake.
        The long-lived API_AUTH_TOKEN never travels in a URL; a leaked
        ticket is worthless after 60s / first use."""
        if app.state.auth_token is None:
            return {"required": False, "ticket": None, "expires_in": 0}
        now = time.monotonic()
        # prune expired tickets so the store cannot grow unbounded
        for stale in [t for t, exp in app.state.ws_tickets.items() if exp < now]:
            del app.state.ws_tickets[stale]
        ticket = uuid.uuid4().hex + uuid.uuid4().hex
        app.state.ws_tickets[ticket] = now + WS_TICKET_TTL_SECONDS
        return {
            "required": True,
            "ticket": ticket,
            "expires_in": WS_TICKET_TTL_SECONDS,
        }

    @app.websocket("/ws/task/{task_id}")
    async def ws_task(websocket: WebSocket, task_id: str) -> None:
        """Task 1 (web UI): live event stream for a task - replays the
        already-recorded events, then streams new ones until the final
        result. The static UI falls back to polling GET /task/{id}/steps
        when a WebSocket cannot be established."""
        # Hardening (WS auth): a SINGLE-USE, short-lived ticket from
        # POST /ws/ticket (exchanged for the Bearer token over HTTP where
        # headers work). The static API_AUTH_TOKEN itself never appears in
        # the URL. pop() = one-time use: a replayed ticket is rejected.
        if app.state.auth_token is not None:
            ticket = websocket.query_params.get("ticket")
            expiry = app.state.ws_tickets.pop(ticket, None) if ticket else None
            if expiry is None or expiry < time.monotonic():
                await websocket.close(code=4401)
                return

        record = app.state.tasks.get(task_id)
        if record is None or record.get("tenant_id", DEFAULT_TENANT_ID) != (
            websocket.query_params.get("tenant", DEFAULT_TENANT_ID)
        ):
            # Same bucket rule as the HTTP endpoints: another tenant's task
            # is indistinguishable from a nonexistent one.
            await websocket.close(code=4404)
            return

        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue()
        record["subscribers"].append(queue)
        try:
            for event in record["steps"]:
                await websocket.send_text(json.dumps(event, default=str))
            if record["state"] != "finished":
                while True:
                    event = await queue.get()
                    await websocket.send_text(json.dumps(event, default=str))
                    if event.get("type") == "final":
                        break
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug(f"WebSocket /ws/task/{task_id} error: {e}")
        finally:
            try:
                record["subscribers"].remove(queue)
            except ValueError:
                pass

    @app.get("/config", dependencies=protected)
    async def get_config() -> dict:
        """Task 1 (web UI): read-only view of the active configuration.
        Any field whose name looks like a secret (api key/token/...) is
        masked before leaving the process."""
        settings = _settings(app)
        if settings is None:
            raise HTTPException(status_code=503, detail="settings unavailable")
        dump = settings.model_dump()
        return {"settings": mask_settings(dump)}

    @app.get("/reports", dependencies=protected)
    async def list_reports() -> dict:
        """Task 1 (web UI): available per-run reports (reports/run_*.json)."""
        reports_dir = _reports_dir(app)
        items = []
        if reports_dir.is_dir():
            for path in sorted(reports_dir.glob("*.json"), reverse=True):
                items.append({"run_id": path.stem, "file": path.name, "size": path.stat().st_size})
        return {"reports": items}

    @app.get("/reports/{run_id}", dependencies=protected)
    async def get_report(run_id: str) -> dict:
        """Task 1 (web UI): one report's contents. run_id is strictly
        validated and resolved inside REPORTS_DIR (path traversal guard)."""
        if not _SAFE_RUN_ID.match(run_id):
            raise HTTPException(status_code=400, detail="invalid run_id")
        reports_dir = _reports_dir(app)
        path = (reports_dir / f"{run_id}.json").resolve()
        if reports_dir.resolve() not in path.parents:
            raise HTTPException(status_code=400, detail="invalid run_id")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="unknown run_id")
        try:
            return json.loads(path.read_text())
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"corrupt report: {e}") from e

    @app.get("/usage/{tenant_id}", dependencies=protected)
    async def get_usage(tenant_id: str) -> dict:
        """Rate-limiting/usage accounting: current per-tenant consumption -
        tasks in the sliding window, lifetime totals, LLM tokens and the
        estimated cost, plus the effective limits. Same no-auth caveat as
        tenant_id itself: this reports whatever bucket is asked for."""
        if not _SAFE_TENANT_ID.match(tenant_id):
            raise HTTPException(status_code=400, detail="invalid tenant_id")
        return app.state.usage.snapshot(tenant_id)

    # Static web UI. Mounted LAST so the explicit API routes above always
    # win; html=True serves index.html for "/".
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="ui")

    return app


def _settings(app: FastAPI) -> Any:
    """Injected settings when present; else lazily loaded once (and
    cached). Returns None if settings cannot be loaded (e.g. tests with no
    .env) - callers degrade to safe defaults."""
    if app.state.settings is not None or app.state._settings_loaded:
        return app.state.settings
    from ..config import load_settings  # noqa: PLC0415 - lazy: tests inject fakes

    try:
        app.state.settings = load_settings()
    except Exception as e:
        logger.debug(f"lazy load_settings failed: {e}")
        app.state.settings = None
    app.state._settings_loaded = True
    # auth token may only be knowable after lazy load (rare path: create_app
    # called without settings in a deployment that sets API_AUTH_TOKEN)
    if app.state.settings is not None and app.state.auth_token is None:
        app.state.auth_token = getattr(app.state.settings, "api_auth_token", None)
    return app.state.settings


def _reports_dir(app: FastAPI) -> Path:
    """REPORTS_DIR from settings when available, else ./reports."""
    settings = getattr(app.state, "settings", None)
    if settings is not None:
        return Path(getattr(settings, "reports_dir", Path("./reports")))
    return Path("./reports")


def _detect_public_bind(settings: Any) -> bool:
    """True when the API will listen on a non-loopback 'all interfaces'
    address - either via Settings (API_BIND_HOST) or via an explicit
    `--host` on the uvicorn command line (as the Dockerfile CMD does,
    which bypasses Settings entirely)."""
    # NOTE: the "0.0.0.0"/"::" literals below are DETECTION values, not a
    # bind call - this guard is what REFUSES to start on them (see
    # _enforce_public_bind_auth_policy). Hence the targeted nosec B104.
    host = str(getattr(settings, "api_bind_host", "127.0.0.1") or "").strip()
    if host in ("0.0.0.0", "::", ""):  # nosec B104
        return True
    argv = sys.argv
    for i, arg in enumerate(argv[:-1]):
        if arg == "--host" and str(argv[i + 1]).strip() in ("0.0.0.0", "::", ""):  # nosec B104
            return True
    return False


def _enforce_public_bind_auth_policy(settings: Any) -> None:
    """Fix (0.0.0.0 bind without mandatory auth): the API drives a real
    browser holding persistent cookies, and API_AUTH_TOKEN defaults to
    None (auth off). Anyone running the container with `-p 8000:8000` and
    no token would publish a fully open endpoint controlling that browser.
    Refuse to start in that state unless the operator explicitly sets
    ALLOW_UNAUTHENTICATED_PUBLIC_BIND=true (e.g. an isolated trusted
    network / an auth-ing reverse proxy in front)."""
    if not _detect_public_bind(settings):
        return
    if getattr(settings, "api_auth_token", None):
        return
    if getattr(settings, "allow_unauthenticated_public_bind", False):
        logger.critical(
            "API binds to all interfaces WITHOUT authentication "
            "(ALLOW_UNAUTHENTICATED_PUBLIC_BIND=true). Anyone who can reach "
            "this port controls the agent's browser, including its stored "
            "cookies. Use only on a trusted/isolated network."
        )
        return
    raise ConfigurationError(
        "Refusing to start: the API would bind to all interfaces "
        "(0.0.0.0/::) without API_AUTH_TOKEN. This publishes an open "
        "endpoint that drives a real browser with persistent cookies. "
        "Either set API_AUTH_TOKEN (>= 16 chars), or bind to a loopback "
        "address, or explicitly acknowledge the risk with "
        "ALLOW_UNAUTHENTICATED_PUBLIC_BIND=true."
    )


def build_default_app() -> FastAPI:
    """Production wiring: real Settings/BrowserService/LLMService with a
    graceful-shutdown flag the API sets on SIGTERM. The orchestrator also
    receives the per-task emit/stop channels so the UI endpoints and
    WebSocket have live data.

    Browser lifecycle history:
    - Originally: full Chromium launch + teardown per task (`async with
      browser, llm:` inside _run_task) - seconds of latency per queued task,
      persistent-profile lock errors under queue pressure.
    - Fix (browser relaunch per task): ONE shared BrowserService for the
      app lifetime, each task on its own Page.
    - Multi-tenancy (current): a TenantContextPool - one isolated persistent
      context (own user_data_dir) per tenant, lazily started, closed after
      the idle TTL by the app-level sweeper. With the default tenant_id
      this degrades to exactly one long-lived context = the previous
      behavior, just under pool management.
    """
    import signal

    from ..agent import AgentOrchestrator
    from ..config import load_settings
    from ..infrastructure import LLMService, TenantContextPool

    settings = load_settings()
    _enforce_public_bind_auth_policy(settings)

    pool = TenantContextPool(settings)
    llm = LLMService(settings)
    shutdown_requested = {"flag": False}

    def _request_shutdown(signum, frame):
        shutdown_requested["flag"] = True

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    async def _on_startup() -> None:  # contexts launch lazily per tenant
        return None

    async def _on_shutdown() -> None:
        # asyncio.shield for the same reason as BrowserService.__aexit__:
        # cleanup must complete even when shutdown races cancellation.
        await asyncio.shield(pool.close_all())
        await llm.close()

    async def _run_task(
        task: str,
        starting_url: str | None,
        emit: Callable[[dict], None] | None = None,
        stop_check: Callable[[], bool] | None = None,
        on_step: Callable[[int, Any, Any], None] | None = None,
        tenant_id: str = "default",
    ) -> TaskResult:
        # Exclusive checkout of the tenant's persistent context; the
        # orchestrator gets a lightweight per-page view (own element_map),
        # so tasks cannot leak selectors into each other either. The
        # context STAYS OPEN after release - warm cookies for the next
        # task of the same tenant; the idle sweeper closes it later.
        service = await pool.acquire(tenant_id)
        try:
            page_view = await service.new_page()
            try:
                orchestrator = AgentOrchestrator(
                    settings,
                    page_view,
                    llm,
                    shutdown_check=lambda: shutdown_requested["flag"]
                    or bool(stop_check and stop_check()),
                    event_sink=emit,
                    on_step=on_step,
                )
                return await orchestrator.run(task, starting_url=starting_url)
            finally:
                # Close ONLY this task's page - the tenant context stays up.
                try:
                    await page_view.page.close()
                except Exception as e:
                    logger.debug(f"Per-task page close failed (non-fatal): {e}")
        finally:
            pool.release(tenant_id)

    return create_app(
        _run_task,
        settings=settings,
        on_startup=_on_startup,
        on_shutdown=_on_shutdown,
        context_pool=pool,
        # /health probes: LLM provider liveness (lightweight GET /models,
        # cached by the app). Browser engine status comes from the pool
        # itself; store status from app.state.task_store.
        health_providers={"llm": llm.health_check},
    )
