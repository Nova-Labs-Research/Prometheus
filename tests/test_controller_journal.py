from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError
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
from prometheus_lab.lab_control.controller_journal import ControllerJournal, TestObservation as Observation
from prometheus_lab.lab_control.durable import DurableLedger, RecoveryRequired, StoreError, _hash, ZERO
from test_controller import request
from test_phase2a_contract import approval, clock, manifest, uid


def observation(number=1, state="off", **changes):
    values = dict(schema_version="controller-test-observation-v1", source="test_only",
        observation_id=uid(100 + number), store_id=uid(900), attempt_id=uid(10),
        process_epoch=uid(2), run_id=uid(1), vm_id=uid(3), manifest_digest=manifest().digest(),
        clock=clock(number), reported_state=state)
    values.update(changes)
    return Observation(**values)


# Fixed trusted harness only: temporary SQLite contract tests, never VM/candidate code.
WORKER = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_controller_journal import observation
from test_controller import request
from test_phase2a_contract import clock, uid
from prometheus_lab.lab_control.contracts import Context
from prometheus_lab.lab_control.controller_journal import ControllerJournal
from prometheus_lab.lab_control.durable import StoreError
path, mode = Path(sys.argv[2]), sys.argv[3]
store = ControllerJournal.reopen(path, uid(900))
def checkpoint(name):
    if name == mode.removeprefix('reject_'):
        os._exit(73)
store._checkpoint = checkpoint
try:
    if mode.startswith('session'):
        store.begin_session(Context(process_epoch=uid(2), origin=clock()))
    elif mode.startswith(('prepare', 'reject_prepare')):
        store.begin_session(Context(process_epoch=uid(3), origin=clock()))
        from test_phase2a_contract import manifest
        item = manifest().model_copy(update={'process_epoch': uid(3)})
        store.prepare(request(item=item, watchdog_ready=not mode.startswith('reject_')))
    elif mode.startswith('observation'):
        store.record_observation(observation())
    elif mode.startswith('reconcile'):
        store.reconcile(uid(10), store.snapshot().head)
    elif mode == 'race':
        epoch = int(sys.argv[4])
        store.begin_session(Context(process_epoch=uid(epoch), origin=clock()))
        from test_phase2a_contract import manifest
        item = manifest().model_copy(update={'process_epoch': uid(epoch)})
        print(store.prepare(request(item=item, attempt=epoch)).state, flush=True)
except StoreError:
    print('closed', flush=True)
'''


class ControllerJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "controller.sqlite"
        self.store = ControllerJournal.create(self.path, uid(900))

    def begin(self, store=None, epoch=2, seconds=0):
        (store or self.store).begin_session(Context(process_epoch=uid(epoch), origin=clock(seconds)))

    def reopen(self):
        return ControllerJournal.reopen(self.path, uid(900))

    def rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT seq,kind,body,digest FROM events ORDER BY seq").fetchall()

    def worker(self, mode):
        return subprocess.run([sys.executable, "-c", WORKER, str(Path(__file__).parent),
            str(self.path), mode], capture_output=True, text=True, timeout=20)

    def rewrite_last(self, change):
        rows = self.rows()
        body = json.loads(rows[-1][2])
        change(body)
        encoded = json.dumps(body, separators=(",", ":"))
        previous = rows[-2][3] if len(rows) > 1 else ZERO
        digest = _hash(previous, rows[-1][1], encoded)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE events SET body=?,digest=? WHERE seq=?", (encoded, digest, rows[-1][0]))
            connection.execute("UPDATE metadata SET head=?", (digest,))

    def test_separate_version_exclusive_create_and_existing_only_open(self):
        with self.assertRaises(FileExistsError):
            ControllerJournal.create(self.path, uid(900))
        with self.assertRaises(StoreError):
            ControllerJournal.reopen(self.path.with_name("missing.sqlite"), uid(900))
        self.assertFalse(self.path.with_name("missing.sqlite").exists())
        with self.assertRaises(StoreError):
            ControllerJournal.reopen(self.path, uid(901))
        with self.assertRaises(StoreError):
            DurableLedger.reopen(self.path, uid(900))
        old = self.path.with_name("old.sqlite")
        DurableLedger.create(old, uid(900))
        with self.assertRaises(StoreError):
            ControllerJournal.reopen(old, uid(900))

    def test_rejection_persists_complete_preflight_and_consumes_approval(self):
        self.begin()
        result = self.store.prepare(request(watchdog_ready=False, collector_ready=False))
        self.assertEqual(result.state, "rejected")
        self.assertEqual(result.prepared.gates, ("collector_unavailable", "watchdog_unavailable"))
        self.assertFalse(result.prepared.request.readiness.watchdog_ready)
        rows = self.rows()
        self.assertEqual([r[1] for r in rows], ["session", "prepared"])
        persisted = json.loads(rows[-1][2])
        self.assertEqual(persisted["disposition"], "rejected")
        self.assertFalse(persisted["request"]["readiness"]["collector_ready"])
        other = self.reopen()
        self.assertEqual(other.snapshot().attempts[0], result)
        self.begin(other, epoch=20)
        item = manifest().model_copy(update={"process_epoch": uid(20)})
        replay = other.prepare(request(attempt=11, item=item))
        self.assertEqual(replay.prepared.approval_reason, "replay")
        self.assertEqual(replay.state, "rejected")

    def test_intent_commits_atomically_without_any_effect_invocation(self):
        self.begin()
        observed = []
        def checkpoint(name):
            if name == "prepare_after_commit":
                observed.append([row[1] for row in self.rows()])
        with patch.object(self.store, "_checkpoint", side_effect=checkpoint), patch(
                "prometheus_lab.lab_control.durable.request_vm_operation") as boundary, patch(
                "subprocess.Popen") as process, patch("socket.create_connection") as network:
            result = self.store.prepare(request())
            boundary.assert_not_called()
            process.assert_not_called()
            network.assert_not_called()
        self.assertEqual(observed, [["session", "prepared"]])
        self.assertEqual(result.state, "unresolved")
        self.assertEqual(self.reopen().snapshot().status, "reconciliation_required")
        self.assertFalse(result.valid_completion)
        self.assertFalse(result.confers_authority)

    def test_unresolved_intent_blocks_prepare_and_new_sessions(self):
        self.begin()
        self.store.prepare(request())
        with self.assertRaises(RecoveryRequired):
            self.store.prepare(request(attempt=11))
        with self.assertRaises(RecoveryRequired):
            self.begin(self.reopen(), epoch=20)
        self.assertEqual(len(self.rows()), 2)

    def test_denial_and_exact_expiry_are_durable_rejections(self):
        self.begin()
        for n, record, reading, expected in [
            (10, approval(decision="deny"), clock(), "mock_denial"),
            (11, approval(approval_id=uid(50)), clock(900), "expired"),
        ]:
            with self.subTest(reason=expected):
                result = self.store.prepare(request(attempt=n, record=record, reading=reading))
                self.assertEqual(result.state, "rejected")
                self.assertEqual(result.prepared.approval_reason, expected)
        self.assertEqual(len(self.reopen().snapshot().attempts), 2)

    def test_observations_stay_unverified_and_contradictions_are_preserved(self):
        self.begin()
        self.store.prepare(request())
        for n, state in enumerate(("off", "running", "unknown"), 1):
            self.reopen().record_observation(observation(n, state))
        snapshot = self.reopen().snapshot()
        item = snapshot.attempts[0]
        self.assertEqual(item.state, "unresolved")
        self.assertEqual(item.uncertainty, "conflicting_reports")
        self.assertEqual(tuple(x.reported_state for x in item.observations), ("off", "running", "unknown"))
        self.assertFalse(snapshot.valid_completion)
        self.assertFalse(snapshot.confers_authority)

    def test_every_observation_binding_and_clock_is_checked(self):
        self.begin()
        self.store.prepare(request(reading=clock(2)))
        baseline = observation(clock=clock(2))
        variants = {"store_id": uid(901), "attempt_id": uid(11), "process_epoch": uid(21),
            "run_id": uid(31), "vm_id": uid(41), "manifest_digest": "f" * 64, "clock": clock(1)}
        for field, value in variants.items():
            with self.subTest(field=field):
                with self.assertRaises(StoreError):
                    self.reopen().record_observation(baseline.model_copy(update={field: value}))
        self.assertEqual(len(self.rows()), 2)
        accepted = self.store.record_observation(baseline)
        self.assertEqual(accepted.observations, (baseline,))

    def test_observation_schema_excludes_authentication_completion_and_unknown_fields(self):
        self.begin()
        self.store.prepare(request())
        for change in ({"source": "authenticated-host"}, {"reported_state": "completed"},
                       {"authorized": True}, {"schema_version": "future"}):
            with self.subTest(change=change):
                wire = observation().model_dump(mode="json")
                wire.update(change)
                with self.assertRaises(ValueError):
                    self.store.record_observation(json.dumps(wire).encode())
        self.assertEqual(len(self.rows()), 2)
        self.store.record_observation(observation())  # Malformed calls did not poison.

    def test_duplicate_or_old_observation_and_post_reconciliation_report_refused(self):
        self.begin()
        self.store.prepare(request())
        self.store.record_observation(observation(2))
        for item in (observation(2), observation(3, clock=clock(1))):
            with self.subTest(item=item.observation_id):
                with self.assertRaises(StoreError):
                    self.reopen().record_observation(item)
        other = self.reopen()
        other.reconcile(uid(10), other.snapshot().head)
        with self.assertRaises(StoreError):
            self.reopen().record_observation(observation(3))

    def test_reconciliation_requires_current_head_and_retains_uncertainty(self):
        self.begin()
        self.store.prepare(request())
        old = self.store.snapshot().head
        self.store.record_observation(observation())
        with self.assertRaises(StoreError):
            self.reopen().reconcile(uid(10), old)
        other = self.reopen()
        final = other.reconcile(uid(10), other.snapshot().head)
        self.assertEqual(final.state, "abandoned_unverified")
        self.assertEqual(final.uncertainty, "unverified_reports")
        self.assertFalse(final.valid_completion)
        self.assertEqual(other.snapshot().active_epoch, None)
        with self.assertRaises(StoreError):
            self.store.prepare(request(attempt=11))
        with self.assertRaises(StoreError):
            self.reopen().reconcile(uid(10), other.snapshot().head)

    def test_abandonment_without_reports_is_not_proof_of_no_execution(self):
        self.begin()
        self.store.prepare(request())
        result = self.store.reconcile(uid(10), self.store.snapshot().head)
        self.assertEqual(result.state, "abandoned_unverified")
        self.assertEqual(result.uncertainty, "no_observations")
        self.assertFalse(result.valid_completion)
        self.assertFalse(result.confers_authority)

    def test_reopen_requires_new_epoch_and_old_consumption_survives_abandonment(self):
        self.begin()
        self.store.prepare(request())
        other = self.reopen()
        other.reconcile(uid(10), other.snapshot().head)
        with self.assertRaises(StoreError):
            self.reopen().prepare(request(attempt=11))
        with self.assertRaises(StoreError):
            self.begin(self.reopen())
        new = self.reopen()
        self.begin(new, epoch=20)
        item = manifest().model_copy(update={"process_epoch": uid(20)})
        result = new.prepare(request(attempt=11, item=item))
        self.assertEqual(result.prepared.approval_reason, "replay")

    def test_new_epoch_wall_floor_includes_observations(self):
        self.begin()
        self.store.prepare(request())
        self.store.record_observation(observation(50))
        self.store.reconcile(uid(10), self.store.snapshot().head)
        with self.assertRaises(StoreError):
            self.begin(self.reopen(), epoch=20, seconds=49)
        self.begin(self.reopen(), epoch=21, seconds=50)

    def test_actual_process_exit_at_every_transaction_boundary(self):
        for label in ("session", "prepare", "observation", "reconcile"):
            for side in ("before", "after"):
                with self.subTest(label=label, side=side):
                    path = self.path.with_name(label + side + ".sqlite")
                    store = ControllerJournal.create(path, uid(900))
                    if label in ("observation", "reconcile"):
                        self.begin(store)
                        store.prepare(request())
                    result = subprocess.run([sys.executable, "-c", WORKER, str(Path(__file__).parent),
                        str(path), label + "_" + side + "_commit"], capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 73, result.stderr)
                    recovered = ControllerJournal.reopen(path, uid(900)).snapshot()
                    if label == "session":
                        self.assertEqual(len(recovered.contexts), int(side == "after"))
                    elif label == "prepare":
                        self.assertEqual(len(recovered.attempts), int(side == "after"))
                        if side == "after":
                            self.assertEqual(recovered.status, "reconciliation_required")
                    elif label == "observation":
                        self.assertEqual(len(recovered.attempts[0].observations), int(side == "after"))
                    else:
                        self.assertEqual(recovered.attempts[0].state,
                            "abandoned_unverified" if side == "after" else "unresolved")

    def test_commit_ack_loss_poison_and_reopen_preserves_committed_intent(self):
        self.begin()
        def checkpoint(name):
            if name == "prepare_after_commit":
                raise OSError("lost acknowledgment")
        with patch.object(self.store, "_checkpoint", side_effect=checkpoint):
            with self.assertRaises(OSError):
                self.store.prepare(request())
        with self.assertRaisesRegex(StoreError, "poisoned"):
            self.store.prepare(request(attempt=11))
        self.assertEqual(self.reopen().snapshot().status, "reconciliation_required")

    def test_actual_rejection_crash_retains_consumption_only_after_commit(self):
        for side in ("before", "after"):
            with self.subTest(side=side):
                path = self.path.with_name("rejected-" + side + ".sqlite")
                ControllerJournal.create(path, uid(900))
                result = subprocess.run([sys.executable, "-c", WORKER, str(Path(__file__).parent),
                    str(path), "reject_prepare_" + side + "_commit"],
                    capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 73, result.stderr)
                other = ControllerJournal.reopen(path, uid(900))
                self.assertEqual(len(other.snapshot().attempts), int(side == "after"))
                if side == "after":
                    retained = other.snapshot().attempts[0]
                    self.assertEqual(retained.prepared.gates, ("watchdog_unavailable",))
                    self.assertEqual(retained.state, "rejected")
                self.begin(other, epoch=20)
                item = manifest().model_copy(update={"process_epoch": uid(20)})
                retried = other.prepare(request(attempt=11, item=item))
                self.assertEqual(retried.prepared.approval_reason, "replay" if side == "after" else None)

    def test_busy_store_poisons_handle_without_creating_partial_event(self):
        self.begin()
        with closing(sqlite3.connect(self.path, isolation_level=None)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(StoreError):
                self.store.prepare(request())
            blocker.rollback()
        self.assertEqual(len(self.rows()), 1)
        with self.assertRaisesRegex(StoreError, "poisoned"):
            self.store.prepare(request())

    def test_duplicate_attempt_id_is_not_a_second_durable_rejection(self):
        self.begin()
        self.store.prepare(request(watchdog_ready=False))
        with self.assertRaises(StoreError):
            self.store.prepare(request(record=approval(approval_id=uid(77)), watchdog_ready=False))
        self.assertEqual(len(self.rows()), 2)

    def test_precommit_rejection_failure_leaves_no_record_or_consumption(self):
        self.begin()
        def checkpoint(name):
            if name == "prepare_before_commit":
                raise OSError("write failed")
        with patch.object(self.store, "_checkpoint", side_effect=checkpoint):
            with self.assertRaises(OSError):
                self.store.prepare(request(watchdog_ready=False))
        self.assertEqual(len(self.rows()), 1)
        other = self.reopen()
        self.begin(other, epoch=20)
        item = manifest().model_copy(update={"process_epoch": uid(20)})
        self.assertEqual(other.prepare(request(item=item)).state, "unresolved")

    def test_postcommit_readback_failure_never_acknowledges_success(self):
        self.begin()
        with patch.object(self.store, "snapshot", side_effect=StoreError("read unavailable")):
            with self.assertRaises(StoreError):
                self.store.prepare(request())
        self.assertEqual(self.reopen().snapshot().attempts[0].state, "unresolved")
        with self.assertRaisesRegex(StoreError, "poisoned"):
            self.store.prepare(request(attempt=11))

    def test_concurrent_prepares_and_cross_process_race_have_one_intent(self):
        self.begin()
        def run(number):
            try:
                return self.store.prepare(request(attempt=number)).state
            except StoreError:
                return "closed"
        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(run, (10, 11)))
        self.assertEqual(sorted(states), ["closed", "unresolved"])
        path = self.path.with_name("race.sqlite")
        ControllerJournal.create(path, uid(900))
        processes = [subprocess.Popen([sys.executable, "-c", WORKER, str(Path(__file__).parent),
            str(path), "race", str(n)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for n in (20, 21)]
        outputs = [p.communicate(timeout=20) for p in processes]
        self.assertTrue(all(p.returncode == 0 for p in processes), outputs)
        self.assertEqual(sum("unresolved" in stdout for stdout, _ in outputs), 1)
        self.assertEqual(len(ControllerJournal.reopen(path, uid(900)).snapshot().attempts), 1)

    def test_observation_racing_reconciliation_cannot_be_silently_discarded(self):
        self.begin()
        self.store.prepare(request())
        head = self.store.snapshot().head
        def observe():
            try:
                self.reopen().record_observation(observation())
                return "observed"
            except StoreError:
                return "closed"
        def reconcile():
            try:
                self.reopen().reconcile(uid(10), head)
                return "reconciled"
            except StoreError:
                return "closed"
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(observe), pool.submit(reconcile)
            outcomes = (a.result(), b.result())
        self.assertIn(outcomes, (("observed", "closed"), ("closed", "reconciled")))
        item = self.reopen().snapshot().attempts[0]
        self.assertEqual(len(item.observations), int(outcomes[0] == "observed"))

    def test_semantic_corruption_is_rejected_even_with_recomputed_hashes(self):
        self.begin()
        self.store.prepare(request(watchdog_ready=False))
        self.rewrite_last(lambda body: body.update(disposition="intent_recorded", gates=[]))
        with self.assertRaises(StoreError):
            self.reopen()

    def test_corrupt_unknown_schema_duplicate_json_and_missing_events_fail_closed(self):
        self.begin()
        self.store.prepare(request())
        original = self.path.read_bytes()
        mutations = ["UPDATE metadata SET version=99", "DELETE FROM events WHERE seq=2",
            "UPDATE events SET kind='completed' WHERE seq=2", "UPDATE metadata SET head='bad'",
            "CREATE TABLE unexpected(value TEXT)"]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.path.write_bytes(original)
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute(mutation)
                with self.assertRaises(StoreError):
                    self.reopen()
        self.path.write_bytes(original)
        rows = self.rows()
        body = rows[-1][2].replace('"epoch":', '"epoch":"00000000-0000-0000-0000-000000000002","epoch":', 1)
        digest = _hash(rows[-2][3], rows[-1][1], body)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE events SET body=?,digest=? WHERE seq=2", (body, digest))
            connection.execute("UPDATE metadata SET head=?", (digest,))
        with self.assertRaises(StoreError):
            self.reopen()

    def test_read_only_snapshot_does_not_write_or_repair_and_is_immutable(self):
        self.begin()
        self.store.prepare(request())
        before = self.path.read_bytes()
        snapshot = self.store.snapshot()
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(FrozenInstanceError):
            snapshot.head = "f" * 64
        self.assertFalse(snapshot.valid_completion)
        self.assertFalse(snapshot.confers_authority)

    def test_rollback_of_entire_store_remains_undetectable_not_authenticated(self):
        self.begin()
        backup = self.path.with_name("backup.sqlite")
        shutil.copyfile(self.path, backup)
        self.store.prepare(request())
        shutil.copyfile(backup, self.path)
        rolled_back = self.reopen().snapshot()
        self.assertEqual(rolled_back.attempts, ())
        self.assertFalse(rolled_back.confers_authority)

    def test_bounded_json_and_model_bypass_are_revalidated_before_io(self):
        self.begin()
        with patch.object(self.store, "_connection") as connection:
            for value in (b" " * 65537, b"\xff", b"[]", b'{"schema_version":NaN}',
                          request().model_copy(update={"attempt_id": "not-a-uuid"})):
                with self.subTest(value=type(value).__name__):
                    with self.assertRaises(ValueError):
                        self.store.prepare(value)
            connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
