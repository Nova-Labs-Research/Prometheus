import json
import os
from pathlib import Path

from prometheus_lab.audit.models import AuditEvent


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"Nonstandard JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


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
                    # Reject ambiguous JSON before validation can discard duplicate keys.
                    json.loads(line, object_pairs_hook=_unique_object, parse_constant=_reject_nonfinite)
                    entries.append(AuditEvent.model_validate_json(line, strict=True))
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
