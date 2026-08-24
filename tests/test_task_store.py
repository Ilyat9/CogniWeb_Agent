"""Task 1 (persistence) tests: the SQLite TaskStore and its integration
with the API app (hydration on restart, interrupted-task marking, TTL
pruning, write-through of finished results).

The store is exercised directly (aiosqlite, tmp_path DB files - no real
network/browser), plus one API-level restart test through TestClient.
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
pytest.importorskip("aiosqlite")

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.app import create_app  # noqa: E402
from src.api.task_store import TaskStore  # noqa: E402
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
        "task_db_path": tmp_path / "tasks.db",
    }
    base.update(overrides)
    return Settings(**base)


def make_record(task_id: str, state: str = "finished", **overrides):
    record = {
        "task_id": task_id,
        "state": state,
        "submitted_at": "2026-08-24T10:00:00",
        "task": f"task {task_id}",
        "starting_url": "https://example.com",
        "result": None,
        "steps": [{"type": "step", "step": 1, "tool": "navigate"}],
        "stop_requested": False,
        "latest_screenshot": None,
        "current_step": None,
        "last_tool": None,
    }
    if state == "finished":
        record["result"] = {
            "success": True,
            "summary": "done",
            "steps_taken": 1,
            "total_duration_seconds": 1.0,
        }
    record.update(overrides)
    return record


class TestTaskStoreUnit:
    @pytest.mark.asyncio
    async def test_save_load_roundtrip(self, tmp_path):
        store = TaskStore(tmp_path / "t.db")
        await store.initialize()
        try:
            record = make_record("abc", current_step=3, last_tool="click_element")
            await store.save(record)
            loaded = await store.load_all()
            assert len(loaded) == 1
            got = loaded[0]
            assert got["task_id"] == "abc"
            assert got["state"] == "finished"
            assert got["result"]["summary"] == "done"
            assert got["steps"][0]["tool"] == "navigate"
            assert got["current_step"] == 3
            assert got["last_tool"] == "click_element"
            assert got["starting_url"] == "https://example.com"
            # live-only key is materialized empty for hydrated records
            assert got["subscribers"] == []
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_upsert_replaces_previous_state(self, tmp_path):
        store = TaskStore(tmp_path / "t.db")
        await store.initialize()
        try:
            queued = make_record("abc", state="queued", result=None)
            await store.save(queued)
            finished = make_record("abc", state="finished")
            await store.save(finished)
            loaded = await store.load_all()
            assert len(loaded) == 1
            assert loaded[0]["state"] == "finished"
            assert loaded[0]["result"]["summary"] == "done"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_persistence_across_restart(self, tmp_path):
        """The whole point: a NEW store instance over the same file sees
        the previous process's records."""
        path = tmp_path / "t.db"
        first = TaskStore(path)
        await first.initialize()
        await first.save(make_record("keep-me"))
        await first.close()

        second = TaskStore(path)
        await second.initialize()
        try:
            ids = [r["task_id"] for r in await second.load_all()]
            assert ids == ["keep-me"]
        finally:
            await second.close()

    @pytest.mark.asyncio
    async def test_delete_removes_row(self, tmp_path):
        store = TaskStore(tmp_path / "t.db")
        await store.initialize()
        try:
            await store.save(make_record("gone"))
            await store.delete("gone")
            assert await store.load_all() == []
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_corrupt_json_degrades_not_raises(self, tmp_path):
        import aiosqlite

        path = tmp_path / "t.db"
        async with aiosqlite.connect(str(path)) as db:
            await db.execute(
                "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, state TEXT, "
                "submitted_at TEXT, task TEXT, starting_url TEXT, result TEXT, "
                "steps TEXT, stop_requested INTEGER, latest_screenshot TEXT, "
                "current_step INTEGER, last_tool TEXT, last_success INTEGER, "
                "updated_at REAL)"
            )
            await db.execute(
                "INSERT INTO tasks VALUES ('bad', 'finished', '2026-01-01', 't', "
                "NULL, '{not json', '[also bad', 0, NULL, NULL, NULL, NULL, 0)"
            )
            await db.commit()
        store = TaskStore(path)
        await store.initialize()
        try:
            loaded = await store.load_all()
            assert len(loaded) == 1
            assert loaded[0]["result"] is None
            assert loaded[0]["steps"] == []
        finally:
            await store.close()


class TestAppPersistenceIntegration:
    def _make_client(self, runner, settings):
        app = create_app(runner, settings=settings)
        client = fastapi_testclient.TestClient(app)
        client.__enter__()
        client.app = app
        return client

    def test_finished_task_survives_app_restart(self, tmp_path):
        settings = make_settings(tmp_path)

        async def runner(task, starting_url, emit=None):
            emit({"type": "step", "step": 1, "tool": "navigate", "success": True})
            return TaskResult(
                success=True, summary="persisted", steps_taken=1, total_duration_seconds=0.1
            )

        client = self._make_client(runner, settings)
        try:
            task_id = client.post("/task", json={"task": "survivor"}).json()["task_id"]
            deadline = time.time() + 5
            while time.time() < deadline:
                status = client.get(f"/task/{task_id}").json()
                if status["state"] == "finished":
                    break
                time.sleep(0.05)
            assert status["state"] == "finished"
        finally:
            client.__exit__(None, None, None)
            # give fire-and-forget write-through saves a moment to land
            time.sleep(0.2)

        # "Restart": a brand-new app instance over the same DB file must
        # serve the same history through the unchanged API contract.
        async def noop_runner(task, starting_url):
            raise AssertionError("no task should run after restart")

        client2 = self._make_client(noop_runner, settings)
        try:
            status = client2.get(f"/task/{task_id}").json()
            assert status["state"] == "finished"
            assert status["result"]["summary"] == "persisted"

            listed = client2.get("/tasks").json()["tasks"]
            assert [t["task_id"] for t in listed] == [task_id]

            steps = client2.get(f"/task/{task_id}/steps").json()
            kinds = [e["type"] for e in steps["steps"]]
            assert "step" in kinds and "final" in kinds
        finally:
            client2.__exit__(None, None, None)

    def test_interrupted_running_task_marked_on_restart(self, tmp_path):
        """A task that was 'running' when the old process died must not
        come back as a zombie 'running' record."""
        settings = make_settings(tmp_path)
        db_path = settings.task_db_path

        async def seed():
            store = TaskStore(db_path)
            await store.initialize()
            await store.save(make_record("zombie", state="running", current_step=4))
            await store.close()

        asyncio.run(seed())

        async def noop_runner(task, starting_url):
            raise AssertionError("interrupted task must not be re-run")

        client = self._make_client(noop_runner, settings)
        try:
            status = client.get("/task/zombie").json()
            assert status["state"] == "finished"
            assert status["result"]["error"] == "InterruptedByRestart"
            assert status["result"]["steps_taken"] == 4
        finally:
            client.__exit__(None, None, None)

    def test_ttl_prune_drops_expired_finished_tasks(self, tmp_path):
        """Retention: expired finished records are pruned from memory AND
        the SQLite table; fresh ones survive."""
        settings = make_settings(tmp_path, task_ttl_hours=0.0001)  # ~0.36s
        db_path = settings.task_db_path

        async def seed():
            store = TaskStore(db_path)
            await store.initialize()
            stale = make_record(
                "stale",
                submitted_at="2000-01-01T00:00:00",
            )
            fresh = make_record("fresh", submitted_at="2999-01-01T00:00:00")
            await store.save(stale)
            await store.save(fresh)
            await store.close()

        asyncio.run(seed())

        async def noop_runner(task, starting_url):
            raise AssertionError("no task should run")

        client = self._make_client(noop_runner, settings)
        try:
            # startup hydration + prune already ran; trigger one more sweep
            client.post("/task", json={"task": "trigger"})
            deadline = time.time() + 5
            while time.time() < deadline:
                ids = {t["task_id"] for t in client.get("/tasks").json()["tasks"]}
                if "stale" not in ids:
                    break
                time.sleep(0.05)
            assert "stale" not in ids
            assert "fresh" in ids

            async def check_db():
                store = TaskStore(db_path)
                await store.initialize()
                try:
                    return {r["task_id"] for r in await store.load_all()}
                finally:
                    await store.close()

            db_ids = asyncio.run(check_db())
            assert "stale" not in db_ids
            assert "fresh" in db_ids
        finally:
            client.__exit__(None, None, None)

    def test_no_settings_stays_in_memory(self, tmp_path):
        """Backward-compat degradation: without injected settings there is
        no DB path, so the app runs exactly like before (store is None)."""

        async def runner(task, starting_url):
            return TaskResult(success=True, summary="x", steps_taken=0, total_duration_seconds=0)

        app = create_app(runner, settings=None)
        client = fastapi_testclient.TestClient(app)
        client.__enter__()
        try:
            assert client.app.state.task_store is None
            task_id = client.post("/task", json={"task": "mem"}).json()["task_id"]
            deadline = time.time() + 5
            while time.time() < deadline:
                if client.get(f"/task/{task_id}").json()["state"] == "finished":
                    break
                time.sleep(0.05)
            assert client.get(f"/task/{task_id}").json()["result"]["summary"] == "x"
        finally:
            client.__exit__(None, None, None)