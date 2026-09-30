"""Code-only controller rehearsal. No launch adapter or authenticated authority."""

from dataclasses import dataclass
from threading import Lock
from typing import Literal
from uuid import UUID

from .contracts import (ClockReading, Contract, Digest, Manifest, MockApproval,
                        Tick, parse_contract)
from .durable import DurableLedger, RecoveryRequired, StoreError


# Parser implementation ceiling, not a claim of transport/guest resource isolation.
MAX_ENVELOPE_BYTES = 64 * 1024


class MockReadiness(Contract):
    """Untrusted reported conditions, never host observations or service receipts."""

    source: Literal["mock"]
    store_id: UUID
    attempt_id: UUID
    manifest_digest: Digest
    observed_at: ClockReading
    vm_id: UUID
    vm_state: Literal["off", "running", "unknown"]
    network_adapter_count: Tick
    guest_memory_bytes: Tick
    dynamic_memory: bool
    vcpus: Tick
    evidence_writable: bool
    evidence_remaining_bytes: Tick
    collector_ready: bool
    watchdog_ready: bool


class ControllerRequest(Contract):
    schema_version: Literal["controller-rehearsal-v1"]
    attempt_id: UUID
    manifest: Manifest
    approval: MockApproval
    clock: ClockReading
    readiness: MockReadiness


Gate = Literal["binding_mismatch", "clock_mismatch", "vm_not_off", "network_present",
               "memory_mismatch", "cpu_mismatch", "evidence_unavailable",
               "retention_insufficient", "collector_unavailable", "watchdog_unavailable"]


class ControllerReceipt(Contract):
    schema_version: Literal["controller-receipt-v1"]
    store_id: UUID
    request: ControllerRequest
    status: Literal["preflight_blocked", "denied", "blocked"]
    gates: tuple[Gate, ...]

    @property
    def confers_authority(self) -> bool:
        return False

    @property
    def valid_completion(self) -> bool:
        return False


@dataclass(frozen=True)
class ReceiptVerification:
    status: Literal["consistent_rehearsal", "unpersisted_preflight", "invalid", "incomplete"]
    detail: str

    @property
    def confers_authority(self) -> bool:
        return False

    @property
    def valid_completion(self) -> bool:
        return False


def decode_envelope(model, value):
    """Bound bytes before UTF-8/JSON decoding; reject duplicate keys and extras."""
    if type(value) is bytes:
        if len(value) > MAX_ENVELOPE_BYTES:
            raise ValueError("Controller envelope exceeds 64 KiB parser ceiling")
        try:
            return parse_contract(model, value.decode("utf-8"))
        except (UnicodeError, RecursionError) as error:
            raise ValueError("Malformed controller envelope") from error
    return model.model_validate(value)


def preflight(request: ControllerRequest, store_id: UUID) -> tuple[Gate, ...]:
    """Pure consistency gates. Passing permits only durable mock assessment."""
    request = ControllerRequest.model_validate(request)
    ready, manifest = request.readiness, request.manifest
    failures = []
    if (ready.store_id != store_id or ready.attempt_id != request.attempt_id
            or ready.manifest_digest != manifest.digest() or ready.vm_id != manifest.vm_id):
        failures.append("binding_mismatch")
    # A preparation sample cannot be carried forward to a different decision tick.
    # Both clocks remain caller-supplied data; this is not real freshness proof.
    if ready.observed_at != request.clock:
        failures.append("clock_mismatch")
    if ready.vm_state != "off":
        failures.append("vm_not_off")
    if ready.network_adapter_count != 0:
        failures.append("network_present")
    if ready.dynamic_memory or ready.guest_memory_bytes != manifest.limits.guest_memory_bytes:
        failures.append("memory_mismatch")
    if ready.vcpus != manifest.limits.vcpus:
        failures.append("cpu_mismatch")
    if not ready.evidence_writable:
        failures.append("evidence_unavailable")
    if ready.evidence_remaining_bytes < manifest.limits.output_bytes:
        failures.append("retention_insufficient")
    if not ready.collector_ready:
        failures.append("collector_unavailable")
    if not ready.watchdog_ready:
        failures.append("watchdog_unavailable")
    return tuple(failures)


class CodeOnlyController:
    """Orchestrate preflight -> durable mock admission -> readback verification.

    No callback, executable, backend, enable flag or credential input is accepted.
    The supplied ledger must already have an explicitly begun session. This class
    never initializes, repairs, recovers or resumes a store automatically.
    """

    def __init__(self, ledger: DurableLedger):
        if type(ledger) is not DurableLedger:
            raise TypeError("Expected the existing durable simulation ledger")
        self._ledger = ledger
        self._lock = Lock()
        self._faulted = False

    def rehearse(self, value: ControllerRequest | bytes) -> ControllerReceipt:
        request = decode_envelope(ControllerRequest, value)
        with self._lock:
            if self._faulted:
                raise StoreError("Controller is poisoned; reopen and inspect, never auto-retry")
            try:
                snapshot = self._ledger.snapshot()
                if snapshot.status == "recovery_required":
                    raise RecoveryRequired("Pending evidence requires explicit abandonment")
                if snapshot.active_epoch != request.manifest.process_epoch:
                    raise StoreError("No matching active evidence epoch")
                gates = preflight(request, snapshot.store_id)
                if gates:
                    # This receipt is explicitly not persisted and consumes no ID.
                    return ControllerReceipt(schema_version="controller-receipt-v1",
                        store_id=snapshot.store_id, request=request,
                        status="preflight_blocked", gates=gates)
                result = self._ledger.assess(request.attempt_id, request.manifest,
                                             request.approval, request.clock)
                receipt = ControllerReceipt(schema_version="controller-receipt-v1",
                    store_id=snapshot.store_id, request=request, status=result.status, gates=())
                # No result is returned unless the committed request/outcome can be
                # read back. Failure/ack loss may leave durable pending OR terminal
                # evidence; neither licenses an automatic retry or any execution.
                checked = self.verify(receipt)
                if checked.status != "consistent_rehearsal":
                    raise StoreError("Controller evidence readback failed: " + checked.detail)
                return receipt
            except BaseException:
                self._faulted = True
                raise

    def verify(self, value: ControllerReceipt | bytes) -> ReceiptVerification:
        """Read-only reconciliation, never evidence authentication or repair."""
        try:
            receipt = decode_envelope(ControllerReceipt, value)
            snapshot = self._ledger.snapshot()
            if receipt.store_id != snapshot.store_id:
                raise ValueError("Receipt/store identity mismatch")
            gates = preflight(receipt.request, snapshot.store_id)
            if receipt.gates != gates:
                raise ValueError("Receipt gates contradict its request")
            if gates:
                if receipt.status != "preflight_blocked":
                    raise ValueError("Failed preflight cannot have an admission outcome")
                return ReceiptVerification("unpersisted_preflight",
                    "Consistent reported rejection; preflight receipt is not durable evidence")
            if receipt.status == "preflight_blocked":
                raise ValueError("Preflight rejection has no failing gates")
            matches = [item for item in snapshot.attempts
                       if item.request.attempt_id == receipt.request.attempt_id]
            if not matches:
                return ReceiptVerification("incomplete", "No committed matching attempt")
            item = matches[0]
            request = receipt.request
            if (item.request.manifest != request.manifest or item.request.approval != request.approval
                    or item.request.clock != request.clock or item.request.steps):
                raise ValueError("Receipt differs from the committed request")
            if item.status == "pending":
                return ReceiptVerification("incomplete", "Attempt requires explicit recovery")
            if item.status != receipt.status:
                raise ValueError("Receipt outcome contradicts durable evidence")
            return ReceiptVerification("consistent_rehearsal",
                "Durable denied/blocked mock assessment; readiness claims are unauthenticated and unpersisted")
        except (ValueError, TypeError, StoreError, OSError, OverflowError, RecursionError) as error:
            return ReceiptVerification("invalid", str(error))
