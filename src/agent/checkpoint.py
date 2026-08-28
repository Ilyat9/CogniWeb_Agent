"""
AgentCheckpoint - the persisted-state structure behind checkpoint/resume/
rollback.

This generalizes the checkpoint shape that already existed for a single
scenario (captcha waits, see AgentOrchestrator._save_captcha_checkpoint)
into one Pydantic model reused for every step-level checkpoint the
orchestrator writes. Keeping it a real model (not a bare dict, as before)
gives round-trip validation for free and one place to extend the shape.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AgentCheckpoint(BaseModel):
    """Enough state to resume the ReAct loop from a specific step, or to
    restore a previous step's state for an automatic rollback."""

    task_id: str | None = Field(
        default=None,
        description="Correlates this checkpoint with the API task record "
        "it belongs to, if any (CLI-only runs have no task_id).",
    )
    task: str = Field(..., description="Original task text.")
    step: int = Field(..., ge=0, description="Last step fully executed.")
    current_url: str | None = Field(default=None, description="Browser URL at save time.")
    starting_url: str | None = Field(
        default=None, description="The run's original starting_url argument."
    )
    context_data: dict[str, Any] = Field(default_factory=dict)
    conversation_history: list[dict[str, Any]] = Field(default_factory=list)
    pending_action: dict[str, Any] | None = Field(
        default=None,
        description="Set only for a human-in-the-loop confirmation pause "
        "(see settings.require_confirmation_for): the action that was "
        "withheld pending confirmation, to be executed first on resume.",
    )
    created_at: datetime = Field(default_factory=datetime.now)

    def write(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def read(cls, path: Path) -> "AgentCheckpoint":
        return cls.model_validate_json(path.read_text())
