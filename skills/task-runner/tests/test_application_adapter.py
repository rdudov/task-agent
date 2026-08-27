"""The versioned installation seam stays thin while the public engine owns lifecycle."""

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


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
        self.launches = []

    def launch_policy(self, request):
        self.launches.append(request)
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

    def test_only_read_only_review_reaches_application_as_non_recursive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            args = self._args()
            args.review_kind = "technical"
            task_runner.prepared_application_launch(
                args,
                task,
                access_profile={"sandbox_mode": "read-only", "grants_write": False},
            )
        self.assertEqual(self.module.adapter.launches[-1].role, "reviewer")

    def test_write_capable_review_label_reaches_application_as_author(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            args = self._args()
            args.review_kind = "technical"
            task_runner.prepared_application_launch(
                args,
                task,
                access_profile={"sandbox_mode": "workspace-write", "grants_write": True},
            )
        self.assertEqual(self.module.adapter.launches[-1].role, "author")

    def test_standard_watcher_receives_and_reparses_application_context(self) -> None:
        args = self._args("resume")
        options = task_runner.watcher_options(args)
        argv = [
            "task-agent",
            "_run-child",
            "/tmp/001-example",
            "--runner",
            "claude",
            "--workflow",
            "standard",
            "--launch-token",
            "launch-1",
        ]
        for name, value in options.items():
            argv.extend([f"--{name.replace('_', '-')}", str(value)])
        with mock.patch.object(sys, "argv", argv):
            reparsed = task_runner.parse_args()
        self.assertEqual(reparsed.application, self.spec)
        self.assertEqual(reparsed.destination, "opaque-installation-value")
        self.assertEqual(reparsed.operation, "resume")

    def test_real_start_builds_watcher_command_with_application_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            args = self._args("resume")
            args.task_dir = str(task)
            args.model = None
            args.sandbox_mode = "danger-full-access"
            args.repo = None
            args.dry_run = False
            captured = {}

            class WatcherProcess:
                def __init__(self, command, **kwargs):
                    captured["command"] = command
                    identity = task_runner.process_identity(os.getpid())
                    self.stdout = io.StringIO(
                        json.dumps(
                            {
                                "ok": True,
                                "pid": os.getpid(),
                                "watcher_pid": os.getpid(),
                                "child_started_at": task_runner.utc_now(),
                                "process_identity": identity,
                                "watcher_process_identity": identity,
                            }
                        )
                        + "\n"
                    )

            with mock.patch.object(
                task_runner, "watcher_supervision_boundary", return_value=([], {})
            ), mock.patch.object(
                task_runner.subprocess, "Popen", WatcherProcess
            ), mock.patch.object(
                task_runner.review_admission, "reviewer_available", return_value=True
            ), contextlib.redirect_stdout(io.StringIO()):
                task_runner.cmd_start(args)

            command = captured["command"]
            self.assertEqual(command[command.index("--application") + 1], self.spec)
            self.assertEqual(
                command[command.index("--destination") + 1],
                "opaque-installation-value",
            )
            self.assertEqual(command[command.index("--operation") + 1], "resume")

    def test_standard_watcher_reuses_exact_parent_session_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            prepared = task_runner.prepared_application_launch(self._args("resume"), task)
            task_runner.write_json(
                task_runner.runner_meta_path(task),
                {
                    "launch_pending": {"token": "launch-1"},
                    "destination_binding": hashlib.sha256(
                        b"opaque-installation-value"
                    ).hexdigest()[:12],
                    "application": prepared,
                },
            )
            watcher_args = self._args("resume")
            watcher_args.launch_token = "launch-1"
            reused = task_runner.prepared_application_launch(watcher_args, task)
            self.assertEqual(reused, prepared)

    def test_standard_watcher_refuses_missing_parent_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            prepared = task_runner.prepared_application_launch(self._args(), task)
            task_runner.write_json(
                task_runner.runner_meta_path(task),
                {
                    "launch_pending": {"token": "launch-1"},
                    "destination_binding": hashlib.sha256(
                        b"opaque-installation-value"
                    ).hexdigest()[:12],
                    "application": prepared,
                },
            )
            watcher_args = self._args()
            watcher_args.launch_token = "launch-1"
            watcher_args.application = None
            watcher_args.destination = None
            with self.assertRaisesRegex(
                application_adapter.ApplicationAdapterError,
                "registration changed.*destination binding changed",
            ):
                task_runner.prepared_application_launch(watcher_args, task)

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

    def test_completion_metadata_uses_the_canonical_tasks_index_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            task = root / "tasks" / "001-example"
            task.mkdir(parents=True)
            (task / "task.md").write_text(
                "---\nid: 1\nslug: example\ntitle: Example\n"
                "date: 2026-08-11\nstatus: in_progress\nprojects: []\ntrips: []\n"
                "---\n# Example\n",
                encoding="utf-8",
            )
            task_completion.complete_task_metadata(task)
            self.assertEqual(task_completion.task_status(task), "completed")
            self.assertTrue((root / ".state" / "tasks-index.db").is_file())

    def test_installed_completion_metadata_finds_the_interpreter_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            module = (
                root
                / "lib"
                / "python3.12"
                / "site-packages"
                / "task_agent"
                / "task_completion.py"
            )
            entrypoint = root / "bin" / "task-agent-tasks-index"
            module.parent.mkdir(parents=True)
            entrypoint.parent.mkdir(parents=True)
            module.touch()
            entrypoint.touch()

            resolved = task_completion.resolve_tasks_index_path(
                module, root / "bin" / "python"
            )

            self.assertEqual(resolved, entrypoint)

    def test_optional_completion_preparation_is_additive_to_v1(self) -> None:
        adapter = application_adapter.DefaultApplicationV1()
        self.assertEqual(
            application_adapter.completion_preparation_evidence_ids(adapter), ()
        )

    def test_completion_preparation_declaration_requires_a_method(self) -> None:
        adapter = application_adapter.DefaultApplicationV1()
        adapter.completion_preparation_evidence_ids = ("delivery",)
        with self.assertRaisesRegex(
            application_adapter.ApplicationAdapterError, "prepare_completion"
        ):
            application_adapter.completion_preparation_evidence_ids(adapter)

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
            self.assertIsNone(self.module.adapter.recovery.active_attempt_id)

    def test_transport_recovery_is_bound_to_the_active_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            projector = dev_pipeline_adapter.TaskArtifactProjector(
                task, application=self.spec, destination="opaque-installation-value"
            )
            projector.consume(
                {
                    "schema_version": "1.0",
                    "kind": "attempt_started",
                    "event_id": "event-1",
                    "task_ref": task.name,
                    "attempt_id": "attempt-current",
                    "run_id": "run-current",
                    "sequence": 1,
                    "timestamp": "2026-08-11T16:00:00+00:00",
                    "payload": {
                        "attempt_origin": "fresh",
                        "repository": str(task.parent),
                    },
                }
            )

            self.module.adapter.events.clear()
            dev_pipeline_adapter.TaskArtifactProjector(
                task, application=self.spec, destination="opaque-installation-value"
            )

            self.assertEqual(
                self.module.adapter.recovery.active_attempt_id, "attempt-current"
            )

    def test_review_and_rework_transitions_reach_registered_transport(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            (task / "status.json").write_text(
                '{"current_step": "review transition"}', encoding="utf-8"
            )
            projector = dev_pipeline_adapter.TaskArtifactProjector(
                task, application=self.spec, destination="opaque-installation-value"
            )
            for index, (kind, payload) in enumerate(
                (
                    (
                        "review_started",
                        {
                            "strategy": "cross_provider",
                            "review_provider": "claude",
                            "artifact_digest": "sha256:abc",
                            "author_session_id": "author-1",
                        },
                    ),
                    (
                        "review_rework_required",
                        {
                            "strategy": "cross_provider",
                            "review_provider": "claude",
                            "artifact_digest": "sha256:abc",
                        },
                    ),
                ),
                start=1,
            ):
                projector.offer_to_delivery(
                    {
                        "kind": kind,
                        "event_id": f"event-{index}",
                        "payload": payload,
                    }
                )
            self.assertEqual(
                [item.kind for item in self.module.adapter.events[-2:]],
                ["review_started", "review_rework_required"],
            )

    def test_dev_pipeline_quota_wait_survives_adapter_process_exit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            (task / "status.json").write_text(
                '{"state": "waiting", "current_step": "waiting for quota"}',
                encoding="utf-8",
            )
            task_runner.finalize_child_lifecycle(
                task,
                "dev-pipeline",
                "claude",
                0,
                destination="opaque-installation-value",
            )
            status = json.loads(task_runner.status_path(task).read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "waiting")
            self.assertEqual(status["current_step"], "waiting for quota")

    def test_child_written_completed_state_still_runs_engine_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            task_text = (task / "task.md").read_text(encoding="utf-8")
            (task / "task.md").write_text(
                task_text.replace('status: "completed"', 'status: "planned"'),
                encoding="utf-8",
            )
            task_runner.write_json(
                task_runner.runner_meta_path(task),
                {"workflow": "standard", "application": {"spec": self.spec}},
            )
            task_runner.write_json(
                task_runner.status_path(task),
                {"state": "completed", "current_step": "child says completed"},
            )
            task_runner.finalize_child_lifecycle(
                task,
                "standard",
                "claude",
                0,
                destination="opaque-installation-value",
            )
            status = json.loads(task_runner.status_path(task).read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "blocked")
            self.assertIn("frontmatter status", status["current_step"])

    def test_standard_exit_hook_can_preserve_exact_quota_wait(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw) / "tasks")
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
            self.assertEqual(task_completion.task_status(task), "blocked")
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
