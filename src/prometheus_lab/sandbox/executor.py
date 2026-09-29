from typing import Literal

from pydantic import BaseModel, ConfigDict

from prometheus_lab.agent_core.approval import ApprovalDecision
from prometheus_lab.config.settings import Settings
from prometheus_lab.constraints.runner import run_constraints
from prometheus_lab.proposals.models import CodeChangeProposal


class ExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["simulated"] = "simulated"
    changed_files: tuple[str, ...] = ()
    planned_files: tuple[str, ...]
    detail: str = "Placeholder only: no files changed, no code executed, no improvement evaluated."


class SimulatedSandboxExecutor:
    def execute(
        self, proposal: CodeChangeProposal, decision: ApprovalDecision, settings: Settings,
    ) -> ExecutionResult:
        if not decision.approved or decision.proposal_digest != proposal.digest():
            raise ValueError("Missing approval or approval does not match this proposal")
        if not all(result.passed for result in run_constraints(proposal, settings)):
            raise ValueError("Proposal violates sandbox preconditions")
        return ExecutionResult(planned_files=tuple(change.path for change in proposal.changes))
