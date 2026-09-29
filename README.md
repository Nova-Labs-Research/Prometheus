# prometheus-lab

Research-only prototype for constrained self-improving AI agents.
A controlled "fire" of self-improvement, explicitly caged for research.

**Research only. Not for production or safety-critical use.** Approval is
simulated. The sandbox is conceptual in this MVP and **not a real security
boundary**. No proposed code is applied or executed, and no improvement is
measured. Builder != Reviewer != Authority.

## Quick start

Requires Python 3.10+. From the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
prom-lab run-dummy-loop
```

If activation is unavailable, call `.\.venv\Scripts\python.exe -m pip install -e .`
and `.\.venv\Scripts\prom-lab.exe run-dummy-loop` directly. On Linux/macOS use
`source .venv/bin/activate` after creating the virtual environment.

The command prints proposal generation, each constraint result, simulated human
approval, simulated execution, and the audit location. It appends seven JSON
events to `artifacts/audit/events.jsonl` (relative to the current directory).
Repeated runs preserve existing log bytes and use distinct run IDs.

```powershell
prom-lab run-dummy-loop --deny-approval
prom-lab run-dummy-loop --audit-log artifacts/audit/another-run.jsonl
python -m unittest discover -s tests -v
```

Exit codes: `0` = simulated completion; `2` = constraint rejection or simulated
denial; `1` = loop error, including audit I/O failure. A denial is retained in
the audit log and never invokes the executor.

## Inspect the audit log

These read-only commands inspect the configured log (by default,
`artifacts/audit/events.jsonl` relative to the current working directory):

```powershell
prom-lab audit tail
prom-lab audit tail --last 20
prom-lab audit summary
prom-lab audit by-proposal <proposal_id>
```

Replace `<proposal_id>` with the UUID printed by `run-dummy-loop` or `audit tail`.
`tail` shows the last 10 entries by default, including timestamp, event type,
proposal ID, actor, and a condensed payload. `--last` must be positive. `summary`
shows the total and counts by event type and actor. `by-proposal` prints every
matching event with its full, indented JSON payload.

The original MVP events record proposal IDs inside the proposal payload, not
on every event. Inspection correlates other events via `run_id` only when that
run contains exactly one distinct recorded proposal ID. Explicit proposal IDs
take precedence; ambiguous runs are not assigned an inferred proposal ID.
Actors are read from `payload.actor` when present; existing logs have no actor
metadata and are counted as `unknown`. The summary also shows `agent` and
`human` counts, including zero. Simulated approval never implies a human actor.

A missing log or unmatched proposal produces a friendly message. An empty log
reports zero entries in the summary. Malformed records or read errors produce
an error and exit code 1; entries are never silently skipped or rewritten.
Inspection does not create or modify logs. Approval and execution remain
research-only simulations; the sandbox is not a security boundary.

## Versioning

This project uses Semantic Versioning (`MAJOR.MINOR.PATCH`), starting at `0.1.0`.
`pyproject.toml` is the single source of truth. `prometheus_lab.__version__`
reads installed package metadata; after a version change, reinstall the package.
Release tags must match exactly, for example `v0.1.0`.

Use PATCH for compatible fixes, MINOR for compatible additions, and MAJOR for
breaking stable interfaces. During `0.x`, breaking research changes use a MINOR
bump and must be documented. See [Contributing](CONTRIBUTING.md) for the release
process. Tag pushes build distributions without publishing to PyPI.

## Development & CI

```sh
python -m pip install -e ".[dev]"
pytest
prom-lab run-dummy-loop
```

[CI](https://github.com/Nova-Labs-Research/Prometheus/actions/workflows/ci.yml)
runs tests and a CLI smoke check on Ubuntu with Python 3.10, 3.11, 3.12, and
3.13 for pushes to `main` and pull requests targeting `main`. The smoke check
uses a temporary working directory so audit output stays outside the checkout.

`main` is protected: changes go through pull requests with a human approval and
green CI on all four Python versions. Branch protection is configured in GitHub
settings. See [Contributing](CONTRIBUTING.md) for local setup, review expectations,
and branch protection instructions.

## Layout

```text
src/prometheus_lab/
  config/         Trusted settings, size limits, target namespace
  proposals/      Frozen Pydantic proposal and file-change models
  agent_core/     Dummy proposer, simulated approval, loop orchestration
  constraints/    Path scope, duplicate paths, payload limits, check runner
  audit/          Event schema and local append-only JSONL API
  sandbox/        Simulated executor and result model
  ui/             Typer CLI with Rich output
experiments/target_repo/  Tiny toy codebase
docs/                    Architecture and safety notes
tests/                   Gate, failure, audit, and CLI tests
```

See [architecture](docs/ARCHITECTURE.md) for the event flow and extension points,
and [safety notes](docs/SAFETY_NOTES.md) for the actual assurance boundary.

The MVP has no LLM, credentials, network calls, subprocess runner, real human
review, or recursive self-modification. These are deliberate future research
boundaries, not completed capabilities.
