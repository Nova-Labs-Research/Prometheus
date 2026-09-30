from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from pydantic import ValidationError

from prometheus_lab.lab_control.boundary import VMOperationsUnavailable, request_vm_operation
from prometheus_lab.lab_control.contracts import (
    ClockReading, Context, Evidence, Limits, Manifest, MockApproval, MockIdentity,
    parse_contract,
)
from prometheus_lab.lab_control.state import LedgerUnavailable, SimulationLedger
from prometheus_lab.lab_control.verify import verify_evidence


T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def uid(value):
    return UUID(int=value)


def clock(seconds=0, *, milliseconds=None):
    return ClockReading(wall_time=T0 + timedelta(seconds=seconds),
                        monotonic_ms=seconds * 1000 if milliseconds is None else milliseconds)


def manifest():
    return Manifest(
        schema_version="phase2a-code-v1", run_id=uid(1), process_epoch=uid(2),
        design_digest="1" * 64, fixture_digest="2" * 64, base_image_digest="3" * 64,
        runtime_digest="4" * 64, input_image_digest="5" * 64,
        isolation_config_digest="6" * 64, policy_digest="7" * 64, vm_id=uid(3),
        limits=Limits(guest_memory_bytes=4294967296, fixture_memory_bytes=536870912,
                      vcpus=1, wall_seconds=120, output_bytes=4194304, approval_ttl_seconds=900),
        output_schema="fixture-output-v1", operation="validate_fixture_contract", execution="unavailable")


def approval(item=None, **changes):
    item = item or manifest()
    fields = dict(schema_version="phase2a-mock-approval-v1", approval_id=uid(4), run_id=item.run_id,
                  process_epoch=item.process_epoch, manifest_digest=item.digest(),
                  identity=MockIdentity(kind="mock", label="prepared-test-identity"), decision="approve",
                  issued_at=T0, issued_monotonic_ms=0, expires_at=T0 + timedelta(seconds=900))
    fields.update(changes)
    return MockApproval(**fields)


def ledger():
    return SimulationLedger(Context(process_epoch=uid(2), origin=clock()))


def blocked_bundle():
    instance = ledger()
    instance.assess(uid(10), manifest(), approval(), clock())
    return instance.snapshot()


class Phase2AContractTests(unittest.TestCase):
    def test_mock_approval_only_reaches_unavailable_boundary(self):
        instance = ledger()
        result = instance.assess(uid(10), manifest(), approval(), clock())
        self.assertEqual(result.status, "blocked")
        self.assertEqual([step.kind for step in result.attempt.steps],
                         ["assessment_started", "simulation_consumed", "execution_unavailable"])
        self.assertFalse(result.confers_authority)
        self.assertFalse(result.valid_completion)
        self.assertFalse(result.attempt.approval.confers_authority)
        checked = verify_evidence(instance.snapshot())
        self.assertEqual(checked.status, "consistent_simulation")
        self.assertFalse(checked.confers_authority)
        self.assertFalse(checked.valid_completion)

    def test_denial_never_calls_boundary(self):
        instance = ledger()
        with patch("prometheus_lab.lab_control.state.request_vm_operation") as boundary:
            result = instance.assess(uid(10), manifest(), approval(decision="deny"), clock())
        boundary.assert_not_called()
        self.assertEqual(result.status, "denied")
        self.assertEqual(result.attempt.steps[-1].reason, "mock_denial")
        self.assertTrue(verify_evidence(instance.snapshot()).consistent)

    def test_manifest_has_an_independent_canonical_digest(self):
        # Fixed canonical bytes authored independently of model_dump/digest.
        wire = ('{"base_image_digest":"' + '3' * 64 + '","design_digest":"' + '1' * 64
                + '","execution":"unavailable","fixture_digest":"' + '2' * 64
                + '","input_image_digest":"' + '5' * 64 + '","isolation_config_digest":"' + '6' * 64
                + '","limits":{"approval_ttl_seconds":900,"fixture_memory_bytes":536870912,'
                '"guest_memory_bytes":4294967296,"output_bytes":4194304,"vcpus":1,"wall_seconds":120},'
                '"operation":"validate_fixture_contract","output_schema":"fixture-output-v1",'
                '"policy_digest":"' + '7' * 64 + '","process_epoch":"00000000-0000-0000-0000-000000000002",'
                '"run_id":"00000000-0000-0000-0000-000000000001","runtime_digest":"' + '4' * 64
                + '","schema_version":"phase2a-code-v1","vm_id":"00000000-0000-0000-0000-000000000003"}')
        from hashlib import sha256
        expected = sha256(wire.encode("ascii")).hexdigest()
        self.assertEqual(manifest().digest(), expected)
        self.assertEqual(parse_contract(Manifest, wire), manifest())

    def test_every_changeable_manifest_field_is_bound(self):
        item = manifest()
        variants = {
            "run_id": str(uid(20)), "process_epoch": str(uid(21)), "vm_id": str(uid(22)),
            "output_schema": "different-output",
            **{name: "a" * 64 for name in ("design_digest", "fixture_digest", "base_image_digest",
                "runtime_digest", "input_image_digest", "isolation_config_digest", "policy_digest")},
        }
        for name, value in variants.items():
            with self.subTest(field=name):
                changed = item.model_dump(mode="json")
                changed[name] = value
                changed = parse_contract(Manifest, json.dumps(changed))
                self.assertNotEqual(changed.digest(), item.digest())
                result = ledger().assess(uid(10), changed, approval(item), clock())
                self.assertEqual(result.status, "denied")
                self.assertFalse(result.confers_authority)
        for name in ("vcpus", "wall_seconds", "output_bytes", "approval_ttl_seconds"):
            with self.subTest(limit=name):
                changed = item.model_dump(mode="json")
                changed["limits"][name] += 1
                changed = parse_contract(Manifest, json.dumps(changed))
                self.assertNotEqual(changed.digest(), item.digest())
                result = ledger().assess(uid(10), changed, approval(item), clock())
                self.assertEqual(result.attempt.steps[-1].reason, "manifest_mismatch")

    def test_fixed_schema_operation_execution_and_memory_split_cannot_be_changed(self):
        for name, value in (("schema_version", "phase2a-live-v1"), ("operation", "start_vm"),
                            ("execution", "enabled")):
            with self.subTest(field=name):
                raw = manifest().model_dump(mode="json")
                raw[name] = value
                with self.assertRaises(ValidationError):
                    parse_contract(Manifest, json.dumps(raw))
        for name in ("guest_memory_bytes", "fixture_memory_bytes"):
            with self.subTest(field=name):
                raw = manifest().model_dump(mode="json")
                raw["limits"][name] += 1
                with self.assertRaises(ValidationError):
                    parse_contract(Manifest, json.dumps(raw))

    def test_required_fields_extra_fields_and_strict_types(self):
        for model, value in ((Manifest, manifest()), (MockApproval, approval()), (Evidence, blocked_bundle())):
            raw = value.model_dump(mode="json")
            for name in raw:
                with self.subTest(model=model.__name__, missing=name):
                    missing = deepcopy(raw)
                    del missing[name]
                    with self.assertRaises(ValidationError):
                        parse_contract(model, json.dumps(missing))
            with self.subTest(model=model.__name__, extra=True):
                with self.assertRaises(ValidationError):
                    parse_contract(model, json.dumps({**raw, "authorized": True}))
        for value in (True, 1.0, "1", 0, -1):
            with self.subTest(limit=value):
                raw = manifest().model_dump(mode="json")
                raw["limits"]["vcpus"] = value
                with self.assertRaises(ValidationError):
                    parse_contract(Manifest, json.dumps(raw))

    def test_mock_identity_cannot_assert_os_or_external_authority(self):
        for fields in ({"kind": "windows", "label": "Danny"},
                       {"kind": "mock", "label": "Danny", "sid": "S-1-5-18"},
                       {"kind": "mock", "label": "Danny", "authenticated": True}):
            with self.subTest(fields=fields):
                with self.assertRaises(ValidationError):
                    MockIdentity(**fields)
        named = approval(identity=MockIdentity(kind="mock", label="Administrator"))
        self.assertFalse(named.confers_authority)
        self.assertFalse(ledger().assess(uid(10), manifest(), named, clock()).confers_authority)

    def test_frozen_nested_context_has_no_mutable_aliases(self):
        item = manifest()
        instance = ledger()
        bundle = blocked_bundle()
        for obj, name, replacement in ((item, "execution", "enabled"),
            (item.limits, "vcpus", 20), (instance.context, "process_epoch", uid(30)),
            (instance.context.origin, "monotonic_ms", 30), (bundle, "attempts", ()),
            (bundle.attempts[0].approval.identity, "label", "other")):
            with self.subTest(field=name):
                with self.assertRaises(ValidationError):
                    setattr(obj, name, replacement)
        old = instance.snapshot()
        instance.assess(uid(10), item, approval(), clock())
        self.assertEqual(old.attempts, ())
        self.assertIsInstance(instance.snapshot().attempts[0].steps, tuple)

    def test_model_copy_and_construct_bypasses_are_revalidated(self):
        malformed = manifest().model_copy(update={"execution": "enabled"})
        with self.assertRaises(ValidationError):
            malformed.digest()
        with self.assertRaises(ValidationError):
            ledger().assess(uid(10), malformed, approval(), clock())
        bad_limits = manifest().limits.model_copy(update={"vcpus": True})
        with self.assertRaises(ValidationError):
            ledger().assess(uid(10), manifest().model_copy(update={"limits": bad_limits}), approval(), clock())
        constructed = MockApproval.model_construct(**{**approval().model_dump(), "decision": "allow_real"})
        with self.assertRaises(ValidationError):
            ledger().assess(uid(10), manifest(), constructed, clock())
        bundle = blocked_bundle().model_copy(update={"recording_failed": "false"})
        self.assertEqual(verify_evidence(bundle).status, "invalid")

    def test_expiry_exact_boundary_and_ttl(self):
        for seconds, expected in ((899, "blocked"), (900, "denied"), (901, "denied")):
            with self.subTest(seconds=seconds):
                result = ledger().assess(uid(10), manifest(), approval(), clock(seconds))
                self.assertEqual(result.status, expected)
                if expected == "denied":
                    self.assertEqual(result.attempt.steps[-1].reason, "expired")
        result = ledger().assess(uid(10), manifest(), approval(expires_at=T0 + timedelta(seconds=901)), clock())
        self.assertEqual(result.attempt.steps[-1].reason, "ttl_exceeded")

    def test_stale_future_naive_and_nonpositive_approval_times(self):
        for offset, reason in ((-1, "stale_issuance"), (1, "future_issuance")):
            with self.subTest(offset=offset):
                result = ledger().assess(uid(10), manifest(), approval(issued_at=T0 + timedelta(seconds=offset)), clock())
                self.assertEqual(result.attempt.steps[-1].reason, reason)
        for changes in ({"issued_at": T0.replace(tzinfo=None)}, {"expires_at": T0},
                        {"expires_at": T0 - timedelta(seconds=1)}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    approval(**changes)

    def test_clocks_cannot_reverse_or_stall_past_expiry(self):
        instance = ledger()
        instance.assess(uid(10), manifest(), approval(), clock(10))
        for reading in (clock(9), clock(11, milliseconds=9999)):
            with self.subTest(reading=reading):
                result = instance.assess(uid(20 + len(instance.snapshot().attempts)), manifest(),
                                         approval(approval_id=uid(30 + len(instance.snapshot().attempts))), reading)
                self.assertEqual(result.attempt.steps[-1].reason, "clock_reversal")
        result = ledger().assess(uid(10), manifest(), approval(), clock(0, milliseconds=900000))
        self.assertEqual(result.attempt.steps[-1].reason, "expired")
        self.assertTrue(verify_evidence(instance.snapshot()).consistent)

    def test_epoch_run_and_digest_mismatches_deny_before_boundary(self):
        for changes, reason in (({"process_epoch": uid(99)}, "epoch_mismatch"),
                                ({"run_id": uid(99)}, "run_mismatch"),
                                ({"manifest_digest": "f" * 64}, "manifest_mismatch")):
            with self.subTest(reason=reason):
                with patch("prometheus_lab.lab_control.state.request_vm_operation") as boundary:
                    result = ledger().assess(uid(10), manifest(), approval(**changes), clock())
                boundary.assert_not_called()
                self.assertEqual(result.attempt.steps[-1].reason, reason)

    def test_monotonic_issuance_binds_ttl_despite_forward_wall_jump(self):
        shifted = approval(issued_at=T0 + timedelta(seconds=600), issued_monotonic_ms=0,
                           expires_at=T0 + timedelta(seconds=1500))
        for tick, expected in ((899999, "blocked"), (900000, "denied"), (900001, "denied")):
            with self.subTest(tick=tick):
                result = ledger().assess(uid(10), manifest(), shifted, clock(600, milliseconds=tick))
                self.assertEqual(result.status, expected)
                if expected == "denied":
                    self.assertEqual(result.attempt.steps[-1].reason, "expired")
        result = ledger().assess(uid(10), manifest(), approval(issued_monotonic_ms=1), clock())
        self.assertEqual(result.attempt.steps[-1].reason, "future_issuance")
        later_origin = SimulationLedger(Context(process_epoch=uid(2), origin=clock(0, milliseconds=1)))
        result = later_origin.assess(uid(10), manifest(), approval(), clock(0, milliseconds=1))
        self.assertEqual(result.attempt.steps[-1].reason, "stale_issuance")

    def test_replay_is_denied_even_if_record_content_changes(self):
        instance = ledger()
        instance.assess(uid(10), manifest(), approval(decision="deny"), clock())
        result = instance.assess(uid(11), manifest(), approval(), clock())
        self.assertEqual(result.attempt.steps[-1].reason, "replay")
        self.assertTrue(verify_evidence(instance.snapshot()).consistent)
        with self.assertRaisesRegex(ValueError, "Duplicate attempt"):
            instance.assess(uid(10), manifest(), approval(approval_id=uid(50)), clock())
        self.assertEqual(len(instance.snapshot().attempts), 2)

    def test_concurrent_attempts_consume_at_most_once(self):
        instance = ledger()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: instance.assess(uid(100 + i), manifest(), approval(), clock()), range(16)))
        self.assertEqual(sum(result.status == "blocked" for result in results), 1)
        self.assertEqual(sum(result.attempt.steps[-1].reason == "replay" for result in results), 15)
        self.assertTrue(verify_evidence(instance.snapshot()).consistent)

    def test_restart_protection_is_not_claimed_or_restored(self):
        first, replacement = ledger(), ledger()
        self.assertEqual(first.assess(uid(10), manifest(), approval(), clock()).status, "blocked")
        # Deliberate documented limitation: a fresh object with reused context
        # has no durable history. Both outcomes still confer zero authority.
        result = replacement.assess(uid(10), manifest(), approval(), clock())
        self.assertEqual(result.status, "blocked")
        self.assertFalse(result.confers_authority)
        new_epoch = SimulationLedger(Context(process_epoch=uid(99), origin=clock()))
        self.assertEqual(new_epoch.assess(uid(10), manifest(), approval(), clock()).attempt.steps[-1].reason,
                         "epoch_mismatch")

    def test_boundary_is_unconditional_and_does_not_touch_inputs_or_os(self):
        class Poison:
            def __getattribute__(self, name):
                raise AssertionError("Boundary inspected untrusted object")

        with patch("subprocess.Popen", side_effect=AssertionError("process launch")), \
             patch("os.system", side_effect=AssertionError("shell launch")), \
             patch("socket.socket", side_effect=AssertionError("socket")):
            for args, kwargs in (((Poison(),), {}), ((), {"enabled": True, "authorized": True}),
                                 (("provision",), {"backend": "hyper-v", "manifest": Poison()}),
                                 (("start",), {"force": True})):
                with self.subTest(operation=len(args)):
                    with self.assertRaises(VMOperationsUnavailable):
                        request_vm_operation(*args, **kwargs)

    def test_unexpected_boundary_return_poison_and_failure_evidence(self):
        instance = ledger()
        with patch("prometheus_lab.lab_control.state.request_vm_operation", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "unexpectedly returned"):
                instance.assess(uid(10), manifest(), approval(), clock())
        self.assertEqual(verify_evidence(instance.snapshot()).status, "failed")
        with self.assertRaises(LedgerUnavailable):
            instance.assess(uid(11), manifest(), approval(), clock())

    def test_every_append_fault_before_and_after_recording_stops(self):
        for index in range(3):
            for after in (False, True):
                with self.subTest(index=index, after=after):
                    instance = ledger()
                    original = instance._append
                    calls = []
                    def fault(step):
                        calls.append(step.kind)
                        if len(calls) - 1 == index:
                            if after:
                                original(step)
                            raise OSError("prepared append fault")
                        original(step)
                    with patch.object(instance, "_append", side_effect=fault), \
                         patch("prometheus_lab.lab_control.state.request_vm_operation",
                               side_effect=VMOperationsUnavailable("disabled")) as boundary:
                        with self.assertRaisesRegex(OSError, "prepared append fault"):
                            instance.assess(uid(10), manifest(), approval(), clock())
                    self.assertEqual(boundary.call_count, int(index == 2))
                    self.assertTrue(instance.snapshot().recording_failed)
                    expected = "invalid" if index == 2 and after else "failed"
                    self.assertEqual(verify_evidence(instance.snapshot()).status, expected)
                    self.assertEqual(calls[-1], "attempt_failed")
                    with self.assertRaises(LedgerUnavailable):
                        instance.assess(uid(11), manifest(), approval(approval_id=uid(40)), clock())

    def test_failure_writer_unavailable_preserves_original_and_no_normal_verdict(self):
        instance = ledger()
        with patch.object(instance, "_append", side_effect=OSError("original failure")):
            with self.assertRaisesRegex(OSError, "original failure"):
                instance.assess(uid(10), manifest(), approval(), clock())
        bundle = instance.snapshot()
        self.assertTrue(bundle.recording_failed)
        self.assertEqual(bundle.attempts[0].steps, ())
        self.assertEqual(verify_evidence(bundle).status, "failed")

    def test_denial_terminal_append_fault_is_not_consistent(self):
        instance = ledger()
        original = instance._append
        def fault(step):
            original(step)
            if step.kind == "approval_denied":
                raise OSError("terminal denial write")
        with patch.object(instance, "_append", side_effect=fault):
            with self.assertRaises(OSError):
                instance.assess(uid(10), manifest(), approval(decision="deny"), clock())
        self.assertEqual(verify_evidence(instance.snapshot()).status, "invalid")

    def test_empty_and_all_partial_prefixes_are_incomplete(self):
        self.assertEqual(verify_evidence(ledger().snapshot()).status, "incomplete")
        raw = blocked_bundle().model_dump(mode="json")
        for size in range(3):
            with self.subTest(size=size):
                changed = deepcopy(raw)
                changed["attempts"][0]["steps"] = raw["attempts"][0]["steps"][:size]
                verdict = verify_evidence(json.dumps(changed))
                self.assertEqual(verdict.status, "incomplete")
                self.assertFalse(verdict.valid_completion)

    def test_contradictory_duplicate_missing_and_out_of_order_steps(self):
        raw = blocked_bundle().model_dump(mode="json")
        original = raw["attempts"][0]["steps"]
        variants = (original[::-1], original + original[-1:], original[1:],
                    [original[0], original[2]],
                    [original[0], {"kind": "approval_denied", "reason": "mock_denial"}],
                    original + [{"kind": "attempt_failed", "reason": "internal_failure"}])
        for steps in variants:
            with self.subTest(steps=steps):
                changed = deepcopy(raw)
                changed["attempts"][0]["steps"] = steps
                self.assertEqual(verify_evidence(json.dumps(changed)).status, "invalid")
        raw["attempts"].append(deepcopy(raw["attempts"][0]))
        self.assertEqual(verify_evidence(json.dumps(raw)).status, "invalid")

    def test_changed_evidence_context_and_replay_claims_are_invalid(self):
        raw = blocked_bundle().model_dump(mode="json")
        for mutate in (
            lambda x: x["context"].update(process_epoch=str(uid(50))),
            lambda x: x["attempts"][0]["approval"].update(manifest_digest="f" * 64),
            lambda x: x["attempts"][0]["manifest"]["limits"].update(wall_seconds=121),
            lambda x: x["attempts"][0]["clock"].update(wall_time="2026-09-30T12:15:00Z", monotonic_ms=900000),
        ):
            with self.subTest(mutation=mutate):
                changed = deepcopy(raw)
                mutate(changed)
                self.assertEqual(verify_evidence(json.dumps(changed)).status, "invalid")
        replay = deepcopy(raw["attempts"][0])
        replay["attempt_id"] = str(uid(11))
        raw["attempts"].append(replay)
        self.assertEqual(verify_evidence(json.dumps(raw)).status, "invalid")

    def test_no_attempt_after_failed_or_incomplete_attempt(self):
        raw = blocked_bundle().model_dump(mode="json")
        for terminal in ([], [{"kind": "attempt_failed", "reason": "internal_failure"}]):
            with self.subTest(terminal=terminal):
                changed = deepcopy(raw)
                second = deepcopy(changed["attempts"][0])
                second["attempt_id"] = str(uid(11))
                changed["attempts"][0]["steps"] = terminal
                changed["attempts"].append(second)
                changed["recording_failed"] = bool(terminal)
                self.assertEqual(verify_evidence(json.dumps(changed)).status, "invalid")

    def test_malformed_json_nested_duplicates_nonfinite_and_wrong_types(self):
        for text in ("{", "null", "[]", '{"schema_version":1,"schema_version":2}',
                     '{"nested":{"x":1,"x":2}}', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}'):
            with self.subTest(text=text):
                self.assertEqual(verify_evidence(text).status, "invalid")
        raw = blocked_bundle().model_dump(mode="json")
        for value in (0, "false", None):
            with self.subTest(value=value):
                changed = deepcopy(raw)
                changed["recording_failed"] = value
                self.assertEqual(verify_evidence(json.dumps(changed)).status, "invalid")

    def test_verifier_is_pure_and_cannot_authenticate_fabricated_evidence(self):
        raw = blocked_bundle().model_dump(mode="json")
        # This altered identity is intentionally unauthenticated but consistent.
        raw["attempts"][0]["approval"]["identity"]["label"] = "fabricated approver"
        wire = json.dumps(raw)
        before = deepcopy(raw)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(wire, encoding="utf-8")
            original = path.read_bytes()
            with patch("builtins.open", side_effect=AssertionError("verifier IO")), \
                 patch("subprocess.Popen", side_effect=AssertionError("verifier process")):
                verdict = verify_evidence(wire)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])
        self.assertEqual(raw, before)
        self.assertTrue(verdict.consistent)
        self.assertFalse(verdict.confers_authority)
        self.assertFalse(verdict.valid_completion)


if __name__ == "__main__":
    unittest.main()
