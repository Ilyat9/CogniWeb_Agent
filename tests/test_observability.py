"""
Observability tests (Task 3, public-service iteration): Prometheus /metrics
exposition with task/LLM/browser metrics, structured /health component
status (including down -> 503), and Sentry opt-in behavior.
"""

import sys
from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.app import create_app  # noqa: E402
from src.config.settings import Settings  # noqa: E402
from src.core.models import TaskResult  # noqa: E402
from src.infrastructure import metrics as _metrics  # noqa: E402


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


def _runner(success=True, tokens=100):
    async def runner(task, starting_url, **kw):
        return TaskResult(
            success=success,
            summary="ok" if success else "boom",
            steps_taken=1,
            total_duration_seconds=0.05,
            error=None if success else "SimulatedFailure",
            tokens_used=tokens,
        )

    return runner


def _client(tmp_path, runner=None, **overrides):
    app = create_app(runner or _runner(), settings=make_settings(tmp_path, **overrides))
    client = fastapi_testclient.TestClient(app)
    client.__enter__()
    return client


def _wait_finished(client, task_id, timeout=5.0, tenant_id=None):
    import time

    params = {"tenant_id": tenant_id} if tenant_id else None
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/task/{task_id}", params=params).json()
        if status.get("state") == "finished":
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(not _metrics.AVAILABLE, reason="prometheus_client not installed")
class TestMetricsEndpoint:
    def test_metrics_exposition_format(self, tmp_path):
        client = _client(tmp_path)
        try:
            resp = client.get("/metrics")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/plain")
            body = resp.text
            assert "cogniweb_tasks_total" in body
            assert "cogniweb_task_duration_seconds" in body
            assert "cogniweb_llm_errors_total" in body
            assert "cogniweb_browser_contexts_open" in body
        finally:
            client.close()

    def test_task_lifecycle_counters_increment(self, tmp_path):
        client = _client(tmp_path)
        try:
            before = _metrics.TASKS_TOTAL.labels(
                tenant_id="default", state="finished"
            )._value.get()
            task_id = client.post("/task", json={"task": "count me"}).json()["task_id"]
            assert _wait_finished(client, task_id)
            after = _metrics.TASKS_TOTAL.labels(
                tenant_id="default", state="finished"
            )._value.get()
            assert after == before + 1
        finally:
            client.close()

    def test_failed_tasks_counted_separately(self, tmp_path):
        client = _client(tmp_path, runner=_runner(success=False))
        try:
            task_id = client.post("/task", json={"task": "fail"}).json()["task_id"]
            assert _wait_finished(client, task_id)
            failed = _metrics.TASKS_TOTAL.labels(
                tenant_id="default", state="failed"
            )._value.get()
            assert failed >= 1
        finally:
            client.close()

    def test_tenant_label_isolation(self, tmp_path):
        client = _client(tmp_path)
        try:
            task_id = client.post(
                "/task", json={"task": "t", "tenant_id": "acme"}
            ).json()["task_id"]
            assert _wait_finished(client, task_id, tenant_id="acme")
            acme_queued = _metrics.TASKS_TOTAL.labels(
                tenant_id="acme", state="queued"
            )._value.get()
            assert acme_queued >= 1
        finally:
            client.close()


@pytest.mark.skipif(_metrics.AVAILABLE, reason="only meaningful without the dep")
class TestMetricsGracefulDegradation:
    def test_observe_never_raises_without_library(self):
        # observe_* are no-ops when prometheus_client is absent
        _metrics.observe_task("t", "queued")
        _metrics.observe_llm_error("timeout")
        _metrics.set_browser_contexts(3)
        assert _metrics.render() is None


class TestHealthEndpoint:
    def test_unknown_providers_report_ok(self, tmp_path):
        client = _client(tmp_path)
        try:
            body = client.get("/health").json()
            assert body["status"] == "ok"
            comps = body["components"]
            assert comps["api"] == "ok"
            # no production wiring -> honest "unknown", never a fake "ok"
            assert comps["llm"] == "unknown"
            assert comps["browser"] == "unknown"
        finally:
            client.close()

    def test_down_provider_makes_health_down_503(self, tmp_path):
        async def llm_down():
            return False

        app = create_app(
            _runner(),
            settings=make_settings(tmp_path),
            health_providers={"llm": llm_down},
        )
        client = fastapi_testclient.TestClient(app)
        client.__enter__()
        try:
            resp = client.get("/health")
            assert resp.status_code == 503
            detail = resp.json()["detail"]
            assert detail["status"] == "down"
            assert detail["components"]["llm"] == "down"
        finally:
            client.close()

    def test_degraded_probe_reported(self, tmp_path):
        async def llm_boom():
            raise RuntimeError("probe crashed")

        app = create_app(
            _runner(),
            settings=make_settings(tmp_path),
            health_providers={"llm": llm_boom},
        )
        client = fastapi_testclient.TestClient(app)
        client.__enter__()
        try:
            body = client.get("/health").json()
            assert body["status"] == "degraded"
            assert body["components"]["llm"] == "degraded"
        finally:
            client.close()

    def test_draining_still_503(self, tmp_path):
        client = _client(tmp_path)
        try:
            client.app.state.draining = True
            assert client.get("/health").status_code == 503
        finally:
            client.close()


class TestSentryOptIn:
    def test_no_dsn_no_activation(self):
        from src.api.app import _init_sentry

        import src.api.app as ap

        ap._sentry_initialized = False
        _init_sentry(None)  # no settings at all - must be a no-op
        assert ap._sentry_initialized is False

    def test_dsn_without_package_skips_cleanly(self, tmp_path, monkeypatch):
        import builtins

        from src.api.app import _init_sentry

        import src.api.app as ap

        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name.startswith("sentry_sdk"):
                raise ImportError("blocked for test")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        ap._sentry_initialized = False
        settings = make_settings(tmp_path, sentry_dsn="https://k@sentry.example/1")
        _init_sentry(settings)  # must log-and-continue, not raise
        assert ap._sentry_initialized is False

    def test_sentry_dsn_setting_defaults_empty(self, tmp_path):
        assert make_settings(tmp_path).sentry_dsn == ""
