from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from prometheus_lab.lab_control.contracts import Context
from prometheus_lab.lab_control.durable import DurableLedger, RecoveryRequired, StoreError
from test_phase2a_contract import T0, approval, clock, manifest, uid


# Trusted test harness only: no fixture/candidate execution or OS/VM management.
WORKER = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_phase2a_contract import approval, clock, manifest, uid
from prometheus_lab.lab_control.contracts import Context
from prometheus_lab.lab_control.durable import DurableLedger, StoreError
path, mode, number = Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
store = DurableLedger.reopen(path, uid(900))
context = Context(process_epoch=uid(number), origin=clock())
try:
    if mode != 'race':
        def checkpoint(name):
            if name == mode:
                os._exit(73)
        store._checkpoint = checkpoint
    if mode.startswith('recovery_'):
        store.recover_pending()
        sys.exit(0)
    store.begin_session(context)
    item = manifest().model_copy(update={'process_epoch': uid(number)})
    result = store.assess(uid(number + 1000), item, approval(item), clock())
    print(result.status, flush=True)
except StoreError:
    print('closed', flush=True)
'''


class DurableContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.sqlite'
        self.store = DurableLedger.create(self.path, uid(900))

    def begin(self, store=None, epoch=2, seconds=0):
        store = store or self.store
        store.begin_session(Context(process_epoch=uid(epoch), origin=clock(seconds)))
        return manifest().model_copy(update={'process_epoch': uid(epoch)})

    def reopen(self):
        return DurableLedger.reopen(self.path, uid(900))

    def assess(self, item=None, store=None, attempt=10, record=None, reading=None):
        item, store = item or manifest(), store or self.store
        return store.assess(uid(attempt), item, record or approval(item), reading or clock())

    def rows(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            return connection.execute('SELECT seq, kind, body, digest FROM events ORDER BY seq').fetchall()

    def worker(self, mode, number):
        return subprocess.run([sys.executable, '-c', WORKER, str(Path(__file__).parent),
                               str(self.path), mode, str(number)], capture_output=True, text=True, timeout=20)

    def test_create_existing_only_open_schema_and_identity(self):
        self.assertEqual(self.store.snapshot().status, 'empty')
        original = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            DurableLedger.create(self.path, uid(901))
        with self.assertRaises(StoreError):
            DurableLedger(self.path, uid(901))
        missing = self.path.with_name('missing.sqlite')
        with self.assertRaises(StoreError):
            DurableLedger.reopen(missing, uid(900))
        self.assertFalse(missing.exists())
        self.assertEqual(self.path.read_bytes(), original)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone(), ('delete',))
            self.assertEqual(connection.execute('SELECT version,store_id,event_count FROM metadata').fetchone(),
                             (1, str(uid(900)), 0))

    def test_reservation_and_terminal_are_committed_separately(self):
        item = self.begin()
        observed = []
        def checkpoint(name):
            if name == 'reservation_after_commit':
                observed.append([row[1] for row in self.rows()])
                self.assertEqual(self.reopen().snapshot().status, 'recovery_required')
        with patch.object(self.store, '_checkpoint', side_effect=checkpoint):
            result = self.assess(item)
        self.assertEqual(observed, [['session', 'reserved']])
        self.assertEqual(result.status, 'blocked')
        self.assertFalse(result.valid_completion)
        self.assertFalse(result.confers_authority)
        self.assertEqual([row[1] for row in self.rows()], ['session', 'reserved', 'terminal'])
        self.assertEqual(self.reopen().snapshot().attempts[0].status, 'blocked')

    def test_reopen_never_restores_an_old_session_and_epoch_cannot_repeat(self):
        self.begin()
        reopened = self.reopen()
        with self.assertRaisesRegex(StoreError, 'No active owned session'):
            self.assess(store=reopened)
        reopened = self.reopen()
        with self.assertRaisesRegex(StoreError, 'already used'):
            self.begin(reopened)
        self.assertEqual(len(self.rows()), 1)

    def test_replay_is_denied_after_reopen_and_new_epoch(self):
        item = self.begin()
        self.assess(item)
        reopened = self.reopen()
        new_item = self.begin(reopened, epoch=3)
        result = self.assess(new_item, reopened, attempt=11)
        self.assertEqual((result.status, result.reason), ('denied', 'replay'))
        self.assertEqual(len(self.reopen().snapshot().attempts), 2)

    def test_unconsumed_old_epoch_approval_is_stale_after_reopen(self):
        old = self.begin()
        reopened = self.reopen()
        self.begin(reopened, epoch=3)
        result = self.assess(old, reopened)
        self.assertEqual((result.status, result.reason), ('denied', 'epoch_mismatch'))

    def test_new_session_fences_a_still_live_old_handle(self):
        item = self.begin()
        self.begin(self.reopen(), epoch=3)
        before = self.rows()
        with self.assertRaisesRegex(StoreError, 'old/reopened handles'):
            self.assess(item)
        self.assertEqual(self.rows(), before)

    def test_new_session_rejects_wall_rollback_but_does_not_compare_monotonic_epochs(self):
        self.begin(seconds=10)
        with self.assertRaisesRegex(StoreError, 'older than persisted'):
            self.begin(self.reopen(), epoch=3, seconds=9)
        reopened = self.reopen()
        reopened.begin_session(Context(process_epoch=uid(3), origin=clock(10, milliseconds=0)))
        item = manifest().model_copy(update={'process_epoch': uid(3)})
        record = approval(item, issued_at=T0 + timedelta(seconds=10), issued_monotonic_ms=0,
                          expires_at=T0 + timedelta(seconds=910))
        result = self.assess(item, reopened, record=record, reading=clock(10, milliseconds=0))
        self.assertEqual(result.status, 'blocked')

    def test_denials_and_changed_context_reserve_ids_durably(self):
        item = self.begin()
        denied = self.assess(item, record=approval(item, decision='deny'))
        self.assertEqual(denied.reason, 'mock_denial')
        reopened = self.reopen()
        new_item = self.begin(reopened, epoch=3)
        changed = approval(new_item, identity=approval().identity.model_copy(update={'label': 'different'}))
        replay = self.assess(new_item, reopened, attempt=11, record=changed)
        self.assertEqual(replay.reason, 'replay')

    def test_duplicate_attempt_and_malformed_input_do_not_write(self):
        item = self.begin()
        self.assess(item)
        before = self.rows()
        with self.assertRaises(StoreError):
            self.assess(item)
        self.assertEqual(self.rows(), before)
        with self.assertRaises(ValueError):
            self.assess(item.model_copy(update={'execution': 'enabled'}), store=self.reopen())
        self.assertEqual(self.rows(), before)

    def test_expired_and_tampered_records_remain_denied_after_reopen(self):
        item = self.begin()
        expired = self.assess(item, reading=clock(900))
        self.assertEqual(expired.reason, 'expired')
        reopened = self.reopen()
        new_item = self.begin(reopened, epoch=3, seconds=900)
        result = self.assess(new_item, reopened, attempt=11,
                             record=approval(new_item, approval_id=uid(5), manifest_digest='f' * 64),
                             reading=clock(900))
        self.assertEqual(result.reason, 'manifest_mismatch')

    def test_threaded_consumption_is_serialized(self):
        item = self.begin()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.assess(item, attempt=100 + i), range(16)))
        self.assertEqual(sum(result.status == 'blocked' for result in results), 1)
        self.assertEqual(sum(result.reason == 'replay' for result in results), 15)
        self.assertEqual(len(self.reopen().snapshot().attempts), 16)

    def test_process_concurrency_never_consumes_twice(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: self.worker('race', 20 + i), range(4)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(result.stdout.strip(), ('blocked', 'denied', 'closed'))
        snapshot = self.reopen().snapshot()
        self.assertEqual(sum(item.status == 'blocked' for item in snapshot.attempts), 1)
        self.assertFalse(any(item.status == 'pending' for item in snapshot.attempts))

    def test_actual_process_death_at_each_commit_boundary(self):
        for index, (point, expected) in enumerate((
            ('reservation_before_commit', 'empty'),
            ('reservation_after_commit', 'recovery_required'),
            ('terminal_before_commit', 'recovery_required'),
            ('terminal_after_commit', 'consistent_simulation'),
        )):
            with self.subTest(point=point):
                self.path = Path(self.temp.name) / (point + '.sqlite')
                DurableLedger.create(self.path, uid(900))
                result = self.worker(point, 20 + index)
                self.assertEqual(result.returncode, 73, result.stderr)
                reopened = self.reopen()
                snapshot = reopened.snapshot()
                self.assertEqual(snapshot.status, expected)
                self.assertFalse(snapshot.valid_completion)
                if expected == 'recovery_required':
                    ids = reopened.recover_pending()
                    self.assertEqual(len(ids), 1)
                    final = self.reopen().snapshot()
                    self.assertEqual(final.attempts[0].status, 'recovered_failed')
                    self.assertIsNone(final.active_epoch)
                    fresh = self.reopen()
                    item = self.begin(fresh, epoch=100 + index)
                    self.assertEqual(self.assess(item, fresh, attempt=200 + index).reason, 'replay')

    def test_fault_injection_and_unknown_commit_ack_require_reconciliation(self):
        for point, expected in (('reservation_before_commit', 'empty'),
                                ('reservation_after_commit', 'recovery_required'),
                                ('terminal_before_commit', 'recovery_required'),
                                ('terminal_after_commit', 'consistent_simulation')):
            with self.subTest(point=point):
                self.path = Path(self.temp.name) / (point + '.sqlite')
                self.store = DurableLedger.create(self.path, uid(900))
                item = self.begin()
                def fail(name):
                    if name == point:
                        raise OSError('prepared commit acknowledgment failure')
                with patch.object(self.store, '_checkpoint', side_effect=fail):
                    with self.assertRaisesRegex(OSError, 'acknowledgment'):
                        self.assess(item)
                with self.assertRaisesRegex(StoreError, 'poisoned'):
                    self.assess(item, attempt=11)
                self.assertEqual(self.reopen().snapshot().status, expected)

    def test_boundary_fault_leaves_consumed_pending_and_recovery_never_calls_it(self):
        item = self.begin()
        with patch('prometheus_lab.lab_control.durable.request_vm_operation', return_value=None):
            with self.assertRaisesRegex(StoreError, 'unexpectedly returned'):
                self.assess(item)
        reopened = self.reopen()
        self.assertEqual(reopened.snapshot().status, 'recovery_required')
        with patch('prometheus_lab.lab_control.durable.request_vm_operation', side_effect=AssertionError('execution')):
            self.assertEqual(reopened.recover_pending(), (uid(10),))
            self.assertEqual(reopened.recover_pending(), ())
        self.assertEqual([row[1] for row in self.rows()], ['session', 'reserved', 'recovered'])

    def test_pending_blocks_new_sessions_and_attempts_until_explicit_recovery(self):
        item = self.begin()
        with patch('prometheus_lab.lab_control.durable.request_vm_operation', side_effect=OSError('test failure')):
            with self.assertRaises(OSError):
                self.assess(item)
        with self.assertRaises(RecoveryRequired):
            self.begin(self.reopen(), epoch=3)
        before = self.rows()
        with self.assertRaises(StoreError):
            self.assess(item, store=self.reopen(), attempt=11)
        self.assertEqual(self.rows(), before)
        fresh = self.reopen()
        fresh.recover_pending()
        new_item = self.begin(fresh, epoch=3)
        result = self.assess(new_item, fresh, attempt=11)
        self.assertEqual(result.reason, 'replay')

    def test_recovery_commit_faults_remain_safe(self):
        for point in ('recovery_before_commit', 'recovery_after_commit'):
            with self.subTest(point=point):
                self.path = Path(self.temp.name) / (point + '.sqlite')
                self.store = DurableLedger.create(self.path, uid(900))
                item = self.begin()
                with patch('prometheus_lab.lab_control.durable.request_vm_operation', side_effect=OSError('fault')):
                    with self.assertRaises(OSError):
                        self.assess(item)
                reopened = self.reopen()
                def fail(name):
                    if name == point:
                        raise OSError('recovery fault')
                with patch.object(reopened, '_checkpoint', side_effect=fail):
                    with self.assertRaises(OSError):
                        reopened.recover_pending()
                final = self.reopen()
                self.assertEqual(final.snapshot().status,
                                 'recovery_required' if point.endswith('before_commit') else 'failures_retained')
                final.recover_pending()
                new_item = self.begin(final, epoch=3)
                self.assertEqual(self.assess(new_item, final, attempt=11).reason, 'replay')

    def test_lock_contention_times_out_without_reservation(self):
        item = self.begin()
        blocker = sqlite3.connect(self.path, isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaisesRegex(StoreError, 'locked'):
                self.assess(item)
        finally:
            blocker.close()
        self.assertEqual([row[1] for row in self.rows()], ['session'])
        with self.assertRaisesRegex(StoreError, 'poisoned'):
            self.assess(item)

    def test_session_commit_faults_and_process_death_never_restore_ownership(self):
        for point in ('session_before_commit', 'session_after_commit'):
            for death in (False, True):
                with self.subTest(point=point, death=death):
                    self.path = Path(self.temp.name) / (point + str(death) + '.sqlite')
                    self.store = DurableLedger.create(self.path, uid(900))
                    if death:
                        result = self.worker(point, 2)
                        self.assertEqual(result.returncode, 73, result.stderr)
                    else:
                        def fail(name):
                            if name == point:
                                raise OSError('session acknowledgment fault')
                        with patch.object(self.store, '_checkpoint', side_effect=fail):
                            with self.assertRaises(OSError):
                                self.begin()
                        with self.assertRaisesRegex(StoreError, 'poisoned'):
                            self.begin(epoch=3)
                    reopened = self.reopen()
                    self.assertEqual(len(reopened.snapshot().contexts), int(point.endswith('after_commit')))
                    with self.assertRaisesRegex(StoreError, 'No active owned session'):
                        self.assess(store=reopened)

    def test_actual_process_death_during_recovery_preserves_consumption(self):
        for point in ('recovery_before_commit', 'recovery_after_commit'):
            with self.subTest(point=point):
                self.path = Path(self.temp.name) / (point + '.sqlite')
                self.store = DurableLedger.create(self.path, uid(900))
                item = self.begin()
                with patch('prometheus_lab.lab_control.durable.request_vm_operation', side_effect=OSError('fault')):
                    with self.assertRaises(OSError):
                        self.assess(item)
                result = self.worker(point, 3)
                self.assertEqual(result.returncode, 73, result.stderr)
                reopened = self.reopen()
                expected = 'recovery_required' if point.endswith('before_commit') else 'failures_retained'
                self.assertEqual(reopened.snapshot().status, expected)
                reopened.recover_pending()
                fresh = self.begin(reopened, epoch=3)
                self.assertEqual(self.assess(fresh, reopened, attempt=11).reason, 'replay')

    def test_recovery_racing_finalization_fences_original_handle(self):
        item = self.begin()
        def recover_after_commit(name):
            if name == 'reservation_after_commit':
                self.reopen().recover_pending()
        with patch.object(self.store, '_checkpoint', side_effect=recover_after_commit), \
             patch('prometheus_lab.lab_control.durable.request_vm_operation') as boundary:
            with self.assertRaisesRegex(StoreError, 'changed before finalization'):
                self.assess(item)
        boundary.assert_not_called()
        snapshot = self.reopen().snapshot()
        self.assertEqual(snapshot.status, 'failures_retained')
        self.assertIsNone(snapshot.active_epoch)
        self.assertEqual([row[1] for row in self.rows()], ['session', 'reserved', 'recovered'])

    def test_corrupt_partial_future_and_semantically_invalid_records_fail_closed(self):
        item = self.begin()
        self.assess(item)
        original = self.path.read_bytes()
        sql_cases = (
            "UPDATE metadata SET version=99",
            "DELETE FROM events WHERE seq=3",
            "UPDATE events SET body='{' WHERE seq=2",
            "UPDATE events SET digest='bad' WHERE seq=2",
            "UPDATE metadata SET head='bad'",
            "CREATE TABLE surprise (x)",
        )
        for sql in sql_cases:
            with self.subTest(sql=sql):
                self.path.write_bytes(original)
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute(sql)
                corrupted = self.path.read_bytes()
                with self.assertRaises(StoreError):
                    self.reopen()
                self.assertEqual(self.path.read_bytes(), corrupted)
        self.path.write_bytes(b'SQLite format 3\x00partial')
        with self.assertRaises(StoreError):
            self.reopen()

    def test_rehashed_contradiction_is_rejected_semantically(self):
        self.begin()
        self.assess()
        # Independent hash calculation: a self-consistent hash chain is not enough.
        from hashlib import sha256
        with closing(sqlite3.connect(self.path)) as connection, connection:
            seq, kind, body, _ = self.rows()[-1]
            raw = json.loads(body)
            raw['outcome'], raw['reason'] = 'denied', 'mock_denial'
            body = json.dumps(raw)
            previous = self.rows()[-2][3]
            digest = sha256((previous + '\n' + kind + '\n' + body).encode()).hexdigest()
            connection.execute('UPDATE events SET body=?,digest=? WHERE seq=?', (body, digest, seq))
            connection.execute('UPDATE metadata SET head=?', (digest,))
        with self.assertRaisesRegex(StoreError, 'contradicts'):
            self.reopen()

    def test_rehashed_malformed_json_and_duplicate_keys_are_rejected(self):
        self.begin()
        self.assess()
        original = self.path.read_bytes()
        from hashlib import sha256
        for body in ('{', '{"attempt_id":null,"attempt_id":null}',
                     '{"outcome":NaN}', '{"outcome":"completed_execution"}'):
            with self.subTest(body=body):
                self.path.write_bytes(original)
                previous = self.rows()[-2][3]
                digest = sha256((previous + '\nterminal\n' + body).encode()).hexdigest()
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute('UPDATE events SET body=?,digest=? WHERE seq=3', (body, digest))
                    connection.execute('UPDATE metadata SET head=?', (digest,))
                before = self.path.read_bytes()
                with self.assertRaises(StoreError):
                    self.reopen()
                self.assertEqual(self.path.read_bytes(), before)

    def test_snapshot_is_read_only_and_does_not_claim_authenticity(self):
        self.begin()
        self.assess()
        before = self.path.read_bytes()
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot.status, 'consistent_simulation')
        self.assertFalse(snapshot.confers_authority)
        self.assertFalse(snapshot.valid_completion)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_malicious_backup_rollback_is_explicitly_outside_guarantee(self):
        backup = self.path.with_name('old-copy.sqlite')
        shutil.copyfile(self.path, backup)
        self.begin()
        self.assess()
        shutil.copyfile(backup, self.path)  # Simulate an attacker replacing all evidence.
        rolled_back = self.reopen()
        item = self.begin(rolled_back, epoch=3)
        result = self.assess(item, rolled_back)
        self.assertEqual(result.status, 'blocked')
        self.assertFalse(result.confers_authority)
        self.assertFalse(result.valid_completion)


if __name__ == '__main__':
    unittest.main()
