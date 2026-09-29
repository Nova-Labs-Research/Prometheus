import os
from pathlib import Path

from prometheus_lab.audit.models import AuditEvent


class JsonlAuditStore:
    """Single-writer local store. No update/delete API or tamper resistance."""

    def __init__(self, path: Path):
        self.path = path

    def read_all(self) -> list[AuditEvent]:
        """Read in append order, rejecting malformed records without altering them."""
        entries: list[AuditEvent] = []
        with self.path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    entries.append(AuditEvent.model_validate_json(line))
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid audit entry at line {line_number}; log left unchanged."
                    ) from exc
        return entries

    def append(self, event: AuditEvent) -> None:
        line = event.model_dump_json() + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
