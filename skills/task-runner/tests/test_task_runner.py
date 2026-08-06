import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


def _load_task_runner_module():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    module_path = scripts_dir / "task_runner.py"
    sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location("task_runner_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


task_runner = _load_task_runner_module()


class TaskRunnerSandboxModeTests(unittest.TestCase):
    def test_codex_approval_mode_is_bound_to_the_recorded_constant(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prompt = Path(raw) / "prompt.txt"
            prompt.write_text("test", encoding="utf-8")
            command = task_runner.build_command(
                "codex", prompt, task_runner.repo_root(), None, "workspace-write"
            )
        index = command.index("--ask-for-approval")
        self.assertEqual(command[index + 1], task_runner.CODEX_APPROVAL_MODE)

    def test_codex_read_only_keeps_only_the_task_notebook_writable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            notebook = Path(raw) / "tasks" / "001-review"
            notebook.mkdir(parents=True)
            prompt = Path(raw) / "prompt.txt"
            prompt.write_text("review", encoding="utf-8")
            command = task_runner.build_command(
                "codex", prompt, task_runner.repo_root(), None, "read-only", notebook
            )
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(command[command.index("-C") + 1], str(notebook))
        self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", command)

    def test_claude_read_only_can_write_only_its_notebook_and_index(self) -> None:
        notebook = task_runner.repo_root() / "tasks" / "001-review"
        command = task_runner.claude_access_arguments(
            "read-only", {"needs_weaker_nested_sandbox": False}, notebook
        )
        self.assertIn("Bash", command[command.index("--tools") + 1])
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual(
            settings["sandbox"]["filesystem"]["allowWrite"],
            [str(notebook), str(task_runner.repo_root() / ".state")],
        )

    def test_second_live_run_is_refused_before_metadata_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = Path(raw) / "001-example"
            (task / ".runner").mkdir(parents=True)
            pid = __import__("os").getpid()
            task_runner.write_json(
                task_runner.runner_meta_path(task),
                {
                    "pid": pid,
                    "process_identity": task_runner.process_identity(pid),
                },
            )
            with self.assertRaises(SystemExit) as raised:
                task_runner.require_no_live_run(task)
        self.assertIn("Refusing to start a second run", str(raised.exception))

    def test_supervision_boundary_records_systemd_or_explicit_fallback(self) -> None:
        task = Path("/tmp/001-example")
        with mock.patch.object(task_runner, "host_systemd_scope_available", return_value=False):
            prefix, record = task_runner.watcher_supervision_boundary(task, "a" * 32)
        self.assertEqual(prefix, [])
        self.assertEqual(record["mode"], "process_session")
        with mock.patch.object(task_runner, "host_systemd_scope_available", return_value=True), \
             mock.patch.object(task_runner.shutil, "which", return_value="/usr/bin/systemd-run"):
            prefix, record = task_runner.watcher_supervision_boundary(task, "b" * 32)
        self.assertIn("--scope", prefix)
        self.assertEqual(record["durability"], "independent_cgroup")
    def test_resolve_sandbox_mode_keeps_standard_codex_default_implicit(self) -> None:
        self.assertIsNone(
            task_runner.resolve_sandbox_mode(
                runner="codex",
                workflow="standard",
                sandbox_mode=None,
            )
        )

    def test_resolve_sandbox_mode_preserves_explicit_value(self) -> None:
        self.assertEqual(
            task_runner.resolve_sandbox_mode(
                runner="codex",
                workflow="dev-pipeline",
                sandbox_mode="workspace-write",
            ),
            "workspace-write",
        )

    def test_build_workflow_command_has_no_command_for_the_standard_workflow(self) -> None:
        self.assertIsNone(
            task_runner.build_workflow_command(
                workflow="standard",
                runner="codex",
                task_dir=Path("/tmp/example-task"),
                sandbox_mode=None,
            )
        )

    def test_build_workflow_command_rejects_an_unknown_workflow(self) -> None:
        with self.assertRaises(SystemExit):
            task_runner.build_workflow_command(
                workflow="team-of-agents",
                runner="codex",
                task_dir=Path("/tmp/example-task"),
                sandbox_mode=None,
            )

    def test_dev_pipeline_command_passes_the_model_override(self) -> None:
        command = self._dev_pipeline_command(model="gpt-5.4")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.4")

    def test_resolve_sandbox_mode_defaults_dev_pipeline_to_danger_full_access(self) -> None:
        for runner in ("codex", "claude"):
            with self.subTest(runner=runner):
                self.assertEqual(
                    task_runner.resolve_sandbox_mode(
                        runner=runner,
                        workflow="dev-pipeline",
                        sandbox_mode=None,
                    ),
                    "danger-full-access",
                )

    def _dev_pipeline_command(self, **overrides) -> list[str]:
        arguments = dict(
            workflow="dev-pipeline",
            runner="codex",
            task_dir=Path("/tmp/example-task"),
            sandbox_mode="workspace-write",
            model=None,
            repo="/tmp/target-repo",
        )
        arguments.update(overrides)
        return task_runner.build_workflow_command(**arguments)

    def test_dev_pipeline_command_targets_the_adapter_with_the_owner_runtime(self) -> None:
        command = self._dev_pipeline_command(runner="claude")
        self.assertIn("dev_pipeline_adapter.py", command[1])
        self.assertIn("--repo", command)
        self.assertIn("/tmp/target-repo", command)
        self.assertEqual(command[command.index("--owner-runtime") + 1], "claude")
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(command[command.index("--operation") + 1], "start")

    def test_dev_pipeline_command_passes_retry_state(self) -> None:
        command = self._dev_pipeline_command(
            operation="retry",
            state_dir="/tmp/example-task/dev-pipeline/core-2",
            previous_state_dir="/tmp/example-task/dev-pipeline/core",
            retry_reason="intentional_replacement",
        )
        self.assertEqual(command[command.index("--operation") + 1], "retry")
        self.assertIn("--previous-state-dir", command)
        self.assertEqual(command[command.index("--retry-reason") + 1], "intentional_replacement")

    def test_dev_pipeline_refuses_a_runner_the_core_cannot_drive(self) -> None:
        with self.assertRaises(SystemExit):
            self._dev_pipeline_command(runner="agent")

    def test_dev_pipeline_refuses_to_run_without_a_target_repository(self) -> None:
        with self.assertRaises(SystemExit):
            self._dev_pipeline_command(repo=None)

    def test_build_codex_command_uses_current_approval_flag_without_full_auto(self) -> None:
        prompt_path = Path("/tmp/task-runner-prompt.txt")
        prompt_path.write_text("test prompt", encoding="utf-8")
        self.addCleanup(lambda: prompt_path.unlink(missing_ok=True))

        command = task_runner.build_command(
            runner="codex",
            prompt_path=prompt_path,
            root=Path("/tmp/repo"),
            model=None,
            sandbox_mode="danger-full-access",
        )

        self.assertEqual(command[0:4], ["codex", "--ask-for-approval", "never", "exec"])
        self.assertNotIn("--full-auto", command)
        self.assertIn("--sandbox", command)
        self.assertIn("danger-full-access", command)


if __name__ == "__main__":
    unittest.main()
