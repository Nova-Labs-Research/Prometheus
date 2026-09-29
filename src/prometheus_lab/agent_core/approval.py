from typing import Literal

from pydantic import BaseModel, ConfigDict

from prometheus_lab.proposals.models import CodeChangeProposal


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_digest: str
    approved: bool
    mode: Literal["simulated"] = "simulated"
    reason: str


def simulate_approval(proposal: CodeChangeProposal, approved: bool = True) -> ApprovalDecision:
    return ApprovalDecision(
        proposal_digest=proposal.digest(), approved=approved,
        reason="MVP simulation only; no human reviewed or authorized this proposal.",
    )
