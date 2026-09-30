"""Immutable, strict data contracts; hashes bind claims, not measured reality."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Annotated, Literal, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Label = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Positive = Annotated[int, Field(strict=True, gt=0, le=2**53 - 1)]
Tick = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, revalidate_instances="always")


class Limits(Contract):
    """Explicit contract values; only the memory split has human design approval."""

    guest_memory_bytes: Positive
    fixture_memory_bytes: Positive
    vcpus: Positive
    wall_seconds: Positive
    output_bytes: Positive
    approval_ttl_seconds: Positive

    @model_validator(mode="after")
    def approved_memory_split(self):
        if (self.guest_memory_bytes, self.fixture_memory_bytes) != (4 * 1024**3, 512 * 1024**2):
            raise ValueError("Only the approved 4 GiB guest / 512 MiB fixture split is supported")
        return self


class Manifest(Contract):
    schema_version: Literal["phase2a-code-v1"]
    run_id: UUID
    process_epoch: UUID
    design_digest: Digest
    fixture_digest: Digest
    base_image_digest: Digest
    runtime_digest: Digest
    input_image_digest: Digest
    isolation_config_digest: Digest
    policy_digest: Digest
    vm_id: UUID
    limits: Limits
    output_schema: Label
    operation: Literal["validate_fixture_contract"]
    execution: Literal["unavailable"]

    def digest(self) -> str:
        # Revalidation also rejects model_copy/model_construct bypasses.
        validated = Manifest.model_validate(self)
        wire = json.dumps(validated.model_dump(mode="json"), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return sha256(wire.encode("utf-8")).hexdigest()


class MockIdentity(Contract):
    kind: Literal["mock"]
    label: Label


class MockApproval(Contract):
    schema_version: Literal["phase2a-mock-approval-v1"]
    approval_id: UUID
    run_id: UUID
    process_epoch: UUID
    manifest_digest: Digest
    identity: MockIdentity
    decision: Literal["approve", "deny"]
    issued_at: AwareDatetime
    issued_monotonic_ms: Tick
    expires_at: AwareDatetime

    @field_validator("issued_at", "expires_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def positive_lifetime(self):
        if self.expires_at <= self.issued_at:
            raise ValueError("Approval expiry must follow issuance")
        return self

    @property
    def confers_authority(self) -> bool:
        return False


class ClockReading(Contract):
    """Caller-supplied simulation clock, never an authenticated external clock."""

    wall_time: AwareDatetime
    monotonic_ms: Tick

    @field_validator("wall_time")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class Context(Contract):
    process_epoch: UUID
    origin: ClockReading


Reason = Literal[
    "none", "mock_denial", "epoch_mismatch", "run_mismatch", "manifest_mismatch",
    "clock_reversal", "stale_issuance", "future_issuance", "expired", "ttl_exceeded", "replay",
    "unsupported_execution", "internal_failure",
]


class Step(Contract):
    kind: Literal["assessment_started", "approval_denied", "simulation_consumed",
                  "execution_unavailable", "attempt_failed"]
    reason: Reason


class Attempt(Contract):
    attempt_id: UUID
    manifest: Manifest
    approval: MockApproval
    clock: ClockReading
    steps: tuple[Step, ...]


class Evidence(Contract):
    schema_version: Literal["phase2a-code-evidence-v1"]
    context: Context
    attempts: tuple[Attempt, ...]
    recording_failed: bool


T = TypeVar("T", bound=Contract)


def parse_contract(model: type[T], text: str) -> T:
    """Reject ambiguous JSON before strict typed decoding; no file IO or repair."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def bad_constant(value):
        raise ValueError(f"Non-finite JSON literal: {value}")

    raw = json.loads(text, object_pairs_hook=pairs, parse_constant=bad_constant)
    return model.model_validate_json(json.dumps(raw, allow_nan=False), strict=True)
