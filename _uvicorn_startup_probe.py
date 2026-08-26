"""One-off probe: does a REAL uvicorn server ever serve a request before
the app's startup phase (SQLite hydration) has completed?

Method: start uvicorn programmatically with the production-shaped app from
create_app(), poll the TCP port every ~1ms, and fire the FIRST request the
instant the port accepts a connection. If uvicorn only opens the port after
startup handlers complete, the first request must already observe hydrated
state - regardless of legacy on_event vs lifespan lifecycle style.

Run twice for comparison:
    python _uvicorn_startup_probe.py            # current code (lifespan)
    git checkout HEAD~1 -- src/api/app.py && python _uvicorn_startup_probe.py
"""
import asyncio
import logging
import socket
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).parent))

from src.api.app import create_app  # noqa: E402
from src.api.task_store import TaskStore  # noqa: E402
from src.config.settings import Settings  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8931

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(relativeCreated)8dms %(name)s: %(message)s",
)

tmp = Path(tempfile.mkdtemp())
settings = Settings(
    api_key="sk-super-secret-key-value",
    api_base_url="https://api.test.com/v1",
    model_name="test-provider/test-model",
    user_data_dir=tmp / "browser_data",
    screenshot_dir=tmp / "screenshots",
    checkpoint_dir=tmp / "checkpoints",
    reports_dir=tmp / "reports",
    upload_allowed_dir=tmp / "uploads",
    task_db_path=tmp / "tasks.db",
)

record = {
    "task_id": "zombie",
    "state": "running",
    "submitted_at": (datetime.now() - timedelta(minutes=1)).isoformat(),
    "task": "task zombie",
    "starting_url": "https://example.com",
    "result": None,
    "steps": [{"type": "step", "step": 1, "tool": "navigate"}],
    "stop_requested": False,
    "latest_screenshot": None,
    "current_step": 4,
    "last_tool": None,
}


async def seed() -> None:
    store = TaskStore(settings.task_db_path)
    await store.initialize()
    await store.save(record)
    await store.close()


asyncio.run(seed())


async def noop_runner(task, starting_url):  # pragma: no cover - probe
    raise AssertionError("interrupted task must not be re-run")


app = create_app(noop_runner, settings=settings)


async def wait_port_open(deadline_s: float = 30.0) -> float | None:
    """Poll-connect as fast as possible; return monotonic time of FIRST
    successful connect (the moment traffic could physically reach the app)."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass
            return time.monotonic()
        except OSError:
            await asyncio.sleep(0.001)
    return None


def raw_get(path: str) -> tuple[int, bytes]:
    """Minimal HTTP/1.1 GET over a raw socket (no client deps)."""
    req = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\nConnection: close\r\n\r\n"
    with socket.create_connection(("127.0.0.1", PORT), timeout=10) as s:
        s.sendall(req.encode())
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, body


async def main() -> None:
    t0 = time.monotonic()
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    opened_at = await wait_port_open()
    if opened_at is None:
        print("PROBE FAILED: port never opened")
        server.should_exit = True
        await serve_task
        return

    # First request the very instant the port accepts connections.
    loop = asyncio.get_running_loop()
    status, body = await loop.run_in_executor(None, raw_get, "/task/zombie")
    print(f"RESULT port-open at +{(opened_at - t0) * 1000:.0f}ms; "
          f"first request -> {status} {body[:160].decode(errors='replace')}")

    server.should_exit = True
    await serve_task

    # Post-run evidence straight from the source of truth.
    hydrated_in_memory = "zombie" in app.state.tasks
    print(f"RESULT app.state.tasks after run: "
          f"{{k: v['state'] for k, v in app.state.tasks.items()}} = "
          f"{ {k: v['state'] for k, v in app.state.tasks.items()} }")
    print(f"VERDICT {'PASS' if status == 200 and hydrated_in_memory else 'FAIL'}")


asyncio.run(main())
