"""
Task intake policy: basic sanity/abuse checks BEFORE a task enters the
execution queue (src/api/app.py::submit_task).

Scope (deliberate):
- Length bounds, empty/garbage input, control characters - always on.
- OPTIONAL content filter (ENABLE_TASK_CONTENT_FILTER, default OFF) against
  operator-configured forbidden regex patterns (TASK_FORBIDDEN_PATTERNS).
  This is NOT content moderation: a plain regex list catches only the most
  obvious abuse phrasings the operator chooses to name. It cannot classify
  intent, and it is trivially bypassed by paraphrasing. For a genuinely
  public deployment you need a smarter layer (classification and/or human
  review) - see README.md / ARCHITECTURE.md notes.

Every rejection is written to a DEDICATED audit trail (JSON lines, one file,
separate from agent.log / uvicorn access logs) so an operator can review what
was filtered without grepping mixed logs: see TaskPolicy._audit.

Why not an extra LLM moderation call: it would add cost and latency to EVERY
submission to defend against a threat model (obviously abusive phrasings)
that a static filter already covers; the honest limitation is documented
instead of hidden behind an expensive half-measure.
"""

import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Rejections go here AND to the dedicated audit file. A dedicated named
# logger (not this module's logger) lets operators route the JSONL stream
# to its own destination via logging config if they prefer that over the
# built-in file handler.
AUDIT_LOGGER_NAME = "cogniweb.audit.task_policy"

# Module-level singleton guard: logging.FileHandler would happily append a
# second handler to the same logger on every create_app() call (tests build
# many apps), duplicating every audit line.
_audit_handler_attached_for: str | None = None


def _parse_patterns(raw: str) -> list[str]:
    """Split the settings string into individual regex patterns. Both
    newlines and blank entries are handled; strip whitespace. ('|' as
    alternation INSIDE a pattern still works - only whole-line separators
    are split.)"""
    return [p.strip() for p in raw.replace("\r", "").split("\n") if p.strip()]


def _ensure_audit_file_handler(audit_logger: logging.Logger, path: Path) -> None:
    global _audit_handler_attached_for
    resolved = str(path.resolve())
    if _audit_handler_attached_for == resolved:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))  # lines are already JSON
        audit_logger.addHandler(handler)
        audit_logger.setLevel(logging.INFO)
        audit_logger.propagate = False  # keep JSONL out of agent.log
        _audit_handler_attached_for = resolved
    except OSError as e:
        # Auditing must never block the API: fall back to a null handler.
        logger.error(f"Cannot open task-policy audit log {path}: {e}")
        audit_logger.addHandler(logging.NullHandler())
        _audit_handler_attached_for = resolved


class TaskPolicy:
    """Stateless validator; all knobs come from Settings (with the same
    defaults for settings-less test wiring via getattr fallbacks)."""

    def __init__(self, settings: Any | None = None):
        self._settings = settings

    def _setting(self, name: str, default: Any) -> Any:
        if self._settings is None:
            return default
        return getattr(self._settings, name, default)

    @property
    def audit_log_path(self) -> Path:
        return Path(self._setting("task_audit_log_path", Path("./logs/rejected_tasks.log")))

    def validate(self, task_text: str, tenant_id: str = "default") -> str | None:
        """Return None when the task text is acceptable, otherwise a short
        machine-readable rejection rule name. Writes an audit record for
        every rejection. Never raises."""
        reason = self._check(task_text)
        if reason is not None:
            self._audit(task_text, reason, tenant_id)
        return reason

    def _check(self, task_text: str) -> str | None:
        # 1. Empty / whitespace-only (pydantic min_length=1 already blocks
        #    truly empty strings; "   \n\t " passes pydantic but is garbage).
        if not task_text or not task_text.strip():
            return "empty_or_whitespace"

        # 2. Hard upper bound: a multi-megabyte "task" is either a mistake
        #    or an abuse vector (memory, LLM token burn on the first step).
        max_length = int(self._setting("task_max_length", 10000))
        if max_length > 0 and len(task_text) > max_length:
            return "too_long"

        # 3. Control characters (excluding \t \n \r): clipboard dumps and
        #    binary paste junk. Not a security control - input hygiene.
        if any(unicodedata.category(ch) == "Cc" and ch not in "\t\n\r" for ch in task_text):
            return "control_characters"

        # 4. Must contain at least one alphanumeric character anywhere -
        #    rejects pure-punctuation noise ("...", "!!!!") that would only
        #    burn an LLM call before failing.
        if not any(ch.isalnum() for ch in task_text):
            return "no_alphanumeric_content"

        # 5. OPTIONAL content filter - off by default so single-operator
        #    behavior is unchanged. Patterns come entirely from settings
        #    (nothing hardcoded): newline-separated regular expressions,
        #    matched case-insensitively against the raw task text.
        if bool(self._setting("enable_task_content_filter", False)):
            raw_patterns = str(self._setting("task_forbidden_patterns", "") or "")
            for pattern in _parse_patterns(raw_patterns):
                try:
                    if re.search(pattern, task_text, re.IGNORECASE):
                        return "forbidden_pattern"
                except re.error:
                    # A broken operator-supplied regex must not take down
                    # the intake path; skip it loudly instead.
                    logger.warning(f"TASK_FORBIDDEN_PATTERNS: invalid regex skipped: {pattern!r}")
        return None

    def _audit(self, task_text: str, reason: str, tenant_id: str) -> None:
        """Structured, dedicated audit trail for rejected submissions."""
        entry = {
            "ts": datetime.now().isoformat(),
            "event": "task_rejected",
            "rule": reason,
            "tenant_id": tenant_id,
            # Preview only: rejected text is potentially abusive - the
            # audit file must not become a store of full malicious payloads.
            "preview": task_text[:200],
            "length": len(task_text),
        }
        audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
        _ensure_audit_file_handler(audit_logger, self.audit_log_path)
        audit_logger.info(json.dumps(entry, ensure_ascii=False))
        # One line in the regular log too (without payload), so filtering
        # activity is visible in agent.log without reading the audit file.
        logger.warning(f"Task rejected by intake policy: rule={reason} tenant_id={tenant_id}")
