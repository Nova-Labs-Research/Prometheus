from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class FileChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    new_content: str


class CodeChangeProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    kind: Literal["code_change"] = "code_change"
    summary: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=4000)
    changes: tuple[FileChange, ...] = Field(min_length=1)

    def digest(self) -> str:
        """Bind a decision to the entire serialized proposal, including metadata."""
        serialized = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return sha256(serialized.encode("utf-8")).hexdigest()
