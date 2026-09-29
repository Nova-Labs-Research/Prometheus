from collections import Counter
import json
from pathlib import Path
from uuid import UUID

import typer
from rich.console import Console, Group
from rich.json import JSON
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from prometheus_lab.agent_core.approval import simulate_approval
from prometheus_lab.agent_core.loop import run_dummy_loop as run_loop
from prometheus_lab.audit.models import AuditEvent as AuditEntry
from prometheus_lab.audit.store import JsonlAuditStore as AuditStore
from prometheus_lab.config.settings import Settings, load_settings


app = typer.Typer(no_args_is_help=True, help="Research-only constrained-agent prototype.")
audit_app = typer.Typer(no_args_is_help=True, help="Inspect the audit log without modifying it.")
app.add_typer(audit_app, name="audit")
console = Console()


@app.callback()
def main() -> None:
    """prometheus-lab: controlled experiments with simulated approval and execution."""


@app.command("run-dummy-loop")
def run_dummy_loop(
    audit_log: Path = typer.Option(Path("artifacts/audit/events.jsonl"), help="Append-only JSONL path, relative to the working directory."),
    deny_approval: bool = typer.Option(False, "--deny-approval", help="Simulate a denial to exercise the approval gate."),
) -> None:
    console.print("RESEARCH ONLY - Not for production or safety-critical use.", style="bold yellow")
    console.print("Approval is simulated. The conceptual sandbox is NOT a real security boundary.")
    try:
        outcome = run_loop(
            Settings(audit_log=audit_log),
            approve=lambda proposal: simulate_approval(proposal, approved=not deny_approval),
            report=lambda message: console.print(message, markup=False, highlight=False),
        )
    except Exception as exc:
        console.print(f"Loop failed: {type(exc).__name__}: {exc}", style="red", markup=False)
        raise typer.Exit(code=1) from exc
    if outcome != "simulated":
        raise typer.Exit(code=2)


def _read_audit_entries() -> list[AuditEntry] | None:
    """Read the configured log, distinguishing missing logs from empty logs."""
    path = load_settings().audit_log_path
    try:
        return AuditStore(path).read_all()
    except FileNotFoundError:
        console.print(f"No audit log found at {path}. Run prom-lab run-dummy-loop first.", markup=False)
        return None
    except (OSError, ValueError) as exc:
        console.print(f"Unable to read audit log: {exc}", style="red", markup=False)
        raise typer.Exit(code=1) from exc


def _recorded_proposal_id(entry: AuditEntry) -> str | None:
    """Read explicit metadata without changing the stored event schema."""
    value = entry.payload.get("proposal_id")
    proposal = entry.payload.get("proposal")
    if value is None and isinstance(proposal, dict):
        value = proposal.get("proposal_id")
    return str(value) if value is not None else None


def _proposal_ids_by_run(entries: list[AuditEntry]) -> dict[UUID, str]:
    """Correlate legacy loop events only when a run has one recorded proposal."""
    candidates: dict[UUID, set[str]] = {}
    for entry in entries:
        proposal_id = _recorded_proposal_id(entry)
        if proposal_id is not None:
            candidates.setdefault(entry.run_id, set()).add(proposal_id)
    return {run_id: next(iter(ids)) for run_id, ids in candidates.items() if len(ids) == 1}


def _proposal_id(entry: AuditEntry, by_run: dict[UUID, str]) -> str | None:
    return _recorded_proposal_id(entry) or by_run.get(entry.run_id)


def _actor(entry: AuditEntry) -> str:
    """Never infer human authority from a simulated approval event."""
    value = entry.payload.get("actor")
    return value if isinstance(value, str) and value.strip() else "unknown"


@audit_app.command("tail")
def audit_tail(last: int = typer.Option(10, "--last", min=1, help="Number of most recent entries.")) -> None:
    """Print the last N entries in append order with condensed payloads."""
    entries = _read_audit_entries()
    if entries is None:
        return
    if not entries:
        console.print("Audit log is empty.")
        return
    by_run = _proposal_ids_by_run(entries)
    table = Table(title=f"Last {min(last, len(entries))} audit entries")
    for column in ("Timestamp", "Event type", "Proposal ID", "Actor", "Payload"):
        table.add_column(column, overflow="fold")
    for entry in entries[-last:]:
        payload = json.dumps(entry.payload, ensure_ascii=True)
        snippet = payload if len(payload) <= 120 else payload[:117] + "..."
        table.add_row(*(Text(value) for value in (
            entry.timestamp.isoformat(), entry.event_type,
            _proposal_id(entry, by_run) or "-", _actor(entry), snippet,
        )))
    console.print(table)


@audit_app.command("summary")
def audit_summary() -> None:
    """Print total entries and counts by event type and recorded actor."""
    entries = _read_audit_entries()
    if entries is None:
        return
    console.print(f"Total entries: {len(entries)}")
    event_counts = Counter(entry.event_type for entry in entries)
    actor_counts = Counter(_actor(entry) for entry in entries)
    for actor in ("agent", "human"):
        actor_counts.setdefault(actor, 0)
    for label, counts in (("Event type", event_counts), ("Actor", actor_counts)):
        table = Table(title=f"Counts by {label.lower()}")
        table.add_column(label)
        table.add_column("Count", justify="right")
        for name, count in sorted(counts.items()):
            table.add_row(Text(name), str(count))
        console.print(table)


@audit_app.command("by-proposal")
def audit_by_proposal(proposal_id: str) -> None:
    """Print full payloads for a proposal, correlating unambiguous loop events."""
    entries = _read_audit_entries()
    if entries is None:
        return
    by_run = _proposal_ids_by_run(entries)
    matches = [entry for entry in entries if _proposal_id(entry, by_run) == proposal_id]
    if not matches:
        console.print(f"No audit entries found for proposal_id: {proposal_id}", markup=False)
        return
    for entry in matches:
        console.print(Panel(
            Group(
                Text(f"Timestamp: {entry.timestamp.isoformat()}"),
                Text(f"actor: {_actor(entry)}"),
                JSON(json.dumps(entry.payload), indent=2),
            ),
            title=Text(entry.event_type),
            subtitle=Text(f"proposal_id: {proposal_id}"),
        ))


if __name__ == "__main__":
    app()
