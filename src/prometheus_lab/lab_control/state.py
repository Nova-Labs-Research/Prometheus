"""Process-local simulation state; no persistence, credentials or OS authority."""

from dataclasses import dataclass
from datetime import timedelta
from threading import Lock
from typing import Literal
from uuid import UUID

from .boundary import VMOperationsUnavailable, request_vm_operation
from .contracts import Attempt, ClockReading, Context, Evidence, Manifest, MockApproval, Reason, Step


@dataclass(frozen=True)
class Assessment:
    attempt: Attempt
    status: Literal["denied", "blocked"]

    @property
    def confers_authority(self) -> bool:
        return False

    @property
    def valid_completion(self) -> bool:
        return False


class LedgerUnavailable(RuntimeError):
    pass


def clock_reversed(clock: ClockReading, previous: ClockReading) -> bool:
    return clock.wall_time < previous.wall_time or clock.monotonic_ms < previous.monotonic_ms


def denial_reason(manifest: Manifest, approval: MockApproval, clock: ClockReading,
                  context: Context, previous: ClockReading, reserved: set[UUID]) -> Reason | None:
    """Pure consistency policy. None permits only simulated consumption, never execution."""
    if approval.approval_id in reserved:
        return "replay"
    if manifest.process_epoch != context.process_epoch or approval.process_epoch != context.process_epoch:
        return "epoch_mismatch"
    if approval.run_id != manifest.run_id:
        return "run_mismatch"
    if approval.manifest_digest != manifest.digest():
        return "manifest_mismatch"
    if clock_reversed(clock, previous):
        return "clock_reversal"
    if (approval.issued_at < context.origin.wall_time
            or approval.issued_monotonic_ms < context.origin.monotonic_ms):
        return "stale_issuance"
    # A stalled wall clock must not extend the interval beyond monotonic elapsed time.
    effective_now = max(clock.wall_time, context.origin.wall_time + timedelta(
        milliseconds=clock.monotonic_ms - context.origin.monotonic_ms))
    if approval.issued_at > clock.wall_time or approval.issued_monotonic_ms > clock.monotonic_ms:
        return "future_issuance"
    lifetime = approval.expires_at - approval.issued_at
    if (effective_now >= approval.expires_at
            or timedelta(milliseconds=clock.monotonic_ms - approval.issued_monotonic_ms) >= lifetime):
        return "expired"
    if lifetime > timedelta(seconds=manifest.limits.approval_ttl_seconds):
        return "ttl_exceeded"
    if approval.decision == "deny":
        return "mock_denial"
    return None


class SimulationLedger:
    """Serialize attempts and burn IDs in memory, including denials.

    The supplied epoch is a context label, not a credential. A new ledger can
    reuse the same epoch: restart replay protection and durable consumption do
    NOT exist. No snapshot import/resume API is provided. Python/host compromise
    is outside this simulation's guarantees.
    """

    def __init__(self, context: Context):
        self._context = Context.model_validate(context)
        self._previous = self._context.origin
        self._reserved: set[UUID] = set()
        self._attempts: list[Attempt] = []
        self._attempt_ids: set[UUID] = set()
        self._faulted = False
        self._lock = Lock()

    @property
    def context(self) -> Context:
        return self._context

    def snapshot(self) -> Evidence:
        with self._lock:
            return Evidence(schema_version="phase2a-code-evidence-v1", context=self._context,
                            attempts=tuple(self._attempts), recording_failed=self._faulted)

    def _append(self, step: Step) -> None:
        current = self._attempts[-1]
        self._attempts[-1] = Attempt(attempt_id=current.attempt_id, manifest=current.manifest,
                                    approval=current.approval, clock=current.clock,
                                    steps=current.steps + (step,))

    def assess(self, attempt_id: UUID, manifest: Manifest, approval: MockApproval,
               clock: ClockReading) -> Assessment:
        with self._lock:
            if self._faulted:
                raise LedgerUnavailable("Prior failure poisoned this in-memory ledger")
            # Validate before reservation. Malformed API calls raise and are not logged.
            attempt = Attempt(attempt_id=attempt_id, manifest=manifest, approval=approval,
                              clock=clock, steps=())
            if attempt.attempt_id in self._attempt_ids:
                raise ValueError("Duplicate attempt ID")
            manifest, approval, clock = attempt.manifest, attempt.approval, attempt.clock
            self._attempt_ids.add(attempt.attempt_id)
            self._attempts.append(attempt)
            try:
                reason = denial_reason(manifest, approval, clock, self._context,
                                       self._previous, self._reserved)
                # Reservation precedes the first append; never undo it after any fault.
                self._reserved.add(approval.approval_id)
                if not clock_reversed(clock, self._previous):
                    self._previous = clock
                self._append(Step(kind="assessment_started", reason="none"))
                if reason is not None:
                    self._append(Step(kind="approval_denied", reason=reason))
                    return Assessment(self._attempts[-1], "denied")
                self._append(Step(kind="simulation_consumed", reason="none"))
                try:
                    request_vm_operation(manifest=manifest, approval=approval)
                except VMOperationsUnavailable:
                    self._append(Step(kind="execution_unavailable", reason="unsupported_execution"))
                    return Assessment(self._attempts[-1], "blocked")
                # Even a mistakenly replaced boundary that returns cannot imply success.
                raise RuntimeError("Unavailable execution boundary unexpectedly returned")
            except BaseException:
                self._faulted = True
                try:
                    self._append(Step(kind="attempt_failed", reason="internal_failure"))
                except BaseException:
                    pass  # Preserve the original exception and faulted snapshot flag.
                raise
