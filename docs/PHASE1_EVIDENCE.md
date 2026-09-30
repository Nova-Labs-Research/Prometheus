# Phase 1 simulation evidence contract

This milestone tests the supported simulated workflow only. It introduces no
candidate execution, model calls, research runs, new research parameters, or
promotion. The executor remains a placeholder. Builder != Reviewer != Authority.

## Verification contract

`prom-lab audit verify` reads the same configured JSONL file as the other audit
commands. It never repairs, appends, or rewrites evidence. The reader rejects
malformed JSON (including NaN/Infinity literals), duplicate keys (including nested keys), and invalid typed event
envelopes. The verifier then checks the recorded `simulation-v1` workflow:

- Complete event envelopes, supported schema, timezone-bearing timestamps and
  unique event IDs across runs; runs may be interleaved, preserving their order.
- Explicit simulation markers and positive integer file/content limits. New
  `loop_started` payloads include `evidence_contract` and `constraint_limits`.
  Existing inspection commands still read legacy logs. Verification labels logs
  without the new snapshot incomplete rather than guessing historical limits.
  Legacy records are still checked for contradictions: invalid evidence takes
  precedence, and explicit failure remains failed rather than a normal outcome.
- Exact required payload fields, full proposal/decision/result data without
  filling defaults, proposal digest, named constraint results, approval binding,
  execution binding, planned paths, and an empty reported changed-file list.
- Claimed constraint passes agree with the proposal and recorded limits. A check
  may fail closed even when the deterministic check would otherwise pass.
- The exact success/rejection/denial event sequence. A failure may terminate a
  prefix, including failure to record the initial event. Nothing may follow a
  terminal event. Contradictions are invalid even if a failure was also recorded.

Per-run statuses are `completed_simulation`, `rejected`, `denied`, `failed`,
`incomplete`, and `invalid`. Only `completed_simulation` has `valid_completion`.
The aggregate requires a nonempty log and every run to complete simulation;
one failed, partial, contradictory, denied, or rejected run makes it false.
The CLI reports this aggregate separately from consistency.

Exit codes for **verify**: `0` for all runs having consistent completed,
rejected, or denied outcomes; `2` for missing/empty evidence or any failed,
incomplete, or invalid run; `1` for parse/validation/read errors. The preexisting
inspection commands retain their existing exit behavior, including `0` for a
missing log. A summary or tail listing is not a verification verdict.

## Requirements to tests

All tests below are local deterministic fixtures. Names are test methods in the
specified file; no proposed research sample or budget is changed.

| Requirement | Acceptance evidence |
| --- | --- |
| Approval, rejection and denial order; skipped stages | `test_phase1_workflow.py::test_exact_approval_rejection_and_denial_sequences` and existing `test_workflow.py` gate tests |
| Stop after audit failure, including initial and terminal writes | `test_audit_failure_at_every_append_stops_later_stages` injects before/after-persistence errors at all seven successful-flow appends; only best-effort failure recording follows |
| Preserve original errors when the failure writer is unavailable | `test_unavailable_failure_writer_preserves_original_error` |
| Proposal/approval/executor/check/reporting failures remain failures | `test_component_errors_preserve_failure_and_skip_later_stages`, `test_constraint_exception_is_retained_and_rejects`, `test_reporting_failure_never_records_success_terminal` |
| Simulation leaves disposable target bytes and file inventory unchanged | `test_simulation_observed_no_target_writes`; fixture code is never imported or executed |
| Exact file and aggregate UTF-8 boundaries | `test_exact_file_and_utf8_boundaries`: current defaults, exact limit and one over, including multibyte text and combined files |
| Approval binds all mutable proposal content/metadata | `test_digest_binds_content_paths_and_metadata`; roundtrip stability and field-specific mismatches |
| Complete, partial, malformed and contradictory evidence stays distinct | `test_audit_contract.py` prefix, event-order, payload-contradiction, missing-field, schema, and duplicate-ID tests |
| No default reconstruction or guessed legacy limits | `test_required_fields_cannot_be_reconstructed_from_defaults`, `test_legacy_unsupported_schema_and_naive_timestamps`, `test_complete_simulation_and_nondefault_limit_snapshot` |
| Read-only behavior and syntactic ambiguity rejection | `test_verification_is_read_only_and_does_not_authenticate`, `test_reader_rejects_syntax_ambiguity_and_invalid_envelopes_without_writes` |
| Audit CLI coverage | `test_cli_missing_and_empty_logs`, `test_cli_malformed_records_and_read_errors`, `test_cli_tail_summary_and_by_proposal_preserve_bytes`, `test_cli_explicit_ids_ambiguous_legacy_runs_and_actors`, `test_cli_verdicts_and_exit_codes` |
| Review regressions: legacy contradictions and nonstandard JSON | `test_legacy_contradictions_are_invalid_not_merely_incomplete`, `test_reader_and_cli_reject_nonfinite_json_literals` |
| Independent transcript and actual store failure path | `test_hand_authored_transcript_has_independent_expected_verdicts` uses a fixed externally calculated digest; `test_real_store_fsync_error_stops_proposal_and_retains_failure` inspects actual file bytes after injected fsync failure |

Run from the checkout using the existing environment:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

CI defines pytest and a CLI smoke test on Ubuntu/Python 3.10–3.13. No lint or
type-check command is configured. A local Windows result does not establish
those other-platform results. Keep smoke output in a temporary working directory.

## Assurance limits and stopping point

Consistency is not authenticity. A fabricated but internally consistent
transcript can pass. The limits snapshot is a claim in the same unauthenticated
log; timestamps do not establish an external clock or trusted ordering. The
verifier reruns only trusted lexical/size checks, never proposed code. It cannot
establish filesystem containment, real human approval, actual effects, identity,
anti-replay protection, completeness of all runs ever attempted, or improvement.
The no-write fixture corroborates only the supported simulation in that fixture.

JSONL remains a single-writer local store without immutable storage or atomic
effect/log transactions. A failed append can leave partial bytes; these are
preserved and refused by the reader. A persisted terminal record followed by a
recorded failure is invalid. If an error happens after a terminal record is
persisted and all subsequent failure recording is unavailable, the log alone
cannot reveal that unrecorded failure. Preserve process errors alongside logs;
external evidence and reconciliation belong to a separately approved phase.

Stop after implementation and local validation for human review. No executable
lab or research search is authorized by this milestone. Later work must preserve
design registration before search, candidate lock before final evaluation,
protected evaluation without feedback to improvement, external authority, and
separate promotion approval.
