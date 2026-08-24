"""
Task 1 (web UI backend) tests: the new API endpoints, the WebSocket live
channel, config masking, report serving, and the static UI mount.

All tests use an injected fake task_runner (no browser/LLM), following the
existing TestApiService conventions in test_phase_features.py.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.app import create_app, mask_settings  # noqa: E402
from src.config.settings import Settings  # noqa: E402
from src.core.models import TaskResult  # noqa: E402


def make_settings(tmp_path, **overrides):
    base = {
        "api_key": "sk-super-secret-key-value",
        "api_base_url": "https://api.test.com/v1",
        "model_name": "test-provider/test-model",
        "user_data_dir": tmp_path / "browser_data",
        "screenshot_dir": tmp_path / "screenshots",
        "checkpoint_dir": tmp_path / "checkpoints",
        "reports_dir": tmp_path / "reports",
        "upload_allowed_dir": tmp_path / "uploads",
        # Task 1 (persistence): keep each test's SQLite store inside its
        # own tmp_path - otherwise every test would share ./data/tasks.db
        # and leak task history into each other's assertions.
        "task_db_path": tmp_path / "tasks.db",
    }
    base.update(overrides)
    return Settings(**base)


def make_client(runner, settings=None):
    app = create_app(runner, settings=settings)
    client = fastapi_testclient.TestClient(app)
    client.__enter__()
    client.app = app
    return client


def wait_for_state(client, task_id, state, timeout=5.0):
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        status = client.get(f"/task/{task_id}").json()
        if status["state"] == state:
            return status
        time.sleep(0.05)
    return status


# ============================================================================
# mask_settings
# ============================================================================


class TestMaskSettings:
    def test_secrets_masked_non_secrets_kept(self, tmp_path):
        s = make_settings(tmp_path, proxy_url=None)
        masked = mask_settings(s.model_dump())
        assert masked["api_key"].startswith("***masked")
        assert "super-secret" not in json.dumps(masked)
        assert masked["model_name"] == "test-provider/test-model"
        assert masked["enable_stealth_mode"] is True
        # Path fields become plain strings (JSON-serializable)
        assert isinstance(masked["screenshot_dir"], str)

    def test_generic_secret_names_masked(self):
        masked = mask_settings(
            {"api_key": "k", "model_name": "m", "llm_token": "abc", "safe_list": [1, 2]}
        )
        assert masked["llm_token"].startswith("***masked")
        assert masked["safe_list"] == [1, 2]


# ============================================================================
# Step events / tasks list / screenshot
# ============================================================================


class TestTaskEndpoints:
    def test_steps_and_tasks_listed(self, tmp_path):
        async def runner(task, starting_url, emit=None):
            emit(
                {
                    "type": "step",
                    "step": 1,
                    "tool": "navigate",
                    "success": True,
                    "message": "Navigated",
                    "thought": "go",
                    "args": {"url": "https://x"},
                }
            )
            return TaskResult(success=True, summary="ok", steps_taken=1, total_duration_seconds=0.1)

        client = make_client(runner, make_settings(tmp_path))
        try:
            task_id = client.post("/task", json={"task": "demo"}).json()["task_id"]
            wait_for_state(client, task_id, "finished")

            steps = client.get(f"/task/{task_id}/steps").json()
            assert steps["state"] == "finished"
            kinds = [e["type"] for e in steps["steps"]]
            assert "step" in kinds and "final" in kinds
            step_event = next(e for e in steps["steps"] if e["type"] == "step")
            assert step_event["tool"] == "navigate"
            assert step_event["task_id"] == task_id  # emit() enriches events

            tasks = client.get("/tasks").json()["tasks"]
            assert len(tasks) == 1
            assert tasks[0]["task_id"] == task_id
            assert tasks[0]["success"] is True
            assert tasks[0]["summary"] == "ok"
        finally:
            client.__exit__(None, None, None)

    def test_unknown_task_404s(self, tmp_path):
        async def runner(task, starting_url):
            return TaskResult(success=True, summary="x", steps_taken=0, total_duration_seconds=0)

        client = make_client(runner, make_settings(tmp_path))
        try:
            assert client.get("/task/nope/steps").status_code == 404
            assert client.get("/task/nope/screenshot").status_code == 404
            assert client.post("/task/nope/stop").status_code == 404
        finally:
            client.__exit__(None, None, None)

    def test_screenshot_served_and_missing(self, tmp_path):
        # Hardening: the served path must live inside SCREENSHOT_DIR.
        settings = make_settings(tmp_path)
        shot = settings.screenshot_dir / "shot.png"
        shot.write_bytes(b"\x89PNG fake")

        async def plain_runner(task, starting_url):
            return TaskResult(success=True, summary="ok", steps_taken=1, total_duration_seconds=0.1)

        async def shooting_runner(task, starting_url, emit=None):
            emit(
                {
                    "type": "step",
                    "step": 1,
                    "tool": "take_screenshot",
                    "success": True,
                    "screenshot_path": str(shot),
                }
            )
            return TaskResult(success=True, summary="ok", steps_taken=1, total_duration_seconds=0.1)

        # task without screenshots -> 404
        client = make_client(plain_runner, settings)
        try:
            no_shot = client.post("/task", json={"task": "a"}).json()["task_id"]
            wait_for_state(client, no_shot, "finished")
            assert client.get(f"/task/{no_shot}/screenshot").status_code == 404
        finally:
            client.__exit__(None, None, None)

        # task that took a screenshot -> file bytes
        client2 = make_client(shooting_runner, settings)
        try:
            with_shot = client2.post("/task", json={"task": "b"}).json()["task_id"]
            wait_for_state(client2, with_shot, "finished")
            resp = client2.get(f"/task/{with_shot}/screenshot")
            assert resp.status_code == 200
            assert resp.content == b"\x89PNG fake"
        finally:
            client2.__exit__(None, None, None)


# ============================================================================
# Per-task graceful stop
# ============================================================================


class TestTaskStop:
    def test_stop_running_task(self, tmp_path):
        async def runner(task, starting_url, stop_check=None):
            while stop_check is not None and not stop_check():
                await asyncio.sleep(0.02)
            return TaskResult(
                success=False,
                summary="stopped mid-run",
                steps_taken=2,
                total_duration_seconds=0.2,
                error="ShutdownRequested",
            )

        client = make_client(runner, make_settings(tmp_path))
        try:
            task_id = client.post("/task", json={"task": "long"}).json()["task_id"]
            wait_for_state(client, task_id, "running")
            resp = client.post(f"/task/{task_id}/stop")
            assert resp.status_code == 200
            assert resp.json()["stop_requested"] is True
            status = wait_for_state(client, task_id, "finished")
            assert status["result"]["error"] == "ShutdownRequested"
            # already finished -> 409, no second stop
            assert client.post(f"/task/{task_id}/stop").status_code == 409
        finally:
            client.__exit__(None, None, None)

    def test_stop_queued_task_skips_execution(self, tmp_path):
        release = asyncio.Event()

        async def runner(task, starting_url):
            while not release.is_set():
                await asyncio.sleep(0.02)
            return TaskResult(
                success=True, summary="first done", steps_taken=1, total_duration_seconds=0.1
            )

        client = make_client(runner, make_settings(tmp_path))
        try:
            first = client.post("/task", json={"task": "first"}).json()["task_id"]
            wait_for_state(client, first, "running")
            second = client.post("/task", json={"task": "second"}).json()["task_id"]
            assert client.get(f"/task/{second}").json()["state"] == "queued"
            assert client.post(f"/task/{second}/stop").status_code == 200
            release.set()
            status = wait_for_state(client, second, "finished")
            assert status["result"]["error"] == "StoppedByUser"
            assert wait_for_state(client, first, "finished")["result"]["success"] is True
        finally:
            release.set()
            client.__exit__(None, None, None)


# ============================================================================
# WebSocket live channel
# ============================================================================


class TestWebSocket:
    def test_replays_events_and_final(self, tmp_path):
        async def runner(task, starting_url, emit=None):
            emit(
                {
                    "type": "step",
                    "step": 1,
                    "tool": "click_element",
                    "success": True,
                    "message": "Clicked",
                }
            )
            return TaskResult(
                success=True, summary="ws done", steps_taken=1, total_duration_seconds=0.1
            )

        client = make_client(runner, make_settings(tmp_path))
        try:
            task_id = client.post("/task", json={"task": "ws"}).json()["task_id"]
            wait_for_state(client, task_id, "finished")
            with client.websocket_connect(f"/ws/task/{task_id}") as ws:
                first = ws.receive_json()
                assert first["type"] == "step"
                assert first["task_id"] == task_id
                final = ws.receive_json()
                assert final["type"] == "final"
                assert final["result"]["summary"] == "ws done"
        finally:
            client.__exit__(None, None, None)

    def test_unknown_task_rejected(self, tmp_path):
        async def runner(task, starting_url):
            return TaskResult(success=True, summary="x", steps_taken=0, total_duration_seconds=0)

        client = make_client(runner, make_settings(tmp_path))
        try:
            from starlette.websockets import WebSocketDisconnect

            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws/task/does-not-exist"):
                    pass
        finally:
            client.__exit__(None, None, None)


# ============================================================================
# /config, /reports, static UI
# ============================================================================


class TestConfigReportsAndStatic:
    def test_config_with_injected_settings(self, tmp_path):
        async def runner(task, starting_url):
            return TaskResult(success=True, summary="x", steps_taken=0, total_duration_seconds=0)

        client = make_client(runner, make_settings(tmp_path, enable_evaluator=True))
        try:
            resp = client.get("/config")
            assert resp.status_code == 200
            cfg = resp.json()["settings"]
            assert cfg["api_key"].startswith("***masked")
            assert "super-secret" not in json.dumps(cfg)
            assert cfg["enable_evaluator"] is True
            assert cfg["enable_stealth_mode"] is True
        finally:
            client.__exit__(None, None, None)

    def test_config_lazy_load_failure_503(self, tmp_path, monkeypatch):
        async def runner(task, starting_url):
            return TaskResult(success=True, summary="x", steps_taken=0, total_duration_seconds=0)

        import src.config as config_mod

        def boom():
            raise RuntimeError("no .env")

        monkeypatch.setattr(config_mod, "load_settings", boom)
        client = make_client(runner)  # no settings injected
        try:
            assert client.get("/config").status_code == 503
        finally:
            client.__exit__(None, None, None)

    def test_reports_listing_and_content(self, tmp_path):
        settings = make_settings(tmp_path)
        report = {
            "run_id": "abc12345",
            "task": "t",
            "success": True,
            "steps": 3,
            "tokens": 42,
            "captcha_events": 0,
        }
        (settings.reports_dir / "run_abc12345.json").write_text(json.dumps(report))

        async def runner(task, starting_url):
            return TaskResult(success=True, summary="x", steps_taken=0, total_duration_seconds=0)

        client = make_client(runner, settings)
        try:
            listing = client.get("/reports").json()["reports"]
            assert listing and listing[0]["run_id"] == "run_abc12345"

            detail = client.get("/reports/run_abc12345")
            assert detail.status_code == 200
            assert detail.json()["tokens"] == 42

            assert client.get("/reports/run_missing").status_code == 404
            # traversal-ish / invalid ids rejected by the strict pattern
            assert client.get("/reports/..%2Fetc%2Fpasswd").status_code in (400, 404)
            assert client.get("/reports/run_abc12345.json").status_code in (400, 404)
        finally:
            client.__exit__(None, None, None)

    def test_static_ui_served(self, tmp_path):
        async def runner(task, starting_url):
            return TaskResult(success=True, summary="x", steps_taken=0, total_duration_seconds=0)

        client = make_client(runner, make_settings(tmp_path))
        try:
            resp = client.get("/")
            assert resp.status_code == 200
            assert "CogniWeb Agent" in resp.text
            # API routes still take precedence over the static mount
            assert client.get("/health").status_code == 200
        finally:
            client.__exit__(None, None, None)


# ============================================================================
# Runner introspection: 2-arg runners keep working (backward compat)
# ============================================================================


class TestRunnerCompat:
    def test_two_arg_runner_still_supported(self, tmp_path):
        calls = []

        async def old_style_runner(task, starting_url):  # no emit/stop_check
            calls.append((task, starting_url))
            return TaskResult(
                success=True, summary="legacy", steps_taken=1, total_duration_seconds=0.1
            )

        client = make_client(old_style_runner, make_settings(tmp_path))
        try:
            task_id = client.post(
                "/task", json={"task": "legacy", "starting_url": "https://x"}
            ).json()["task_id"]
            status = wait_for_state(client, task_id, "finished")
            assert status["result"]["summary"] == "legacy"
            assert calls == [("legacy", "https://x")]
        finally:
            client.__exit__(None, None, None)
