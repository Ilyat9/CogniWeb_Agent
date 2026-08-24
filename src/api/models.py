"""Pydantic-схемы API (TaskSubmission / TaskStatus).

Вынесено из src/api/app.py без изменения логики - чистое перемещение
кода (структурный рефакторинг); app.py реэкспортирует эти имена,
так что существующие импорты продолжают работать.
"""

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Multi-tenancy: tenant_id is an IDENTIFIER, not an identity claim. The
# regex keeps it a safe path segment (it becomes
# {USER_DATA_DIR}/tenants/{tenant_id}) and a safe log/label value.
_SAFE_TENANT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_TENANT_ID = "default"


class TaskSubmission(BaseModel):
    task: str = Field(min_length=1)
    starting_url: str | None = None
    # Multi-tenancy: optional, defaults to the historical single-tenant id
    # so existing clients/UI/tests are byte-compatible. There is NO
    # authentication behind it on purpose (documented scope decision): any
    # client may claim any tenant_id; proving who may claim what is a
    # separate auth problem, not solved here.
    tenant_id: str = Field(
        default=DEFAULT_TENANT_ID,
        min_length=1,
        max_length=64,
        description="Tenant identifier; isolates browser profile, queue "
        "scheduling and usage accounting per tenant.",
    )

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, v: str) -> str:
        if not _SAFE_TENANT_ID.match(v):
            raise ValueError("tenant_id must match ^[A-Za-z0-9_-]{1,64}$")
        return v


class TaskStatus(BaseModel):
    task_id: str
    state: str  # queued | running | finished
    submitted_at: str
    result: dict[str, Any] | None = None
    # Hardening supplement (on_step hook): live progress DURING a run -
    # non-None after the loop's first executed step, None while queued.
    current_step: int | None = None
    last_tool: str | None = None
    # Multi-tenancy: echoed so a client can confirm which tenant bucket
    # its task landed in.
    tenant_id: str = DEFAULT_TENANT_ID
