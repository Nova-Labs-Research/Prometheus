"""Read-only simulation consistency, with no evidence-authenticity verdict."""

from dataclasses import dataclass
from typing import Literal

from .contracts import Evidence, Step, parse_contract
from .state import clock_reversed, denial_reason


@dataclass(frozen=True)
class Verification:
    status: Literal["consistent_simulation", "failed", "incomplete", "invalid"]
    detail: str

    @property
    def consistent(self) -> bool:
        return self.status == "consistent_simulation"

    @property
    def confers_authority(self) -> bool:
        return False

    @property
    def valid_completion(self) -> bool:
        return False


def verify_evidence(value: Evidence | str) -> Verification:
    """Reject malformed/contradictory data; accept only code-contract consistency.

    No file IO, execution, identity authentication, clock authentication or
    durable-history reconciliation occurs. A forged consistent bundle can pass.
    """
    try:
        bundle = parse_contract(Evidence, value) if isinstance(value, str) else Evidence.model_validate(value)
        previous = bundle.context.origin
        reserved = set()
        attempt_ids = set()
        incomplete = not bundle.attempts
        failed = bundle.recording_failed
        terminal_problem = False
        for attempt in bundle.attempts:
            if terminal_problem:
                raise ValueError("An attempt follows incomplete or failed evidence")
            if attempt.attempt_id in attempt_ids:
                raise ValueError("Duplicate attempt ID")
            attempt_ids.add(attempt.attempt_id)
            reason = denial_reason(attempt.manifest, attempt.approval, attempt.clock,
                                   bundle.context, previous, reserved)
            reserved.add(attempt.approval.approval_id)
            if not clock_reversed(attempt.clock, previous):
                previous = attempt.clock
            expected = [Step(kind="assessment_started", reason="none")]
            if reason is None:
                expected += [Step(kind="simulation_consumed", reason="none"),
                             Step(kind="execution_unavailable", reason="unsupported_execution")]
            else:
                expected += [Step(kind="approval_denied", reason=reason)]
            steps = attempt.steps
            failed_step = bool(steps and steps[-1] == Step(kind="attempt_failed", reason="internal_failure"))
            prefix = steps[:-1] if failed_step else steps
            if len(prefix) > len(expected) or tuple(expected[:len(prefix)]) != prefix:
                raise ValueError("Contradictory step sequence or denial reason")
            if failed_step:
                if len(prefix) == len(expected):
                    raise ValueError("Failure after terminal evidence is contradictory")
                if not bundle.recording_failed:
                    raise ValueError("Failure step contradicts healthy ledger flag")
                failed = terminal_problem = True
            elif len(prefix) < len(expected):
                incomplete = terminal_problem = True
        if failed:
            return Verification("failed", "A recording/internal failure prevents any normal outcome")
        if incomplete:
            return Verification("incomplete", "Evidence is empty or lacks a terminal code-contract outcome")
        return Verification("consistent_simulation", "Consistent denied/blocked mock attempts; no real authority or execution")
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        return Verification("invalid", str(error))
