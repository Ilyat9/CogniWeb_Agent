"""
Multi-tenancy tests (Task 1, public-service iteration): tenant_id on
submissions/statuses, per-tenant record isolation in the API, the
TenantContextPool lifecycle, and round-robin fairness of the dispatcher.
"""

import asyncio
import sys
import time
from collections import deque
from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.app import create_app  # noqa: E402
from src.config.settings import Settings  # noqa: E402
from src.core.models import TaskResult  # noqa: E402
from src.infrastructure.browser import TenantContextPool  # noqa: E402


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


def _ok_runner_factory(delay=0.0):
    async def runner(task, starting_url, tenant_id="default", **kw):
        if delay:
            await asyncio.sleep(delay)
        return TaskResult(
            success=True,
            summary=f"{tenant_id}:{task}",
            steps_taken=0,
            total_duration_seconds=delay or 0.01,
        )

    return runner


def wait_for_state(client, task_id, state, timeout=5.0, tenant_id=None):
    params = {"tenant_id": tenant_id} if tenant_id else None
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        status = client.get(f"/task/{task_id}", params=params).json()
        if status.get("state") == state:
            return status
        time.sleep(0.05)
    return status


# ============================================================================
# Submission / status model
# ============================================================================


class TestSubmissionModel:
    def test_default_tenant(self, tmp_path):
        client = fastapi_testclient.TestClient(
            create_app(_ok_runner_factory(), settings=make_settings(tmp_path))
        )
        client.__enter__()
        try:
            resp = client.post("/task", json={"task": "hello"})
            assert resp.status_code == 202
            assert resp.json()["tenant_id"] == "default"
            task_id = resp.json()["task_id"]
            status = wait_for_state(client, task_id, "finished")
            assert status["tenant_id"] == "default"
            # summary proves the runner RECEIVED tenant_id="default"
            assert status["result"]["summary"].startswith("default:")
        finally:
            client.close()

    @pytest.mark.parametrize("bad", ["../evil", "", "a b", "x" * 65, "tenant/id"])
    def test_invalid_tenant_rejected_422(self, tmp_path, bad):
        client = fastapi_testclient.TestClient(
            create_app(_ok_runner_factory(), settings=make_settings(tmp_path))
        )
        client.__enter__()
        try:
            resp = client.post("/task", json={"task": "t", "tenant_id": bad})
            assert resp.status_code == 422
        finally:
            client.close()


# ============================================================================
# Record isolation between tenants
# ============================================================================


class TestIsolation:
    def test_other_tenant_task_is_404(self, tmp_path):
        client = fastapi_testclient.TestClient(
            create_app(_ok_runner_factory(), settings=make_settings(tmp_path))
        )
        client.__enter__()
        try:
            task_id = client.post(
                "/task", json={"task": "secret", "tenant_id": "acme"}
            ).json()["task_id"]
            wait_for_state(client, task_id, "finished", tenant_id="acme")
            # default tenant cannot see acme's task - and vice versa
            assert client.get(f"/task/{task_id}").status_code == 404
            assert (
                client.get(f"/task/{task_id}", params={"tenant_id": "acme"}).status_code
                == 200
            )
            assert (
                client.get(f"/task/{task_id}/steps").status_code == 404
            )  # default bucket again
            # stop from the wrong bucket must NOT reach the task either
            assert client.post(f"/task/{task_id}/stop").status_code == 404
        finally:
            client.close()

    def test_list_filtered_by_tenant_with_all_escape_hatch(self, tmp_path):
        client = fastapi_testclient.TestClient(
            create_app(_ok_runner_factory(), settings=make_settings(tmp_path))
        )
        client.__enter__()
        try:
            id_a = client.post("/task", json={"task": "a"}).json()["task_id"]
            id_b = client.post(
                "/task", json={"task": "b", "tenant_id": "beta"}
            ).json()["task_id"]
            wait_for_state(client, id_a, "finished")
            wait_for_state(client, id_b, "finished", tenant_id="beta")

            mine = [t["task_id"] for t in client.get("/tasks").json()["tasks"]]
            assert id_a in mine and id_b not in mine

            beta = [
                t["task_id"]
                for t in client.get("/tasks", params={"tenant_id": "beta"}).json()["tasks"]
            ]
            assert id_b in beta and id_a not in beta

            everything = [
                t["task_id"] for t in client.get("/tasks", params={"tenant_id": "all"}).json()["tasks"]
            ]
            assert {id_a, id_b} <= set(everything)
            listed = {
                t["task_id"]: t for t in client.get("/tasks", params={"tenant_id": "all"}).json()["tasks"]
            }
            assert listed[id_b]["tenant_id"] == "beta"
        finally:
            client.close()


# ============================================================================
# TenantContextPool (fake services - no real Chromium)
# ============================================================================


class FakeBrowserService:
    def __init__(self, settings):
        self.settings = settings
        self.started = False
        self.closed = False
        self.pages = 0

    async def start(self):
        self.started = True

    def new_page(self):
        self.pages += 1
        return self

    @property
    def page(self):
        return None

    async def close(self):
        self.closed = True


def make_pool(tmp_path, **overrides):
    settings = make_settings(tmp_path, **overrides)
    created: list[FakeBrowserService] = []

    def factory(s):
        svc = FakeBrowserService(s)
        created.append(svc)
        return svc

    return TenantContextPool(settings, service_factory=factory), created


class TestTenantContextPool:
    def test_per_tenant_isolated_dirs(self, tmp_path):
        pool, _ = make_pool(tmp_path)
        assert pool.tenant_data_dir("acme") == (tmp_path / "browser_data" / "tenants" / "acme")
        assert pool.tenant_data_dir("default") != pool.tenant_data_dir("acme")

    def test_invalid_tenant_rejected(self, tmp_path):
        from src.core.exceptions import BrowserError

        pool, _ = make_pool(tmp_path)
        with pytest.raises(BrowserError):
            pool.tenant_data_dir("../evil")

    @pytest.mark.asyncio
    async def test_acquire_launches_and_reuses(self, tmp_path):
        pool, created = make_pool(tmp_path)
        svc = await pool.acquire("acme")
        assert svc.started and len(created) == 1
        assert svc.settings.user_data_dir == str(
            tmp_path / "browser_data" / "tenants" / "acme"
        )
        pool.release("acme")
        again = await pool.acquire("acme")
        # warm reuse: same service, no second launch
        assert again is svc and len(created) == 1
        await pool.close_all()

    @pytest.mark.asyncio
    async def test_same_tenant_is_exclusive_waits(self, tmp_path):
        pool, created = make_pool(tmp_path)
        first = asyncio.create_task(pool.acquire("acme"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(pool.acquire("acme"))
        await first
        done, _ = await asyncio.wait([second], timeout=0.2)
        assert not done  # busy tenant: second acquire waits
        pool.release("acme")
        await asyncio.wait_for(second, timeout=2)
        assert len(created) == 1  # no double-launch onto the same profile dir
        await pool.close_all()

    @pytest.mark.asyncio
    async def test_full_pool_evicts_lru_idle_context(self, tmp_path):
        """At the limit, acquiring a new tenant EVICTS the least-recently-
        used idle context (its profile dir survives; only the process is
        freed) - a queued task must never wait behind cold, unused
        contexts."""
        pool, created = make_pool(tmp_path, max_concurrent_tenant_contexts=1)
        a = await pool.acquire("a")
        pool.release("a")
        b = await pool.acquire("b")
        assert a.closed  # evicted to make room
        assert b is not a and len(created) == 2
        assert set(pool._contexts) == {"b"}
        await pool.close_all()

    @pytest.mark.asyncio
    async def test_busy_context_never_evicted(self, tmp_path):
        pool, created = make_pool(tmp_path, max_concurrent_tenant_contexts=1)
        a = await pool.acquire("a")  # BUSY until released
        b_task = asyncio.create_task(pool.acquire("b"))
        await asyncio.sleep(0.15)
        assert not b_task.done() and not a.closed  # waits, no eviction
        pool.release("a")
        b = await asyncio.wait_for(b_task, timeout=2)
        assert a.closed and len(created) == 2  # evicted only after release
        await pool.close_all()

    @pytest.mark.asyncio
    async def test_close_idle_respects_ttl_and_busy(self, tmp_path):
        pool, created = make_pool(
            tmp_path,
            max_concurrent_tenant_contexts=2,
            tenant_context_idle_ttl_seconds=30.0,
        )
        idle = await pool.acquire("idle")
        pool.release("idle")
        busy = await pool.acquire("busy")
        # simulate long idleness of 'idle' only
        pool._last_used["idle"] -= 60.0
        closed = await pool.close_idle()
        assert closed == 1 and idle.closed and not busy.closed
        assert "idle" not in pool._contexts and "busy" in pool._contexts
        await pool.close_all()
        assert busy.closed


# ============================================================================
# Dispatcher fairness: round-robin across tenants
# ============================================================================


class TestDispatcherFairness:
    @pytest.mark.asyncio
    async def test_second_tenant_not_starved_by_backlog(self, tmp_path):
        """With parallelism 1 (default), a flood of tasks from tenant A must
        still interleave with B's task instead of running after all of A."""
        order: list[str] = []

        async def runner(task, starting_url, tenant_id="default", **kw):
            order.append(f"{tenant_id}:{task}")
            await asyncio.sleep(0.01)
            return TaskResult(success=True, summary="ok", steps_taken=0, total_duration_seconds=0)

        app = create_app(runner, settings=make_settings(tmp_path))
        client = fastapi_testclient.TestClient(app)
        client.__enter__()
        try:
            for i in range(4):
                client.post("/task", json={"task": f"a{i}", "tenant_id": "a"})
            client.post("/task", json={"task": "b1", "tenant_id": "b"})
            deadline = time.time() + 5
            while time.time() < deadline and len(order) < 5:
                await asyncio.sleep(0.02)
            assert len(order) == 5
            # B's single task must run BEFORE A's last backlog item
            assert order.index("b:b1") < order.index("a:a3")
        finally:
            client.close()

    def test_pending_backpressure_still_global(self, tmp_path):
        from datetime import datetime as dt

        from src.api.app import MAX_PENDING_TASKS

        client = fastapi_testclient.TestClient(
            create_app(_ok_runner_factory(), settings=make_settings(tmp_path))
        )
        client.__enter__()
        try:
            for i in range(MAX_PENDING_TASKS):
                client.app.state.tasks[f"seeded-{i}"] = {
                    "state": "queued",
                    "submitted_at": dt.now().isoformat(),
                    "tenant_id": f"t{i}",
                }
            resp = client.post("/task", json={"task": "over the cap"})
            assert resp.status_code == 429
        finally:
            client.close()

