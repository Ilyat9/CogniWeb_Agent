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


def _exposition_value(sample_name: str, labels: dict | None = None) -> float:
    """Sum one sample family (e.g. 'cogniweb_rate_limit_wait_seconds_count',
    optionally filtered by label substrings) from the current registry
    exposition; 0.0 when absent. Histograms in prometheus_client expose
    _count/_sum only via the text payload, and labelled samples carry
    '{...}' between name and value."""
    body = _metrics.render() or ""
    total = 0.0
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name_part = line.split(" ", 1)[0]
        if name_part.split("{", 1)[0] != sample_name:
            continue
        if labels and not all(f'{k}="{v}"' in name_part for k, v in labels.items()):
            continue
        try:
            total += float(line.rsplit(" ", 1)[-1])
        except ValueError:
            continue
    return total


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
            before = _metrics.TASKS_TOTAL.labels(tenant_id="default", state="finished")._value.get()
            task_id = client.post("/task", json={"task": "count me"}).json()["task_id"]
            assert _wait_finished(client, task_id)
            after = _metrics.TASKS_TOTAL.labels(tenant_id="default", state="finished")._value.get()
            assert after == before + 1
        finally:
            client.close()

    def test_failed_tasks_counted_separately(self, tmp_path):
        client = _client(tmp_path, runner=_runner(success=False))
        try:
            task_id = client.post("/task", json={"task": "fail"}).json()["task_id"]
            assert _wait_finished(client, task_id)
            failed = _metrics.TASKS_TOTAL.labels(tenant_id="default", state="failed")._value.get()
            assert failed >= 1
        finally:
            client.close()

    def test_tenant_label_isolation(self, tmp_path):
        client = _client(tmp_path)
        try:
            task_id = client.post("/task", json={"task": "t", "tenant_id": "acme"}).json()[
                "task_id"
            ]
            assert _wait_finished(client, task_id, tenant_id="acme")
            acme_queued = _metrics.TASKS_TOTAL.labels(tenant_id="acme", state="queued")._value.get()
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
        _metrics.observe_tool_call("navigate", True, 0.1)
        _metrics.observe_http_request("GET", "/tasks", 200, 0.01)
        _metrics.observe_rate_limit_wait(1.5)
        _metrics.observe_usage_rejection("t", "quota_exceeded")
        _metrics.observe_tenant_tokens("t", 100)
        _metrics.observe_evaluator_verdict("pass", 0.4)
        _metrics.observe_browser_action_error("navigate", "timeout")
        _metrics.observe_task_finished("t", success=True, duration_seconds=1.0, steps_taken=3)
        _metrics.observe_llm_retry("cloud")
        _metrics.observe_llm_failover()
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
        import src.api.app as ap
        from src.api.app import _init_sentry

        ap._sentry_initialized = False
        _init_sentry(None)  # no settings at all - must be a no-op
        assert ap._sentry_initialized is False

    def test_dsn_without_package_skips_cleanly(self, tmp_path, monkeypatch):
        import builtins

        import src.api.app as ap
        from src.api.app import _init_sentry

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


@pytest.mark.skipif(not _metrics.AVAILABLE, reason="prometheus_client not installed")
class TestRuntimeMetrics:
    """Runtime observability extension: tools, HTTP middleware, usage/quota
    limits, evaluator verdicts, browser errors, LLM retries/failover."""

    NEW_METRIC_NAMES = (
        "cogniweb_tool_duration_seconds",
        "cogniweb_tool_calls_total",
        "cogniweb_http_requests_total",
        "cogniweb_http_request_duration_seconds",
        "cogniweb_rate_limit_wait_seconds",
        "cogniweb_usage_rejections_total",
        "cogniweb_tenant_tokens_used_total",
        "cogniweb_evaluator_verdicts_total",
        "cogniweb_evaluator_verdict_duration_seconds",
        "cogniweb_browser_action_errors_total",
        "cogniweb_task_steps_total",
        "cogniweb_llm_retries_total",
        "cogniweb_llm_failover_total",
    )

    def test_all_new_metric_families_exposed(self, tmp_path):
        client = _client(tmp_path)
        try:
            body = client.get("/metrics").text
            for name in self.NEW_METRIC_NAMES:
                assert name in body, f"{name} missing from /metrics exposition"
        finally:
            client.close()

    # ---- HTTP middleware -------------------------------------------------

    def test_http_requests_counted_with_path_template(self, tmp_path):
        client = _client(tmp_path)
        try:
            labels = {"method": "GET", "path_template": "/tasks", "status": "200"}
            before = _metrics.HTTP_REQUESTS.labels(**labels)._value.get()
            assert client.get("/tasks").status_code == 200
            assert _metrics.HTTP_REQUESTS.labels(**labels)._value.get() == before + 1
            assert (
                _metrics.HTTP_DURATION.labels(method="GET", path_template="/tasks")._sum.get()
                >= 0.0
            )
        finally:
            client.close()

    def test_http_metrics_use_template_not_concrete_path(self, tmp_path):
        """Concrete task ids must not leak into labels: matched routes use
        the route TEMPLATE (/task/{task_id}); unmatched routes (404 before
        routing) bucket under the fixed 'unmatched' template."""
        client = _client(tmp_path)
        try:
            assert client.get("/task/definitely-not-a-real-id").status_code == 404
            assert client.get("/totally-unknown-path-xyz").status_code == 404
            body = client.get("/metrics").text
            assert "definitely-not-a-real-id" not in body
            assert 'path_template="/task/{task_id}"' in body
            assert 'path_template="unmatched"' in body
        finally:
            client.close()

    def test_http_metrics_exclude_scrape_and_probe_paths(self, tmp_path):
        """/metrics and /health are self-scrape/probe noise - excluded."""
        client = _client(tmp_path)
        try:
            assert client.get("/metrics").status_code == 200
            assert client.get("/health").status_code == 200
            body = client.get("/metrics").text  # itself excluded as well
            for line in body.splitlines():
                if line.startswith("cogniweb_http_requests_total{"):
                    assert 'path_template="/metrics"' not in line
                    assert 'path_template="/health"' not in line
        finally:
            client.close()

    # ---- usage/quota (per-tenant) vs rate limiting (LLM pacing) ----------

    def test_usage_rejection_counter_incremented(self, tmp_path):
        # token_budget=1: the first finished task (runner reports 100 tokens)
        # pushes the tenant over budget -> the next submission is refused
        # with reason="quota_exceeded" and must be counted.
        client = _client(tmp_path, tenant_token_budget=1)
        try:
            task_id = client.post("/task", json={"task": "burn budget"}).json()["task_id"]
            assert _wait_finished(client, task_id)
            resp = client.post("/task", json={"task": "should be rejected"})
            assert resp.status_code == 429
            assert resp.json()["detail"]["reason"] == "quota_exceeded"
            value = _metrics.USAGE_REJECTIONS.labels(
                tenant_id="default", reason="quota_exceeded"
            )._value.get()
            assert value == 1
        finally:
            client.close()

    def test_tenant_tokens_counter_mirrors_record_completion(self, tmp_path):
        client = _client(tmp_path)
        try:
            before = _metrics.TENANT_TOKENS.labels(tenant_id="default")._value.get()
            task_id = client.post("/task", json={"task": "tokens"}).json()["task_id"]
            assert _wait_finished(client, task_id)
            after = _metrics.TENANT_TOKENS.labels(tenant_id="default")._value.get()
            assert after == before + 100  # _runner() reports tokens=100
        finally:
            client.close()

    def test_rate_limit_wait_histogram_observed(self):
        before = _exposition_value("cogniweb_rate_limit_wait_seconds_count")
        _metrics.observe_rate_limit_wait(1.5)
        assert _exposition_value("cogniweb_rate_limit_wait_seconds_count") == before + 1
        assert _exposition_value("cogniweb_rate_limit_wait_seconds_sum") >= 1.5

    # ---- task steps histogram --------------------------------------------

    def test_task_steps_histogram_observed_per_finished_task(self, tmp_path):
        client = _client(tmp_path)
        try:
            before_success = _exposition_value(
                "cogniweb_task_steps_total_count", {"outcome": "success"}
            )
            task_id = client.post(
                "/task", json={"task": "steps", "tenant_id": "steps-tenant"}
            ).json()["task_id"]
            assert _wait_finished(client, task_id, tenant_id="steps-tenant")
            assert (
                _exposition_value("cogniweb_task_steps_total_count", {"outcome": "success"})
                == before_success + 1
            )
            assert _exposition_value("cogniweb_task_steps_total_sum") >= 1
        finally:
            client.close()

    # ---- tool dispatch (orchestrator._execute_action) ---------------------

    async def test_execute_action_records_tool_metrics(self, tmp_path):
        """End-to-end through _execute_action: one dispatch -> exactly one
        cogniweb_tool_calls_total increment + one duration observation."""
        from unittest.mock import AsyncMock, MagicMock

        from src.agent.orchestrator import AgentOrchestrator
        from src.core.models import ActionResult, AgentAction

        browser = MagicMock()
        browser.element_map = {}
        browser.navigate = AsyncMock(return_value=ActionResult(success=True, message="ok"))
        orchestrator = AgentOrchestrator(make_settings(tmp_path), browser, MagicMock())
        action = AgentAction(tool="navigate", args={"url": "https://example.com"})

        before = _metrics.TOOL_CALLS.labels(tool="navigate", outcome="success")._value.get()
        duration_before = _exposition_value(
            "cogniweb_tool_duration_seconds_count", {"tool": "navigate"}
        )

        result = await orchestrator._execute_action(action)
        assert result.success is True
        assert (
            _metrics.TOOL_CALLS.labels(tool="navigate", outcome="success")._value.get()
            == before + 1
        )
        assert (
            _exposition_value("cogniweb_tool_duration_seconds_count", {"tool": "navigate"})
            == duration_before + 1
        )

    async def test_execute_action_records_failure_outcome(self, tmp_path):
        from unittest.mock import MagicMock

        from src.agent.orchestrator import AgentOrchestrator
        from src.core.models import AgentAction

        orchestrator = AgentOrchestrator(make_settings(tmp_path), MagicMock(), MagicMock())
        before = _metrics.TOOL_CALLS.labels(tool="wait", outcome="failure")._value.get()
        # wait with a non-numeric 'seconds' passes the Pydantic schema but
        # fails in the dispatcher branch (InvalidType) - a deterministic
        # failure whose outcome is recorded by the dispatch-level metric.
        result = await orchestrator._execute_action(
            AgentAction(tool="wait", args={"seconds": "not-a-number"})
        )
        assert result.success is False
        assert _metrics.TOOL_CALLS.labels(tool="wait", outcome="failure")._value.get() == before + 1

    # ---- evaluator verdicts (ECE proxy, variant A) ------------------------

    def test_evaluator_verdict_metrics_observed(self):
        pass_before = _metrics.EVALUATOR_VERDICTS.labels(verdict="pass")._value.get()
        fail_before = _metrics.EVALUATOR_VERDICTS.labels(verdict="fail")._value.get()
        error_before = _metrics.EVALUATOR_VERDICTS.labels(verdict="error")._value.get()
        dur_before = _exposition_value("cogniweb_evaluator_verdict_duration_seconds_count")
        _metrics.observe_evaluator_verdict("pass", 0.4)
        _metrics.observe_evaluator_verdict("fail", 0.5)
        _metrics.observe_evaluator_verdict("error", 0.6)
        assert _metrics.EVALUATOR_VERDICTS.labels(verdict="pass")._value.get() == pass_before + 1
        assert _metrics.EVALUATOR_VERDICTS.labels(verdict="fail")._value.get() == fail_before + 1
        assert _metrics.EVALUATOR_VERDICTS.labels(verdict="error")._value.get() == error_before + 1
        assert _exposition_value("cogniweb_evaluator_verdict_duration_seconds_count") == (
            dur_before + 3
        )

    async def test_orchestrator_evaluator_records_verdicts(self, tmp_path):
        """_evaluate_completion maps: PASS->pass, FAIL->fail, crash AND
        unparsable->error (both return None - the metric disambiguates)."""
        from unittest.mock import AsyncMock, MagicMock

        from src.agent.orchestrator import AgentOrchestrator
        from src.core.models import AgentAction

        def make_orch(response=None, side_effect=None):
            llm = MagicMock()
            llm.generate_text = (
                AsyncMock(return_value=response)
                if side_effect is None
                else AsyncMock(side_effect=side_effect)
            )
            # rate_limit_seconds=0: the local (non-LLMService) pacing path
            # would otherwise sleep the default 15s between evaluator calls.
            settings = make_settings(tmp_path, rate_limit_seconds=0.0)
            return AgentOrchestrator(settings, MagicMock(), llm)

        action = AgentAction(tool="done", args={"summary": "did it"})

        error_before = _metrics.EVALUATOR_VERDICTS.labels(verdict="error")._value.get()
        orch = make_orch(response="looks fine but no VERDICT line here")
        assert await orch._evaluate_completion(action) is None
        assert _metrics.EVALUATOR_VERDICTS.labels(verdict="error")._value.get() == error_before + 1

        pass_before = _metrics.EVALUATOR_VERDICTS.labels(verdict="pass")._value.get()
        orch = make_orch(response="VERDICT:PASS")
        assert await orch._evaluate_completion(action) is None
        assert _metrics.EVALUATOR_VERDICTS.labels(verdict="pass")._value.get() == pass_before + 1

        fail_before = _metrics.EVALUATOR_VERDICTS.labels(verdict="fail")._value.get()
        orch = make_orch(response="VERDICT:FAIL - summary is empty")
        reason = await orch._evaluate_completion(action)
        assert reason
        assert _metrics.EVALUATOR_VERDICTS.labels(verdict="fail")._value.get() == fail_before + 1

        crash_before = _metrics.EVALUATOR_VERDICTS.labels(verdict="error")._value.get()
        orch = make_orch(side_effect=RuntimeError("boom"))
        assert await orch._evaluate_completion(action) is None
        assert _metrics.EVALUATOR_VERDICTS.labels(verdict="error")._value.get() == crash_before + 1

    # ---- browser action errors --------------------------------------------

    def test_browser_action_errors_observed_with_closed_error_type(self):
        before = _metrics.BROWSER_ACTION_ERRORS.labels(
            tool="navigate", error_type="timeout"
        )._value.get()
        _metrics.observe_browser_action_error("navigate", "timeout")
        assert (
            _metrics.BROWSER_ACTION_ERRORS.labels(
                tool="navigate", error_type="timeout"
            )._value.get()
            == before + 1
        )
        # the classifier output stays within the closed set
        from src.infrastructure.browser import _browser_error_type

        assert _browser_error_type(TimeoutError("t/o")) == "timeout"
        assert _browser_error_type(ValueError("something else")) == "other"

    # ---- LLM retries / failover -------------------------------------------

    def test_llm_retry_and_failover_counters(self):
        retry_before = _metrics.LLM_RETRIES.labels(provider="cloud")._value.get()
        failover_before = _metrics.LLM_FAILOVERS._value.get()
        _metrics.observe_llm_retry("cloud")
        _metrics.observe_llm_failover()
        assert _metrics.LLM_RETRIES.labels(provider="cloud")._value.get() == retry_before + 1
        assert _metrics.LLM_FAILOVERS._value.get() == failover_before + 1

    def test_llm_retry_hook_counts_and_never_raises(self, tmp_path):
        """The tenacity before_sleep hook: increments with a live service and
        swallows ANY internal exception (must not break the retry flow)."""
        from src.infrastructure.llm import LLMService, _observe_retry_before_sleep

        service = LLMService(make_settings(tmp_path))
        retry_before = _metrics.LLM_RETRIES.labels(provider="cloud")._value.get()

        class _State:
            args = (service,)
            kwargs = {}

        _observe_retry_before_sleep(_State())
        assert _metrics.LLM_RETRIES.labels(provider="cloud")._value.get() == retry_before + 1

        class _BadState:
            @property
            def args(self):
                raise RuntimeError("boom")

        _observe_retry_before_sleep(_BadState())  # must not raise
