"""SQLite-backed simulation journal. No real authority, execution or rollback resistance."""

from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Literal
from uuid import UUID

from .boundary import VMOperationsUnavailable, request_vm_operation
from .contracts import Attempt, ClockReading, Context, Contract, Manifest, MockApproval, Reason, parse_contract
from .state import clock_reversed, denial_reason


SCHEMA_VERSION = 1
TABLES = {
    "metadata": "CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL, store_id TEXT NOT NULL, event_count INTEGER NOT NULL, head TEXT NOT NULL)",
    "events": "CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL)",
}
ZERO = "0" * 64


class StoreError(RuntimeError):
    """Store cannot be trusted or operation could not be acknowledged; no automatic retry."""


class RecoveryRequired(StoreError):
    pass


class SessionOpened(Contract):
    context: Context


class Reserved(Contract):
    session_epoch: UUID
    attempt: Attempt


class Terminal(Contract):
    attempt_id: UUID
    outcome: Literal["blocked", "denied"]
    reason: Reason


class Recovered(Contract):
    attempt_ids: tuple[UUID, ...]
    reason: Literal["abandoned_without_execution"]


EVENT_MODELS = {"session": SessionOpened, "reserved": Reserved, "terminal": Terminal, "recovered": Recovered}


@dataclass(frozen=True)
class DurableAttempt:
    request: Attempt
    session_epoch: UUID
    status: Literal["pending", "blocked", "denied", "recovered_failed"]
    reason: Reason | None

    @property
    def confers_authority(self) -> bool:
        return False

    @property
    def valid_completion(self) -> bool:
        return False


@dataclass(frozen=True)
class DurableSnapshot:
    store_id: UUID
    contexts: tuple[Context, ...]
    active_epoch: UUID | None
    attempts: tuple[DurableAttempt, ...]
    event_count: int

    @property
    def status(self) -> str:
        if any(item.status == "pending" for item in self.attempts):
            return "recovery_required"
        if any(item.status == "recovered_failed" for item in self.attempts):
            return "failures_retained"
        return "consistent_simulation" if self.attempts else "empty"

    @property
    def confers_authority(self) -> bool:
        return False

    @property
    def valid_completion(self) -> bool:
        return False


def _hash(previous: str, kind: str, body: str) -> str:
    return sha256((previous + "\n" + kind + "\n" + body).encode("utf-8")).hexdigest()


def _load(connection: sqlite3.Connection, expected_id: UUID) -> DurableSnapshot:
    """Check physical/schema/chain integrity AND replay semantic transitions."""
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise StoreError("SQLite integrity check failed")
    schema = connection.execute("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if dict(schema) != TABLES or len(schema) != len(TABLES):
        raise StoreError("Unexpected storage schema; migration/repair is not automatic")
    metadata = connection.execute("SELECT singleton, version, store_id, event_count, head FROM metadata").fetchall()
    if len(metadata) != 1:
        raise StoreError("Missing/duplicate metadata")
    singleton, version, store_id, count, head = metadata[0]
    if singleton != 1 or type(version) is not int or version != SCHEMA_VERSION:
        raise StoreError("Unsupported storage schema version")
    if store_id != str(expected_id) or type(count) is not int or count < 0:
        raise StoreError("Wrong store identity or invalid event count")
    rows = connection.execute("SELECT seq, kind, body, digest FROM events ORDER BY seq").fetchall()
    if len(rows) != count:
        raise StoreError("Missing/extra journal events")
    contexts = []
    active = None
    attempts = []
    ids = set()
    reserved = set()
    previous = None
    last_wall = None
    chain = ZERO
    for expected_seq, (seq, kind, body, digest) in enumerate(rows, 1):
        if type(seq) is not int or seq != expected_seq or kind not in EVENT_MODELS or not isinstance(body, str):
            raise StoreError("Invalid journal envelope/sequence")
        if digest != _hash(chain, kind, body):
            raise StoreError("Journal hash mismatch (not an authenticity check)")
        chain = digest
        event = parse_contract(EVENT_MODELS[kind], body)
        pending = [item for item in attempts if item.status == "pending"]
        if kind == "session":
            context = event.context
            if pending or context.process_epoch in {item.process_epoch for item in contexts}:
                raise StoreError("Pending attempt or reused session epoch")
            if last_wall is not None and context.origin.wall_time < last_wall:
                raise StoreError("Session wall clock moved backwards")
            contexts.append(context)
            active = context.process_epoch
            previous = context.origin
            last_wall = context.origin.wall_time
        elif kind == "reserved":
            request = event.attempt
            if active is None or event.session_epoch != active or pending:
                raise StoreError("Reservation outside the active session or while recovery is required")
            if request.attempt_id in ids or request.steps:
                raise StoreError("Duplicate attempt ID or pre-populated request steps")
            reason = denial_reason(request.manifest, request.approval, request.clock,
                                   contexts[-1], previous, reserved)
            ids.add(request.attempt_id)
            reserved.add(request.approval.approval_id)
            if not clock_reversed(request.clock, previous):
                previous = request.clock
            last_wall = max(last_wall, request.clock.wall_time)
            attempts.append(DurableAttempt(request, active, "pending", reason))
        elif kind == "terminal":
            if len(pending) != 1 or pending[0].request.attempt_id != event.attempt_id:
                raise StoreError("Terminal lacks exactly one matching pending reservation")
            item = pending[0]
            expected = "denied" if item.reason is not None else "blocked"
            reason = item.reason or "unsupported_execution"
            if (event.outcome, event.reason) != (expected, reason):
                raise StoreError("Terminal contradicts admission decision")
            attempts[-1] = replace(item, status=expected)
        else:
            if not pending or event.attempt_ids != tuple(item.request.attempt_id for item in pending):
                raise StoreError("Recovery does not identify all pending attempts exactly once")
            attempts = [replace(item, status="recovered_failed") if item.status == "pending" else item
                        for item in attempts]
            active = None  # Recovery fences every old handle; a fresh epoch is mandatory.
    if chain != head:
        raise StoreError("Journal head mismatch")
    return DurableSnapshot(expected_id, tuple(contexts), active, tuple(attempts), count)


def _append(connection: sqlite3.Connection, kind: str, event: Contract) -> None:
    count, head = connection.execute("SELECT event_count, head FROM metadata WHERE singleton=1").fetchone()
    body = event.model_dump_json()
    digest = _hash(head, kind, body)
    connection.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (count + 1, kind, body, digest))
    connection.execute("UPDATE metadata SET event_count=?, head=? WHERE singleton=1", (count + 1, digest))


class DurableLedger:
    """Local SQLite writer, explicit creation, no automatic session resume.

    One pending operation globally is intentional. Separate handles/processes
    serialize via SQLite. Any transaction error poisons that handle: reopen,
    inspect and explicitly recover rather than guessing commit success.
    """

    def __init__(self, path: Path, expected_store_id: UUID):
        if not isinstance(expected_store_id, UUID):
            raise TypeError("Expected an explicit UUID store identity")
        self.path = Path(path).absolute()
        self.store_id = expected_store_id
        self._session = None
        self._faulted = False
        self._lock = Lock()
        self.snapshot()  # Existing-only open; never initialize a missing/corrupt store.

    @classmethod
    def create(cls, path: Path, store_id: UUID):
        if not isinstance(store_id, UUID):
            raise TypeError("Expected an explicit UUID store identity")
        path = Path(path).absolute()
        if any(Path(str(path) + suffix).exists() for suffix in ("-journal", "-wal", "-shm")):
            raise StoreError("Orphan SQLite sidecar present; preserve it for inspection")
        with path.open("xb"):
            pass
        # A failed creation is deliberately preserved, never overwritten on retry.
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise StoreError("DELETE journal mode unavailable")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            for sql in TABLES.values():
                connection.execute(sql)
            connection.execute("INSERT INTO metadata VALUES (1, ?, ?, 0, ?)",
                               (SCHEMA_VERSION, str(store_id), ZERO))
            connection.commit()
        finally:
            connection.close()
        return cls(path, store_id)

    @contextmanager
    def _connection(self, *, readonly=False):
        if self.path.is_symlink():
            raise StoreError("Symbolic-link stores are unsupported")
        mode = "ro" if readonly else "rw"
        connection = None
        try:
            connection = sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True,
                                         timeout=5, isolation_level=None)
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise StoreError("Unexpected journal mode; refusing to change it")
            connection.execute("PRAGMA synchronous=FULL")
            if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise StoreError("FULL synchronization unavailable")
            if readonly:
                connection.execute("PRAGMA query_only=ON")
            yield connection
        except (sqlite3.Error, ValueError, TypeError, OverflowError, RecursionError) as error:
            raise StoreError(str(error)) from error
        finally:
            if connection is not None:
                connection.close()

    def snapshot(self) -> DurableSnapshot:
        """Application-read-only view; refuses if SQLite needs writable journal recovery."""
        with self._connection(readonly=True) as connection:
            connection.execute("BEGIN")
            return _load(connection, self.store_id)

    def _checkpoint(self, name: str) -> None:
        """No-op test seam for exception/process-death injection; no callback API."""

    @contextmanager
    def _write(self, label: str):
        if self._faulted:
            raise StoreError("Handle is poisoned; reopen and inspect before another operation")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                snapshot = _load(connection, self.store_id)
                yield connection, snapshot
                self._checkpoint(label + "_before_commit")
                connection.commit()
                self._checkpoint(label + "_after_commit")
        except BaseException:
            self._faulted = True
            raise

    def begin_session(self, context: Context) -> None:
        context = Context.model_validate(context)
        with self._lock:
            with self._write("session") as (connection, snapshot):
                if self._session is not None:
                    raise StoreError("A handle cannot restart its session")
                if any(item.status == "pending" for item in snapshot.attempts):
                    raise RecoveryRequired("Explicit abandonment of pending attempts is required")
                if context.process_epoch in {item.process_epoch for item in snapshot.contexts}:
                    raise StoreError("Session epoch was already used; provide a fresh epoch")
                walls = [item.origin.wall_time for item in snapshot.contexts]
                walls += [item.request.clock.wall_time for item in snapshot.attempts]
                if walls and context.origin.wall_time < max(walls):
                    raise StoreError("New session wall clock is older than persisted evidence")
                _append(connection, "session", SessionOpened(context=context))
            self._session = context

    def assess(self, attempt_id: UUID, manifest: Manifest, approval: MockApproval,
               clock: ClockReading) -> DurableAttempt:
        request = Attempt(attempt_id=attempt_id, manifest=manifest, approval=approval, clock=clock, steps=())
        with self._lock:
            with self._write("reservation") as (connection, snapshot):
                if self._session is None or snapshot.active_epoch != self._session.process_epoch:
                    raise StoreError("No active owned session; old/reopened handles cannot resume")
                if any(item.status == "pending" for item in snapshot.attempts):
                    raise RecoveryRequired("Pending operation requires explicit recovery")
                if attempt_id in {item.request.attempt_id for item in snapshot.attempts}:
                    raise StoreError("Duplicate attempt ID")
                _append(connection, "reserved", Reserved(session_epoch=self._session.process_epoch, attempt=request))
                pending = _load(connection, self.store_id).attempts[-1]
            # Only after acknowledged reservation commit; this boundary always refuses.
            try:
                with self._write("terminal") as (connection, snapshot):
                    if (snapshot.active_epoch != self._session.process_epoch
                            or not snapshot.attempts or snapshot.attempts[-1] != pending):
                        raise StoreError("Attempt/session changed before finalization")
                    # Fence recovery/session changes for this pure unavailable call.
                    # This must never be replaced with a live executor under a DB lock.
                    if pending.reason is None:
                        try:
                            request_vm_operation(manifest=request.manifest, approval=request.approval)
                        except VMOperationsUnavailable:
                            pass
                        else:
                            raise StoreError("Unavailable VM boundary unexpectedly returned")
                    result = replace(pending, status="denied" if pending.reason else "blocked")
                    _append(connection, "terminal", Terminal(attempt_id=attempt_id, outcome=result.status,
                            reason=pending.reason or "unsupported_execution"))
                return result
            except BaseException:
                self._faulted = True  # Reservation remains; recovery never resumes it.
                raise

    def recover_pending(self) -> tuple[UUID, ...]:
        """Explicitly abandon pending work; retain consumption, revoke the epoch."""
        with self._lock:
            with self._write("recovery") as (connection, snapshot):
                pending = tuple(item.request.attempt_id for item in snapshot.attempts if item.status == "pending")
                if pending:
                    _append(connection, "recovered", Recovered(attempt_ids=pending, reason="abandoned_without_execution"))
            if pending:
                self._session = None
            return pending

    @classmethod
    def reopen(cls, path: Path, expected_store_id: UUID):
        """Writable SQLite hot-journal recovery, then strict existing-store validation.

        Unlike snapshot(), SQLite may roll back an interrupted DB transaction.
        No application event is repaired or resumed and no session is restored.
        """
        instance = object.__new__(cls)
        if not isinstance(expected_store_id, UUID):
            raise TypeError("Expected an explicit UUID store identity")
        instance.path = Path(path).absolute()
        instance.store_id = expected_store_id
        instance._session = None
        instance._faulted = False
        instance._lock = Lock()
        with instance._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _load(connection, expected_store_id)
            connection.rollback()
        return instance
