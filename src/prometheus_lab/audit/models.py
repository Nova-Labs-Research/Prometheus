from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


EventType = Literal[
    "loop_started", "proposal_generated", "constraints_checked", "approval_decided",
    "execution_started", "execution_completed", "loop_finished", "loop_failed",
]


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    run_id: UUID
    event_type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
