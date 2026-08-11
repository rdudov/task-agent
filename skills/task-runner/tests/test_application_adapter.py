"""The versioned installation seam stays thin while the public engine owns lifecycle."""

import argparse
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import application_adapter  # noqa: E402
import dev_pipeline_adapter  # noqa: E402
import task_completion  # noqa: E402
import task_runner  # noqa: E402


class RecordingApplication:
    api_version = 1

    def __init__(self) -> None:
        self.events = []

    def launch_policy(self, request):
        return application_adapter.LaunchPolicyV1(request.requested_memory_limit_bytes)

    def standard_session(self, request):
        native = request.previous_state.get("native_session_id", "native-1")
        option = "--resume" if request.operation == "resume" else "--session-id"
        return application_adapter.StandardSessionV1(
            (option, native), {"native_session_id": native}
        )

    def deliver_event(self, event):
        self.events.append(event)
        return application_adapter.DeliveryResultV1(True, "recorded")

    def recover_transport(self, request):
        self.recovery = request

    def standard_run_finished(self, result):
        if result.log_path.exists() and "exact-reset:2026-08-12T00:00:00Z" in result.log_path.read_text(encoding="utf-8"):
            return application_adapter.StandardRunDispositionV1(
                "waiting_for_quota",
                "Native session preserved until exact quota reset",
                {"quota_wait": {"resets_at": "2026-08-12T00:00:00Z"}},
            )
        return None

    def completion_problems(self, request):
        return ["installation delivery receipt is absent"] if (request.task_dir / "refuse").exists() else []


class ApplicationAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = types.ModuleType("task_agent_test_application")
        self.module.adapter = RecordingApplication()
        sys.modules[self.module.__name__] = self.module
        self.spec = f"{self.module.__name__}:adapter"
        self.addCleanup(sys.modules.pop, self.module.__name__, None)

    def _task(self, root: Path) -> Path:
        task = root / "001-example"
        (task / ".runner").mkdir(parents=True)
        (task / "task.md").write_text(
            '---\nid: 1\nslug: "example"\ntitle: "x"\ndate: 2026-08-11\nstatus: "completed"\n---\n# x\n',
            encoding="utf-8",
        )
        (task / "plan.md").write_text("# Plan\n", encoding="utf-8")
        (task / "task_contract.json").write_text('{"version": 1}', encoding="utf-8")
        return task

    def _args(self, operation: str = "start"):
        return argparse.Namespace(
            application=self.spec,
            destination="opaque-installation-value",
            memory_limit="2G",
            runner="claude",
            workflow="standard",
            operation=operation,
        )

    def test_standard_native_session_state_survives_into_resume(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            started = task_runner.prepared_application_launch(self._args(), task)
            self.assertEqual(
                started["standard_session"]["command_arguments"],
                ["--session-id", "native-1"],
            )
            task_runner.write_json(
                task_runner.runner_meta_path(task),
                {"application": started},
            )
            resumed = task_runner.prepared_application_launch(self._args("resume"), task)
            self.assertEqual(
                resumed["standard_session"]["command_arguments"],
                ["--resume", "native-1"],
            )
            self.assertEqual(resumed["memory_limit_bytes"], 2 * 1024**3)

    def test_destination_is_redacted_from_persisted_command(self) -> None:
        command = ["python", "adapter.py", "--destination", "private-value"]
        redacted = task_runner.redact_sensitive_arguments(command)
        self.assertEqual(redacted[-1], "<application-destination>")
        self.assertNotIn("private-value", redacted)

    def test_application_completion_policy_can_refuse_the_shared_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            task_runner.write_json(
                task_runner.runner_meta_path(task),
                {"workflow": "standard", "application": {"spec": self.spec}},
            )
            (task / "refuse").write_text("x", encoding="utf-8")
            ready, reason = task_completion.completion_ready(task)
            self.assertFalse(ready)
            self.assertIn("installation delivery receipt", reason)

    def test_dev_pipeline_notable_event_reaches_registered_transport(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            (task / "status.json").write_text(
                '{"current_step": "owner started"}', encoding="utf-8"
            )
            projector = dev_pipeline_adapter.TaskArtifactProjector(
                task, application=self.spec, destination="opaque-installation-value"
            )
            projector.offer_to_delivery(
                {
                    "kind": "attempt_started",
                    "event_id": "event-1",
                    "payload": {},
                }
            )
            delivered = self.module.adapter.events[-1]
            self.assertEqual(delivered.kind, "attempt_started")
            self.assertEqual(delivered.destination, "opaque-installation-value")
            self.assertEqual(self.module.adapter.recovery.event_log_path, projector.event_path)

    def test_standard_exit_hook_can_preserve_exact_quota_wait(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            started = task_runner.prepared_application_launch(self._args(), task)
            task_runner.write_json(
                task_runner.runner_meta_path(task),
                {"workflow": "standard", "application": started},
            )
            task_runner.runner_log_path(task).write_text(
                "provider exact-reset:2026-08-12T00:00:00Z", encoding="utf-8"
            )
            task_runner.finalize_child_lifecycle(
                task,
                "standard",
                "claude",
                1,
                destination="opaque-installation-value",
            )
            status = json.loads(task_runner.status_path(task).read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "waiting_for_quota")
            self.assertEqual(
                status["quota_wait"]["resets_at"], "2026-08-12T00:00:00Z"
            )

    def test_secret_bearing_session_state_is_refused(self) -> None:
        with self.assertRaisesRegex(application_adapter.ApplicationAdapterError, "secret-bearing"):
            application_adapter.json_session_state(
                {"quota_wait": {"destination": "never persist this"}}
            )


if __name__ == "__main__":
    unittest.main()
