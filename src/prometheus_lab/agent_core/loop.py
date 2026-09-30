from collections.abc import Callable
from typing import Literal, Protocol
from uuid import uuid4

from prometheus_lab.agent_core.approval import ApprovalDecision, simulate_approval
from prometheus_lab.agent_core.dummy import DummyResearchAgent
from prometheus_lab.audit.models import AuditEvent, EventType
from prometheus_lab.audit.store import JsonlAuditStore
from prometheus_lab.config.settings import Settings
from prometheus_lab.constraints.runner import run_constraints
from prometheus_lab.proposals.models import CodeChangeProposal
from prometheus_lab.sandbox.executor import SimulatedSandboxExecutor


class ProposalAgent(Protocol):
    def propose(self) -> CodeChangeProposal: ...


def run_dummy_loop(
    settings: Settings, *,
    agent: ProposalAgent | None = None,
    store: JsonlAuditStore | None = None,
    executor: SimulatedSandboxExecutor | None = None,
    approve: Callable[[CodeChangeProposal], ApprovalDecision] = simulate_approval,
    report: Callable[[str], None] = print,
) -> Literal["simulated", "rejected", "denied"]:
    agent = agent if agent is not None else DummyResearchAgent()
    store = store if store is not None else JsonlAuditStore(settings.audit_log)
    executor = executor if executor is not None else SimulatedSandboxExecutor()
    run_id = uuid4()

    def record(event_type: EventType, **payload: object) -> None:
        store.append(AuditEvent(run_id=run_id, event_type=event_type, payload=payload))

    try:
        record("loop_started", research_only=True, approval_mode="simulated", sandbox_mode="simulated",
               evidence_contract="simulation-v1",
               constraint_limits={"max_files": settings.max_files,
                                  "max_content_bytes": settings.max_content_bytes})
        proposal = agent.propose()
        record("proposal_generated", proposal=proposal.model_dump(mode="json"), digest=proposal.digest())
        report(f"1. Proposal: {proposal.summary} ({proposal.proposal_id})")

        results = run_constraints(proposal, settings)
        record("constraints_checked", results=[result.model_dump(mode="json") for result in results])
        for result in results:
            report(f"2. Constraint {result.name}: {'PASS' if result.passed else 'FAIL'} - {result.detail}")
        if not all(result.passed for result in results):
            report("Rejected by constraints; approval and execution skipped.")
            record("loop_finished", outcome="rejected")
            return "rejected"

        decision = approve(proposal)
        record("approval_decided", decision=decision.model_dump(mode="json"))
        if decision.proposal_digest != proposal.digest():
            raise ValueError("Approval decision does not match the proposal")
        report(f"3. Simulated human approval: {'APPROVED' if decision.approved else 'DENIED'}")
        if not decision.approved:
            report("Approval denied; execution skipped.")
            record("loop_finished", outcome="denied")
            return "denied"

        record("execution_started", proposal_digest=proposal.digest(), mode="simulated")
        result = executor.execute(proposal, decision, settings)
        record("execution_completed", result=result.model_dump(mode="json"))
        report(f"4. Sandbox: {result.status.upper()} - {result.detail}")
        report(f"5. Audit events appended to {settings.audit_log.resolve()}")
        record("loop_finished", outcome="simulated")
        return "simulated"
    except Exception as exc:
        # Best effort only: an unavailable audit store cannot log its own failure.
        try:
            record("loop_failed", error_type=type(exc).__name__, error=str(exc))
        except Exception:
            pass
        raise
