# Durable controller preparation and unverified reconciliation

This code-only API records complete preflight rejection or effect intent, then
supports explicit abandonment of unresolved intent. It cannot invoke any VM,
subprocess, model, candidate or external operation. No mock record confers real
authority, and no state claims actual completion, successful shutdown or isolation.

`lab_control.controller_journal.ControllerJournal` is opt-in and uses a new
**version 2 store**. It rejects version 1 stores; the older `DurableLedger` rejects
version 2. There is no migration or automatic store creation. Existing
`CodeOnlyController` behavior and its unpersisted receipts remain unchanged.
Use one new trusted test database for this API, not two journals for one attempt.

## Durable transitions

```text
explicit fresh session
    -> preparation COMMIT: full request + gates + approval reason + ID consumption
       -> rejected                         (durable; no effect intent)
       -> unresolved intent                (durable; no effect is performed)
          -> zero or more test observations (still unresolved)
          -> explicit current-head reconciliation COMMIT
             -> abandoned_unverified       (IDs retained; old epoch fenced)
```

`prepare()` recomputes readiness gates and approval policy under SQLite's writer
lock. It retains the complete immutable request, including all mock readiness
claims, failed gates, and mock-approval denial reason. Unlike the older controller's
unpersisted preflight, **every committed preparation consumes its approval ID,
including rejected preflight**. A second request with that ID is rejected as
replay even after reopen/new epoch. Attempt IDs must be unique throughout the store.
Preflight failure and approval denial may both be retained for the same attempt.

Malformed arguments fail before transaction admission and are not recorded. An
unavailable/corrupt store, duplicate attempt ID, unowned session, or pending intent
can also prevent recording a new request. This is not a complete log of every API
call. A failed recording is never represented as a successfully recorded rejection.

Passing gates creates only an intent for the existing manifest's unavailable
code-contract operation. No boundary/executor/backend is called, and no enable
flag exists. The intent is **unresolved**, not launched or completed. Further
preparation and new sessions are blocked until explicit reconciliation. There is
one unresolved intent per store, not a multi-run scheduler.

## Observations and reconciliation

`TestObservation` accepts only `source="test_only"` and reported states `off`,
`running`, or `unknown`. Each record binds exact store/attempt/run/VM/epoch/manifest,
has a globally unique observation ID, and includes a caller-supplied clock. An
observation may be appended after reopen without restoring session ownership:
this records an untrusted report, not permission to do work. Reports must use the
intent's declared epoch/clock coordinates; they are not measured freshness across
real process restarts. Reversed clocks or changed bindings are rejected.

All reports remain preserved. Differing off/running reports are flagged as
`conflicting_reports`, without deciding which was accurate. One or many matching
reports remain `unverified_reports`. No reports means `no_observations`. None of
these states proves the guest's actual state or grants authority.

`reconcile(attempt_id, expected_head)` has exactly one action: `abandon_unverified`.
It must identify the sole unresolved intent and the exact current journal head.
If an observation commits first, an older reconciliation head is rejected. If
reconciliation commits first, subsequent observations for that intent are refused.
No report is silently overwritten. This is trusted-caller explicit bookkeeping,
not an authenticated human approval mechanism or a determination of real effects.

Abandonment keeps all observations, consumes no new execution permission, retains
all previously consumed IDs and revokes the active epoch. It does not assert
"nothing ran" or "the VM stopped." Another preparation requires an explicitly
begun, never-used epoch. No recovery API retries an effect or restores an old owner.

Snapshot states are `empty`, `rejections_retained`, `reconciliation_required` and
`unverified_abandonments_retained`. Snapshots and intent results always expose
`confers_authority=False` and `valid_completion=False`. Invalid/corrupt evidence
raises rather than returning a normal snapshot.

## Storage, crash and time assumptions

The implementation reuses the earlier journal's low-level schema/hash helpers,
with a separate version and strict event models/semantic replay. It uses exclusive
creation, existing-only open, SQLite DELETE rollback journaling, `synchronous=FULL`,
`BEGIN IMMEDIATE` writer serialization and a five-second busy timeout. Every write
validates the full updated journal before commit. Preparation, observation and
reconciliation return only after readback; readback failure poisons the handle.

An exception in a mutating transaction poisons that handle. Before-commit failure
leaves no committed record/consumption; after-commit acknowledgment loss may leave
the entire record. Never retry on the poisoned handle. Reopen and inspect first.
Committed unresolved intent remains unresolved until explicit reconciliation;
missing records are not invented. Observation and reconciliation commit uncertainty
are resolved by inspecting retained history, never by assuming the return was sent.

`snapshot()` is read-only and cannot repair a hot journal. `reopen()` can perform
SQLite's writable crash recovery but does not restore session ownership or repair
application records. Unknown versions, schema changes, gaps, broken hashes and
rehashed semantic contradictions fail closed. Failed creation/partial/corrupt
stores are preserved, not overwritten.

The filesystem, parent directories, caller, host OS and SQLite/storage behavior are
trusted. Whole-history loading is unbounded. This is not hostile-file denial-of-
service protection, a retention quota, OS access control or evidence authentication.
Hashes cannot detect malicious replacement with an older consistent database.
Process-exit tests do not establish power-loss, kernel/storage-fault durability.

Clocks remain unauthenticated caller data. New session origins cannot precede any
retained request/observation wall time. A future reported timestamp can therefore
raise that floor and prevent starting a session with an earlier supplied time;
the API does not guess or repair it. Monotonic coordinates from different epochs
are not compared. No clock source, external rollback anchor or clock permission
is installed by this milestone.

## Tests and review

`tests/test_controller_journal.py` covers:

- Exclusive/version-separated stores, current identity, missing/corrupt records,
  strict JSON, unknown schemas and rehashed semantic contradictions.
- Independent SQL reads proving full rejection/intent commit; rejected-ID replay
  after reopen; no effect/backend invocation; missing/duplicate attempts.
- Exact expiry, all observation bindings, stale/duplicate reports, changed epochs,
  caller-clock floors and immutable/read-only snapshots.
- Actual `os._exit` in trusted temporary-store subprocesses before/after session,
  intent preparation, rejection preparation, observation and reconciliation commits.
- Exception/acknowledgment/readback failures, writer lock timeout and poisoning.
- Thread/process races allowing one intent; observation versus reconciliation
  races preserving the winning transaction and rejecting a stale decision.
- Preserved contradictory/absent reports, explicit unverified abandonment and a
  whole-store rollback test demonstrating the lack of rollback protection.

These are local contract/storage tests, never guest fixtures or agent candidate
code. Standard-library SQLite is used; no dependencies or software are installed.

Final local validation on Windows/Python 3.13.7:

- `.\.venv\Scripts\python.exe -m pytest -q`: **150 unique tests and 352 subtests
  passed**, including **27 journal tests and 35 journal subtests**.
- `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`: the same
  **150 tests passed**; repeated runners do not add unique tests.
- Existing installed `prom-lab run-dummy-loop`, `prom-lab audit summary` and
  `prom-lab audit verify` passed in a fresh temporary directory, with seven
  simulation events. No guest/candidate operation was invoked.
- `git diff --check` and whitespace checks for new files passed. No lint/type
  command is configured; other OS/Python CI jobs and live isolation were not run.

A separate read-only AI reviewer identified one medium-severity coverage flaw:
observation-binding negative tests unintentionally also supplied a stale clock.
Each negative now mutates only one field from a valid baseline, and a positive
baseline acceptance is asserted. The reviewer verified the fix and reported no
outstanding findings. Both full test runners were rerun after the correction.
The reviewer changed no files and ran no tests; this is independent AI static
review, not independent test execution or security certification. No configured
test failed. The first targeted journal run passed 24 tests/33 subtests before
additional crash, lock-timeout and duplicate-attempt coverage was added.

## Next decision, still before activation

The next recommended code-only slice is an authenticated-observation interface
design plus recovery policy tests for real lifecycle ambiguity. Review an exact
trust/privilege manifest before implementing any live adapter: approver and
broker/watchdog/collector identities, narrowly scoped OS rights, approved VM/run
bindings, independent clock/rollback policy, bounded evidence retention and
unknown-stop handling. The earlier [activation proposal](CONTROLLER_REHEARSAL.md)
and [VM plan](PHASE2A_VM_PLAN_PROPOSED.md) remain applicable.

No external observation mechanism is activated here. Real authentication, service
registration, endpoint/ACL changes, VM start/stop, guest fixtures, models and new
commit/push require separate authorization. The approved memory split and all
other PROPOSED research parameters are unchanged. Preserve design preregistration
before search, candidate lock before final evaluation, protected evaluation without
feedback to improvement, external human authority and separate promotion approval.
