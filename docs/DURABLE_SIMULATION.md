# Durable simulation journal and crash recovery

This milestone adds local persistence for mock approval/evidence contracts.
It never grants external authority or enables VM operations. No model, candidate,
guest fixture or research experiment runs. The existing Phase 1 JSONL API and
in-memory `SimulationLedger` retain their own documented semantics; the new
`lab_control.durable.DurableLedger` is an explicit, separate API with no CLI.

## Storage and threat model

The implementation uses Python's standard-library SQLite on a trusted local
filesystem with SQLite-compatible locking. It explicitly creates a new database
with exclusive file creation; opening/reopening requires an existing database
and caller-pinned UUID store identity. The UUID is a label, not a credential.
Missing, wrong-identity, corrupt, unexpected-schema and future-version stores
fail closed. Creation never overwrites a file or orphan journal. A failed
creation remains for inspection; there is no automatic repair or migration.

Supported storage uses SQLite DELETE rollback journaling and per-connection
`synchronous=FULL`. Writes use `BEGIN IMMEDIATE`, so validation, replay checks,
reservation and journal metadata updates occur under the same writer lock.
Separate threads/handles/processes serialize through SQLite; busy timeout is
five seconds. Only one pending operation globally is supported. This deliberately
small contract is not a scalable job queue.

Two tables hold schema/store identity, event count/head, and append-only typed
events. A SHA-256 chain detects accidental modifications, gaps and head mismatch;
strict JSON/schema validation and full semantic replay reject contradictory
records even when their chain is recomputed. Physical integrity uses SQLite
`quick_check`. Neither the hash chain nor a store UUID authenticates its writer.

The host OS, filesystem, SQLite library, paths/parent directories, application
code and caller are trusted. Symlink database paths are refused, but this is
not a hostile-path or in-process attacker containment design. Do not use network
shares, synchronized folders, databases being externally replaced, or manually
move/delete journal files. No OS ACLs, external identities or credentials are
configured. Whole-history checks are unbounded and are not hostile-file DoS
protection. Store size/retention limits require another design before real use.

SQLite's atomicity depends on filesystem locking and storage flush behavior;
see [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html) and
[locking](https://www.sqlite.org/lockingv3.html). `FULL` requests SQLite's documented
synchronization behavior; it is not proof this laptop/storage survives power loss.
See [synchronous settings](https://www.sqlite.org/pragma.html#pragma_synchronous).
Tests here exercise ordinary process exits, exceptions and local concurrency,
not power cuts, kernel crashes, disk/controller fault injection or malicious rollback.

## Durable transitions and recovery

```text
new session -> reservation COMMIT -> terminal COMMIT (blocked or denied)
                         |
                         +-> interruption -> pending
                                             |
                                  explicit recovery COMMIT
                                             |
                                      recovered_failed
```

Reservation commits the complete typed request before any invocation of the
unconditionally unavailable boundary. Every reserved approval ID is retained,
including denials and recovered failures. IDs are checked against all sessions,
so changing the record or reopening the store cannot reuse a committed ID.
Attempt IDs are also unique across the store. A terminal denial's reason and a
blocked outcome must agree with recomputed context/digest/clock/replay checks.

The terminal transaction rechecks session ownership and the pending request
before invoking the pure unavailable boundary. Explicit recovery racing that
transaction either follows its terminal commit or fences it before invocation.
This short lock is safe only because no external work is performed. Do not
substitute real execution into this transaction.

`snapshot()` uses SQLite URI `mode=ro`, query-only access and a read transaction.
It creates no application records and cannot repair evidence. It can fail when
SQLite requires writable hot-journal recovery. `reopen()` explicitly uses
existing-only `mode=rw`: SQLite may roll back its interrupted transaction before
application validation. This is not a read-only operation and is not application
repair. Unknown/corrupt semantic records are preserved and refused.

Reopening never restores session ownership. `begin_session()` requires a fresh,
never-used epoch and no pending operation; it revokes old handles. A pending
reservation blocks admission until `recover_pending()` explicitly appends
`recovered_failed` evidence and revokes the epoch. Recovery abandons work;
it never retries, resumes, executes or releases approval consumption. Recovery
without pending work is a no-op. It does not determine whether a process actually
crashed: it records the explicit decision to abandon an incomplete operation.

| Snapshot state | Meaning |
| --- | --- |
| `empty` | No attempted operation; session records may exist |
| `recovery_required` | A committed reservation lacks a terminal/recovery record |
| `failures_retained` | Recovered failures remain in history; no pending operation |
| `consistent_simulation` | Recorded attempts terminate consistently as denied/blocked |
| Exception | Invalid/corrupt/unsupported/unreadable evidence; no successful verdict |

All results and snapshots have `confers_authority=False` and
`valid_completion=False`, regardless of the state. A blocked terminal records
only the unavailable software boundary, never actual execution or containment.

Any error within a mutating transaction poisons that handle, including lock
timeout and commit-acknowledgment uncertainty. Do not retry on it; reopen and
inspect. Malformed API arguments are validated before transaction admission and
are not recorded; those validation errors do not poison a clean handle.

A failure before reservation commit leaves no durable consumption and cannot
reach the unavailable boundary. A committed reservation without terminal evidence
remains pending. A terminal commit followed by lost acknowledgment may reopen as
consistent simulation: the journal knows what committed, not whether the caller
received the return value. Original exceptions propagate; no exception path
pretends it returned success. Recovery commit uncertainty is handled the same way.

## Clock and rollback limits

Clocks and mock identities remain caller-supplied, unauthenticated simulation data.
The existing exact expiry and issuance-monotonic lifetime checks apply within a
session. A new epoch does not compare old and new monotonic coordinates. Old-epoch
approvals, even unconsumed ones, cannot authorize a new session. The new wall-clock
origin cannot precede persisted wall evidence, but this is only a consistency
check: it does not establish real elapsed time, prevent forged clocks, or detect
all clock manipulation while the process was absent. Fresh simulated records are
required after reopen; future real approval needs an authenticated clock/expiry
policy and external authority.

There is **no malicious rollback resistance**. Replacing the database and metadata
with an older internally consistent backup removes history; a regression test
demonstrates the limitation. File hashes, SQL transactions and UUIDs cannot detect
that attack without an independent trusted anchor. Losing/deleting the entire
store is not recoverable by this API; do not recreate it and claim continuity.
No independent provenance, complete attempt history, external witness or
power-loss durability is established by these tests.

## Test evidence and stopping point

`tests/test_durable_contract.py` tests:

- Exclusive creation, existing-only reopen, pinned identity, storage versions,
  strict JSON, hash mismatches and rehashed semantic contradictions.
- Independent SQL reads between reservation and terminal commits; read-only
  snapshots preserve bytes and do not create files.
- Replay across reopen/new epochs, denied-ID reuse, stale old records, epoch
  fencing, clock rollback and fresh monotonic coordinates.
- Threads, separate test subprocesses and lock contention; at most one
  consumption of a shared mock approval.
- Actual test-process `os._exit` before/after reservation, terminal, session and
  recovery commits, plus exception/acknowledgment failures at those boundaries.
- Explicit abandonment retaining consumed IDs, no automatic resume, recovery
  racing finalization, and unexpected return from the unavailable boundary.
- A restored old backup intentionally demonstrating absent rollback resistance.

Subprocesses contain only the fixed reviewed test harness and temporary SQLite
stores. They never run candidate, model or VM code. SQLite inspection connections
are explicitly closed so Windows cleanup does not rely on garbage collection.

Run with the existing development environment:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_durable_contract.py -q
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

Final local validation on Windows/Python 3.13: 97 unique tests and 249 subtests
passed under pytest; the same 97 tests passed under unittest. This milestone
adds 26 tests and 26 subtests. The unchanged CLI simulation, audit summary and
audit verifier also passed in a temporary directory. No lint/type-check command
is configured. The first targeted run found Windows cleanup failures in the
test-only inspection connections; explicit connection closure fixed them.

A separate static AI review covered the accumulated changes and final recovery
fencing. It found no outstanding high/medium defect. Its attempted test launch
was blocked by sandbox process permissions, so it contributes review evidence,
not another passing test run. Local results do not establish other operating
systems, Python versions, power-loss behavior or security certification.

The existing CI workflow triggers only pushes to `main` and pull requests
targeting `main`; publishing a feature branch alone does not trigger it. A lack
of workflow runs is not a CI pass, and PR creation is a separate authorization.

Human authorization covers this simulation persistence/recovery milestone and
publication of reviewed code/tests/project documentation on the working branch.
It does not authorize a main merge, PR creation, release, service/security changes,
model integration, VM provisioning or guest execution. The approved 4 GiB guest /
512 MiB fixture split remains a design amendment; other research parameters stay
PROPOSED. Preserve design preregistration, candidate lock before final evaluation,
protected final evaluation without feedback to improvement, external authority
and separate promotion approval.
