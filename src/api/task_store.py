"""SQLite-backed persistent store for API task records (aiosqlite).

Task 1 (persistence): previously the API kept every task record - state,
result, full step-event buffer - in a plain in-memory dict
(`app.state.tasks`). Any container restart or redeploy wiped the whole
history. This module moves the durable copy into a single-file SQLite
database while the API keeps its hot in-memory working set (see app.py
for the write-through arrangement and why).

Why SQLite + raw SQL, not an ORM/Alembic:
- Single operator, single process, single file: no server to run, no
  connection pool, no migration framework to babysit. `CREATE TABLE IF
  NOT EXISTS` plus INSERT OR REPLACE covers every need this tool has.
- aiosqlite runs each statement on its own thread, so awaits never block
  the event loop that also drives Playwright and the WebSocket fan-out.

Schema note: only fields that must SURVIVE a restart are persisted.
`subscribers` / `emit` / `on_step` are live pub/sub wiring for a running
task; they are meaningless after a restart and are rebuilt (or simply not
needed - hydrated tasks are always already finished) by the API layer.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id          TEXT PRIMARY KEY,
    state            TEXT NOT NULL,
    submitted_at     TEXT NOT NULL,
    task             TEXT NOT NULL,
    starting_url     TEXT,
    result           TEXT,
    steps            TEXT NOT NULL DEFAULT '[]',
    stop_requested   INTEGER NOT NULL DEFAULT 0,
    latest_screenshot TEXT,
    current_step     INTEGER,
    last_tool        TEXT,
    last_success     INTEGER,
    updated_at       REAL NOT NULL
);
"""


class TaskStore:
    """Async SQLite store for task records. One connection, serialized
    writes (aiosqlite executes statements sequentially on its worker
    thread), so concurrent save() callers from the event loop are safe."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._db: Any | None = None

    async def initialize(self) -> None:
        """Open the database and create the table if it does not exist."""
        import aiosqlite  # noqa: PLC0415 - lazy: optional dependency

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        await self._db.execute(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            try:
                await self._db.close()
            except Exception as e:
                logger.debug(f"TaskStore close failed (non-fatal): {e}")
            finally:
                self._db = None

    def _require_db(self) -> Any:
        if self._db is None:
            raise RuntimeError("TaskStore.initialize() was not called")
        return self._db

    @staticmethod
    def _to_row(record: dict[str, Any]) -> tuple:
        result = record.get("result")
        steps = record.get("steps") or []
        return (
            record["task_id"],
            record.get("state", "queued"),
            record.get("submitted_at", ""),
            record.get("task", ""),
            record.get("starting_url"),
            json.dumps(result, default=str) if result is not None else None,
            json.dumps(steps, default=str),
            1 if record.get("stop_requested") else 0,
            record.get("latest_screenshot"),
            record.get("current_step"),
            record.get("last_tool"),
            None if record.get("last_success") is None else int(record["last_success"]),
            time.time(),
        )

    @staticmethod
    def _from_row(row: tuple) -> dict[str, Any]:
        (
            task_id,
            state,
            submitted_at,
            task,
            starting_url,
            result_json,
            steps_json,
            stop_requested,
            latest_screenshot,
            current_step,
            last_tool,
            last_success,
        ) = row
        try:
            result = json.loads(result_json) if result_json else None
        except (TypeError, ValueError):
            logger.warning(f"Corrupt result JSON for task {task_id}; dropped")
            result = None
        try:
            steps = json.loads(steps_json) if steps_json else []
        except (TypeError, ValueError):
            logger.warning(f"Corrupt steps JSON for task {task_id}; reset to empty")
            steps = []
        return {
            "task_id": task_id,
            "state": state,
            "submitted_at": submitted_at,
            "task": task,
            "starting_url": starting_url,
            "result": result,
            "steps": steps,
            "stop_requested": bool(stop_requested),
            "latest_screenshot": latest_screenshot,
            "current_step": current_step,
            "last_tool": last_tool,
            "last_success": None if last_success is None else bool(last_success),
            # Live-only keys are NOT persisted; give hydrated records the
            # shape the API endpoints expect (empty pub/sub state).
            "subscribers": [],
        }

    async def save(self, record: dict[str, Any]) -> None:
        """Upsert one task record (write-through from the API's hot cache)."""
        db = self._require_db()
        await db.execute(
            """
            INSERT OR REPLACE INTO tasks (
                task_id, state, submitted_at, task, starting_url, result,
                steps, stop_requested, latest_screenshot, current_step,
                last_tool, last_success, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._to_row(record),
        )
        await db.commit()

    async def delete(self, task_id: str) -> None:
        db = self._require_db()
        await db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        await db.commit()

    # Columns consumed by _from_row, in order. Explicit list instead of
    # SELECT * so adding a future metadata column (e.g. updated_at) does
    # not silently break row unpacking.
    _ROW_COLUMNS = (
        "task_id, state, submitted_at, task, starting_url, result, steps, "
        "stop_requested, latest_screenshot, current_step, last_tool, last_success"
    )

    async def load_all(self) -> list[dict[str, Any]]:
        """Load every persisted record (used once at startup to hydrate the
        API's in-memory working set)."""
        db = self._require_db()
        cursor = await db.execute(
            f"SELECT {self._ROW_COLUMNS} FROM tasks ORDER BY submitted_at ASC"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._from_row(row) for row in rows]