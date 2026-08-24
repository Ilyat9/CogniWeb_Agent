"""
Per-tenant usage accounting and rate limiting (API service mode).

In-memory by design: the scale is one process serving a handful of known
tenants, not a multi-instance fleet. A restart resets the sliding window -
the honest cost of avoiding Redis for a counter; the durable task history
lives in SQLite (task_store) and usage totals are re-derivable from it if
that ever matters.

Three limit kinds, all per tenant:
1. Concurrent running tasks (counted from live task records by the API -
   no drift when a worker crashes mid-task).
2. Sliding-window submission count (e.g. max N tasks per hour).
3. OPTIONAL hard token budget (disabled by default): once a tenant's
   cumulative tokens cross the budget, new submissions are refused. The
   budget is process-lifetime (in-memory), NOT calendar-monthly - that
   would require persistence and is deliberately out of scope here.
"""

import math
import time
from collections import deque


class UsageTracker:
    def __init__(
        self,
        max_concurrent_per_tenant: int = 2,
        tasks_per_hour: int = 60,
        window_seconds: float = 3600.0,
        token_budget: int = 0,
        cost_per_1k_tokens: float = 0.0,
    ):
        self.max_concurrent_per_tenant = max(0, int(max_concurrent_per_tenant))
        self.tasks_per_hour = max(0, int(tasks_per_hour))
        self.window_seconds = float(window_seconds)
        self.token_budget = max(0, int(token_budget))  # 0 = disabled
        self.cost_per_1k_tokens = max(0.0, float(cost_per_1k_tokens))

        # tenant -> timestamps of accepted submissions inside the window
        self._window: dict[str, deque[float]] = {}
        # tenant -> lifetime aggregates (never decay)
        self._totals: dict[str, dict[str, float]] = {}

    # ---- admission -----------------------------------------------------

    def check_submission(
        self, tenant_id: str, running_tasks: int, now: float | None = None
    ) -> tuple[bool, str | None, int]:
        """Return (allowed, reason_code, retry_after_seconds). reason codes:
        'concurrent_limit' | 'hourly_limit' | 'quota_exceeded'. Pure check -
        call record_submission() only after the task is actually accepted,
        so rejected requests never consume quota."""
        now = time.monotonic() if now is None else now

        if (
            self.token_budget > 0
            and self._totals.get(tenant_id, {}).get("tokens", 0.0) >= self.token_budget
        ):
            return False, "quota_exceeded", 0

        if self.max_concurrent_per_tenant > 0 and running_tasks >= self.max_concurrent_per_tenant:
            # Unknown retry time - depends on the running task's duration.
            return False, "concurrent_limit", 30

        if self.tasks_per_hour > 0:
            window = self._window.get(tenant_id, deque())
            cutoff = now - self.window_seconds
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self.tasks_per_hour:
                oldest = window[0]
                retry_after = max(1, math.ceil(oldest + self.window_seconds - now))
                return False, "hourly_limit", retry_after
        return True, None, 0

    def record_submission(self, tenant_id: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._window.setdefault(tenant_id, deque()).append(now)
        totals = self._totals.setdefault(tenant_id, {"tasks": 0.0, "tokens": 0.0})
        totals["tasks"] += 1

    def _prune_window(self, tenant_id: str, now: float) -> None:
        window = self._window.get(tenant_id)
        if not window:
            return
        cutoff = now - self.window_seconds
        while window and window[0] <= cutoff:
            window.popleft()

    # ---- accounting ------------------------------------------------------

    def record_completion(self, tenant_id: str, tokens_used: int | None) -> None:
        """Called by the dispatcher when a task finishes; tokens come from
        TaskResult.tokens_used (None = provider reported nothing)."""
        if tokens_used:
            totals = self._totals.setdefault(tenant_id, {"tasks": 0.0, "tokens": 0.0})
            totals["tokens"] += int(tokens_used)

    def estimated_cost_usd(self, tenant_id: str) -> float:
        tokens = self._totals.get(tenant_id, {}).get("tokens", 0.0)
        return round(tokens / 1000.0 * self.cost_per_1k_tokens, 6)

    # ---- reporting -------------------------------------------------------

    def snapshot(self, tenant_id: str, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        self._prune_window(tenant_id, now)
        totals = self._totals.get(tenant_id, {"tasks": 0.0, "tokens": 0.0})
        tokens = int(totals["tokens"])
        budget_info: dict = {"limit": self.token_budget, "exceeded": False}
        if self.token_budget > 0:
            budget_info.update(
                {
                    "used": tokens,
                    "remaining": max(0, self.token_budget - tokens),
                    "exceeded": tokens >= self.token_budget,
                }
            )
        return {
            "tenant_id": tenant_id,
            "window_seconds": self.window_seconds,
            "tasks_in_window": len(self._window.get(tenant_id, ())),
            "total_tasks": int(totals["tasks"]),
            "total_tokens": tokens,
            "estimated_cost_usd": self.estimated_cost_usd(tenant_id),
            "limits": {
                "max_concurrent_tasks": self.max_concurrent_per_tenant,
                "tasks_per_hour": self.tasks_per_hour or None,
                "token_budget": budget_info,
            },
        }
