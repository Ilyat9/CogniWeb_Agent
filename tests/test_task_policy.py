"""
Task intake policy (sanitization) tests: TaskPolicy rules, audit trail,
API-level rejection, and - critically - NO false positives on the kind of
legitimate task phrasings used across the project's own tests/docs.
"""

import json
import logging
import sys
from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.app import create_app  # noqa: E402
from src.config.settings import Settings  # noqa: E402
from src.core.models import TaskResult  # noqa: E402
from src.infrastructure.task_policy import (  # noqa: E402
    TaskPolicy,
    _parse_patterns,
)


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
        "task_audit_log_path": tmp_path / "logs" / "rejected_tasks.log",
    }
    base.update(overrides)
    return Settings(**base)


# ============================================================================
# TaskPolicy unit rules
# ============================================================================


class TestTaskPolicyRules:
    def test_accepts_normal_task(self):
        assert TaskPolicy().validate("Open hh.ru and find python vacancies") is None

    def test_empty_and_whitespace_rejected(self):
        assert TaskPolicy().validate("") == "empty_or_whitespace"
        assert TaskPolicy().validate("   \n\t  ") == "empty_or_whitespace"

    def test_too_long_rejected(self):
        policy = TaskPolicy()
        assert policy.validate("x" * 10001) == "too_long"
        # exactly at the limit passes
        assert policy.validate("x" * 10000) is None

    def test_custom_max_length(self):
        class Cfg:
            task_max_length = 5

        assert TaskPolicy(Cfg()).validate("abcdef") == "too_long"

    def test_control_characters_rejected(self):
        assert TaskPolicy().validate("do it\x00now") == "control_characters"
        # tab/newline are legitimate formatting and must pass
        assert TaskPolicy().validate("line one\nline two\tindented") is None

    def test_no_alphanumeric_rejected(self):
        assert TaskPolicy().validate("...!!!???---") == "no_alphanumeric_content"


# ============================================================================
# Optional content filter
# ============================================================================


class FilterCfg:
    enable_task_content_filter = True
    task_forbidden_patterns = ""


def cfg_with_patterns(patterns: str) -> type[FilterCfg]:
    return type("Cfg", (FilterCfg,), {"task_forbidden_patterns": patterns})


class TestContentFilter:
    def test_disabled_by_default(self):
        class Cfg:
            task_max_length = 10000
            enable_task_content_filter = False
            task_forbidden_patterns = "spam"

        # pattern present in text but filter is OFF -> accepted
        assert TaskPolicy(Cfg()).validate("send spam to everyone") is None

    def test_pattern_match_rejects(self):
        policy = TaskPolicy(cfg_with_patterns("bulk spam blast"))
        assert policy.validate("Please send a BULK SPAM BLAST for me") == "forbidden_pattern"
        assert policy.validate("Buy groceries") is None

    def test_multiple_patterns_newline_separated(self):
        policy = TaskPolicy(cfg_with_patterns("ddos\ncaptcha.?farm"))
        assert policy.validate("run a ddos against x") == "forbidden_pattern"
        assert policy.validate("captcha farming service") == "forbidden_pattern"
        assert policy.validate("solve my homework") is None

    def test_invalid_regex_skipped_not_fatal(self):
        policy = TaskPolicy(cfg_with_patterns("[unclosed\ngood-pattern-xyz"))
        # first pattern would raise re.error -> skipped with warning
        assert policy.validate("anything at all") is None
        assert policy.validate("contains good-pattern-xyz inside") == "forbidden_pattern"

    def test_parse_patterns(self):
        assert _parse_patterns("a\n b \n\n c\r\n") == ["a", "b", "c"]
        assert _parse_patterns("") == []


# ============================================================================
# No false positives on the project's own legitimate phrasings
# ============================================================================


class TestNoFalsePositives:
    @pytest.mark.parametrize(
        "task",
        [
            "Open hh.ru and find python developer vacancies in Moscow",
            "Go to google.com, search for 'weather', and report the forecast",
            "Log into the site with the given credentials and download the report",
            "Fill the form: name Ada Lovelace, email ada@example.com, submit",
            "Extract all product prices from the catalog page into context_data",
            "Найди на сайте вакансии python-разработчика и сохрани ссылки",
            "line one\nline two\tindented",
        ],
    )
    def test_legitimate_tasks_pass(self, task):
        assert TaskPolicy().validate(task) is None

    def test_legitimate_tasks_pass_with_filter_on_but_empty_patterns(self):
        # filter enabled, operator configured nothing -> everything passes
        policy = TaskPolicy(cfg_with_patterns(""))
        assert policy.validate("any legitimate task") is None


# ============================================================================
# Audit trail
# ============================================================================


class TestAuditTrail:
    def test_rejection_written_to_dedicated_jsonl(self, tmp_path, monkeypatch):
        # isolate the module-level handler guard per tmp_path
        import src.infrastructure.task_policy as tp

        audit_path = tmp_path / "logs" / "rejected.log"

        class Cfg:
            task_max_length = 10
            task_audit_log_path = audit_path

        monkeypatch.setattr(tp, "_audit_handler_attached_for", None)
        policy = TaskPolicy(Cfg())
        assert policy.validate("way too long task text here", tenant_id="acme") == "too_long"

        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "task_rejected"
        assert entry["rule"] == "too_long"
        assert entry["tenant_id"] == "acme"
        assert len(entry["preview"]) <= 200

    def test_acceptance_writes_nothing(self, tmp_path, monkeypatch):
        import src.infrastructure.task_policy as tp

        audit_path = tmp_path / "logs" / "rejected.log"

        class Cfg:
            task_audit_log_path = audit_path

        monkeypatch.setattr(tp, "_audit_handler_attached_for", None)
        TaskPolicy(Cfg()).validate("fine")
        assert not audit_path.exists()

    def test_unwritable_audit_path_does_not_break_validation(self, tmp_path, caplog, monkeypatch):
        # A read-only parent directory must degrade to logging, not raise.
        import src.infrastructure.task_policy as tp

        blocked = tmp_path / "file.txt" / "rejected.log"  # parent is a FILE

        class Cfg:
            task_audit_log_path = blocked

        monkeypatch.setattr(tp, "_audit_handler_attached_for", None)
        with caplog.at_level(logging.ERROR):
            assert TaskPolicy(Cfg()).validate("") == "empty_or_whitespace"


# ============================================================================
# API-level integration
# ============================================================================


class TestApiIntake:
    def _client(self, tmp_path, **overrides):
        settings = make_settings(tmp_path, **overrides)
        app = create_app(_ok_runner, settings=settings)
        client = fastapi_testclient.TestClient(app)
        client.__enter__()
        return client

    def test_garbage_rejected_400_with_rule(self, tmp_path):
        client = self._client(tmp_path)
        try:
            resp = client.post("/task", json={"task": "...!!!"})
            assert resp.status_code == 400
            assert resp.json()["detail"]["rule"] == "no_alphanumeric_content"
        finally:
            client.close()

    def test_too_long_rejected_400(self, tmp_path):
        client = self._client(tmp_path, task_max_length=10)
        try:
            resp = client.post("/task", json={"task": "a" * 11})
            assert resp.status_code == 400
            assert resp.json()["detail"]["rule"] == "too_long"
        finally:
            client.close()

    def test_filter_off_by_default_keeps_legacy_behavior(self, tmp_path):
        # With the filter OFF (default), a text matching a would-be pattern
        # is accepted - single-operator behavior unchanged.
        client = self._client(tmp_path)
        try:
            resp = client.post("/task", json={"task": "bulk spam blast please"})
            assert resp.status_code == 202
        finally:
            client.close()

    def test_filter_on_rejects_and_audits(self, tmp_path):
        audit_path = tmp_path / "audit" / "rejected.log"
        import src.infrastructure.task_policy as tp

        tp._audit_handler_attached_for = None  # re-attach for this tmp path
        try:
            client = self._client(
                tmp_path,
                enable_task_content_filter=True,
                task_forbidden_patterns="spam\\s+blast",
                task_audit_log_path=audit_path,
            )
            try:
                bad = client.post("/task", json={"task": "send a SPAM BLAST now"})
                assert bad.status_code == 400
                assert bad.json()["detail"]["error"] == "task_rejected"

                good = client.post("/task", json={"task": "totally fine task"})
                assert good.status_code == 202

                lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
                assert len(lines) == 1
                assert json.loads(lines[0])["rule"] == "forbidden_pattern"
            finally:
                client.close()
        finally:
            tp._audit_handler_attached_for = None


async def _ok_runner(task, starting_url):
    return TaskResult(success=True, summary="ok", steps_taken=0, total_duration_seconds=0.0)
