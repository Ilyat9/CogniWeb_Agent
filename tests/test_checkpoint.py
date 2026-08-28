"""
Tests for checkpoint/resume/rollback (AgentCheckpoint, AgentOrchestrator.
_save_checkpoint/_restore_from_checkpoint/resume(), and the bounded
automatic-rollback path).

All tests use mocks - no real API calls, no browser launches. Follows the
fixture patterns already established in tests/test_phase_features.py
(make_settings/make_browser/action/patch_dom_empty).
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent.checkpoint import AgentCheckpoint
from src.agent.orchestrator import AgentOrchestrator
from src.config.settings import Settings
from src.core.models import ActionResult, AgentAction
from src.utils.dom import DOMProcessor


def make_settings(tmp_path, **overrides):
    base = {
        "api_key": "sk-test-key-not-real",
        "api_base_url": "https://api.test.com/v1",
        "model_name": "test-provider/test-model",
        "user_data_dir": tmp_path / "browser_data",
        "screenshot_dir": tmp_path / "screenshots",
        "checkpoint_dir": tmp_path / "checkpoints",
        "reports_dir": tmp_path / "reports",
        "upload_allowed_dir": tmp_path / "uploads",
        "agent_step_delay": 0.0,
        "rate_limit_seconds": 0.0,
        "enable_context_compaction": False,
        # Keep the pre-existing loop detector out of the way - these tests
        # exercise the NEW bounded-rollback mechanism, not the older
        # same-action-repeated circuit breaker.
        "loop_detection_window": 10,
        "max_identical_states": 10,
    }
    base.update(overrides)
    return Settings(**base)


def make_browser():
    browser = AsyncMock()
    browser.element_map = {}
    browser.navigate = AsyncMock(return_value=ActionResult(success=True, message="Navigated"))
    browser.get_current_url = AsyncMock(return_value="https://example.com")
    browser.get_page_title = AsyncMock(return_value="Test Page")
    browser.detect_captcha = AsyncMock(return_value=False)
    browser.page = AsyncMock()
    return browser


def action(tool, **args):
    return AgentAction(thought="t", tool=tool, args=args)


def patch_dom_with_one_element(monkeypatch):
    """Always expose one clickable element (id=1) so click_element keeps
    resolving to the same, valid target across steps."""

    async def fake(self, page):
        return ([{"id": 1, "tag": "a", "text": "target", "selector": "[data-agent-id='1']"}], None)

    monkeypatch.setattr(DOMProcessor, "get_interactive_elements", fake)


# ============================================================================
# AgentCheckpoint round-trip
# ============================================================================


class TestAgentCheckpointRoundTrip:
    def test_write_read_round_trip(self, tmp_path):
        checkpoint = AgentCheckpoint(
            task_id="task-123",
            task="Do the thing",
            step=4,
            current_url="https://example.com/page",
            starting_url="https://example.com",
            context_data={"price": "10"},
            conversation_history=[{"role": "system", "content": "sys"}],
        )
        path = tmp_path / "checkpoint.json"
        checkpoint.write(path)

        loaded = AgentCheckpoint.read(path)

        assert loaded.task_id == "task-123"
        assert loaded.task == "Do the thing"
        assert loaded.step == 4
        assert loaded.current_url == "https://example.com/page"
        assert loaded.starting_url == "https://example.com"
        assert loaded.context_data == {"price": "10"}
        assert loaded.conversation_history == [{"role": "system", "content": "sys"}]
        assert loaded.pending_action is None


# ============================================================================
# Rotation
# ============================================================================


class TestCheckpointRotation:
    def test_only_keeps_last_n_step_checkpoints(self, tmp_path):
        settings = make_settings(tmp_path, checkpoint_rotation_keep=3)
        orch = AgentOrchestrator(settings, make_browser(), AsyncMock())
        orch.task = "task"

        for step in range(1, 6):  # 5 steps, keep=3
            orch._save_checkpoint(step, "https://example.com", None)

        on_disk = sorted(settings.checkpoint_dir.glob("step_*.json"))
        assert len(on_disk) == 3
        remaining_steps = sorted(s for s, _ in orch._step_checkpoints)
        assert remaining_steps == [3, 4, 5]


# ============================================================================
# resume(): continues from checkpoint.step + 1, not from scratch
# ============================================================================


class TestResume:
    @pytest.mark.asyncio
    async def test_resume_continues_from_correct_step_and_navigates(self, tmp_path):
        settings = make_settings(tmp_path)
        browser = make_browser()
        llm = AsyncMock()
        orch = AgentOrchestrator(settings, browser, llm)

        checkpoint = AgentCheckpoint(
            task="Do the thing",
            step=2,
            current_url="https://example.com/resumed-page",
            context_data={"found": "yes"},
            conversation_history=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "obs"},
            ],
        )
        checkpoint_path = tmp_path / "manual_checkpoint.json"
        checkpoint.write(checkpoint_path)

        # Only ONE more LLM call should happen (step 3): the loop must not
        # replay steps 1-2 nor start step numbering over from 1.
        llm.generate_action = AsyncMock(return_value=action("done", summary="finished"))

        result = await orch.resume(str(checkpoint_path))

        assert result.success is True
        assert result.steps_taken == 3
        assert llm.generate_action.await_count == 1
        # Restoring state must also restore the BROWSER, not just text
        # state - otherwise the model's belief and the real page diverge.
        browser.navigate.assert_awaited_once_with("https://example.com/resumed-page")
        assert orch.context_data == {"found": "yes"}


# ============================================================================
# Automatic rollback: bounded, triggered only by a REPEATED identical
# tool-error pattern.
# ============================================================================


class TestAutoRollback:
    @pytest.mark.asyncio
    async def test_rollback_to_step_n_minus_2_on_repeated_error(self, tmp_path, monkeypatch):
        patch_dom_with_one_element(monkeypatch)
        settings = make_settings(tmp_path, max_steps=10, max_auto_rollbacks=2)
        browser = make_browser()
        browser.click_element_safe = AsyncMock(
            return_value=ActionResult(success=False, message="click failed", error="ClickFailed")
        )
        llm = AsyncMock()
        # 3 failing clicks (steps 1-3, rollback fires on step 3) then done.
        llm.generate_action = AsyncMock(
            side_effect=[
                action("click_element", element_id=1),
                action("click_element", element_id=1),
                action("click_element", element_id=1),
                action("done", summary="ok"),
            ]
        )
        orch = AgentOrchestrator(settings, browser, llm)

        result = await asyncio.wait_for(orch.run("Click the thing"), timeout=10)

        assert orch._auto_rollbacks_used == 1
        assert result.success is True
        # _restore_from_checkpoint() re-navigates the browser - the only
        # other caller of browser.navigate() in this run is the (absent)
        # starting_url, so any navigate call here proves rollback restored
        # the browser too, not just conversation_history/context_data.
        browser.navigate.assert_awaited()

    @pytest.mark.asyncio
    async def test_max_auto_rollbacks_ends_run_instead_of_looping_forever(
        self, tmp_path, monkeypatch
    ):
        patch_dom_with_one_element(monkeypatch)
        settings = make_settings(tmp_path, max_steps=6, max_auto_rollbacks=1)
        browser = make_browser()
        browser.click_element_safe = AsyncMock(
            return_value=ActionResult(success=False, message="click failed", error="ClickFailed")
        )
        llm = AsyncMock()
        llm.generate_action = AsyncMock(return_value=action("click_element", element_id=1))
        orch = AgentOrchestrator(settings, browser, llm)

        # Test-level timeout: a bug that turns bounded rollback into an
        # infinite loop must fail this test instead of hanging CI forever.
        result = await asyncio.wait_for(orch.run("Click the thing"), timeout=10)

        # The rollback budget itself must never be exceeded - whether the
        # run ultimately ends via MaxStepsExceeded or the pre-existing
        # same-action loop breaker is a separate, orthogonal safety net.
        assert orch._auto_rollbacks_used <= 1
        assert result.success is False
        assert result.error in ("MaxStepsExceeded", "LoopDetected")


# ============================================================================
# Human-in-the-loop confirmation pause (settings.require_confirmation_for)
# ============================================================================


class TestConfirmationGate:
    @pytest.mark.asyncio
    async def test_pauses_before_listed_tool_and_resumes_on_confirm(self, tmp_path, monkeypatch):
        patch_dom_with_one_element(monkeypatch)
        settings = make_settings(tmp_path, require_confirmation_for=["click_element"])
        browser = make_browser()
        browser.click_element_safe = AsyncMock(
            return_value=ActionResult(success=True, message="Clicked")
        )
        llm = AsyncMock()
        llm.generate_action = AsyncMock(return_value=action("click_element", element_id=1))
        orch = AgentOrchestrator(settings, browser, llm)

        paused = await orch.run("Click the risky button")

        assert paused.success is False
        assert paused.error == "AwaitingConfirmation"
        browser.click_element_safe.assert_not_awaited()
        confirm_files = list(settings.checkpoint_dir.glob("confirm_*.json"))
        assert len(confirm_files) == 1

        llm.generate_action = AsyncMock(return_value=action("done", summary="done"))
        result = await orch.resume(str(confirm_files[0]), confirm=True)

        assert result.success is True
        browser.click_element_safe.assert_awaited_once()
