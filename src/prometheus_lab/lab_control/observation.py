"""Pure observation/recovery rehearsal. Every peer and clock is test-only."""

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import Field, model_validator

from .contracts import ClockReading, Context, Contract, Digest, Positive, Tick
from .controller import ControllerRequest, preflight
from .state import clock_reversed, denial_reason


Role = Literal["observer", "broker", "watchdog", "collector"]


def canonical(value: Contract) -> bytes:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


class ExternalAuthenticationUnavailable(RuntimeError):
    pass


def authenticate_external_peer(*args: object, **kwargs: object):
    """No OS token, service, transport, credential or enable flag is implemented."""
    raise ExternalAuthenticationUnavailable("External authentication is unavailable")


class TestPeer(Contract):
    kind: Literal["test_only"]
    role: Role
    instance_id: UUID


class MockContextConsent(Contract):
    kind: Literal["mock_context_consent"]
    approval_id: UUID
    context_digest: Digest


class ObservationContext(Contract):
    schema_version: Literal["observation-rehearsal-context-v1"]
    store_id: UUID
    installation_id: UUID
    operation_id: UUID
    deployment_digest: Digest
    disk_id: UUID
    initial_disk_digest: Digest
    recovery_policy_digest: Digest
    session: Context
    request: ControllerRequest
    peers: tuple[TestPeer, ...]
    freshness_ms: Positive
    lease_until_ms: Tick
    consent: MockContextConsent

    @model_validator(mode="after")
    def consistent(self):
        roles = [peer.role for peer in self.peers]
        if sorted(roles) != ["broker", "collector", "observer", "watchdog"]:
            raise ValueError("Exactly one test peer for every role is required")
        if len({peer.instance_id for peer in self.peers}) != 4:
            raise ValueError("Test peer instances must be distinct")
        request = self.request
        if (preflight(request, self.store_id) or denial_reason(request.manifest,
                request.approval, request.clock, self.session, self.session.origin, set())):
            raise ValueError("Context must describe a consistent admitted mock intent")
        if self.lease_until_ms <= request.clock.monotonic_ms:
            raise ValueError("Test lease must initially be unexpired")
        if (self.consent.approval_id != request.approval.approval_id or
                self.consent.context_digest != self.consent_digest()):
            raise ValueError("Mock consent does not bind this complete observation context")
        return self

    def consent_digest(self) -> str:
        # Canonical unsigned context; excludes only the self-referential consent.
        raw = json.dumps(self.model_dump(mode="json", exclude={"consent"}), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return sha256(raw.encode("utf-8")).hexdigest()

    def digest(self) -> str:
        return sha256(canonical(ObservationContext.model_validate(self))).hexdigest()

    def binding(self):
        request = self.request
        return Binding(context_digest=self.digest(), store_id=self.store_id,
            attempt_id=request.attempt_id, approval_id=request.approval.approval_id,
            operation_id=self.operation_id, run_id=request.manifest.run_id,
            process_epoch=request.manifest.process_epoch, vm_id=request.manifest.vm_id,
            manifest_digest=request.manifest.digest())


class Binding(Contract):
    context_digest: Digest
    store_id: UUID
    attempt_id: UUID
    approval_id: UUID
    operation_id: UUID
    run_id: UUID
    process_epoch: UUID
    vm_id: UUID
    manifest_digest: Digest


class Claim(Contract):
    category: Literal["host_lifecycle", "broker_quiescence", "watchdog_status", "guest_claim"]
    state: Literal["off", "running", "unknown", "settled", "pending", "armed", "lost", "finished", "failed"]
    prior_broker_fenced: bool

    @model_validator(mode="after")
    def consistent(self):
        allowed = {"host_lifecycle": {"off", "running", "unknown"},
            "broker_quiescence": {"settled", "pending", "unknown"},
            "watchdog_status": {"armed", "lost", "unknown"},
            "guest_claim": {"finished", "failed", "unknown"}}
        if self.state not in allowed[self.category]:
            raise ValueError("Claim state does not match category")
        if self.prior_broker_fenced and self.category != "broker_quiescence":
            raise ValueError("Only a broker claim may report a fenced prior instance")
        return self


class Challenge(Contract):
    kind: Literal["challenge"]
    binding: Binding
    challenge_id: UUID
    peer: TestPeer
    clock: ClockReading
    expires_ms: Tick


class Observation(Contract):
    schema_version: Literal["observation-rehearsal-v1"]
    binding: Binding
    observation_id: UUID
    challenge_id: UUID
    reporter_sequence: Positive
    query_start_ms: Tick
    query_end_ms: Tick
    claim: Claim
    payload_digest: Digest
    payload_bytes: Positive

    @model_validator(mode="after")
    def consistent(self):
        raw = canonical(self.claim)
        if (sha256(raw).hexdigest(), len(raw)) != (self.payload_digest, self.payload_bytes):
            raise ValueError("Claim bytes/digest mismatch")
        if self.query_start_ms > self.query_end_ms:
            raise ValueError("Query interval is reversed")
        return self


class Received(Contract):
    kind: Literal["received"]
    peer: TestPeer  # A separate test transport context, never a real credential.
    observation: Observation
    clock: ClockReading  # Test ingress clock, not guest-reported time.


class Action(Contract):
    kind: Literal["action"]
    action: Literal["fence", "dispatch", "stop_requested", "interrupt", "abandon"]
    clock: ClockReading


class RecordedEvent(Contract):
    event: Annotated[Union[Challenge, Received, Action], Field(discriminator="kind")]


@dataclass(frozen=True)
class RecoveryView:
    context: ObservationContext
    events: tuple[RecordedEvent, ...]
    observations: tuple[Received, ...]
    issues: tuple[str, ...]
    scenario_state: str
    simulated_dispatches: int
    watchdog_required_in_scenario: bool
    head: str = ""

    @property
    def confers_authority(self) -> bool:
        return False

    @property
    def valid_completion(self) -> bool:
        return False

    @property
    def requires_external_quarantine(self) -> bool:
        return True  # Requirement only: no quarantine is actually enforced here.

    @property
    def actual_vm_state(self) -> str:
        return "unknown"

    @property
    def evidence_classifications(self) -> tuple[str, ...]:
        return tuple("guest_claim" if r.peer.role == "collector" else "test_host_claim"
                     for r in self.observations)


def replay(context: ObservationContext, events: tuple[RecordedEvent, ...]) -> RecoveryView:
    """Recompute every transition from immutable claims; never invoke an effect."""
    context = ObservationContext.model_validate(context)
    binding = context.binding()
    peers = {p.role: p for p in context.peers}
    challenges, answered, seen, sequences, latest = {}, set(), {}, {}, {}
    observations, retained, issues = [], [], set()
    clock = context.request.clock
    fenced = dispatched = stopped = interrupted = abandoned = False
    barrier = clock.monotonic_ms

    def fresh(report, now):
        return (report is not None and
                0 <= now - report.observation.query_end_ms < context.freshness_ms)

    def launch_gates(now):
        host, watchdog = latest.get("observer"), latest.get("watchdog")
        return (not issues and not interrupted and not stopped and
            denial_reason(context.request.manifest, context.request.approval, now,
                          context.session, context.request.clock, set()) is None and
            now.monotonic_ms < context.lease_until_ms and
            fresh(host, now.monotonic_ms) and host.observation.claim.state == "off" and
            fresh(watchdog, now.monotonic_ms) and watchdog.observation.claim.state == "armed")

    for value in events:
        record = RecordedEvent.model_validate(value)
        event = record.event
        if isinstance(event, Received):
            prior = seen.get(event.observation.observation_id)
            if prior and prior.peer == event.peer and prior.observation == event.observation:
                continue  # Redelivery does not advance ingress clock, even after abandonment.
        if abandoned:
            raise ValueError("Abandoned rehearsal cannot accept later events")
        if clock_reversed(event.clock, clock):
            raise ValueError("Ingress clock moved backwards")
        clock = event.clock
        now = clock.monotonic_ms
        if isinstance(event, Challenge):
            if event.binding != binding or event.peer != peers[event.peer.role]:
                raise ValueError("Challenge binding/peer mismatch")
            if event.challenge_id in challenges:
                raise ValueError("Challenge ID cannot be reused")
            if not now < event.expires_ms <= now + context.freshness_ms:
                raise ValueError("Challenge must have a bounded positive lifetime")
            challenges[event.challenge_id] = event
        elif isinstance(event, Received):
            report = event.observation
            if report.binding != binding or event.peer != peers[event.peer.role]:
                raise ValueError("Observation binding/peer mismatch")
            category_role = {"host_lifecycle": "observer", "broker_quiescence": "broker",
                             "watchdog_status": "watchdog", "guest_claim": "collector"}
            if category_role[report.claim.category] != event.peer.role:
                raise ValueError("Peer role cannot assert this category")
            previous = seen.get(report.observation_id)
            if previous:
                if previous.peer == event.peer and previous.observation == report:
                    continue  # Equivalent redelivery never changes evidence state.
                issues.add("conflicting_duplicate")
            query = challenges.get(report.challenge_id)
            invalid = previous is not None
            if query is None or query.peer != event.peer:
                raise ValueError("Observation lacks its exact peer challenge")
            if report.challenge_id in answered:
                issues.add("challenge_replay")
                invalid = True
            if not (query.clock.monotonic_ms <= report.query_start_ms <= report.query_end_ms <= now
                    < query.expires_ms):
                issues.add("stale_or_invalid_interval")
                invalid = True
            last_seq = sequences.get(event.peer.role, 0)
            if report.reporter_sequence != last_seq + 1:
                issues.add("sequence_gap_or_replay")
                invalid = True
            observations.append(event)
            seen.setdefault(report.observation_id, event)
            answered.add(report.challenge_id)
            sequences[event.peer.role] = max(last_seq, report.reporter_sequence)
            old = latest.get(event.peer.role)
            if old and (report.query_start_ms < old.observation.query_start_ms or
                        report.query_end_ms < old.observation.query_end_ms):
                issues.add("sample_order_reversal")
                invalid = True
            for prior in observations[:-1]:
                sample = prior.observation
                if (prior.peer == event.peer and report.query_start_ms <= sample.query_end_ms
                        and sample.query_start_ms <= report.query_end_ms and report.claim != sample.claim):
                    issues.add("contradictory_interval")
                    invalid = True
            if not invalid:
                if event.peer.role == "observer":
                    if report.claim.state == "running" and not dispatched:
                        issues.add("running_unattributed")
                    if (old and old.observation.claim.state == "running" and
                            report.claim.state == "off" and not stopped):
                        issues.add("unexplained_transition")
                if event.peer.role == "watchdog" and report.claim.state != "armed":
                    issues.add("watchdog_unavailable")
                latest[event.peer.role] = event
        else:
            if event.action == "fence":
                if fenced or dispatched:
                    raise ValueError("A second dispatch fence is prohibited")
                if launch_gates(clock):
                    fenced = True
                else:
                    issues.add("dispatch_denied")
                barrier = now
            elif event.action == "dispatch":
                if not fenced or dispatched:
                    raise ValueError("Dispatch requires one unused fence")
                if launch_gates(clock):
                    dispatched = True  # A retained conceptual dispatch, never a call.
                else:
                    issues.add("dispatch_denied")
                barrier = now
            elif event.action == "stop_requested":
                if not fenced or stopped:
                    raise ValueError("Stop rehearsal requires one unresolved fence")
                stopped = True
                barrier = now
            elif event.action == "interrupt":
                interrupted = True
                answered.update(challenges)  # Old challenges cannot survive a modeled restart.
                latest.clear()
                barrier = now
            else:
                host, broker = latest.get("observer"), latest.get("broker")
                blocking = issues - {"dispatch_denied", "watchdog_unavailable",
                                     "running_unattributed", "unexplained_transition"}
                if (blocking or not fresh(host, now) or not fresh(broker, now) or
                    host.observation.claim.state != "off" or
                    broker.observation.claim.state != "settled" or
                    not broker.observation.claim.prior_broker_fenced or
                    broker.observation.query_start_ms < barrier or
                    host.observation.query_start_ms <= broker.observation.query_end_ms):
                    raise ValueError("Fresh off plus settled/fenced broker evidence is required")
                abandoned = True  # Test abandonment only, never releases a real VM.
        retained.append(record)
    if abandoned:
        state = "abandoned_unverified"
    elif issues:
        state = "quarantined"
    elif dispatched or fenced:
        state = "dispatch_unknown"
    else:
        state = "reconciliation_required"
    if not observations:
        issues.add("missing_observations")
    return RecoveryView(context, tuple(retained), tuple(observations), tuple(sorted(issues)),
                        state, int(dispatched), fenced and not abandoned)
