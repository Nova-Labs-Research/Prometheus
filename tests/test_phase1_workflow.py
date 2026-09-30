from datetime import timedelta
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from prometheus_lab.agent_core.approval import simulate_approval
from prometheus_lab.agent_core.dummy import DummyResearchAgent
from prometheus_lab.agent_core.loop import run_dummy_loop
from prometheus_lab.audit.store import JsonlAuditStore
from prometheus_lab.audit.verify import verify_audit
from prometheus_lab.config.settings import Settings
from prometheus_lab.constraints.runner import run_constraints
from prometheus_lab.proposals.models import CodeChangeProposal, FileChange
from prometheus_lab.sandbox.executor import SimulatedSandboxExecutor


SUCCESS = ["loop_started", "proposal_generated", "constraints_checked", "approval_decided",
           "execution_started", "execution_completed", "loop_finished"]


class Phase1WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(audit_log=self.root / "events.jsonl")

    def events(self, settings=None):
        return JsonlAuditStore((settings or self.settings).audit_log).read_all()

    def proposal(self, paths=("experiments/target_repo/calculator.py",), content="pass\n"):
        return CodeChangeProposal(summary="Fixture", rationale="Simulation test", changes=tuple(
            FileChange(path=path, new_content=content) for path in paths))

    def test_exact_approval_rejection_and_denial_sequences(self):
        cases = (
            (self.proposal(), True, "simulated", SUCCESS),
            (self.proposal(("outside.py",)), True, "rejected", SUCCESS[:3] + ["loop_finished"]),
            (self.proposal(), False, "denied", SUCCESS[:4] + ["loop_finished"]),
        )
        for index, (proposal, approved, outcome, sequence) in enumerate(cases):
            with self.subTest(outcome=outcome):
                settings = Settings(audit_log=self.root / f"{index}.jsonl")
                agent = Mock(propose=Mock(return_value=proposal))
                approve = Mock(side_effect=lambda p: simulate_approval(p, approved))
                executor = Mock(wraps=SimulatedSandboxExecutor())
                self.assertEqual(run_dummy_loop(settings, agent=agent, approve=approve,
                                               executor=executor, report=lambda _: None), outcome)
                self.assertEqual([e.event_type for e in self.events(settings)], sequence)
                self.assertEqual(approve.call_count, int(outcome != "rejected"))
                self.assertEqual(executor.execute.call_count, int(outcome == "simulated"))
                verdict = verify_audit(self.events(settings))
                self.assertEqual(verdict.runs[0].status,
                                 "completed_simulation" if outcome == "simulated" else outcome)
                self.assertTrue(verdict.consistent)
                self.assertEqual(verdict.valid_completion, outcome == "simulated")

    def test_real_store_fsync_error_stops_proposal_and_retains_failure(self):
        agent, approve, executor = Mock(), Mock(), Mock()
        with patch("prometheus_lab.audit.store.os.fsync", side_effect=OSError("prepared fsync failure")):
            with self.assertRaisesRegex(OSError, "prepared fsync failure"):
                run_dummy_loop(self.settings, agent=agent, approve=approve, executor=executor,
                               report=lambda _: None)
        agent.propose.assert_not_called()
        approve.assert_not_called()
        executor.execute.assert_not_called()
        # Both writes reached the real file before fsync raised. This observes
        # retained bytes, not a guarantee of crash durability.
        events = self.events()
        self.assertEqual([event.event_type for event in events], ["loop_started", "loop_failed"])
        self.assertEqual(events[-1].payload["error"], "prepared fsync failure")
        self.assertFalse(verify_audit(events).valid_completion)

    def test_audit_failure_at_every_append_stops_later_stages(self):
        # Test both a failed write and a write that persisted before reporting an
        # error. Only the best-effort failure record may follow the failing append.
        for failure_index, failure_kind in enumerate(SUCCESS):
            for persist_first in (False, True):
                with self.subTest(event=failure_kind, persist_first=persist_first):
                    settings = Settings(audit_log=self.root / f"{failure_index}-{persist_first}.jsonl")
                    real_store = JsonlAuditStore(settings.audit_log)
                    attempted = []
                    def append(event):
                        attempted.append(event.event_type)
                        if event.event_type == failure_kind:
                            if persist_first:
                                real_store.append(event)
                            raise OSError("injected audit failure")
                        real_store.append(event)
                    agent = Mock(wraps=DummyResearchAgent())
                    approve = Mock(side_effect=simulate_approval)
                    executor = Mock(wraps=SimulatedSandboxExecutor())
                    with self.assertRaisesRegex(OSError, "injected audit failure"):
                        run_dummy_loop(settings, store=Mock(append=append), agent=agent,
                                       approve=approve, executor=executor, report=lambda _: None)
                    self.assertEqual(attempted, SUCCESS[:failure_index + 1] + ["loop_failed"])
                    self.assertEqual(agent.propose.call_count, int(failure_index >= 1))
                    self.assertEqual(approve.call_count, int(failure_index >= 3))
                    self.assertEqual(executor.execute.call_count, int(failure_index >= 5))
                    self.assertFalse(verify_audit(self.events(settings)).valid_completion)

    def test_unavailable_failure_writer_preserves_original_error(self):
        for fail_initial in (True, False):
            with self.subTest(fail_initial=fail_initial):
                attempted = []
                def append(event):
                    attempted.append(event.event_type)
                    if event.event_type == "loop_failed":
                        raise OSError("secondary failure writer error")
                    if fail_initial:
                        raise OSError("original start error")
                agent = Mock()
                agent.propose.side_effect = RuntimeError("original proposer error")
                approve, executor = Mock(), Mock()
                error = OSError if fail_initial else RuntimeError
                with self.assertRaisesRegex(error, "original"):
                    run_dummy_loop(self.settings, agent=agent, approve=approve, executor=executor,
                                   store=Mock(append=append), report=lambda _: None)
                self.assertEqual(attempted, ["loop_started", "loop_failed"])
                approve.assert_not_called()
                executor.execute.assert_not_called()

    def test_rejected_and_denied_terminal_write_failures_remain_failures(self):
        for outcome in ("rejected", "denied"):
            with self.subTest(outcome=outcome):
                settings = Settings(audit_log=self.root / f"terminal-{outcome}.jsonl")
                real_store = JsonlAuditStore(settings.audit_log)
                attempted = []
                def append(event):
                    attempted.append(event.event_type)
                    if event.event_type == "loop_finished":
                        raise OSError("terminal write failed")
                    real_store.append(event)
                proposal = self.proposal(("outside.py",)) if outcome == "rejected" else self.proposal()
                executor = Mock()
                with self.assertRaisesRegex(OSError, "terminal write failed"):
                    run_dummy_loop(settings, store=Mock(append=append),
                                   agent=Mock(propose=Mock(return_value=proposal)),
                                   approve=lambda p: simulate_approval(p, False), executor=executor,
                                   report=lambda _: None)
                executor.execute.assert_not_called()
                self.assertEqual(attempted[-2:], ["loop_finished", "loop_failed"])
                self.assertFalse(verify_audit(self.events(settings)).valid_completion)

    def test_component_errors_preserve_failure_and_skip_later_stages(self):
        for stage in ("proposal", "approval", "executor"):
            with self.subTest(stage=stage):
                settings = Settings(audit_log=self.root / f"{stage}.jsonl")
                agent = Mock(wraps=DummyResearchAgent())
                approve = Mock(side_effect=simulate_approval)
                executor = Mock(wraps=SimulatedSandboxExecutor())
                target = {"proposal": agent.propose, "approval": approve, "executor": executor.execute}[stage]
                target.side_effect = RuntimeError(f"injected {stage} failure")
                with self.assertRaisesRegex(RuntimeError, f"injected {stage}"):
                    run_dummy_loop(settings, agent=agent, approve=approve, executor=executor,
                                   report=lambda _: None)
                events = self.events(settings)
                self.assertEqual(events[-1].event_type, "loop_failed")
                self.assertEqual(events[-1].payload["error"], f"injected {stage} failure")
                self.assertNotIn("loop_finished", [event.event_type for event in events])
                if stage != "executor":
                    executor.execute.assert_not_called()
                if stage == "proposal":
                    approve.assert_not_called()
                self.assertEqual(verify_audit(events).runs[0].status, "failed")

    def test_constraint_exception_is_retained_and_rejects(self):
        def broken(proposal, settings):
            raise RuntimeError("prepared broken check")
        approve, executor = Mock(), Mock()
        with patch("prometheus_lab.agent_core.loop.run_constraints",
                   side_effect=lambda p, s: run_constraints(p, s, checks=(broken,))):
            outcome = run_dummy_loop(self.settings, approve=approve, executor=executor, report=lambda _: None)
        self.assertEqual(outcome, "rejected")
        self.assertIn("prepared broken check", self.events()[2].payload["results"][0]["detail"])
        approve.assert_not_called()
        executor.execute.assert_not_called()

    def test_reporting_failure_never_records_success_terminal(self):
        for approved in (True, False):
            settings = Settings(audit_log=self.root / f"report-{approved}.jsonl")
            def report(message):
                if message.startswith(("5.", "Approval denied")):
                    raise RuntimeError("reporting failed")
            with self.assertRaisesRegex(RuntimeError, "reporting failed"):
                run_dummy_loop(settings, approve=lambda p: simulate_approval(p, approved), report=report)
            self.assertEqual(self.events(settings)[-1].event_type, "loop_failed")
            self.assertNotIn("loop_finished", [e.event_type for e in self.events(settings)])

    def test_simulation_observed_no_target_writes(self):
        # Work only in a disposable fixture tree, never import or execute its code.
        previous_directory = Path.cwd()
        os.chdir(self.root)
        try:
            target = Path("experiments/target_repo")
            target.mkdir(parents=True)
            (target / "calculator.py").write_bytes(b"def add(a, b):\n    return a + b\n")
            (target / "sentinel.bin").write_bytes(b"\x00unchanged\xff")
            before = {str(p): p.read_bytes() for p in target.rglob("*") if p.is_file()}
            run_dummy_loop(Settings(), report=lambda _: None)
            after = {str(p): p.read_bytes() for p in target.rglob("*") if p.is_file()}
            self.assertEqual(after, before)
            self.assertEqual({str(p) for p in Path(".").rglob("*") if p.is_file()},
                             set(before) | {str(Path("artifacts/audit/events.jsonl"))})
        finally:
            os.chdir(previous_directory)

    def test_exact_file_and_utf8_boundaries(self):
        paths = tuple(f"experiments/target_repo/f{i}.py" for i in range(3))
        self.assertTrue(all(r.passed for r in run_constraints(self.proposal(paths), self.settings)))
        self.assertFalse(run_constraints(self.proposal(paths + ("experiments/target_repo/f3.py",)),
                                         self.settings)[2].passed)
        for content, passed in (("x" * 16384, True), ("x" * 16385, False),
                                ("\u00e9" * 8192, True), ("\u00e9" * 8192 + "x", False)):
            with self.subTest(byte_count=len(content.encode("utf-8"))):
                self.assertEqual(run_constraints(self.proposal(content=content), self.settings)[2].passed, passed)
        combined = self.proposal(paths[:2], content="\u00e9" * 4096)
        self.assertTrue(run_constraints(combined, self.settings)[2].passed)
        combined = combined.model_copy(update={"changes": (
            combined.changes[0], combined.changes[1].model_copy(update={"new_content": "\u00e9" * 4096 + "x"}))})
        self.assertFalse(run_constraints(combined, self.settings)[2].passed)

    def test_digest_binds_content_paths_and_metadata(self):
        original = self.proposal()
        decision = simulate_approval(original)
        updates = ({"summary": "changed"}, {"rationale": "changed"}, {"proposal_id": uuid4()},
                   {"created_at": original.created_at + timedelta(seconds=1)},
                   {"changes": (FileChange(path=original.changes[0].path, new_content="changed"),)},
                   {"changes": (FileChange(path="experiments/target_repo/other.py", new_content="pass\n"),)})
        executor = SimulatedSandboxExecutor()
        self.assertEqual(executor.execute(original, decision, self.settings).status, "simulated")
        for update in updates:
            with self.subTest(field=next(iter(update))):
                changed = original.model_copy(update=update)
                self.assertNotEqual(changed.digest(), original.digest())
                with self.assertRaisesRegex(ValueError, "does not match"):
                    executor.execute(changed, decision, self.settings)
        roundtrip = CodeChangeProposal.model_validate_json(original.model_dump_json())
        self.assertEqual(roundtrip.digest(), original.digest())
