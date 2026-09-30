from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from rich.console import Console
from typer.testing import CliRunner

from prometheus_lab.agent_core.approval import simulate_approval
from prometheus_lab.agent_core.loop import run_dummy_loop
from prometheus_lab.audit.models import AuditEvent
from prometheus_lab.audit.store import JsonlAuditStore
from prometheus_lab.audit.verify import verify_audit
from prometheus_lab.config.settings import Settings
from prometheus_lab.ui.cli import app


class AuditContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "events.jsonl"
        self.settings = Settings(audit_log=self.path)
        self.store = JsonlAuditStore(self.path)
        self.runner = CliRunner()

    def generate(self, **kwargs):
        run_dummy_loop(self.settings, report=lambda _: None, **kwargs)
        return self.store.read_all()

    def invoke(self, *args):
        with patch("prometheus_lab.ui.cli.load_settings", return_value=self.settings), \
             patch("prometheus_lab.ui.cli.console", Console(width=240)):
            return self.runner.invoke(app, ["audit", *args])

    def write(self, events):
        self.path.write_text("".join(event.model_dump_json() + "\n" for event in events), encoding="utf-8")

    def mutate(self, events, index, **payload_changes):
        changed = deepcopy(events)
        changed[index].payload.update(payload_changes)
        return changed

    def test_complete_simulation_and_nondefault_limit_snapshot(self):
        events = self.generate()
        verdict = verify_audit(events)
        self.assertTrue(verdict.consistent)
        self.assertTrue(verdict.valid_completion)
        self.assertEqual(verdict.runs[0].status, "completed_simulation")
        self.settings = Settings(audit_log=self.root / "limited.jsonl", max_content_bytes=1)
        run_dummy_loop(self.settings, report=lambda _: None)
        limited = JsonlAuditStore(self.settings.audit_log).read_all()
        self.assertEqual(limited[0].payload["constraint_limits"]["max_content_bytes"], 1)
        self.assertEqual(verify_audit(limited).runs[0].status, "rejected")

    def test_hand_authored_transcript_has_independent_expected_verdicts(self):
        # No workflow, proposer, constraint runner or proposal.digest() produces
        # this fixture. The canonical JSON SHA-256 was calculated separately.
        digest = "17f0b9e06be3f421747fe48db915ba6d665db8d24db76cba7320e1708eb38adb"
        proposal = {"proposal_id": "11111111-1111-4111-8111-111111111111",
                    "created_at": "2026-09-30T00:00:00Z", "kind": "code_change",
                    "summary": "Fixture", "rationale": "Independent fixture",
                    "changes": [{"path": "experiments/target_repo/calculator.py", "new_content": "pass\n"}]}
        records = [
            ("loop_started", {"research_only": True, "approval_mode": "simulated", "sandbox_mode": "simulated",
                              "evidence_contract": "simulation-v1",
                              "constraint_limits": {"max_files": 1, "max_content_bytes": 5}}),
            ("proposal_generated", {"proposal": proposal, "digest": digest}),
            ("constraints_checked", {"results": [
                {"name": name, "passed": True, "detail": "Prepared check"}
                for name in ("target_scope", "unique_paths", "proposal_limits")]}),
            ("approval_decided", {"decision": {"proposal_digest": digest, "approved": True,
                                                "mode": "simulated", "reason": "Prepared decision"}}),
            ("execution_started", {"proposal_digest": digest, "mode": "simulated"}),
            ("execution_completed", {"result": {"status": "simulated", "changed_files": [],
                                                "planned_files": ["experiments/target_repo/calculator.py"],
                                                "detail": "Prepared result"}}),
            ("loop_finished", {"outcome": "simulated"}),
        ]
        events = [AuditEvent.model_validate_json(json.dumps({
            "schema_version": 1, "event_id": f"00000000-0000-4000-8000-{index:012d}",
            "run_id": "22222222-2222-4222-8222-222222222222", "timestamp": "2026-09-30T00:00:00Z",
            "event_type": kind, "payload": payload})) for index, (kind, payload) in enumerate(records)]
        self.assertTrue(verify_audit(events).valid_completion)
        self.assertEqual(verify_audit(events[:-1]).runs[0].status, "incomplete")
        denied = deepcopy(events[:4] + events[-1:])
        denied[3].payload["decision"]["approved"] = False
        denied[-1].payload["outcome"] = "denied"
        self.assertEqual(verify_audit(denied).runs[0].status, "denied")
        self.assertTrue(verify_audit(denied).consistent)
        self.assertFalse(verify_audit(denied).valid_completion)
        rejected = deepcopy(events[:3] + events[-1:])
        rejected[0].payload["constraint_limits"]["max_content_bytes"] = 4
        rejected[2].payload["results"][2]["passed"] = False
        rejected[-1].payload["outcome"] = "rejected"
        self.assertEqual(verify_audit(rejected).runs[0].status, "rejected")
        self.assertTrue(verify_audit(rejected).consistent)
        self.assertFalse(verify_audit(rejected).valid_completion)

    def test_empty_and_every_incomplete_prefix_never_complete(self):
        events = self.generate()
        self.assertFalse(verify_audit([]).consistent)
        for length in range(len(events)):
            with self.subTest(length=length):
                verdict = verify_audit(events[:length])
                self.assertFalse(verdict.valid_completion)
                if length:
                    self.assertEqual(verdict.runs[0].status, "incomplete")

    def test_missing_reordered_duplicate_and_post_terminal_events(self):
        events = self.generate()
        variants = [events[:index] + events[index + 1:] for index in range(len(events))]
        variants += [events[:2] + [events[3], events[2]] + events[4:], events + [events[-1]],
                     events + [AuditEvent.model_validate_json(AuditEvent(
                         run_id=events[0].run_id, event_type="loop_failed",
                         payload={"error_type": "OSError", "error": "late failure"}).model_dump_json())]]
        for index, records in enumerate(variants):
            with self.subTest(case=index):
                self.assertFalse(verify_audit(records).valid_completion)

    def test_payload_contradictions_never_complete(self):
        events = self.generate()
        decision = deepcopy(events[3].payload["decision"])
        result = deepcopy(events[5].payload["result"])
        proposal = deepcopy(events[1].payload["proposal"])
        cases = [
            (0, {"research_only": False}), (0, {"sandbox_mode": "real"}),
            (0, {"evidence_contract": "future"}),
            (0, {"constraint_limits": {"max_files": True, "max_content_bytes": 16384}}),
            (0, {"constraint_limits": {"max_files": 3, "max_content_bytes": 1}}),
            (1, {"digest": "bad"}), (1, {"proposal": {**proposal, "summary": "altered"}}),
            (2, {"results": []}),
            (2, {"results": [events[2].payload["results"][0]] * 3}),
            (3, {"decision": {**decision, "approved": False}}),
            (3, {"decision": {**decision, "approved": "true"}}),
            (3, {"decision": {**decision, "proposal_digest": "bad"}}),
            (3, {"decision": {**decision, "mode": "human"}}),
            (4, {"proposal_digest": "bad"}), (4, {"mode": "real"}),
            (5, {"result": {**result, "changed_files": result["planned_files"]}}),
            (5, {"result": {**result, "planned_files": []}}),
            (5, {"result": {**result, "status": "executed"}}),
            (6, {"outcome": "denied"}), (6, {"outcome": "rejected"}),
        ]
        for index, changes in cases:
            with self.subTest(event=index, changes=changes):
                verdict = verify_audit(self.mutate(events, index, **changes))
                self.assertFalse(verdict.valid_completion)
                self.assertEqual(verdict.runs[0].status, "invalid")

    def test_lying_constraint_pass_and_unapproved_execution_are_invalid(self):
        from prometheus_lab.proposals.models import CodeChangeProposal

        events = self.generate()
        changed = deepcopy(events)
        changed[1].payload["proposal"]["changes"][0]["path"] = "outside.py"
        proposal = CodeChangeProposal.model_validate(changed[1].payload["proposal"])
        digest = proposal.digest()
        changed[1].payload["digest"] = digest
        changed[3].payload["decision"]["proposal_digest"] = digest
        changed[4].payload["proposal_digest"] = digest
        changed[5].payload["result"]["planned_files"] = ["outside.py"]
        verdict = verify_audit(changed)
        self.assertEqual(verdict.runs[0].status, "invalid")
        self.assertIn("Constraint pass contradicts", verdict.runs[0].detail)
        for index in (2, 3):
            self.assertFalse(verify_audit(events[:index] + events[index + 1:]).valid_completion)

    def test_required_fields_cannot_be_reconstructed_from_defaults(self):
        events = self.generate()
        for index, field in ((1, "created_at"), (1, "proposal_id"), (1, "kind"),
                             (3, "mode"), (5, "changed_files"), (5, "status")):
            with self.subTest(event=index, field=field):
                changed = deepcopy(events)
                container = {1: "proposal", 3: "decision", 5: "result"}[index]
                del changed[index].payload[container][field]
                self.assertEqual(verify_audit(changed).runs[0].status, "invalid")
        for field in ("event_id", "schema_version", "timestamp"):
            with self.subTest(envelope=field):
                data = events[0].model_dump(mode="json")
                del data[field]
                changed = [AuditEvent.model_validate_json(json.dumps(data)), *events[1:]]
                self.assertEqual(verify_audit(changed).runs[0].status, "invalid")

    def test_legacy_unsupported_schema_and_naive_timestamps(self):
        events = self.generate()
        old = deepcopy(events)
        del old[0].payload["evidence_contract"]
        self.assertEqual(verify_audit(old).runs[0].status, "incomplete")
        for update in ({"schema_version": 2}, {"timestamp": events[0].timestamp.replace(tzinfo=None)}):
            changed = [events[0].model_copy(update=update), *events[1:]]
            self.assertEqual(verify_audit(changed).runs[0].status, "invalid")

    def test_multiple_interleaved_runs_and_global_duplicate_ids(self):
        first = self.generate()
        second = self.generate()[len(first):]
        interleaved = [event for pair in zip(first, second) for event in pair]
        self.assertTrue(verify_audit(interleaved).valid_completion)
        self.assertFalse(verify_audit(first + second[:-1]).valid_completion)
        duplicate = second[0].model_copy(update={"event_id": first[0].event_id})
        verdict = verify_audit(first + [duplicate] + second[1:])
        self.assertEqual([run.status for run in verdict.runs], ["invalid", "invalid"])

    def test_legacy_contradictions_are_invalid_not_merely_incomplete(self):
        events = self.generate()
        del events[0].payload["evidence_contract"]
        del events[0].payload["constraint_limits"]
        self.assertEqual(verify_audit(events).runs[0].status, "incomplete")
        late_failure = AuditEvent.model_validate_json(AuditEvent(
            run_id=events[0].run_id, event_type="loop_failed",
            payload={"error_type": "OSError", "error": "late failure"}).model_dump_json())
        variants = [events + [late_failure], self.mutate(events, -1, outcome="denied"),
                    self.mutate(events, 0, sandbox_mode="real"),
                    [*events[:-1], events[-1].model_copy(update={"schema_version": 2})]]
        for records in variants:
            with self.subTest(last_event=records[-1].event_type):
                self.assertEqual(verify_audit(records).runs[0].status, "invalid")
                self.write(records)
                before = self.path.read_bytes()
                result = self.invoke("verify")
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertIn("INVALID", result.output)
                self.assertEqual(self.path.read_bytes(), before)

    def test_reader_and_cli_reject_nonfinite_json_literals(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                raw = ('{"schema_version":1,"event_id":"11111111-1111-4111-8111-111111111111",'
                       '"timestamp":"2026-09-30T00:00:00Z",'
                       '"run_id":"22222222-2222-4222-8222-222222222222",'
                       '"event_type":"loop_failed","payload":{"error_type":"Error","error":'
                       + literal + '}}\n')
                self.path.write_text(raw, encoding="utf-8")
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "Invalid audit entry at line 1"):
                    self.store.read_all()
                for command in (("tail",), ("summary",), ("by-proposal", "missing"), ("verify",)):
                    result = self.invoke(*command)
                    self.assertEqual(result.exit_code, 1, result.output)
                    self.assertIn("Invalid audit entry at line 1", result.output)
                self.assertEqual(self.path.read_bytes(), before)

    def test_verification_is_read_only_and_does_not_authenticate(self):
        events = self.generate()
        before_bytes = self.path.read_bytes()
        before_events = [e.model_dump_json() for e in events]
        verify_audit(events)
        self.assertEqual(before_events, [e.model_dump_json() for e in events])
        self.assertEqual(before_bytes, self.path.read_bytes())
        # A fabricated identity with a consistent transcript is indistinguishable
        # from a genuine writer: deliberately document this assurance limit.
        invented_run = uuid4()
        fabricated = [e.model_copy(update={"run_id": invented_run, "event_id": uuid4()}) for e in events]
        self.assertTrue(verify_audit(fabricated).valid_completion)
        self.assertIn("authenticity are unverified", verify_audit(fabricated).runs[0].detail)

    def test_reader_rejects_syntax_ambiguity_and_invalid_envelopes_without_writes(self):
        events = self.generate()
        good = events[0].model_dump_json()
        invalid_inputs = ["{", "\n", good + "\n{" ,
                          good.replace('"schema_version":1', '"schema_version":"1"'),
                          good.replace('"schema_version":1', '"schema_version":1,"schema_version":2'),
                          good.replace('"research_only":true', '"research_only":false,"research_only":true')]
        for raw in invalid_inputs:
            with self.subTest(raw=raw[:60]):
                self.path.write_text(raw, encoding="utf-8")
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "Invalid audit entry at line"):
                    self.store.read_all()
                self.assertEqual(self.path.read_bytes(), before)

    def test_cli_missing_and_empty_logs(self):
        for command in (("tail",), ("summary",), ("by-proposal", "missing"), ("verify",)):
            result = self.invoke(*command)
            self.assertEqual(result.exit_code, 2 if command[0] == "verify" else 0, result.output)
            self.assertIn("No audit log found", result.output)
            self.assertFalse(self.path.exists())
        self.path.touch()
        for command, message in ((("tail",), "empty"), (("summary",), "Total entries: 0"),
                                 (("by-proposal", "missing"), "No audit entries found"),
                                 (("verify",), "no valid completion")):
            result = self.invoke(*command)
            self.assertIn(message, result.output)
            self.assertEqual(result.exit_code, 2 if command[0] == "verify" else 0, result.output)
        self.assertEqual(self.path.read_bytes(), b"")

    def test_cli_malformed_records_and_read_errors(self):
        self.path.write_text('{"broken":', encoding="utf-8")
        before = self.path.read_bytes()
        for command in (("tail",), ("summary",), ("by-proposal", "missing"), ("verify",)):
            result = self.invoke(*command)
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("Unable to read audit log", result.output)
            self.assertEqual(self.path.read_bytes(), before)
        with patch.object(JsonlAuditStore, "read_all", side_effect=PermissionError("prepared denial")):
            result = self.invoke("verify")
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("prepared denial", result.output)

    def test_cli_tail_summary_and_by_proposal_preserve_bytes(self):
        events = self.generate()
        before = self.path.read_bytes()
        proposal_id = events[1].payload["proposal"]["proposal_id"]
        result = self.invoke("tail", "--last", "2")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Last 2 audit entries", result.output)
        self.assertIn("execution_completed", result.output)
        self.assertIn("loop_finished", result.output)
        self.assertNotIn("loop_started", result.output)
        result = self.invoke("summary")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Total entries: 7", result.output)
        self.assertRegex(result.output, r"unknown\s+.*7")
        self.assertRegex(result.output, r"human\s+.*0")
        result = self.invoke("by-proposal", proposal_id)
        self.assertEqual(result.exit_code, 0, result.output)
        for event in events:
            self.assertIn(event.event_type, result.output)
        self.assertIn("no human reviewed", result.output)
        self.assertIn("No audit entries found", self.invoke("by-proposal", "missing").output)
        self.assertEqual(self.invoke("tail", "--last", "0").exit_code, 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_explicit_ids_ambiguous_legacy_runs_and_actors(self):
        run_id = uuid4()
        def event(kind, payload):
            return AuditEvent(run_id=run_id, event_type=kind, payload=payload)
        # The nested id and explicit id intentionally disagree. Explicit metadata
        # wins for display; ambiguous runs must not assign otherwise unlabelled events.
        events = [event("proposal_generated", {"proposal_id": "explicit-a", "proposal": {"proposal_id": "nested"},
                                                "actor": "agent", "marker": "explicit-marker"}),
                  event("proposal_generated", {"proposal": {"proposal_id": "other"}, "actor": "human"}),
                  event("loop_finished", {"outcome": "simulated", "actor": "  ", "marker": "unassigned-marker"})]
        self.write(events)
        before = self.path.read_bytes()
        result = self.invoke("by-proposal", "explicit-a")
        self.assertIn("explicit-marker", result.output)
        self.assertNotIn("unassigned-marker", result.output)
        self.assertIn("No audit entries found", self.invoke("by-proposal", "nested").output)
        result = self.invoke("summary")
        for actor in ("agent", "human", "unknown"):
            self.assertRegex(result.output, rf"{actor}\s+.*1")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.invoke("verify").exit_code, 2)

    def test_cli_verdicts_and_exit_codes(self):
        events = self.generate()
        before = self.path.read_bytes()
        result = self.invoke("verify")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("COMPLETED_SIMULATION", result.output)
        self.assertIn("authenticity and actual effects are UNVERIFIED", result.output)
        self.assertIn("All runs completed simulation: yes", result.output)
        self.assertEqual(self.path.read_bytes(), before)
        for records, status in ((events[:-1], "INCOMPLETE"),
                                (self.mutate(events, -1, outcome="denied"), "INVALID")):
            self.write(records)
            before = self.path.read_bytes()
            result = self.invoke("verify")
            self.assertEqual(result.exit_code, 2, result.output)
            self.assertIn(status, result.output)
            self.assertIn("All runs completed simulation: no", result.output)
            self.assertEqual(self.path.read_bytes(), before)
        self.path.write_bytes(b"")
        self.generate(approve=lambda p: simulate_approval(p, False))
        result = self.invoke("verify")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("DENIED", result.output)
        self.assertIn("All runs completed simulation: no", result.output)
        failure = AuditEvent.model_validate_json(AuditEvent(
            run_id=events[0].run_id, event_type="loop_failed",
            payload={"error_type": "RuntimeError", "error": "prepared failure"}).model_dump_json())
        self.write([events[0], failure])
        result = self.invoke("verify")
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("FAILED", result.output)
        self.assertIn("All runs completed simulation: no", result.output)
