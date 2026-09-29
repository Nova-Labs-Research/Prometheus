from prometheus_lab.proposals.models import CodeChangeProposal, FileChange


class DummyResearchAgent:
    def propose(self) -> CodeChangeProposal:
        return CodeChangeProposal(
            summary="Document and type the toy addition function.",
            rationale="A deterministic readability proposal for testing the review workflow; quality is not measured.",
            changes=(FileChange(
                path="experiments/target_repo/calculator.py",
                new_content='def add(a: int, b: int) -> int:\n    """Return the sum of two integers."""\n    return a + b\n',
            ),),
        )
