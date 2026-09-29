# Architecture

## Purpose and authority

This is a research-only orchestration skeleton. The proposer supplies structured
data. Trusted orchestration checks it, records a simulated decision, and invokes
a placeholder executor. The proposal cannot select constraints, rewrite policy,
choose audit paths, or provide executable shell commands through its schema.
The dummy proposal is a readability hypothesis; no evaluation establishes that
it improves anything.

## Flow and events

1. `loop_started`: new run UUID, research-only and simulation markers.
2. `proposal_generated`: full `CodeChangeProposal` plus SHA-256 digest.
3. `constraints_checked`: all constraint results, including failures.
4. `approval_decided`: simulated decision, rationale, and proposal digest.
5. `execution_started`: recorded before invoking the placeholder executor.
6. `execution_completed`: simulated result, planned paths, empty changed paths.
7. `loop_finished`: explicit outcome (`simulated`, `rejected`, or `denied`).

These are seven event types in the successful flow, producing **seven events**.
Constraint rejection finishes after step 3; denial finishes after step 4.
An unexpected exception attempts to append `loop_failed`, then propagates to
the CLI, which exits with code 1. If the store itself fails, recording that
failure is only best effort. A missing terminal event is an incomplete run,
not evidence of success. The CLI's final audit-location message is output, not
an additional event.

## Modules and contracts

- `config`: immutable settings hold audit location, maximum three files, and a
  16 KiB combined UTF-8 content limit by default. The target prefix is a trusted
  constant, `experiments/target_repo/`.
- `proposals`: Pydantic v2 models reject unknown fields. File changes contain
  repository-relative paths and proposed replacement text. Frozen models and
  tuple collections reduce accidental mutation; they do not defend against
  arbitrary Python code in the same process.
- `agent_core`: `ProposalAgent.propose()` supplies the extension point for a
  future proposer. The coordinator owns sequencing. Approval is a separate
  callable, and the MVP decision is always labeled `simulated`.
- `constraints`: checks restrict paths to simple relative Python filenames in
  the target namespace, reject duplicate paths case-insensitively, and bound
  file count/content size. Absolute paths, traversal, backslashes, alternate
  data streams, and Windows device names are rejected. All checks run; a check
  exception produces a failed result. An empty check set raises an error.
- `sandbox`: verifies approval matches the full proposal digest and reruns the
  default constraints. It returns a simulation result without touching target
  files. Direct use of this class does not create audit records; the coordinator
  is the auditable entry point.
- `audit`: one JSON object per line, UUID event/run IDs, UTC timestamps, schema
  version and event payload. Each append opens in append mode, flushes, and
  requests `fsync`. There is no update/delete API.
- `ui`: Typer exposes `prom-lab run-dummy-loop`; Rich displays progress and clear
  simulation disclaimers. `--deny-approval` exercises the denial path.

## Extension boundaries

A real LLM agent can implement `ProposalAgent`, but validate its output as a
`CodeChangeProposal` before entering this workflow. Add invariant functions to
`DEFAULT_CHECKS`; keep trusted policy outside agent-editable content.

Real review requires a separate authenticated human decision interface and
decision schema, binding the exact proposal and relevant policy/context. The
current simulated decision is not evidence of human authorization.

A real executor requires a separately designed and reviewed isolation system,
filesystem containment with symlink/reparse-point handling, resource limits,
network/credential controls, and baseline/patch verification. Do not simply add
`exec` or a subprocess to this placeholder. Evaluation and promotion require
their own evidence and human authority gates.

The current audit store supports one trusted local writer. Concurrent writers,
partial-write recovery, authenticity, tamper evidence, retention and replication
require additional design. Completion is recorded only after the result append
succeeds; a logging failure stops progression to subsequent stages.

## Dependency references

The schema implementation uses [Pydantic model validation and serialization](https://docs.pydantic.dev/latest/concepts/models/).
The CLI uses a callback to retain the named subcommand with a single command,
as described in [Typer's command documentation](https://typer.tiangolo.com/tutorial/commands/one-or-multiple/).
