from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from prometheus_lab.lab_control.boundary import VMOperationsUnavailable
from prometheus_lab.lab_control.contracts import Context
from prometheus_lab.lab_control.controller import (
    CodeOnlyController, ControllerReceipt, ControllerRequest, MAX_ENVELOPE_BYTES,
    MockReadiness, decode_envelope, preflight,
)
from prometheus_lab.lab_control.durable import DurableLedger, RecoveryRequired, StoreError
from test_phase2a_contract import approval, clock, manifest, uid


def request(*, attempt=10, item=None, record=None, reading=None, **ready_changes):
    item, reading = item or manifest(), reading or clock()
    ready = dict(source="mock", store_id=uid(900), attempt_id=uid(attempt),
        manifest_digest=item.digest(), observed_at=reading, vm_id=item.vm_id,
        vm_state="off", network_adapter_count=0, guest_memory_bytes=4294967296,
        dynamic_memory=False, vcpus=1, evidence_writable=True,
        evidence_remaining_bytes=4194304, collector_ready=True, watchdog_ready=True)
    ready.update(ready_changes)
    return ControllerRequest(schema_version="controller-rehearsal-v1", attempt_id=uid(attempt),
        manifest=item, approval=record or approval(item), clock=reading,
        readiness=MockReadiness(**ready))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "controller.sqlite"
        self.store = DurableLedger.create(self.path, uid(900))
        self.store.begin_session(Context(process_epoch=uid(2), origin=clock()))
        self.controller = CodeOnlyController(self.store)

    def rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT kind,body FROM events ORDER BY seq").fetchall()

    def test_passing_preflight_reaches_only_disabled_boundary_after_commit(self):
        calls = []
        def unavailable(**kwargs):
            rows = self.rows()
            self.assertEqual([row[0] for row in rows], ["session", "reserved"])
            body = json.loads(rows[-1][1])["attempt"]
            self.assertEqual(body["manifest"], request().manifest.model_dump(mode="json"))
            self.assertEqual(body["approval"], request().approval.model_dump(mode="json"))
            calls.append(kwargs)
            raise VMOperationsUnavailable("no launch implementation")
        with patch("prometheus_lab.lab_control.durable.request_vm_operation", side_effect=unavailable):
            receipt = self.controller.rehearse(request())
        self.assertEqual(len(calls), 1)
        self.assertEqual(receipt.status, "blocked")
        self.assertEqual([row[0] for row in self.rows()], ["session", "reserved", "terminal"])
        self.assertEqual(self.controller.verify(receipt).status, "consistent_rehearsal")
        self.assertFalse(receipt.confers_authority)
        self.assertFalse(receipt.valid_completion)

    def test_every_failed_readiness_gate_prevents_consumption_and_boundary(self):
        cases = [
            ({"store_id": uid(901)}, "binding_mismatch"),
            ({"attempt_id": uid(90)}, "binding_mismatch"),
            ({"manifest_digest": "a" * 64}, "binding_mismatch"),
            ({"vm_id": uid(91)}, "binding_mismatch"),
            ({"observed_at": clock(1)}, "clock_mismatch"),
            ({"vm_state": "running"}, "vm_not_off"),
            ({"vm_state": "unknown"}, "vm_not_off"),
            ({"network_adapter_count": 1}, "network_present"),
            ({"guest_memory_bytes": 4294967295}, "memory_mismatch"),
            ({"dynamic_memory": True}, "memory_mismatch"),
            ({"vcpus": 2}, "cpu_mismatch"),
            ({"evidence_writable": False}, "evidence_unavailable"),
            ({"evidence_remaining_bytes": 4194303}, "retention_insufficient"),
            ({"collector_ready": False}, "collector_unavailable"),
            ({"watchdog_ready": False}, "watchdog_unavailable"),
        ]
        original = self.path.read_bytes()
        with patch("prometheus_lab.lab_control.durable.request_vm_operation") as boundary:
            for changes, expected in cases:
                with self.subTest(changes=changes):
                    receipt = self.controller.rehearse(request(**changes))
                    self.assertEqual(receipt.status, "preflight_blocked")
                    self.assertEqual(receipt.gates, (expected,))
                    self.assertEqual(self.controller.verify(receipt).status, "unpersisted_preflight")
            boundary.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_all_readiness_faults_are_retained_in_deterministic_order(self):
        gates = preflight(request(vm_id=uid(91), observed_at=clock(1), vm_state="unknown",
            network_adapter_count=1, dynamic_memory=True, vcpus=2, evidence_writable=False,
            evidence_remaining_bytes=0, collector_ready=False, watchdog_ready=False), uid(900))
        self.assertEqual(gates, ("binding_mismatch", "clock_mismatch", "vm_not_off", "network_present",
            "memory_mismatch", "cpu_mismatch", "evidence_unavailable", "retention_insufficient",
            "collector_unavailable", "watchdog_unavailable"))

    def test_output_reservation_exact_boundary_is_not_a_real_quota(self):
        for remaining, expected in [(4194303, ("retention_insufficient",)), (4194304, ()), (4194305, ())]:
            with self.subTest(remaining=remaining):
                self.assertEqual(preflight(request(evidence_remaining_bytes=remaining), uid(900)), expected)

    def test_all_manifest_changes_invalidate_old_preflight_digest(self):
        original = request()
        changes = {"run_id": uid(20), "process_epoch": uid(21), "vm_id": uid(22),
                   "output_schema": "new-schema"}
        changes.update({key: "a" * 64 for key in (
            "design_digest", "fixture_digest", "base_image_digest", "runtime_digest",
            "input_image_digest", "isolation_config_digest", "policy_digest")})
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = original.model_copy(update={"manifest": original.manifest.model_copy(update={field: value})})
                self.assertIn("binding_mismatch", preflight(changed, uid(900)))
        changed_limits = original.manifest.limits.model_copy(update={"output_bytes": 4194305})
        changed = original.model_copy(update={"manifest": original.manifest.model_copy(update={"limits": changed_limits})})
        self.assertEqual(preflight(changed, uid(900)), ("binding_mismatch", "retention_insufficient"))

    def test_denial_and_exact_expiry_never_reach_boundary(self):
        for number, record, reading, reason in [
            (10, approval(decision="deny"), clock(), "mock_denial"),
            (11, approval(approval_id=uid(41)), clock(900), "expired"),
        ]:
            with self.subTest(reason=reason):
                with patch("prometheus_lab.lab_control.durable.request_vm_operation") as boundary:
                    receipt = self.controller.rehearse(request(attempt=number, record=record, reading=reading))
                boundary.assert_not_called()
                self.assertEqual(receipt.status, "denied")
                self.assertEqual(self.store.snapshot().attempts[-1].reason, reason)

    def test_concurrent_requests_consume_approval_only_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda n: self.controller.rehearse(request(attempt=n)), (10, 11)))
        self.assertEqual(sorted(item.status for item in results), ["blocked", "denied"])
        self.assertEqual(self.store.snapshot().attempts[-1].reason, "replay")

    def test_replay_survives_reopen_with_new_controller_and_epoch(self):
        self.controller.rehearse(request())
        store = DurableLedger.reopen(self.path, uid(900))
        store.begin_session(Context(process_epoch=uid(20), origin=clock(1)))
        item = manifest().model_copy(update={"process_epoch": uid(20)})
        record = approval(item, issued_at=clock(1).wall_time, issued_monotonic_ms=1000)
        result = CodeOnlyController(store).rehearse(request(attempt=11, item=item, record=record, reading=clock(1)))
        self.assertEqual(result.status, "denied")
        self.assertEqual(store.snapshot().attempts[-1].reason, "replay")

    def test_reopened_handle_never_automatically_resumes_session(self):
        controller = CodeOnlyController(DurableLedger.reopen(self.path, uid(900)))
        with patch("prometheus_lab.lab_control.durable.request_vm_operation") as boundary:
            with self.assertRaises(StoreError):
                controller.rehearse(request())
            boundary.assert_not_called()
        self.assertEqual([row[0] for row in self.rows()], ["session"])

    def test_failed_preflight_can_be_retried_but_is_not_durable_evidence(self):
        rejected = self.controller.rehearse(request(watchdog_ready=False))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.controller.rehearse(request()).status, "blocked")
        self.assertEqual(self.controller.verify(rejected).status, "unpersisted_preflight")

    def test_corrupt_store_blocks_before_assessment_and_poison_is_retained(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE metadata SET head='broken'")
        with patch.object(self.store, "assess") as assess:
            with self.assertRaises(StoreError):
                self.controller.rehearse(request())
            with self.assertRaisesRegex(StoreError, "poisoned"):
                self.controller.rehearse(request(attempt=11))
            assess.assert_not_called()

    def test_pending_failure_requires_explicit_recovery_not_controller_retry(self):
        with patch("prometheus_lab.lab_control.durable.request_vm_operation", side_effect=OSError("fault")):
            with self.assertRaises(OSError):
                self.controller.rehearse(request())
        self.assertEqual(self.store.snapshot().status, "recovery_required")
        other = CodeOnlyController(DurableLedger.reopen(self.path, uid(900)))
        with patch.object(other._ledger, "assess") as assess:
            with self.assertRaises(RecoveryRequired):
                other.rehearse(request(attempt=11))
            assess.assert_not_called()
        self.assertEqual([row[0] for row in self.rows()], ["session", "reserved"])

    def test_unexpected_boundary_return_is_failure_and_never_receipt_success(self):
        with patch("prometheus_lab.lab_control.durable.request_vm_operation", return_value=True):
            with self.assertRaisesRegex(StoreError, "unexpectedly returned"):
                self.controller.rehearse(request())
        self.assertEqual(self.store.snapshot().status, "recovery_required")

    def test_reservation_fault_prevents_boundary_and_all_further_stages(self):
        def fault(name):
            if name == "reservation_before_commit":
                raise OSError("recording failed")
        with patch.object(self.store, "_checkpoint", side_effect=fault), patch(
                "prometheus_lab.lab_control.durable.request_vm_operation") as boundary:
            with self.assertRaises(OSError):
                self.controller.rehearse(request())
            boundary.assert_not_called()
        self.assertEqual([row[0] for row in self.rows()], ["session"])

    def test_terminal_commit_acknowledgment_loss_never_returns_receipt(self):
        def fault(name):
            if name == "terminal_after_commit":
                raise OSError("acknowledgment lost")
        with patch.object(self.store, "_checkpoint", side_effect=fault):
            with self.assertRaises(OSError):
                self.controller.rehearse(request())
        self.assertEqual(self.store.snapshot().attempts[-1].status, "blocked")
        with self.assertRaisesRegex(StoreError, "poisoned"):
            self.controller.rehearse(request(attempt=11))

    def test_readback_failure_preserves_terminal_but_poison_controller(self):
        snapshot = self.store.snapshot
        calls = []
        def fail_second():
            calls.append(1)
            if len(calls) == 2:
                raise StoreError("readback unavailable")
            return snapshot()
        with patch.object(self.store, "snapshot", side_effect=fail_second):
            with self.assertRaisesRegex(StoreError, "readback failed"):
                self.controller.rehearse(request())
        self.assertEqual(snapshot().attempts[-1].status, "blocked")
        with self.assertRaisesRegex(StoreError, "poisoned"):
            self.controller.rehearse(request(attempt=11))

    def test_receipt_reconciliation_rejects_tampered_context_status_and_gates(self):
        receipt = self.controller.rehearse(request())
        changes = [receipt.model_copy(update={"store_id": uid(901)}),
            receipt.model_copy(update={"status": "denied"}),
            receipt.model_copy(update={"gates": ("watchdog_unavailable",)}),
            receipt.model_copy(update={"request": receipt.request.model_copy(update={"approval": approval(decision="deny")})}),
            receipt.model_copy(update={"status": "completed"}),
        ]
        for changed in changes:
            with self.subTest(changed=changed.status):
                result = self.controller.verify(changed)
                self.assertEqual(result.status, "invalid")
                self.assertFalse(result.valid_completion)

    def test_missing_pending_and_recovered_evidence_never_verifies_as_complete(self):
        claimed = ControllerReceipt(schema_version="controller-receipt-v1", store_id=uid(900),
                                    request=request(), status="blocked", gates=())
        self.assertEqual(self.controller.verify(claimed).status, "incomplete")
        with patch("prometheus_lab.lab_control.durable.request_vm_operation", side_effect=OSError("fault")):
            with self.assertRaises(OSError):
                self.controller.rehearse(request())
        self.assertEqual(self.controller.verify(claimed).status, "incomplete")
        store = DurableLedger.reopen(self.path, uid(900))
        store.recover_pending()
        self.assertEqual(self.controller.verify(claimed).status, "invalid")

    def test_verifier_is_read_only_and_flags_never_authorize(self):
        receipt = self.controller.rehearse(request())
        original = self.path.read_bytes()
        with patch.object(self.store, "assess") as assess:
            result = self.controller.verify(receipt.model_dump_json().encode())
            assess.assert_not_called()
        self.assertEqual(result.status, "consistent_rehearsal")
        self.assertFalse(result.confers_authority)
        self.assertFalse(result.valid_completion)
        self.assertEqual(self.path.read_bytes(), original)

    def test_strict_schema_mock_identity_and_bypass_objects(self):
        wire = request().model_dump(mode="json")
        variants = []
        for field, value in [("source", "authenticated-host"), ("watchdog_ready", "true"),
                             ("network_adapter_count", False), ("evidence_remaining_bytes", -1)]:
            item = json.loads(json.dumps(wire))
            item["readiness"][field] = value
            variants.append(item)
        for name in ("enabled", "authorized", "backend", "command"):
            item = dict(wire, **{name: True})
            variants.append(item)
        with patch.object(self.store, "snapshot") as snapshot:
            for item in variants:
                with self.subTest(item=item):
                    with self.assertRaises(ValueError):
                        self.controller.rehearse(json.dumps(item).encode())
            bad = request().model_copy(update={"readiness": request().readiness.model_copy(update={"watchdog_ready": "yes"})})
            with self.assertRaises(ValidationError):
                self.controller.rehearse(bad)
            snapshot.assert_not_called()

    def test_bounded_envelope_exact_limit_and_oversize_before_decoding(self):
        raw = request().model_dump_json().encode()
        exact = raw + b" " * (MAX_ENVELOPE_BYTES - len(raw))
        self.assertEqual(decode_envelope(ControllerRequest, exact), request())
        with patch("prometheus_lab.lab_control.controller.parse_contract") as parser:
            with self.assertRaisesRegex(ValueError, "ceiling"):
                self.controller.rehearse(exact + b" ")
            parser.assert_not_called()

    def test_malformed_duplicate_unknown_missing_and_nonfinite_json(self):
        raw = request().model_dump_json().encode()
        bad = [b"\xff", b"{", b"[]", b"null", b'{"schema_version":"controller-rehearsal-v1"}',
            raw.replace(b'"network_adapter_count":0', b'"network_adapter_count":0,"network_adapter_count":0'),
            raw.replace(b'"network_adapter_count":0', b'"network_adapter_count":NaN'),
            raw.replace(b'controller-rehearsal-v1', b'controller-rehearsal-v2'),
            b"[" * 2000 + b"]" * 2000]
        for item in bad:
            with self.subTest(item=item[:80]):
                with self.assertRaises(ValueError):
                    self.controller.rehearse(item)
                self.assertEqual(self.controller.verify(item).status, "invalid")
        self.assertEqual(len(self.rows()), 1)

    def test_request_is_immutable_and_no_os_or_launch_adapter_exists(self):
        item = request()
        with self.assertRaises(ValidationError):
            item.readiness.watchdog_ready = False
        with self.assertRaises(TypeError):
            CodeOnlyController(object())
        with self.assertRaises(TypeError):
            CodeOnlyController(self.store, backend=lambda: True)
        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen, patch(
                "socket.create_connection") as network, patch("os.system") as system:
            receipt = self.controller.rehearse(item)
            for action in (run, popen, network, system):
                action.assert_not_called()
        self.assertEqual(receipt.status, "blocked")

    def test_fabricated_ready_claims_cannot_confer_authority(self):
        receipt = self.controller.rehearse(request())
        self.assertFalse(receipt.confers_authority)
        self.assertFalse(receipt.valid_completion)
        self.assertFalse(self.controller.verify(receipt).confers_authority)
        self.assertFalse(self.controller.verify(receipt).valid_completion)

    def test_rebound_readiness_does_not_rebind_an_old_approval(self):
        changed = manifest().model_copy(update={"base_image_digest": "f" * 64})
        with patch("prometheus_lab.lab_control.durable.request_vm_operation") as boundary:
            receipt = self.controller.rehearse(request(item=changed, record=approval()))
            boundary.assert_not_called()
        self.assertEqual(receipt.status, "denied")
        self.assertEqual(self.store.snapshot().attempts[-1].reason, "manifest_mismatch")

    def test_missing_readiness_fields_stop_before_store_and_do_not_poison(self):
        wire = request().model_dump(mode="json")
        with patch.object(self.store, "snapshot") as snapshot:
            for field in MockReadiness.model_fields:
                with self.subTest(field=field):
                    changed = json.loads(json.dumps(wire))
                    del changed["readiness"][field]
                    with self.assertRaises(ValueError):
                        self.controller.rehearse(json.dumps(changed).encode())
            snapshot.assert_not_called()
        self.assertEqual(self.controller.rehearse(request()).status, "blocked")


if __name__ == "__main__":
    unittest.main()
