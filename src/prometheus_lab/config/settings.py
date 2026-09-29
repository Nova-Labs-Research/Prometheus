from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    audit_log: Path = Path("artifacts/audit/events.jsonl")
    max_files: int = Field(default=3, ge=1)
    max_content_bytes: int = Field(default=16_384, ge=1)

    @property
    def audit_log_path(self) -> Path:
        """Expose the audit path without changing the existing settings field."""
        return self.audit_log


def load_settings() -> Settings:
    """Load the MVP defaults; relative paths use the current working directory."""
    return Settings()


# Deliberately not supplied by the proposing agent.
TARGET_PREFIX = "experiments/target_repo/"
