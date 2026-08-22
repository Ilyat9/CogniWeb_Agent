"""
3.3 (optional [api] extra): a small FastAPI service wrapper around the
orchestrator.

NOT part of base requirements.txt - install via requirements-api.txt
(fastapi + uvicorn) or requirements-ui.txt (fastapi + uvicorn + websockets,
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

One asyncio.Queue + one worker task: tasks run strictly one at a time,
matching the single-browser, rate-limited reality of the agent.
SIGTERM = drain: new submissions are refused with 503, the currently
running task is allowed to finish (the in-process graceful shutdown
flag additionally tells the orchestrator loop to stop at the next step
boundary, so "finish" means: stop cleanly at the earliest safe point).
"""

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.staticfiles import StaticFiles

from ..core.models import ActionResult, AgentAction, TaskResult

logger = logging.getLogger(__name__)

TaskRunner = Callable[..., Awaitable[TaskResult]]
# A task runner may optionally accept extra keyword arguments injected by
# the worker (both are always passed by keyword, so 2-positional-arg
# runners - like every existing test fixture - keep working):
#   emit:       sync callable(dict) - live step-event publisher
#   stop_check: zero-arg callable -> bool - per-task graceful stop flag
#   on_step:    sync callable(step, action, result) - hardening supplement
#               live-status hook; the worker updates the task record's
#               current_step/last_tool from it (in-memory writes only)
_RUNNER_OPTIONAL_KWARGS = ("emit", "stop_check", "on_step")


class TaskSubmission(BaseModel):
    task: str = Field(min_length=1)
    starting_url: str | None = None


class TaskStatus(BaseModel):
    task_id: str
    state: str  # queued | running | finished
    submitted_at: str
    result: dict[str, Any] | None = None
    # Hardening supplement (on_step hook): live progress DURING a run -
    # non-None after the loop's first executed step, None while queued.
    current_step: int | None = None
    last_tool: str | None = None


# /config masks any field whose name looks secret-ish. Deliberately
# over-broad (key/token/secret/password/credential): false positives cost a
# masked value, false negatives leak a credential to the UI.
_SECRET_NAME_HINTS = ("key", "token", "secret", "password", "credential")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Hardening (WS auth): browsers cannot set Authorization headers on a
# WebSocket, so the browser UI exchanges its Bearer token for a
# SHORT-LIVED, SINGLE-USE ticket via POST /ws/ticket and passes only that
# ticket in the WS query string. A long-lived static token must never
# appear in a URL: URLs leak into server/proxy access logs, browser
# history and Referer headers.
WS_TICKET_TTL_SECONDS = 60


def _mask_value(value: Any) -> Any:
    if isinstance(value, str) and value:
        return f"***masked ({len(value)} chars)***"
    return "***masked***"


def mask_settings(settings_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a UI-safe copy of a settings dump: every secret-looking field
    masked, everything else (bools, ints, paths, lists) passed through."""
    masked = {}
    for name, value in settings_dict.items():
        if any(hint in name.lower() for hint in _SECRET_NAME_HINTS):
            masked[name] = _mask_value(value)
        elif isinstance(value, Path):
            masked[name] = str(value)
        else:
            masked[name] = value
    return masked


def create_app(
    task_runner: TaskRunner,
    settings: Any | None = None,
) -> FastAPI:
    """Build the API app with an injected task runner (async callable
    (task, starting_url) -> TaskResult; may additionally accept keyword
    args `emit`, `stop_check` and `on_step`, see _RUNNER_OPTIONAL_KWARGS).
    Injection keeps this module testable without launching a browser or an
    LLM client. `settings` is optional: when given, GET /config reflects it
    and API_AUTH_TOKEN (if set) enables bearer auth; otherwise settings
    are lazily loaded on first request."""

    app = FastAPI(title="CogniWeb Agent API", version="1.1")
    # Internal records: plain dicts (not TaskStatus) so we can carry the
    # raw task text / starting_url alongside the visible status fields,
    # plus the UI-facing step-event buffer and pub/sub state.
    app.state.tasks: dict[str, dict[str, Any]] = {}
    app.state.queue: asyncio.Queue = asyncio.Queue()
    app.state.draining = False
    app.state.settings = settings
    app.state._settings_loaded = settings is not None
    # Hardening supplement (access control): optional bearer token. None
    # (default) keeps every endpoint open - backwards compatible with
    # already-deployed installations. Read from settings when they carry
    # the field (build_default_app path), else None until lazy load.
    app.state.auth_token = getattr(settings, "api_auth_token", None)
    # one-time WS tickets: ticket -> expiry (time.monotonic)
    app.state.ws_tickets: dict[str, float] = {}

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

    async def _worker() -> None:
        while True:
            task_id = await app.state.queue.get()
            record = app.state.tasks.get(task_id)
            if record is None:
                app.state.queue.task_done()
                continue

            # Stop requested while still queued: finish without running.
            if record["stop_requested"]:
                record["result"] = TaskResult(
                    success=False,
                    summary="Task stopped by user before it started",
                    steps_taken=0,
                    total_duration_seconds=0.0,
                    error="StoppedByUser",
                ).model_dump()
                record["state"] = "finished"
                _publish(record, {"type": "final", "task_id": task_id, "result": record["result"]})
                app.state.queue.task_done()
                continue

            record["state"] = "running"
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
                record["state"] = "finished"
                _publish(
                    record,
                    {
                        "type": "final",
                        "task_id": task_id,
                        "state": "finished",
                        "result": record["result"],
                    },
                )
                app.state.queue.task_done()

    @app.on_event("startup")
    async def _start_worker() -> None:
        app.state.task_runner = task_runner
        app.state.worker = asyncio.create_task(_worker())

    @app.on_event("shutdown")
    async def _stop_worker() -> None:
        app.state.draining = True
        worker = getattr(app.state, "worker", None)
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    @app.get("/health")
    async def health() -> dict:
        if app.state.draining:
            raise HTTPException(status_code=503, detail="draining")
        return {"status": "ok"}

    @app.post("/task", status_code=202, dependencies=protected)
    async def submit_task(submission: TaskSubmission) -> dict:
        if app.state.draining:
            raise HTTPException(status_code=503, detail="draining: no new tasks accepted")
        task_id = uuid.uuid4().hex
        record: dict[str, Any] = {
            "task_id": task_id,
            "state": "queued",
            "submitted_at": datetime.now().isoformat(),
            "task": submission.task,
            "starting_url": submission.starting_url,
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
        await app.state.queue.put(task_id)
        return {"task_id": task_id}

    @app.get("/task/{task_id}", dependencies=protected)
    async def get_task(task_id: str) -> TaskStatus:
        record = app.state.tasks.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown task_id")
        return TaskStatus(
            **{k: record[k] for k in ("task_id", "state", "submitted_at", "result")},
            current_step=record.get("current_step"),
            last_tool=record.get("last_tool"),
        )

    @app.get("/tasks", dependencies=protected)
    async def list_tasks() -> dict:
        """Task 1 (web UI): task history - newest first."""
        items = []
        for record in app.state.tasks.values():
            result = record.get("result") or {}
            items.append(
                {
                    "task_id": record["task_id"],
                    "state": record["state"],
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
    async def get_task_steps(task_id: str) -> dict:
        """Task 1 (web UI): full step-event history for a task (the polling
        fallback for clients that cannot hold a WebSocket)."""
        record = app.state.tasks.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown task_id")
        return {
            "task_id": task_id,
            "state": record["state"],
            "steps": record["steps"],
        }

    @app.get("/task/{task_id}/screenshot", dependencies=protected)
    async def get_task_screenshot(task_id: str):
        """Task 1 (web UI): the most recent screenshot taken during the run
        (from take_screenshot steps). Served as a file response.

        Hardening supplement (path traversal): the task_id is only a dict
        key (uuid hex), but the stored path itself is still resolved and
        MUST stay inside settings.screenshot_dir - a tampered/absolute/
        escaping path is rejected instead of being served."""
        record = app.state.tasks.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown task_id")
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
    async def stop_task(task_id: str) -> dict:
        """Task 1 (web UI): per-task graceful stop. Sets the same kind of
        flag the global SIGTERM handler sets, but scoped to one task's
        orchestrator (shutdown_check) - the loop exits at the next step
        boundary with whatever progress is in context_data."""
        record = app.state.tasks.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown task_id")
        if record["state"] == "finished":
            raise HTTPException(status_code=409, detail="task already finished")
        record["stop_requested"] = True
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
        if record is None:
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


def build_default_app() -> FastAPI:
    """Production wiring: real Settings/BrowserService/LLMService with a
    graceful-shutdown flag the API sets on SIGTERM. The orchestrator also
    receives the per-task emit/stop channels so the UI endpoints and
    WebSocket have live data."""
    import signal

    from ..agent import AgentOrchestrator
    from ..config import load_settings
    from ..infrastructure import BrowserService, LLMService

    settings = load_settings()
    browser = BrowserService(settings)
    llm = LLMService(settings)
    shutdown_requested = {"flag": False}

    def _request_shutdown(signum, frame):
        shutdown_requested["flag"] = True

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    async def _run_task(
        task: str,
        starting_url: str | None,
        emit: Callable[[dict], None] | None = None,
        stop_check: Callable[[], bool] | None = None,
        on_step: Callable[[int, Any, Any], None] | None = None,
    ) -> TaskResult:
        async with browser, llm:
            orchestrator = AgentOrchestrator(
                settings,
                browser,
                llm,
                shutdown_check=lambda: shutdown_requested["flag"]
                or bool(stop_check and stop_check()),
                event_sink=emit,
                on_step=on_step,
            )
            return await orchestrator.run(task, starting_url=starting_url)

    return create_app(_run_task, settings=settings)
