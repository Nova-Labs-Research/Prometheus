from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from pydantic import ValidationError
from typer.testing import CliRunner

from prometheus_lab.agent_core.approval import simulate_approval
from prometheus_lab.agent_core.dummy import DummyResearchAgent
from prometheus_lab.agent_core.loop import run_dummy_loop
from prometheus_lab.audit.models import AuditEvent
from prometheus_lab.audit.store import JsonlAuditStore
from prometheus_lab.config.settings import Settings
from prometheus_lab.constraints.runner import run_constraints
from prometheus_lab.proposals.models import CodeChangeProposal, FileChange
from prometheus_lab.sandbox.executor import SimulatedSandboxExecutor
from prometheus_lab.ui.cli import app


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "audit" / "events.jsonl"
        self.settings = Settings(audit_log=self.path)

    def events(self):
        return [AuditEvent.model_validate_json(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def proposal(self, paths=("experiments/target_repo/calculator.py",), content="pass\n"):
        return CodeChangeProposal(summary="Test", rationale="Test invariant gates", changes=tuple(
            FileChange(path=path, new_content=content) for path in paths
        ))

    def test_success_and_repeat_append_preserve_prior_bytes(self):
        self.assertEqual(run_dummy_loop(self.settings, report=lambda _: None), "simulated")
        before = self.path.read_bytes()
        expected = ["loop_started", "proposal_generated", "constraints_checked", "approval_decided",
                    "execution_started", "execution_completed", "loop_finished"]
        self.assertEqual([event.event_type for event in self.events()], expected)
        result = self.events()[-2].payload["result"]
        self.assertEqual(result["changed_files"], [])
        self.assertEqual(result["status"], "simulated")
        run_dummy_loop(self.settings, report=lambda _: None)
        self.assertTrue(self.path.read_bytes().startswith(before))
        self.assertEqual(len(self.events()), 14)
        self.assertEqual(len({event.run_id for event in self.events()}), 2)
        self.assertEqual(len({event.event_id for event in self.events()}), 14)

    def test_rejected_proposal_skips_approval_and_executor(self):
        agent, approve, executor = Mock(), Mock(), Mock()
        agent.propose.return_value = self.proposal(("src/prometheus_lab/constraints/runner.py",))
        self.assertEqual(run_dummy_loop(self.settings, agent=agent, approve=approve, executor=executor,
                                        report=lambda _: None), "rejected")
        approve.assert_not_called()
        executor.execute.assert_not_called()
        self.assertEqual(self.events()[-1].payload["outcome"], "rejected")

    def test_denial_skips_executor(self):
        executor = Mock()
        outcome = run_dummy_loop(self.settings, executor=executor,
                                 approve=lambda p: simulate_approval(p, False), report=lambda _: None)
        self.assertEqual(outcome, "denied")
        executor.execute.assert_not_called()
        self.assertEqual(self.events()[-1].payload["outcome"], "denied")

    def test_unsafe_and_ambiguous_paths_fail(self):
        paths = ["/experiments/target_repo/a.py", "C:/experiments/target_repo/a.py",
                 "experiments/target_repo/../a.py", "experiments/target_repo/./a.py",
                 "experiments/target_repo//a.py", "experiments/target_repo/a.py:stream",
                 "experiments\\target_repo\\a.py", "experiments/target_repo2/a.py",
                 "experiments/target_repo/a.txt", "experiments/target_repo/NUL.py",
                 "experiments/target_repo/COM1/a.py", "experiments/target_repo/a./b.py"]
        for path in paths:
            with self.subTest(path=path):
                self.assertFalse(run_constraints(self.proposal((path,)), self.settings)[0].passed)

    def test_duplicate_paths_are_case_insensitive(self):
        proposal = self.proposal(("experiments/target_repo/a.py", "experiments/target_repo/A.py"))
        self.assertFalse(run_constraints(proposal, self.settings)[1].passed)

    def test_limits_count_utf8_bytes_and_files(self):
        settings = Settings(max_content_bytes=3)
        self.assertFalse(run_constraints(self.proposal(content="éé"), settings)[2].passed)
        paths = tuple(f"experiments/target_repo/file{i}.py" for i in range(4))
        self.assertFalse(run_constraints(self.proposal(paths), self.settings)[2].passed)

    def test_constraint_exception_and_empty_set_fail_closed(self):
        def broken(proposal, settings):
            raise RuntimeError("deliberate check failure")
        result = run_constraints(self.proposal(), self.settings, checks=(broken,))[0]
        self.assertFalse(result.passed)
        self.assertIn("RuntimeError: deliberate check failure", result.detail)
        with self.assertRaises(ValueError):
            run_constraints(self.proposal(), self.settings, checks=())

    def test_schema_rejects_extra_fields_and_empty_changes(self):
        with self.assertRaises(ValidationError):
            CodeChangeProposal(summary="Test", rationale="Test", changes=())
        with self.assertRaises(ValidationError):
            FileChange(path="a.py", new_content="", shell_command="echo unsafe")

    def test_audit_failure_before_execution_stops_executor(self):
        real_store = JsonlAuditStore(self.path)
        store, executor = Mock(), Mock()
        def append(event):
            if event.event_type == "execution_started":
                raise OSError("deliberate audit failure")
            real_store.append(event)
        store.append.side_effect = append
        with self.assertRaisesRegex(OSError, "deliberate audit failure"):
            run_dummy_loop(self.settings, store=store, executor=executor, report=lambda _: None)
        executor.execute.assert_not_called()
        self.assertEqual(self.events()[-1].event_type, "loop_failed")

    def test_executor_failure_is_audited_and_propagated(self):
        executor = Mock()
        executor.execute.side_effect = RuntimeError("deliberate executor failure")
        with self.assertRaisesRegex(RuntimeError, "deliberate executor failure"):
            run_dummy_loop(self.settings, executor=executor, report=lambda _: None)
        self.assertEqual(self.events()[-1].event_type, "loop_failed")
        self.assertNotIn("execution_completed", [event.event_type for event in self.events()])

    def test_mismatched_approval_stops_execution(self):
        executor = Mock()
        with self.assertRaisesRegex(ValueError, "does not match"):
            run_dummy_loop(self.settings, executor=executor,
                           approve=lambda _: simulate_approval(DummyResearchAgent().propose()), report=lambda _: None)
        executor.execute.assert_not_called()
        self.assertEqual(self.events()[-1].event_type, "loop_failed")

    def test_direct_executor_rechecks_gates(self):
        executor = SimulatedSandboxExecutor()
        valid = self.proposal()
        for proposal, decision in (
            (valid, simulate_approval(valid, False)),
            (valid, simulate_approval(self.proposal(content="different"))),
            (self.proposal(("outside.py",)), simulate_approval(self.proposal(("outside.py",)))),
        ):
            with self.assertRaises(ValueError):
                executor.execute(proposal, decision, self.settings)
        invalid = self.proposal(("outside.py",))
        with self.assertRaisesRegex(ValueError, "preconditions"):
            executor.execute(invalid, simulate_approval(invalid), self.settings)

    def test_cli_success_denial_and_help(self):
        runner = CliRunner()
        result = runner.invoke(app, ["run-dummy-loop", "--audit-log", str(self.path)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("NOT a real security boundary", result.output)
        self.assertIn("SIMULATED", result.output)
        result = runner.invoke(app, ["run-dummy-loop", "--audit-log", str(self.path), "--deny-approval"])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(self.events()[-1].payload["outcome"], "denied")
        self.assertEqual(runner.invoke(app, ["--help"]).exit_code, 0)

    def test_cli_unwritable_audit_path(self):
        self.path.parent.mkdir(parents=True)
        self.path.mkdir()
        result = CliRunner().invoke(app, ["run-dummy-loop", "--audit-log", str(self.path)])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Loop failed", result.output)


if __name__ == "__main__":
    unittest.main()
