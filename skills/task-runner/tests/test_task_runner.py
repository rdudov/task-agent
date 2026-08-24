import importlib.util
import argparse
import contextlib
import io
import json
import subprocess
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
    def test_repeatable_repo_preserves_exact_input_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            first = Path(raw) / "first"
            second = Path(raw) / "second"
            first.mkdir()
            second.mkdir()
            self.assertEqual(
                task_runner.resolve_access_directories(
                    "codex", [str(second), str(first)]
                ),
                [second.resolve(), first.resolve()],
            )

    def test_repeatable_repo_refuses_missing_member(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            present = Path(raw) / "present"
            present.mkdir()
            missing = Path(raw) / "missing"
            with self.assertRaisesRegex(SystemExit, str(missing)):
                task_runner.resolve_access_directories(
                    "codex", [str(present), str(missing)]
                )

    def test_start_and_review_accept_caller_owned_foreground_supervision(self) -> None:
        for arguments in (
            ["task_runner.py", "start", "/tmp/001-task", "--foreground"],
            ["task_runner.py", "review", "/tmp/001-task", "--foreground"],
        ):
            with self.subTest(command=arguments[1]), mock.patch.object(
                sys, "argv", arguments
            ):
                self.assertTrue(task_runner.parse_args().foreground)

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

    def test_claude_read_only_notebook_uses_outer_boundary_tools(self) -> None:
        notebook = task_runner.repo_root() / "tasks" / "001-review"
        command = task_runner.claude_access_arguments(
            "read-only", {"needs_weaker_nested_sandbox": False}, notebook
        )
        tools = command[command.index("--tools") + 1]
        self.assertIn("Bash", tools)
        self.assertIn("Write", tools)
        self.assertIn("Edit", tools)
        self.assertIn("--dangerously-skip-permissions", command)
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertFalse(settings["sandbox"]["enabled"])

    def test_claude_read_only_command_has_outer_mount_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            notebook = Path(raw) / "001-review"
            notebook.mkdir()
            prompt = Path(raw) / "prompt.txt"
            prompt.write_text("review", encoding="utf-8")
            command = task_runner.build_command(
                "claude",
                prompt,
                task_runner.repo_root(),
                None,
                "read-only",
                notebook,
                [task_runner.repo_root()],
            )
        self.assertEqual(command[0], "bwrap")
        self.assertEqual(command[1:4], ["--ro-bind", "/", "/"])
        self.assertIn(str(notebook), command)
        self.assertIn("claude", command)
        self.assertLess(command.index("--"), command.index("claude"))

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

    def test_terminal_launch_failure_releases_launch_pending_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = Path(raw) / "tasks" / "001-example"
            (task / ".runner").mkdir(parents=True)
            (task / "task.md").write_text("---\nstatus: in_progress\n---\n", encoding="utf-8")
            (task / "plan.md").write_text("# Plan\n", encoding="utf-8")
            task_runner.write_json(
                task_runner.runner_meta_path(task),
                {"launch_pending": {"token": "old"}, "runner": "agent"},
            )
            args = argparse.Namespace(runner="agent", workflow="standard")
            with contextlib.redirect_stdout(io.StringIO()):
                task_runner.report_launch_failure(task, args, RuntimeError("no child"))

            meta = task_runner.read_json(task_runner.runner_meta_path(task))
            self.assertNotIn("launch_pending", meta)
            self.assertEqual(meta["outcome"], "failed_to_launch")
            task_runner.require_no_live_run(task)

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

    def test_pid_namespace_state_preserves_observer_vocabulary(self) -> None:
        with mock.patch.object(task_runner, "pid_namespace_identity", return_value="ns-current"):
            self.assertEqual(
                task_runner.runner_pid_namespace_state({"pid_namespace": "ns-current"}),
                "local",
            )
        with mock.patch.object(
            task_runner, "pid_namespace_identity", return_value="ns-current"
        ), mock.patch.object(
            task_runner,
            "observed_pid_namespace_identities",
            return_value=({"ns-foreign"}, True),
        ):
            self.assertEqual(
                task_runner.runner_pid_namespace_state({"pid_namespace": "ns-foreign"}),
                "foreign_live",
            )
        with mock.patch.object(
            task_runner, "pid_namespace_identity", return_value="ns-current"
        ), mock.patch.object(
            task_runner, "observed_pid_namespace_identities", return_value=(set(), True)
        ), mock.patch.object(task_runner, "host_systemd_scope_available", return_value=True):
            self.assertEqual(
                task_runner.runner_pid_namespace_state({"pid_namespace": "ns-gone"}),
                "recorded_namespace_absent",
            )

    def test_process_is_live_delegates_to_identity_aware_owner(self) -> None:
        with mock.patch.object(
            task_runner,
            "process_is_recorded_instance",
            return_value=(True, "identity_match"),
        ) as owner:
            self.assertTrue(task_runner.process_is_live(123, "identity"))
        owner.assert_called_once_with(123, "identity")

    def test_default_pipeline_cli_is_resolved_from_the_readme_virtualenv(self) -> None:
        expected = task_runner.repo_root() / ".venv" / "bin" / "dev-pipeline"
        self.assertTrue(expected.is_file())
        command = task_runner.build_dev_pipeline_command(
            "codex",
            Path("/tmp/001-example"),
            "danger-full-access",
            None,
            str(task_runner.repo_root()),
            None,
            "start",
            None,
            None,
            None,
        )
        self.assertEqual(command[command.index("--dev-pipeline-bin") + 1], str(expected))

    def test_pipeline_cli_resolution_covers_explicit_env_path_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / ".venv" / "bin" / "dev-pipeline"
            local.parent.mkdir(parents=True)
            local.write_text("#!/bin/sh\n", encoding="utf-8")
            local.chmod(0o755)
            explicit = root / "explicit"
            explicit.write_text("#!/bin/sh\n", encoding="utf-8")
            explicit.chmod(0o755)

            with mock.patch.object(task_runner, "repo_root", return_value=root):
                self.assertEqual(task_runner.resolve_dev_pipeline_bin(str(explicit)), str(explicit))
                with mock.patch.dict(
                    task_runner.os.environ,
                    {task_runner.DEV_PIPELINE_BIN_ENV: str(explicit)},
                    clear=False,
                ):
                    self.assertEqual(task_runner.resolve_dev_pipeline_bin(), str(explicit))
                with mock.patch.dict(task_runner.os.environ, {}, clear=True):
                    self.assertEqual(task_runner.resolve_dev_pipeline_bin(), str(local))
                    local.unlink()
                    with mock.patch.object(task_runner.shutil, "which", return_value="/bin/dev-pipeline"):
                        self.assertEqual(task_runner.resolve_dev_pipeline_bin(), "/bin/dev-pipeline")
                    with mock.patch.object(task_runner.shutil, "which", return_value=None):
                        with self.assertRaisesRegex(SystemExit, "not installed"):
                            task_runner.resolve_dev_pipeline_bin()

    def test_review_candidate_uses_the_shared_pipeline_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = Path(raw) / "task"
            repo = Path(raw) / "repo"
            task.mkdir()
            repo.mkdir()
            args = argparse.Namespace(
                task_dir=str(task), repo=str(repo), dev_pipeline_bin=None, model=None
            )
            with mock.patch.object(task_runner, "resolve_task_dir", return_value=task), \
                 mock.patch.object(task_runner, "ensure_task_contract"), \
                 mock.patch.object(
                     task_runner, "resolve_dev_pipeline_bin", side_effect=SystemExit("shared-resolver")
                 ) as resolver:
                with self.assertRaisesRegex(SystemExit, "shared-resolver"):
                    task_runner.cmd_review_candidate(args)
            resolver.assert_called_once_with(None)
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

    def test_dev_pipeline_command_passes_automatic_assurance_inputs(self) -> None:
        command = self._dev_pipeline_command(
            assurance_config="/tmp/example-task/dev-pipeline/assurance.json",
            review_packet="/tmp/example-task/dev-pipeline/review-packet.json",
        )
        self.assertEqual(
            command[command.index("--assurance-config") + 1],
            "/tmp/example-task/dev-pipeline/assurance.json",
        )
        self.assertEqual(
            command[command.index("--review-packet") + 1],
            "/tmp/example-task/dev-pipeline/review-packet.json",
        )

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

    def test_dev_pipeline_maps_the_agent_runner_to_the_cursor_owner_runtime(self) -> None:
        command = self._dev_pipeline_command(runner="agent")
        self.assertEqual(command[command.index("--owner-runtime") + 1], "cursor")

    def test_dev_pipeline_refuses_a_runner_the_core_cannot_drive(self) -> None:
        with self.assertRaises(SystemExit):
            self._dev_pipeline_command(runner="no-such-runner")

    def test_dev_pipeline_refuses_to_run_without_a_target_repository(self) -> None:
        with self.assertRaises(SystemExit):
            self._dev_pipeline_command(repo=None)

    def test_build_codex_command_uses_current_approval_flag_without_full_auto(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prompt_path = Path(raw) / "task-runner-prompt.txt"
            prompt_path.write_text("test prompt", encoding="utf-8")
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

    def test_standard_repo_is_granted_to_all_supported_runners(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw).resolve()
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            prompt = target / "prompt.txt"
            prompt.write_text("test", encoding="utf-8")
            for runner in ("codex", "claude", "agent"):
                directories, grant = task_runner.prepare_access_grant(
                    runner, "workspace-write", target
                )
                self.assertEqual(directories, [target])
                self.assertTrue(grant["grants_write"])
                self.assertEqual(
                    grant["writable_directories"],
                    [str(target), str(target / ".git")],
                )
                command_directories = task_runner.command_access_directories(
                    directories, grant
                )
                self.assertEqual(command_directories, [target, target / ".git"])
                command = task_runner.build_command(
                    runner,
                    prompt,
                    task_runner.repo_root(),
                    None,
                    "workspace-write",
                    access_directories=command_directories,
                )
                self.assertIn(str(target), command)
                self.assertIn(str(target / ".git"), command)
                self.assertEqual(command.count("--add-dir"), 2 if runner != "claude" else 1)
                if runner == "agent":
                    self.assertEqual(
                        command[command.index("--workspace") + 1],
                        str(task_runner.repo_root()),
                    )
                self.assertEqual(command[command.index("--add-dir") + 1], str(target))

            read_only_codex = task_runner.build_command(
                "codex",
                prompt,
                task_runner.repo_root(),
                None,
                "read-only",
                access_directories=[target],
            )
            self.assertNotIn("--add-dir", read_only_codex)

    def test_write_profile_refuses_a_non_git_or_non_root_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plain = root / "plain"
            plain.mkdir()
            with self.assertRaisesRegex(SystemExit, "exact Git worktree"):
                task_runner.prepare_access_grant("codex", "workspace-write", plain)

            repository = root / "repository"
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            child = repository / "child"
            child.mkdir()
            with self.assertRaisesRegex(SystemExit, "exact Git worktree root"):
                task_runner.prepare_access_grant("codex", "workspace-write", child)

    def test_nested_pid_namespace_cannot_replace_or_signal_host_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = Path(raw)
            (task / ".runner").mkdir()
            (task / "task.md").write_text("# Task\n", encoding="utf-8")
            (task / "plan.md").write_text("# Plan\n", encoding="utf-8")
            task_runner.write_json(
                task_runner.runner_meta_path(task),
                {
                    "runner": "codex",
                    "workflow": "standard",
                    "pid": 987654,
                    "process_identity": "host-child",
                    "pid_namespace": "pid:[host]",
                },
            )
            with mock.patch.object(
                task_runner, "pid_namespace_identity", return_value="pid:[nested]"
            ):
                with self.assertRaisesRegex(SystemExit, "different PID namespace"):
                    task_runner.require_no_live_run(task)
                with self.assertRaisesRegex(SystemExit, "different PID namespace"):
                    task_runner.cmd_stop(argparse.Namespace(task_dir=str(task)))
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    task_runner.cmd_status(argparse.Namespace(task_dir=str(task)))
            payload = json.loads(stdout.getvalue())
            self.assertIsNone(payload["runner"]["process_alive"])
            self.assertEqual(payload["runner"]["process_visibility"], "different_pid_namespace")


class SupportedPipelineStopTests(unittest.TestCase):
    def test_public_phase_marker_is_written_before_the_process_group_is_signalled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = Path(raw) / "1064-example"
            (task / ".runner").mkdir(parents=True)
            (task / "task.md").write_text("# Task\n", encoding="utf-8")
            (task / "plan.md").write_text("# Plan\n", encoding="utf-8")
            task_runner.write_json(
                task_runner.runner_meta_path(task),
                {"pid": 12345, "workflow": "dev-pipeline"},
            )
            order = []
            with (
                mock.patch.object(task_runner, "runner_pid_namespace_visible", return_value=True),
                mock.patch.object(
                    task_runner, "process_is_recorded_instance", return_value=(True, "identity")
                ),
                mock.patch.object(
                    task_runner,
                    "request_dev_pipeline_phase_stop",
                    side_effect=lambda *_: order.append("marker"),
                ),
                mock.patch.object(
                    task_runner.os,
                    "killpg",
                    side_effect=lambda *_: order.append("signal"),
                ),
                mock.patch.object(
                    task_runner,
                    "try_send_pipeline_stop_message",
                    return_value=(False, "not configured"),
                ),
            ):
                task_runner.cmd_stop(argparse.Namespace(task_dir=str(task)))
            self.assertEqual(order, ["marker", "signal"])

    def test_phase_marker_refusal_prevents_the_signal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = Path(raw) / "1064-example"
            (task / ".runner").mkdir(parents=True)
            (task / "task.md").write_text("# Task\n", encoding="utf-8")
            (task / "plan.md").write_text("# Plan\n", encoding="utf-8")
            task_runner.write_json(
                task_runner.runner_meta_path(task),
                {"pid": 12345, "workflow": "dev-pipeline"},
            )
            with (
                mock.patch.object(task_runner, "runner_pid_namespace_visible", return_value=True),
                mock.patch.object(
                    task_runner, "process_is_recorded_instance", return_value=(True, "identity")
                ),
                mock.patch.object(
                    task_runner,
                    "request_dev_pipeline_phase_stop",
                    side_effect=SystemExit("marker refused"),
                ),
                mock.patch.object(task_runner.os, "killpg") as killpg,
            ):
                with self.assertRaisesRegex(SystemExit, "marker refused"):
                    task_runner.cmd_stop(argparse.Namespace(task_dir=str(task)))
            killpg.assert_not_called()


class TaskNumberWidthTests(unittest.TestCase):
    """A task number is as many digits as `tasks_index.py` allocated, not three.

    A pattern pinned to three digits does not fail loudly on `1000-slug`: it
    never matches, so the number stops being a way to name a task and the task
    simply does not start.
    """

    def test_resolve_task_dir_follows_a_four_digit_number(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            tasks = repo / "tasks"
            tasks.mkdir()
            renamed = tasks / "1000-new-title"
            renamed.mkdir()

            with mock.patch.object(task_runner, "repo_root", return_value=repo):
                self.assertEqual(
                    task_runner.resolve_task_dir(str(tasks / "1000-old-title")),
                    renamed.resolve(),
                )

    def test_a_directory_named_after_a_date_carries_no_task_number(self) -> None:
        # The three digits also refused an archive named after its date, and
        # `tasks_index.py` refuses such a name explicitly for the same reason.
        self.assertIsNone(task_runner.task_number_prefix("2026-05-26-openclaw"))
        self.assertEqual(task_runner.task_number_prefix("1000-slug"), "1000")
        self.assertEqual(task_runner.task_number_prefix("123-slug"), "123")


if __name__ == "__main__":
    unittest.main()
