"""Read-only consistency checks for simulation-v1, never evidence authentication."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from prometheus_lab.agent_core.approval import ApprovalDecision
from prometheus_lab.audit.models import AuditEvent
from prometheus_lab.config.settings import Settings
from prometheus_lab.constraints.runner import DEFAULT_CHECKS, ConstraintResult, run_constraints
from prometheus_lab.proposals.models import CodeChangeProposal
from prometheus_lab.sandbox.executor import ExecutionResult


Status = Literal["completed_simulation", "rejected", "denied", "failed", "incomplete", "invalid"]


@dataclass(frozen=True)
class RunVerification:
    run_id: UUID
    status: Status
    detail: str

    @property
    def valid_completion(self) -> bool:
        return self.status == "completed_simulation"


@dataclass(frozen=True)
class AuditVerification:
    runs: tuple[RunVerification, ...]

    @property
    def consistent(self) -> bool:
        """All recorded runs have complete, internally consistent normal outcomes."""
        return bool(self.runs) and all(
            run.status in {"completed_simulation", "rejected", "denied"} for run in self.runs
        )

    @property
    def valid_completion(self) -> bool:
        return bool(self.runs) and all(run.valid_completion for run in self.runs)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _object(value: object, fields: set[str]) -> dict:
    _require(isinstance(value, dict), "Expected an object")
    _require(set(value) == fields, f"Expected exactly these fields: {', '.join(sorted(fields))}")
    return value


def _full_model(model: type[BaseModel], value: object) -> BaseModel:
    # Defaults must not reconstruct missing evidence. Strict JSON validation still
    # accepts the UUID/date/tuple representations emitted by model_dump(mode='json').
    _object(value, set(model.model_fields))
    return model.model_validate_json(json.dumps(value), strict=True)


def _check_run(events: Sequence[AuditEvent]) -> tuple[Status, str]:
    expected = "loop_started"
    outcome = None
    proposal = None
    settings = None
    digest = None
    terminal = None
    missing_context = False
    for event in events:
        _require(terminal is None, "Record after a terminal event (including duplicate terminals)")
        _require(event.model_fields_set == set(AuditEvent.model_fields), "Incomplete event envelope")
        _require(type(event.schema_version) is int and event.schema_version == 1,
                 "Unsupported event schema")
        _require(event.timestamp.utcoffset() is not None, "Timestamp must include a timezone")
        kind, payload = event.event_type, event.payload
        if kind == "loop_failed":
            _object(payload, {"error_type", "error"})
            _require(isinstance(payload["error_type"], str) and bool(payload["error_type"])
                     and isinstance(payload["error"], str), "Invalid failure payload")
            terminal = "failed"
            continue
        _require(kind == expected, f"Expected {expected}, found {kind}")
        if kind == "loop_started":
            # Keep checking legacy records for contradictions. Missing context
            # prevents completion, but must not hide invalid subsequent evidence.
            context_fields = {"evidence_contract", "constraint_limits"}
            missing_context = not context_fields <= payload.keys()
            _object(payload, {"research_only", "approval_mode", "sandbox_mode",
                              *context_fields.intersection(payload)})
            _require(payload["research_only"] is True
                     and payload["approval_mode"] == payload["sandbox_mode"] == "simulated"
                     and ("evidence_contract" not in payload
                          or payload["evidence_contract"] == "simulation-v1"), "Invalid simulation markers")
            if "constraint_limits" in payload:
                limits = _object(payload["constraint_limits"], {"max_files", "max_content_bytes"})
                _require(all(type(value) is int and value >= 1 for value in limits.values()),
                         "Invalid constraint limits")
                settings = Settings(**limits)
            expected = "proposal_generated"
        elif kind == "proposal_generated":
            _object(payload, {"proposal", "digest"})
            proposal = _full_model(CodeChangeProposal, payload["proposal"])
            _require(proposal.created_at.utcoffset() is not None, "Proposal timestamp lacks timezone")
            digest = proposal.digest()
            _require(payload["digest"] == digest, "Proposal digest mismatch")
            expected = "constraints_checked"
        elif kind == "constraints_checked":
            _object(payload, {"results"})
            _require(isinstance(payload["results"], list), "Constraint results must be a list")
            results = [_full_model(ConstraintResult, item) for item in payload["results"]]
            _require([item.name for item in results] == [check.__name__ for check in DEFAULT_CHECKS],
                     "Missing, duplicate, reordered or unknown constraint checks")
            # A check may fail closed because of an exception. A claimed pass
            # must nevertheless agree with the recorded proposal and limits.
            if settings is not None:
                expected_results = run_constraints(proposal, settings)
                _require(all(not item.passed or reference.passed
                             for item, reference in zip(results, expected_results)),
                         "Constraint pass contradicts the proposal or recorded limits")
            if all(item.passed for item in results):
                expected = "approval_decided"
            else:
                expected, outcome = "loop_finished", "rejected"
        elif kind == "approval_decided":
            _object(payload, {"decision"})
            decision = _full_model(ApprovalDecision, payload["decision"])
            _require(decision.proposal_digest == digest, "Approval digest mismatch")
            if decision.approved:
                expected = "execution_started"
            else:
                expected, outcome = "loop_finished", "denied"
        elif kind == "execution_started":
            _object(payload, {"proposal_digest", "mode"})
            _require(payload["proposal_digest"] == digest and payload["mode"] == "simulated",
                     "Execution binding or simulation marker mismatch")
            expected = "execution_completed"
        elif kind == "execution_completed":
            _object(payload, {"result"})
            result = _full_model(ExecutionResult, payload["result"])
            _require(not result.changed_files, "Simulation claims changed files")
            _require(result.planned_files == tuple(change.path for change in proposal.changes),
                     "Planned files differ from proposal")
            expected, outcome = "loop_finished", "simulated"
        elif kind == "loop_finished":
            _object(payload, {"outcome"})
            _require(payload["outcome"] == outcome, "Terminal outcome contradicts gates")
            terminal = "completed_simulation" if outcome == "simulated" else outcome
    if terminal is None:
        return "incomplete", f"Missing terminal evidence; next expected event: {expected}"
    if missing_context and terminal != "failed":
        return "incomplete", "Legacy run lacks simulation-v1 contract/limit evidence"
    return terminal, "Recorded workflow only; origin, effects and authenticity are unverified"


def verify_audit(events: Sequence[AuditEvent]) -> AuditVerification:
    """Check each run in append order, preserving interleaved runs and all inputs.

    No candidate content is executed, no files are written, and no missing fields
    are repaired. Even a fully consistent fabricated log can pass these checks.
    """
    grouped: dict[UUID, list[AuditEvent]] = {}
    counts = Counter(event.event_id for event in events)
    for event in events:
        grouped.setdefault(event.run_id, []).append(event)
    verdicts = []
    for run_id, records in grouped.items():
        try:
            _require(not any(counts[event.event_id] != 1 for event in records), "Duplicate event ID")
            status, detail = _check_run(records)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            status, detail = "invalid", str(exc)
        verdicts.append(RunVerification(run_id, status, detail))
    return AuditVerification(tuple(verdicts))
