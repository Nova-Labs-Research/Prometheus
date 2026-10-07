# Observation and recovery rehearsal

This is a code-only contract and temporary-store harness. No host reporter,
identity authentication, pipe, service, VM adapter, guest fixture or model exists
in this layer. Both `authenticate_external_peer()` and the existing VM boundary
unconditionally raise unsupported-operation errors. There is no enable flag.

See [the PROPOSED design](EXTERNAL_OBSERVATION_RECOVERY_PROPOSED.md) for the future
activation requirements. That design is preserved; this implementation covers its
bounded next contract milestone, not its host/guest deployment plan.

## Contract and evidence distinctions

`lab_control.observation.ObservationContext` freezes the existing mock request,
installation/store/operation/disk identities, initial disk hash, deployment and
recovery policy hashes, test peer roster and explicit test freshness/lease values.
`MockContextConsent` binds the canonical complete context excluding the consent
itself, with the original mock approval ID. Altering expanded context requires
rebinding this mock consent; it is never a real authenticated human decision.
These supplied test values are not new approved research parameters.

Each `Observation` binds the full context digest plus explicit store, attempt,
approval, operation, run, process epoch, VM and manifest. It contains a unique
observation ID, challenge ID, per-reporter sequence, sample interval and exact
canonical claim length/SHA-256. Strict immutable models reject extra fields,
unsupported versions, role/category substitution and malformed payloads.

`Received` supplies a separate **test-only** peer and ingress clock. Payload fields
cannot assert authenticated origin. Observer/broker/watchdog data are classified
`test_host_claim`; collector data are `guest_claim`. All clocks and instance labels
are prepared claims, not OS-derived provenance. Passing consistency checks proves
neither human intention, reporter authenticity nor true guest measurements.

Every `RecoveryView` has `confers_authority=False`, `valid_completion=False`,
`actual_vm_state="unknown"` and `requires_external_quarantine=True`. The last value
is a requirement, not a statement that this code enforced any quarantine.

## Replayed state and recovery rules

`replay()` recomputes state from context and typed events without external effects:

- `challenge`: exact peer/context, unique ID and bounded positive lifetime.
- `received`: exact challenge/peer/category and binding; ordered sample and ingress
  clocks; contiguous per-reporter sequence; digest/length consistency.
- `fence` and `dispatch`: conceptual transitions only. Both check current mock
  approval expiry, lease and fresh off/armed claims. Dispatch requires one unused
  fence. Stop intent or interruption prevents later dispatch. A delayed request
  past expiry is denied while unresolved watchdog protection remains required.
- `stop_requested`: records uncertainty, never calls a stop method.
- `interrupt`: invalidates outstanding challenges/readiness and inhibits all later
  dispatch in this scenario. Fresh recovery challenges remain possible.
- `abandon`: explicit failed/unverified bookkeeping only, requiring fresh settled
  broker claims including prior-instance fencing, then a later off sample, all
  after the most recent fence/dispatch/stop/interruption barrier.

One identical redelivery with the same peer and observation is a no-op: it does
not advance the ingress clock, consume a second challenge or write another record.
Conflicting duplicate bytes, reused challenges/sequences, missing sequence segments,
stale samples, reversed sample order and contradictory overlapping intervals are
retained as issues and quarantine the scenario. Contradictions are compared with
all retained same-peer samples, not just the latest report.

Ordered off/running/off can be consistent when dispatch and stop intents explain
it. Unknown VM/job state, missing broker fencing or off observed before provider
quiescence cannot support abandonment. Collector `finished` never substitutes for
host/broker reports. Later clean samples cannot erase contradictions or gaps.
Dispatch denial, watchdog unavailability and unexplained transitions may be
explicitly abandoned after fresh recovery evidence, but their issue labels remain.
No state is a valid-completion verdict, even after unverified abandonment.

## Version 3 store and limits of durability

`lab_control.observation_store.ObservationJournal` uses a **separate version 3**
SQLite store with one immutable mock attempt. Existing v1/v2 stores/APIs remain
unchanged; versions reject each other without migration. Creation consumes this
attempt within that store. There is no global approval registry: using another
store or rolling back an entire valid database can reuse claims. This is not
cross-installation replay protection or external authority.

The store uses exclusive creation, existing-only open, DELETE rollback journaling,
FULL synchronous mode, serialized `BEGIN IMMEDIATE` writes, strict schema/hash and
semantic replay, and readback before acknowledgment. Expected-head checks serialize
competing decisions. Snapshots use read-only connections and do not repair journals.
Reopen permits SQLite crash recovery but never restores dispatch ownership.

After reopen, a first new write must durably record `interrupt`; identical existing
receipt lookup/redelivery stays read-only. Interruption fences an old live handle's
subsequent dispatch through semantic replay. Reopen alone does not write an
interruption, stop a process, observe a real restart or cancel a provider request.
The caller supplies interruption time and all subsequent same-scenario clock data;
no continuity across real boots/processes is authenticated here.

Write/commit/readback exceptions poison that handle. Before-commit failure leaves
no record; after-commit acknowledgment loss may leave a complete record. Reopen,
inspect, record interruption and reconcile; never blindly retry conceptual dispatch.
The six fixed subprocess fault cases terminate before/after interruption, fence
and dispatch commits. They test process death, not power loss or real provider jobs.

The implementation caps each stored JSON envelope at 64 KiB and a test store at
512 records including context; exhaustion rejects new writes without deleting old
records. These are parser/harness limits, not OS quotas or research-budget changes.
A full store may lack room for an interruption/recovery record and must remain
quarantined for manual inspection; no repair/compaction bypass is provided. Replay
is bounded by those stored limits but SQLite/file internals and caller objects are
not a hostile-input isolation boundary. Pure `replay()` expects trusted local
constructed inputs and does not implement a transport allocation limit.

Filesystem ancestors, caller, Python process and host remain trusted. No new ACL,
reparse/hardlink-safe privileged file layer, signing key, rollback anchor, genuine
approver identity or authenticated clock is provided. Hashes are consistency checks,
not authenticity. Physical media/power-loss guarantees and live stop enforcement
are untested. New external evidence must never be relabeled from these test stores.

## Requirement-to-test mapping and validation

All new tests are in `tests/test_observation.py` and use prepared data and temporary
SQLite stores. Test subprocesses run fixed harness code only.

| Requirement | Evidence |
| --- | --- |
| Exact full context/consent/report binding | Individual binding and expanded-context mutations with valid positive controls; mock consent rebinding |
| Trust separation and immutable schema | Unsupported-auth/VM seams, forged peer/category/schema fields, payload digest/length and frozen views |
| Missing/replay/conflicting evidence | Independent stored-row checks, identical-redelivery database bytes, gap/duplicate/overlap and late-history conflict tests |
| Expiry/stop/recovery ordering | Exact TTL/lease/freshness boundaries, stop-before-dispatch, pending/unfenced broker, off-after-quiescence and invalidation tests |
| Persistence and concurrency | SQL row counts, read-only snapshot bytes, stale-head races, two-handle interruption and serialized competing writes |
| Fault and invalid history | Six actual process exits, exception/ack/readback failures, rehashed semantic corruption, schema/version checks, malformed bytes and quota exhaustion |
| Limits remain explicit | Whole-store rollback counterexample, no effect/auth adapter invocation, all authority/completion flags false |

Run with the existing environment:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Run the installed `prom-lab run-dummy-loop`, `prom-lab audit summary` and
`prom-lab audit verify` in a fresh temporary directory. No lint/type-check command
is configured.

Final local results on the implemented code:

- `.\.venv\Scripts\python.exe -m pytest -q`: **192 unique tests and 440 subtests
  passed**, including **42 new tests and 88 new subtests** (37.60 seconds).
- `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`: the same
  **192 tests passed** (34.957 seconds); this does not add unique tests.
- The three installed CLI commands above passed in a fresh temporary directory,
  with seven simulation events. No guest or candidate code was executed.
- Tracked/new-file whitespace and relative-document-link checks passed.
- The initial targeted run had one failing subtest because its intended overlapping
  sample intervals did not overlap. The fixture was corrected; final tests pass.
- CI/other OS-Python combinations, native authentication, power-loss durability and
  live isolation were not run. Nothing was committed, pushed or activated.

A separate read-only AI reviewer identified and verified fixes for stop-before-
dispatch ordering, complete expanded-context mock consent, interruption before
reopened writes, misleading actual-quarantine labeling, and late samples conflicting
with older history. Tests gained independent positive controls, stored-row/byte
checks and six process-death cases. The reviewer re-read final source/tests/docs and
reported no remaining substantive findings. This was independent AI static review,
not independent test execution, human authorization or security certification.

## Next decision

Stop for human review of this contract and evidence. The next recommended step is
a narrowly bounded, read-only OS identity/transport feasibility test plan, with
exact permissions and identity separation approved before any native integration
is implemented or exercised. Per-VM start/stop rights remain unproven. Real services,
accounts, ACLs, pipe/socket registration, privileged queries and VM activity require
separate authorization; no live implementation can reuse these mock approvals.
The 4 GiB total guest / 512 MiB fixture distinction and all other PROPOSED research
settings remain unchanged. Preserve both preregistration stages, protected evaluation,
no final-test feedback to improvement, and separate human promotion authority.
