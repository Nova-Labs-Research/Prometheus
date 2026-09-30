"""Durable code-only intents and unverified reconciliation. No effect runner."""

from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Literal
from uuid import UUID

from .contracts import ClockReading, Context, Contract, Digest, Reason, parse_contract
from .controller import ControllerRequest, Gate, decode_envelope, preflight
from .durable import TABLES, ZERO, RecoveryRequired, StoreError, _append, _hash
from .state import clock_reversed, denial_reason


VERSION = 2  # Separate opt-in store. Never migrate or open a v1 ledger as this API.


class Session(Contract):
    context: Context


class Prepared(Contract):
    epoch: UUID
    request: ControllerRequest
    gates: tuple[Gate, ...]
    approval_reason: Reason | None
    disposition: Literal["rejected", "intent_recorded"]


class TestObservation(Contract):
    """Reported state only. No OS witness, actual completion or stop guarantee."""

    schema_version: Literal["controller-test-observation-v1"]
    source: Literal["test_only"]
    observation_id: UUID
    store_id: UUID
    attempt_id: UUID
    process_epoch: UUID
    run_id: UUID
    vm_id: UUID
    manifest_digest: Digest
    clock: ClockReading
    reported_state: Literal["off", "running", "unknown"]


class Reconciled(Contract):
    attempt_id: UUID
    prior_head: Digest
    action: Literal["abandon_unverified"]


EVENTS = {"session": Session, "prepared": Prepared,
          "observation": TestObservation, "reconciled": Reconciled}


class NonAuthorizing:
    @property
    def confers_authority(self) -> bool:
        return False

    @property
    def valid_completion(self) -> bool:
        return False


@dataclass(frozen=True)
class IntentRecord(NonAuthorizing):
    prepared: Prepared
    state: Literal["rejected", "unresolved", "abandoned_unverified"]
    observations: tuple[TestObservation, ...] = ()

    @property
    def uncertainty(self) -> str:
        states = {item.reported_state for item in self.observations}
        if {"off", "running"} <= states:
            return "conflicting_reports"
        return "unverified_reports" if states else "no_observations"


@dataclass(frozen=True)
class JournalSnapshot(NonAuthorizing):
    store_id: UUID
    head: str
    event_count: int
    contexts: tuple[Context, ...]
    active_epoch: UUID | None
    attempts: tuple[IntentRecord, ...]

    @property
    def status(self) -> str:
        if any(item.state == "unresolved" for item in self.attempts):
            return "reconciliation_required"
        if any(item.state == "abandoned_unverified" for item in self.attempts):
            return "unverified_abandonments_retained"
        return "rejections_retained" if self.attempts else "empty"


def _load(connection: sqlite3.Connection, store_id: UUID) -> JournalSnapshot:
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise StoreError("Controller journal integrity check failed")
    schema = connection.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if len(schema) != len(TABLES) or dict(schema) != TABLES:
        raise StoreError("Unsupported controller journal schema")
    meta = connection.execute("SELECT singleton,version,store_id,event_count,head FROM metadata").fetchall()
    if len(meta) != 1:
        raise StoreError("Missing or duplicate journal metadata")
    singleton, version, identity, count, head = meta[0]
    if singleton != 1 or type(version) is not int or version != VERSION or identity != str(store_id):
        raise StoreError("Wrong controller store identity or unsupported version")
    rows = connection.execute("SELECT seq,kind,body,digest FROM events ORDER BY seq").fetchall()
    if type(count) is not int or count < 0 or count != len(rows):
        raise StoreError("Missing or extra controller events")
    contexts, attempts = [], []
    active = previous = last_wall = None
    reserved, attempt_ids, observation_ids = set(), set(), set()
    chain = ZERO
    for expected, (seq, kind, body, digest) in enumerate(rows, 1):
        if type(seq) is not int or seq != expected or kind not in EVENTS or not isinstance(body, str):
            raise StoreError("Invalid controller event envelope")
        if _hash(chain, kind, body) != digest:
            raise StoreError("Controller hash chain mismatch; hashes are not authentication")
        event = parse_contract(EVENTS[kind], body)
        pending = [item for item in attempts if item.state == "unresolved"]
        if kind == "session":
            context = event.context
            if pending or context.process_epoch in {item.process_epoch for item in contexts}:
                raise StoreError("Unresolved intent or reused session epoch")
            if last_wall is not None and context.origin.wall_time < last_wall:
                raise StoreError("Session wall clock precedes retained evidence")
            contexts.append(context)
            active, previous, last_wall = context.process_epoch, context.origin, context.origin.wall_time
        elif kind == "prepared":
            request = event.request
            if pending or active is None or event.epoch != active or request.attempt_id in attempt_ids:
                raise StoreError("Preparation lacks a fresh attempt and active resolved session")
            gates = preflight(request, store_id)
            reason = denial_reason(request.manifest, request.approval, request.clock,
                                   contexts[-1], previous, reserved)
            disposition = "rejected" if gates or reason is not None else "intent_recorded"
            if (event.gates, event.approval_reason, event.disposition) != (gates, reason, disposition):
                raise StoreError("Preparation contradicts preflight or approval policy")
            attempt_ids.add(request.attempt_id)
            reserved.add(request.approval.approval_id)
            if not clock_reversed(request.clock, previous):
                previous = request.clock
            last_wall = max(last_wall, request.clock.wall_time)
            attempts.append(IntentRecord(event, "rejected" if disposition == "rejected" else "unresolved"))
        elif kind == "observation":
            if len(pending) != 1:
                raise StoreError("Observation lacks one unresolved intent")
            item = pending[0]
            request, manifest = item.prepared.request, item.prepared.request.manifest
            if (event.store_id, event.attempt_id, event.process_epoch, event.run_id,
                event.vm_id, event.manifest_digest) != (store_id, request.attempt_id,
                manifest.process_epoch, manifest.run_id, manifest.vm_id, manifest.digest()):
                raise StoreError("Observation binding mismatch")
            if event.observation_id in observation_ids:
                raise StoreError("Observation ID was already used")
            prior_clock = item.observations[-1].clock if item.observations else request.clock
            if clock_reversed(event.clock, prior_clock):
                raise StoreError("Observation clock moved backwards")
            observation_ids.add(event.observation_id)
            last_wall = max(last_wall, event.clock.wall_time)
            attempts[-1] = replace(item, observations=item.observations + (event,))
        else:
            if (len(pending) != 1 or event.attempt_id != pending[0].prepared.request.attempt_id
                    or event.prior_head != chain):
                raise StoreError("Reconciliation lacks the exact current unresolved intent/head")
            attempts[-1] = replace(pending[0], state="abandoned_unverified")
            active = None  # Explicit abandonment fences every old session owner.
        chain = digest
    if chain != head:
        raise StoreError("Controller journal head mismatch")
    return JournalSnapshot(store_id, chain, count, tuple(contexts), active, tuple(attempts))


class ControllerJournal:
    """Single-store preparation/reconciliation; no external effect can be invoked.

    Trusted local filesystem/caller; DELETE/FULL SQLite and no malicious rollback
    protection. Reopen is writable SQLite crash recovery, never session resumption.
    """

    def __init__(self, path: Path, store_id: UUID):
        if type(store_id) is not UUID:
            raise TypeError("Expected an explicit UUID controller store identity")
        self.path, self.store_id = Path(path).absolute(), store_id
        self._owner = None
        self._faulted = False
        self._lock = Lock()
        self.snapshot()

    @classmethod
    def create(cls, path: Path, store_id: UUID):
        if type(store_id) is not UUID:
            raise TypeError("Expected an explicit UUID controller store identity")
        path = Path(path).absolute()
        if any(Path(str(path) + suffix).exists() for suffix in ("-journal", "-wal", "-shm")):
            raise StoreError("Orphan sidecar exists; preserve it for inspection")
        with path.open("xb"):
            pass
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise StoreError("DELETE journaling unavailable")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            for sql in TABLES.values():
                connection.execute(sql)
            connection.execute("INSERT INTO metadata VALUES(1,?,?,0,?)", (VERSION, str(store_id), ZERO))
            connection.commit()
        finally:
            connection.close()
        return cls(path, store_id)

    @contextmanager
    def _connection(self, *, readonly=False):
        if self.path.is_symlink():
            raise StoreError("Symlink controller stores are unsupported")
        connection = None
        try:
            connection = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                uri=True, timeout=5, isolation_level=None)
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise StoreError("Unsupported journal mode; not changed automatically")
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

    def snapshot(self) -> JournalSnapshot:
        with self._connection(readonly=True) as connection:
            connection.execute("BEGIN")
            return _load(connection, self.store_id)

    def _checkpoint(self, name: str) -> None:
        """No-op test seam for exception/process-death injection, not a backend."""

    @contextmanager
    def _write(self, label):
        if self._faulted:
            raise StoreError("Controller journal handle is poisoned; reopen and inspect")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                snapshot = _load(connection, self.store_id)
                yield connection, snapshot
                _load(connection, self.store_id)  # Validate new event semantics before commit.
                self._checkpoint(label + "_before_commit")
                connection.commit()
                self._checkpoint(label + "_after_commit")
        except BaseException:
            self._faulted = True
            raise

    def _readback(self, attempt_id):
        try:
            return next(item for item in self.snapshot().attempts
                        if item.prepared.request.attempt_id == attempt_id)
        except BaseException:
            self._faulted = True
            raise

    def begin_session(self, context: Context) -> None:
        context = Context.model_validate(context)
        with self._lock:
            with self._write("session") as (connection, snapshot):
                if self._owner is not None:
                    raise StoreError("A handle cannot begin a second session")
                if snapshot.status == "reconciliation_required":
                    raise RecoveryRequired("Explicit reconciliation is required")
                _append(connection, "session", Session(context=context))
            self._owner = context.process_epoch

    def prepare(self, value: ControllerRequest | bytes) -> IntentRecord:
        request = decode_envelope(ControllerRequest, value)
        with self._lock:
            with self._write("prepare") as (connection, snapshot):
                if self._owner is None or self._owner != snapshot.active_epoch:
                    raise StoreError("No owned active session; reopen never resumes")
                if snapshot.status == "reconciliation_required":
                    raise RecoveryRequired("Unresolved intent blocks new preparation")
                previous = snapshot.contexts[-1].origin
                reserved = set()
                for item in snapshot.attempts:
                    reserved.add(item.prepared.request.approval.approval_id)
                    if item.prepared.epoch == self._owner:
                        reading = item.prepared.request.clock
                        if not clock_reversed(reading, previous):
                            previous = reading
                gates = preflight(request, self.store_id)
                reason = denial_reason(request.manifest, request.approval, request.clock,
                                       snapshot.contexts[-1], previous, reserved)
                event = Prepared(epoch=self._owner, request=request, gates=gates,
                    approval_reason=reason, disposition="rejected" if gates or reason else "intent_recorded")
                _append(connection, "prepared", event)
            return self._readback(request.attempt_id)

    def record_observation(self, value: TestObservation | bytes) -> IntentRecord:
        observation = decode_envelope(TestObservation, value)
        with self._lock:
            with self._write("observation") as (connection, snapshot):
                _append(connection, "observation", observation)
            return self._readback(observation.attempt_id)

    def reconcile(self, attempt_id: UUID, expected_head: str) -> IntentRecord:
        event = Reconciled(attempt_id=attempt_id, prior_head=expected_head, action="abandon_unverified")
        with self._lock:
            with self._write("reconcile") as (connection, snapshot):
                _append(connection, "reconciled", event)
            self._owner = None
            return self._readback(attempt_id)

    @classmethod
    def reopen(cls, path: Path, store_id: UUID):
        # Unlike __init__/snapshot, allow SQLite to recover its interrupted write.
        if type(store_id) is not UUID:
            raise TypeError("Expected an explicit UUID controller store identity")
        instance = object.__new__(cls)
        instance.path, instance.store_id = Path(path).absolute(), store_id
        instance._owner, instance._faulted, instance._lock = None, False, Lock()
        with instance._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _load(connection, store_id)
            connection.rollback()
        return instance
