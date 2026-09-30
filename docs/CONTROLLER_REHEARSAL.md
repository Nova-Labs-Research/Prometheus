# Code-only controller rehearsal

`lab_control.controller.CodeOnlyController` adds an orchestration layer over the
existing durable simulation ledger. It has **no real launch adapter, external
approver, OS observer, watchdog, socket collector, service or CLI**. Supplying
mock readiness cannot activate those capabilities. A manually installed guest
does not change the package's unconditional unavailable VM boundary.

The separate [durable controller journal](CONTROLLER_JOURNAL.md) now provides
opt-in durable preparation, rejection, intent and unverified reconciliation.
It does not change this API or migrate its existing version 1 store.

## Implemented sequence

1. Revalidate the complete frozen request, including nested models. Byte envelopes
   have a 64 KiB ceiling checked before UTF-8/JSON parsing. Reject duplicate keys,
   unknown/missing fields, nonfinite numbers and unsupported schemas. This parser
   ceiling implements a bounded code seam; it does not enforce transport or guest
   resource limits. Already-created Python objects are trusted local API inputs,
   not a bounded hostile IPC interface.
2. Read and semantically verify the existing SQLite evidence. Pending operations
   require explicit recovery; missing/mismatched active epochs stop admission.
   The controller never creates a store, begins a session, or repairs evidence.
3. Check `MockReadiness` against the exact store, attempt, VM, manifest digest and
   decision clock. Check reported off state, zero adapters, matching CPU/static
   memory, writable evidence, output capacity, and ready collector/watchdog.
   All readiness fields are explicitly unauthenticated `source="mock"` claims.
   Matching clocks are a consistency rule, not proof of current host conditions.
4. Failed preflight returns `preflight_blocked` with all gate reasons. It performs
   no ledger mutation and consumes no approval/attempt ID. Its receipt is
   **unpersisted**: retry is possible, and losing the receipt loses that rejection
   history. Verification labels it `unpersisted_preflight`, never durable admission.
5. Passing preflight calls the existing `DurableLedger.assess()`. That API checks
   mock denial, binding, expiry and replay; commits reservation before its pure
   unavailable boundary; then commits `denied` or `blocked` evidence. There is
   no launch-capable callback or backend in the new controller constructor.
6. Read back the committed request/outcome before returning the controller
   receipt. Missing, pending, contradictory or unreadable evidence cannot return
   a normal assessment receipt. An exception after validation poisons this
   controller instance. It performs no retry, recovery, stop action or later stage.

`verify()` is read-only and revalidates a receipt against the journal. It returns
`consistent_rehearsal`, `unpersisted_preflight`, `incomplete` or `invalid`.
**Every receipt and verification has `confers_authority=False` and
`valid_completion=False`.** Malformed API inputs raise before admission and are
not logged; they do not poison a healthy controller. Unexpected failures propagate.

## Persistence and threat boundary

Only the existing attempted manifest/approval/clock and denied/blocked outcome
are journaled. The full readiness report and controller receipt are not persisted
or authenticated. A fabricated passing readiness report can accompany a genuine
durable mock attempt; a consistent verdict cannot establish its provenance or
whether a controller actually observed it. No fields accept a claim of real
authentication, successful launch or valid completion.

Thread serialization within a controller and SQLite serialization across handles
preserve the existing one-use reservation rules. Reopening never restores an owned
session. A new epoch and fresh mock records remain necessary; reserved approval IDs
remain consumed. Reservation failure prevents the unavailable-boundary call;
post-reservation interruption leaves pending evidence. Lost terminal acknowledgment
or readback failure can leave a committed terminal but returns an exception, not a
receipt. Explicit existing recovery abandons work, never resumes it.

No storage schema migration is introduced. Existing crash, clock, malicious
rollback, trusted-directory/caller and power-loss limitations in
[durable simulation](DURABLE_SIMULATION.md) still apply. Do not attach real effects
to the unavailable call inside the SQLite transaction. Readiness checks and real
effects would otherwise have an unresolved race. The new code is not safe for
real execution merely because unit tests pass.

## Requirement-to-test mapping

All new tests are in `tests/test_controller.py`; they use prepared data and
temporary SQLite stores only, never the installed VM or guest code.

| Requirement | Evidence |
| --- | --- |
| Commit before unavailable boundary, then reconcile | Independent SQL inspection in `test_passing_preflight_reaches_only_disabled_boundary_after_commit` |
| Reject every missing/failed gate before admission | `test_every_failed_readiness_gate_prevents_consumption_and_boundary`, `test_missing_readiness_fields_stop_before_store_and_do_not_poison`; unchanged database bytes and boundary spies |
| Exact store/attempt/VM/digest/time/limit binding | `test_all_manifest_changes_invalidate_old_preflight_digest`, `test_rebound_readiness_does_not_rebind_an_old_approval`, `test_output_reservation_exact_boundary_is_not_a_real_quota` |
| Denial, exact expiry, replay, reopen, concurrency | `test_denial_and_exact_expiry_never_reach_boundary`, `test_concurrent_requests_consume_approval_only_once`, `test_replay_survives_reopen_with_new_controller_and_epoch`, `test_reopened_handle_never_automatically_resumes_session` |
| Recording/commit/readback failure stops later stages | Reservation fault, pending recovery, unexpected boundary return, lost acknowledgment and readback-failure tests |
| Incomplete/contradictory receipts cannot imply completion | Receipt tampering and missing/pending/recovered-evidence tests |
| Bounded, strict, immutable inputs; no OS/launch hooks | Envelope boundary, malformed JSON, strict schema and immutable-request tests, with subprocess/network/system-call spies |
| Consistency is not authority or provenance | Fabricated-claims test and read-only verifier test; all authority/completion flags remain false |

Run the existing environment without installation:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py -q
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

The existing CLI simulation smoke check runs in a temporary directory. No lint or
type-check command is configured. A separate static review pass by the implementing
agent is not independent review or security certification. No live VM/OS behavior
is established by these tests.

Validation for this milestone: the full local pytest suite passed **123 unique
tests and 317 subtests**, including **26 controller tests and 68 controller
subtests**. The same 123 tests passed under unittest; these are not additional
unique tests. The temporary-directory `run-dummy-loop`, `audit summary` and
`audit verify` smoke passed with seven simulation events. The first targeted
controller run also passed (24 tests, 53 subtests); review added the changed
approval-binding and missing-readiness regressions before the full runs.

The implementing agent's separate static review pass found no outstanding
in-scope defect. It explicitly checked the unpersisted-readiness limitation,
disabled boundary, post-commit readback failure, missing/pending evidence and
old-approval/new-manifest mismatch. This was not an independent AI/human review.
No configured check failed. Other OS/Python CI jobs, live activation and isolation
tests were not run, and no new commit or push was made.

## PROPOSED activation changes: separate approvals required

No item below is activated by this code. There is no `enabled` flag to flip.
The smallest recommended next development slice is durable preflight/effect-intent
and external-observation reconciliation, still without VM operations. Before live
activation, review and approve an exact deployment/privilege manifest covering:

1. **Authenticated human approval:** choose an OS-authenticated local approval
   interface and exact allowed user SID(s), distinct from the builder/fixture.
   Bind decisions to canonical manifest, VM/run/epoch, operation, one-use ID and
   expiry. Require fresh approval after restart. Do not convert `MockIdentity`,
   JSON labels, copied receipts or reported installation status into authority.
2. **Controller and broker:** specify executable hashes, identities, local IPC ACLs
   and per-VM start/query/stop rights for the exact approved VM. Proposed state
   location is `D:\Prometheus-Lab-VM\control`. Do not add broad Hyper-V
   Administrators membership or use LocalSystem as an implicit fallback. A real
   per-VM permission design must be demonstrated and approved before implementation
   or service registration. No PowerShell command or service definition is shipped.
3. **Durable admission and evidence:** persist preflight reports, effect intent,
   consumption and independently obtained lifecycle observations before enabling
   effects. Specify store ACLs, flush/acknowledgment policy, crash reconciliation,
   clock/boot-epoch source and external rollback anchor, or explicitly accepted
   limitations. Proposed evidence path is `D:\Prometheus-Lab-VM\evidence`; the
   plan's 1 GiB retention allowance remains PROPOSED. Reserve space for failures
   and metadata too; this rehearsal only compares a claimed output-byte allowance.
4. **Independent watchdog and collector:** approve separate service identities and
   narrowly scoped stop/observation rights, deadlines, failure handling, and any
   Hyper-V socket endpoint GUID/registration/ACL. The collector must have no VM
   control rights. Apply bounded framing before decoding, inert host-generated
   filenames and exact VM/run binding. No socket or watchdog is provided here.
5. **Guest enforcement and sealed baseline:** separately inspect/hash the installed
   powered-off disk and approved runtime, prepare immutable inputs and approve any
   guest account/cgroup/device changes. Enforce and measure the approved 4 GiB
   guest / 512 MiB fixture split; static VM memory alone is insufficient. Retain
   zero NICs/shares and require proof of actual controls before fixture approval.
6. **Activation and stop point:** explicitly approve the selected files/services,
   identities/ACLs/endpoints, reversible removal plan and first bounded offline
   fixture suite only after the above are reviewable. Unknown stop state, missing
   watchdog, unavailable evidence or ambiguous recovery blocks new starts. Never
   auto-retry a launch or delete failures. No agents, models or research runs.

Exact service SIDs, endpoint GUID, measured guest hashes and rights are deliberately
unresolved, not guessed or generated. The VM plan and other PROPOSED research
sizes/budgets/thresholds remain unchanged. Preserve design preregistration before
search, candidate lock before final evaluation, protected final evaluation without
feedback to improvement, external human authority and separate promotion approval.
