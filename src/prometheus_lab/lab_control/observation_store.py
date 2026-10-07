"""Separate version 3 temporary-store rehearsal journal; no live authority."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import UUID

from .controller import MAX_ENVELOPE_BYTES, decode_envelope
from .durable import TABLES, ZERO, StoreError, _append, _hash
from .observation import Action, ObservationContext, Received, RecordedEvent, replay

VERSION = 3
MAX_EVENTS = 512  # Implementation test-store ceiling, not a research parameter.


def _load(connection, store_id):
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise StoreError("Observation store integrity check failed")
    schema = connection.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if len(schema) != len(TABLES) or dict(schema) != TABLES:
        raise StoreError("Unsupported observation schema")
    meta = connection.execute("SELECT singleton,version,store_id,event_count,head FROM metadata").fetchall()
    if len(meta) != 1:
        raise StoreError("Missing or duplicate observation metadata")
    singleton, version, identity, count, head = meta[0]
    if (singleton != 1 or type(version) is not int or version != VERSION or
        identity != str(store_id) or type(count) is not int or not 1 <= count <= MAX_EVENTS):
        raise StoreError("Wrong observation identity/version/count")
    rows = connection.execute("SELECT seq,kind,body,digest FROM events ORDER BY seq LIMIT ?",
                              (MAX_EVENTS + 1,)).fetchall()
    if count != len(rows):
        raise StoreError("Missing or extra observation records")
    context, events, chain = None, [], ZERO
    for expected, (seq, kind, body, digest) in enumerate(rows, 1):
        if (type(seq) is not int or seq != expected or not isinstance(body, str) or
            len(body.encode('utf-8')) > MAX_ENVELOPE_BYTES or _hash(chain, kind, body) != digest):
            raise StoreError("Invalid observation event/hash/size")
        if expected == 1 and kind == "observation_context":
            context = decode_envelope(ObservationContext, body.encode('utf-8'))
            if context.store_id != store_id:
                raise StoreError("Context/store identity mismatch")
        elif expected > 1 and kind == "observation_event":
            events.append(decode_envelope(RecordedEvent, body.encode('utf-8')))
        else:
            raise StoreError("Unsupported observation event kind/order")
        chain = digest
    if chain != head:
        raise StoreError("Observation head mismatch")
    return replace(replay(context, tuple(events)), head=head)


class ObservationJournal:
    """One immutable test attempt per store. Reopening never resumes dispatch.

    Trusted directory/caller, SQLite DELETE/FULL; no malicious rollback protection,
    authenticated clock, OS ownership or proven power-loss durability.
    """

    def __init__(self, path: Path, store_id: UUID):
        if type(store_id) is not UUID:
            raise TypeError("An explicit UUID store identity is required")
        self.path, self.store_id = Path(path).absolute(), store_id
        self._owner, self._faulted, self._lock = False, False, Lock()
        self._needs_interrupt = True
        self.snapshot()

    @classmethod
    def create(cls, path: Path, context: ObservationContext):
        context = decode_envelope(ObservationContext, context)
        path = Path(path).absolute()
        if any(Path(str(path) + suffix).exists() for suffix in ("-journal", "-wal", "-shm")):
            raise StoreError("Orphan sidecars exist; preserve them")
        if len(context.model_dump_json().encode('utf-8')) > MAX_ENVELOPE_BYTES:
            raise ValueError("Context exceeds parser ceiling")
        with path.open('xb'):
            pass
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            if connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise StoreError("DELETE journaling unavailable")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            for sql in TABLES.values():
                connection.execute(sql)
            connection.execute("INSERT INTO metadata VALUES(1,?,?,0,?)", (VERSION, str(context.store_id), ZERO))
            _append(connection, "observation_context", context)
            _load(connection, context.store_id)
            connection.commit()
        finally:
            connection.close()
        instance = cls(path, context.store_id)
        instance._owner = True
        instance._needs_interrupt = False
        return instance

    @contextmanager
    def _connection(self, readonly=False):
        connection = None
        try:
            if self.path.is_symlink():
                raise StoreError("Symlink observation stores unsupported")
            connection = sqlite3.connect(self.path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                uri=True, isolation_level=None, timeout=5)
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise StoreError("Unsupported journal mode")
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

    def snapshot(self):
        with self._connection(readonly=True) as connection:
            connection.execute("BEGIN")
            return _load(connection, self.store_id)

    def _checkpoint(self, name):
        """No-op test fault seam, never a backend."""

    def append(self, value: RecordedEvent | bytes, expected_head: str):
        record = decode_envelope(RecordedEvent, value)
        if len(record.model_dump_json().encode('utf-8')) > MAX_ENVELOPE_BYTES:
            raise ValueError("Event exceeds parser ceiling")
        with self._lock:
            if self._faulted:
                raise StoreError("Observation handle poisoned; reopen and inspect")
            try:
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    before = _load(connection, self.store_id)
                    if expected_head != before.head:
                        raise StoreError("Stale expected head; inspect before deciding")
                    event = record.event
                    if isinstance(event, Action) and event.action in ('fence', 'dispatch') and not self._owner:
                        raise StoreError("Reopen cannot restore rehearsal dispatch ownership")
                    if isinstance(event, Received):
                        for old in before.observations:
                            if old.peer == event.peer and old.observation == event.observation:
                                connection.rollback()
                                return before  # Same report, no second journal record/transition.
                    if self._needs_interrupt and not (isinstance(event, Action) and event.action == 'interrupt'):
                        raise StoreError("Reopened writes require a durable interruption first")
                    count = connection.execute('SELECT event_count FROM metadata').fetchone()[0]
                    if count >= MAX_EVENTS:
                        raise StoreError("Observation test store full; no automatic deletion")
                    _append(connection, 'observation_event', record)
                    _load(connection, self.store_id)
                    self._checkpoint('before_commit')
                    connection.commit()
                    self._checkpoint('after_commit')
                result = self.snapshot()
                if isinstance(event, Action) and event.action in ('interrupt', 'abandon'):
                    self._owner = False
                    self._needs_interrupt = False
                return result
            except BaseException:
                self._faulted = True
                raise

    @classmethod
    def reopen(cls, path: Path, store_id: UUID):
        if type(store_id) is not UUID:
            raise TypeError("An explicit UUID store identity is required")
        instance = object.__new__(cls)
        instance.path, instance.store_id = Path(path).absolute(), store_id
        instance._owner, instance._faulted, instance._lock = False, False, Lock()
        instance._needs_interrupt = True
        with instance._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _load(connection, store_id)
            connection.rollback()
        return instance
