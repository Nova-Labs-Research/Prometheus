import re
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict

from prometheus_lab.config.settings import Settings, TARGET_PREFIX
from prometheus_lab.proposals.models import CodeChangeProposal


class ConstraintResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    passed: bool
    detail: str


Check = Callable[[CodeChangeProposal, Settings], ConstraintResult]


def target_scope(proposal: CodeChangeProposal, settings: Settings) -> ConstraintResult:
    """Accept only simple relative Python paths within the toy target namespace."""
    def allowed(path: str) -> bool:
        parts = path.split("/")
        return (
            path.startswith(TARGET_PREFIX)
            and path.endswith(".py")
            and all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", p) for p in parts)
            and all(not p.endswith((".", " ")) for p in parts)
            and all(p.split(".")[0].upper() not in WINDOWS_DEVICES for p in parts)
        )

    passed = all(allowed(change.path) for change in proposal.changes)
    return ConstraintResult(
        name="target_scope", passed=passed,
        detail="Only canonical relative .py paths under experiments/target_repo/ are allowed.",
    )


WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
}


def unique_paths(proposal: CodeChangeProposal, settings: Settings) -> ConstraintResult:
    paths = [change.path.casefold() for change in proposal.changes]
    return ConstraintResult(
        name="unique_paths", passed=len(paths) == len(set(paths)),
        detail="Change paths must be unique, including on case-insensitive filesystems.",
    )


def proposal_limits(proposal: CodeChangeProposal, settings: Settings) -> ConstraintResult:
    size = sum(len(change.new_content.encode("utf-8")) for change in proposal.changes)
    return ConstraintResult(
        name="proposal_limits",
        passed=len(proposal.changes) <= settings.max_files and size <= settings.max_content_bytes,
        detail=f"{len(proposal.changes)}/{settings.max_files} files; {size}/{settings.max_content_bytes} content bytes.",
    )


DEFAULT_CHECKS: tuple[Check, ...] = (target_scope, unique_paths, proposal_limits)


def run_constraints(
    proposal: CodeChangeProposal, settings: Settings,
    checks: Sequence[Check] = DEFAULT_CHECKS,
) -> tuple[ConstraintResult, ...]:
    if not checks:
        raise ValueError("At least one constraint check is required")
    results = []
    for check in checks:
        try:
            results.append(check(proposal, settings))
        except Exception as exc:
            # A broken check is a failure, never permission to proceed.
            results.append(ConstraintResult(
                name=getattr(check, "__name__", type(check).__name__), passed=False,
                detail=f"Check raised {type(exc).__name__}: {exc}",
            ))
    return tuple(results)
