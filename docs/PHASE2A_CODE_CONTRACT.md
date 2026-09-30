# Phase 2A code-only approval and evidence contract

This human-authorized milestone implements simulation contracts and local unit
tests. It does not implement
external authority, VM operations, guest isolation, a collector, OS identities,
durable storage or research execution. The separately documented
[durable simulation journal](DURABLE_SIMULATION.md) adds persistence without
changing this in-memory API's guarantees. Builder != Reviewer != Authority.

## Implemented boundary

The new `src/prometheus_lab/lab_control/` package is separate from Phase 1.
The existing executor and CLI remain simulation-only and unchanged. There is
no Phase 2A CLI command, Hyper-V adapter, command construction, provisioning,
credential generation, service/ACL setup, dependency addition or execution flag.
`boundary.request_vm_operation()` always raises `VMOperationsUnavailable`,
without inspecting its arguments. Hostile `enabled`, `authorized`, `force` or
backend values cannot activate it. This is absence of execution capability in
the package, not an OS containment boundary against hostile Python/host code.

The public data contracts in `contracts.py` use strict, frozen Pydantic models
with forbidden extra fields and revalidation of existing nested instances.
All fields must be supplied. Nested state uses models and tuples, not mutable
payload dictionaries. Validation also rejects malformed models produced using
`model_copy(update=...)` or `model_construct` when they reach an API boundary.

`Manifest` binds the design, fixture, base image, runtime, input image,
isolation configuration and policy digests, VM/run/process-epoch IDs, all typed
limits, output schema, operation and unavailable execution mode. Its digest is
SHA-256 over sorted compact ASCII-escaped JSON. The approved 4 GiB guest and
512 MiB fixture split is the only supported memory profile. Other limits are
explicit supplied PROPOSED values, not enforcement or research approval.
File/image/configuration digests are claims: this package does not open those
files, measure their contents or attest the actual environment.

`MockApproval` binds the manifest digest, run, process epoch, unique approval ID,
mock identity, decision, aware issuance/expiry times and an issuance monotonic
tick. The identity kind can
only be `mock`. A label such as "Administrator" is still unauthenticated data.
No SID, credentials, external authentication flag or real-authority mode exists.
Changing these claims does not create an authenticated approval.

## State and time semantics

`SimulationLedger` receives an immutable `Context` with an explicit epoch and
caller-supplied clock origin. `assess()` serializes calls with one process-local
lock and has these possible normal paths:

```text
assessment_started -> approval_denied
assessment_started -> simulation_consumed -> execution_unavailable
```

The latter path is `blocked`, never execution success. `Assessment`, approval
and verifier results cannot confer authority; `valid_completion` is always
false for Phase 2A results, including consistent mock evidence.

Each well-formed attempted approval ID is reserved in memory before the first
event append, including denials. Reuse is denied as replay even if the record's
identity, decision or content changes. Each attempt ID must also be unique.
Concurrent attempts can consume an approval at most once in that ledger object.
A different approval ID can describe another attempt at the same run: no real
run scheduler or cross-ledger policy is implemented.

Checks fail closed on epoch/run/digest mismatch, reversed wall or monotonic
clock, issuance before the context origin or after current wall time, expiry,
overlong TTL and mock denial. Exact expiry is denied (`effective_now >= expiry`).
Effective time is the later of supplied wall time and origin plus monotonic
elapsed time. Additionally, elapsed time since the approval's supplied issuance
tick must be less than its declared lifetime. This prevents a forward wall jump
at issuance followed by a stalled wall clock from extending that lifetime.
Issuance ticks before the context origin or after the current tick are denied.
The maximum TTL comes from the bound manifest. Clock readings and the origin
are supplied simulation data, not trusted OS time, monotonic attestation or
proof of freshness outside the ledger. Different timezone offsets normalize
to UTC. Malformed/naive timestamps are rejected.

## Failure, durability and restart limits

Exceptions after an attempt is admitted poison the ledger. The original
exception propagates; a best-effort `attempt_failed` step is appended. Further
calls raise `LedgerUnavailable`, so no additional normal stages occur. Failure
recording never replaces the original exception. Snapshots expose the faulted
flag even when both the original append and failure append fail. A failure after
a terminal event is contradictory evidence and is invalid, not a success.

Malformed API calls and duplicate attempt IDs raise before admitting an attempt;
they are not logged. A snapshot does not prove that all calls/attempts were
recorded. There is no disk write, fsync, transactional effect/evidence operation,
append-only store, external watchdog or atomic crash recovery in this package.

**One-use protection is in-memory only.** A new ledger created with the same
epoch and original clock loses the old reservations and can consume the same
mock approval again. A regression test explicitly demonstrates this limitation.
A different epoch rejects the old record, but callers currently choose the
epoch. There is no restore/resume API and no claim of replay prevention across
restart, independently created ledgers or malicious manipulation of Python
internals. A crash before exporting evidence can lose every record. These
limitations are acceptable only because no external authority or execution is
available. Durable external admission remains a separately reviewed milestone.

## Evidence verification

`verify.verify_evidence()` consumes an `Evidence` instance or JSON text and
performs no file IO. `parse_contract()` rejects duplicate JSON keys, non-finite
literals, wrong types, missing fields, extra fields and unsupported schemas.
Verification recomputes manifest bindings and denial reasons, checks exact step
order, ID reuse, clock progression and terminal/prefix consistency across the
bundle. Nothing may follow a failed or incomplete attempt.

| Verdict | Meaning |
| --- | --- |
| `consistent_simulation` | Every recorded attempt has a consistent denied or execution-unavailable terminal; no execution completion or authenticity claim |
| `failed` | Recording/internal failure is present and no stronger contradiction was found |
| `incomplete` | Empty evidence or a valid nonterminal prefix without a recorded failure |
| `invalid` | Malformed or contradictory claims, including failure after a terminal or continuation after failed/incomplete evidence |

Invalid evidence takes precedence over failure/incompleteness. All verdicts
have `valid_completion=False` and `confers_authority=False`. Phase 1's existing
`completed_simulation` contract is unchanged; it must not be conflated with this
new schema. Evidence can be fabricated consistently, including a fake identity,
context or clock. A test demonstrates that such a bundle can be consistent while
still conferring zero authority. No signatures, external witness, immutable
storage or evidence authenticity are implemented. Parsing arbitrary huge input
is not resource-isolated or transport-bounded here; the proposed bounded output
collector remains unimplemented.

## Requirements to local tests

All names below are methods in `tests/test_phase2a_contract.py`. These tests run
trusted unit-test code and prepared data only, never a candidate or guest fixture.

| Requirement | Tests |
| --- | --- |
| Mock approval cannot execute or authenticate | `test_mock_approval_only_reaches_unavailable_boundary`, `test_denial_never_calls_boundary`, `test_mock_identity_cannot_assert_os_or_external_authority` |
| Complete manifest bindings and approved memory profile | `test_manifest_has_an_independent_canonical_digest`, `test_every_changeable_manifest_field_is_bound`, `test_fixed_schema_operation_execution_and_memory_split_cannot_be_changed` |
| Required fields, strict types and immutable nested context | `test_required_fields_extra_fields_and_strict_types`, `test_frozen_nested_context_has_no_mutable_aliases`, `test_model_copy_and_construct_bypasses_are_revalidated` |
| Expiry, freshness, clocks and changed context | `test_expiry_exact_boundary_and_ttl`, `test_stale_future_naive_and_nonpositive_approval_times`, `test_clocks_cannot_reverse_or_stall_past_expiry`, `test_epoch_run_and_digest_mismatches_deny_before_boundary` |
| Reviewer timing regression | `test_monotonic_issuance_binds_ttl_despite_forward_wall_jump` independently checks just-before, exact and after-expiry monotonic values following a forward wall jump |
| Replay, races and honest restart limitation | `test_replay_is_denied_even_if_record_content_changes`, `test_concurrent_attempts_consume_at_most_once`, `test_restart_protection_is_not_claimed_or_restored` |
| No input enables VM boundary | `test_boundary_is_unconditional_and_does_not_touch_inputs_or_os`, `test_unexpected_boundary_return_poison_and_failure_evidence` |
| Append/terminal/failure-writer faults | `test_every_append_fault_before_and_after_recording_stops`, `test_failure_writer_unavailable_preserves_original_and_no_normal_verdict`, `test_denial_terminal_append_fault_is_not_consistent` |
| Missing, contradictory, duplicate and malformed evidence | `test_empty_and_all_partial_prefixes_are_incomplete`, `test_contradictory_duplicate_missing_and_out_of_order_steps`, `test_changed_evidence_context_and_replay_claims_are_invalid`, `test_no_attempt_after_failed_or_incomplete_attempt`, `test_malformed_json_nested_duplicates_nonfinite_and_wrong_types` |
| Read-only consistency is not authenticity | `test_verifier_is_pure_and_cannot_authenticate_fabricated_evidence` |

Validation uses the existing environment without installation:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase2a_contract.py -q
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

CI also specifies a CLI simulation smoke test in a temporary working directory.
No lint/type-check command is configured. Local Windows tests do not establish
the Ubuntu/Python 3.10-3.13 CI matrix. No version/dependency change or release
build is part of this milestone.

Validation completed locally on Windows/Python 3.13 on 2026-09-30:

- `.\.venv\Scripts\python.exe -m pytest -q`: **71 unique tests and 223 subtests passed**.
  This includes 28 Phase 2A tests and 118 Phase 2A subtests; repeated runners do
  not create additional unique tests.
- `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`: **71 tests passed**.
- Existing installed CLI, from a fresh temporary working directory:
  `prom-lab run-dummy-loop`, then `prom-lab audit summary` and
  `prom-lab audit verify`: all exited 0,
  producing seven Phase 1 simulation events. This smoke executes no candidate.

A separate static AI review identified a timing correctness gap: process-origin
monotonic arithmetic alone could extend an approval after a forward wall-clock
jump. The required issuance monotonic tick, lifetime validation and regression
above fix that gap. The reviewer checked the fix and reported no outstanding
high/medium issue in scope. Its own pytest launch was blocked by sandbox process
permissions before execution; its review therefore does not supply another
passing test run. The successful runs above were performed by the implementing
agent with the authorized permissions. No configured test failed. Lint/type
checks are not configured; other Python/OS CI jobs, VM tests and research were
not run.

## Stop and next decision

Stop after local validation and separate AI review for human review. A separate
AI review is another code-review pass, not independent security certification.
Before any executable lab, a human must separately approve the external authority,
service privilege/ACL manifest, downloads/setup,
VM boot and exact fixture suite. Current Hyper-V readiness does not authorize
those actions. Existing proposed research sizes/budgets/thresholds remain
unchanged. Preserve design registration before search, candidate lock before
final evaluation, protected final evaluation without feedback to improvement,
external human authority and separate promotion approval.
