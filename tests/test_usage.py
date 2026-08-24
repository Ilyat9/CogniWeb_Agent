"""
Rate limiting and usage accounting tests (Task 2, public-service iteration):
UsageTracker unit behavior (sliding window, concurrency, hard quota, cost
estimation) and the API-level 429 contract + GET /usage/{tenant_id}.
"""

import sys
import time
from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.app import create_app  # noqa: E402
from src.config.settings import Settings  # noqa: E402
from src.core.models import TaskResult  # noqa: E402
from src.infrastructure.usage import UsageTracker  # noqa: E402


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


# ============================================================================
# UsageTracker unit behavior
# ============================================================================


class TestUsageTracker:
    def test_sliding_window_rejects_then_frees(self):
        tracker = UsageTracker(max_concurrent_per_tenant=0, tasks_per_hour=2)
        now = 1000.0
        assert tracker.check_submission("t", running_tasks=0, now=now)[0] is True
        tracker.record_submission("t", now=now)
        assert tracker.check_submission("t", running_tasks=0, now=now)[0] is True
        tracker.record_submission("t", now=now)

        allowed, reason, retry_after = tracker.check_submission("t", 0, now=now + 1)
        assert allowed is False and reason == "hourly_limit" and retry_after >= 1

        # window slides: an hour later the slot frees up
        allowed, _, _ = tracker.check_submission("t", 0, now=now + 3601)
        assert allowed is True

    def test_window_is_per_tenant(self):
        tracker = UsageTracker(tasks_per_hour=1)
        tracker.record_submission("a", now=100.0)
        assert tracker.check_submission("a", 0, now=100.5)[1] == "hourly_limit"
        assert tracker.check_submission("b", 0, now=100.5)[0] is True

    def test_concurrent_limit(self):
        tracker = UsageTracker(max_concurrent_per_tenant=2, tasks_per_hour=0)
        allowed, reason, _ = tracker.check_submission("t", running_tasks=2)
        assert allowed is False and reason == "concurrent_limit"
        assert tracker.check_submission("t", running_tasks=1)[0] is True

    def test_hard_token_budget_blocks_after_crossing(self):
        tracker = UsageTracker(token_budget=100)
        assert tracker.check_submission("t", 0)[0] is True
        tracker.record_completion("t", tokens_used=60)
        assert tracker.check_submission("t", 0)[0] is True  # under budget
        tracker.record_completion("t", tokens_used=40)  # exactly at budget
        allowed, reason, _ = tracker.check_submission("t", 0)
        assert allowed is False and reason == "quota_exceeded"

    def test_budget_disabled_by_zero(self):
        tracker = UsageTracker(token_budget=0)
        for _ in range(10):
            tracker.record_completion("t", tokens_used=10**9)
        assert tracker.check_submission("t", 0)[0] is True

    def test_none_tokens_ignored(self):
        tracker = UsageTracker(token_budget=10)
        tracker.record_completion("t", None)
        assert tracker.snapshot("t")["total_tokens"] == 0

    def test_cost_estimation(self):
        tracker = UsageTracker(cost_per_1k_tokens=0.5)
        tracker.record_completion("t", 2000)
        assert tracker.estimated_cost_usd("t") == pytest.approx(1.0)
        snap = tracker.snapshot("t")
        assert snap["estimated_cost_usd"] == pytest.approx(1.0)

    def test_snapshot_shape_and_limits_disabled_as_none(self):
        tracker = UsageTracker(
            max_concurrent_per_tenant=3,
            tasks_per_hour=0,
            token_budget=0,
            window_seconds=1800.0,
        )
        snap = tracker.snapshot("acme")
        assert snap["tenant_id"] == "acme"
        assert snap["window_seconds"] == 1800.0
        assert snap["limits"]["tasks_per_hour"] is None  # disabled -> None
        assert snap["limits"]["max_concurrent_tasks"] == 3
        assert snap["limits"]["token_budget"] == {"limit": 0, "exceeded": False}

    def test_rejected_submissions_do_not_consume_quota(self):
        # check_submission alone must not grow the window
        tracker = UsageTracker(tasks_per_hour=1)
        tracker.check_submission("t", 0)
        tracker.check_submission("t", 0)
        assert tracker.snapshot("t")["tasks_in_window"] == 0


# ============================================================================
# API-level: 429 contract + /usage endpoint
# ============================================================================


def _runner(tokens=1234):
    async def runner(task, starting_url, **kw):
        await asyncio.sleep(0.05)
        return TaskResult(
            success=True,
            summary="ok",
            steps_taken=1,
            total_duration_seconds=0.05,
            tokens_used=tokens,
        )

    return runner


def _client(tmp_path, runner=None, **overrides):
    settings = make_settings(tmp_path, **overrides)
    app = create_app(runner or _runner(), settings=settings)
    client = fastapi_testclient.TestClient(app)
    client.__enter__()
    return client


def _wait_finished(client, task_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get(f"/task/{task_id}").json().get("state") == "finished":
            return True
        time.sleep(0.05)
    return False


class TestApiRateLimiting:
    def test_hourly_limit_429_with_retry_after(self, tmp_path):
        client = _client(tmp_path, rate_limit_tasks_per_hour=2)
        try:
            assert client.post("/task", json={"task": "one"}).status_code == 202
            assert client.post("/task", json={"task": "two"}).status_code == 202
            resp = client.post("/task", json={"task": "three"})
            assert resp.status_code == 429
            body = resp.json()["detail"]
            assert body["error"] == "rate_limited"
            assert body["reason"] == "hourly_limit"
            assert int(resp.headers["Retry-After"]) >= 1
            # another tenant is unaffected by t=default's limit
            ok = client.post("/task", json={"task": "x", "tenant_id": "other"})
            assert ok.status_code == 202
        finally:
            client.close()

    def test_no_rate_limit_by_default_within_cap(self, tmp_path):
        client = _client(tmp_path)
        try:
            for i in range(5):
                assert client.post("/task", json={"task": f"t{i}"}).status_code == 202
        finally:
            client.close()

    def test_usage_endpoint_reflects_completed_tokens(self, tmp_path):
        client = _client(tmp_path)
        try:
            task_id = client.post("/task", json={"task": "run me"}).json()["task_id"]
            assert _wait_finished(client, task_id)
            usage = client.get("/usage/default").json()
            assert usage["total_tokens"] == 1234
            assert usage["total_tasks"] == 1
            assert usage["tasks_in_window"] == 1
            assert usage["tenant_id"] == "default"

            other = client.get("/usage/acme").json()
            assert other["total_tasks"] == 0 and other["total_tokens"] == 0
        finally:
            client.close()

    @pytest.mark.parametrize("bad", ["../evil", "a b", "x/y"])
    def test_usage_endpoint_validates_tenant_id(self, tmp_path, bad):
        client = _client(tmp_path)
        try:
            resp = client.get(f"/usage/{bad}")
            assert resp.status_code in (400, 404)  # path traversal never 200
        finally:
            client.close()

    def test_quota_exceeded_429(self, tmp_path):
        app = create_app(
            _runner(tokens=500),
            settings=make_settings(tmp_path, tenant_token_budget=600),
        )
        client = fastapi_testclient.TestClient(app)
        client.__enter__()
        try:
            assert client.post("/task", json={"task": "first"}).status_code == 202
            deadline = time.time() + 5
            while time.time() < deadline:
                usage = client.get("/usage/default").json()
                if usage["total_tokens"] >= 500:  # first run accounted
                    break
                time.sleep(0.05)
            # budget=600, burned 500: one more task accepted (crosses to
            # 1000 only AFTER completion), then quota blocks.
            second = client.post("/task", json={"task": "second"})
            assert second.status_code in (202, 429)

            refused = None
            deadline = time.time() + 8
            while time.time() < deadline:
                r = client.post("/task", json={"task": "more"})
                if r.status_code == 429:
                    refused = r.json()["detail"]
                    break
                time.sleep(0.1)
            assert refused is not None and refused["reason"] == "quota_exceeded"
        finally:
            client.close()


import asyncio  # noqa: E402  (used by the fake runners above)
