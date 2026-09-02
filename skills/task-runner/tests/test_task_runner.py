import importlib.util
import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
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

# Loading the module above put the scripts directory on `sys.path`; the gate
# owner vocabulary belongs to the completion owner, not to this launcher.
from task_completion import USER_OR_EXTERNAL_COMPLETION_GATE  # noqa: E402
import application_adapter  # noqa: E402


class SandboxedChildAccessGrantTests(unittest.TestCase):
    """Every sandboxed child is launched asking for network and temporary space.

    These are the grants a run needs to finish its own work rather than extra
    privileges: without them a live gate, a `git push` or a `pytest` collection
    fails on the sandbox and reports itself as an outage or as unfinished work.

    Each test asserts the launch setting, and each name says so. Whether the
    setting is honoured is a different question, answerable only by a real
    child: task 1291 measured it from one, reaching `github.com` and pushing
    over the grants these tests keep in the command.
    """

    @staticmethod
    def _codex_command(sandbox_mode: str, notebook: Path | None = None) -> list[str]:
        return task_runner.build_command(
            "codex",
            "work",
            task_runner.repo_root(),
            None,
            sandbox_mode,
            notebook,
        )

    def test_codex_workspace_write_child_is_launched_with_network_access(self) -> None:
        self.assertIn(
            "sandbox_workspace_write.network_access=true",
            self._codex_command("workspace-write"),
        )

    def test_codex_read_only_reviewer_is_launched_with_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            notebook = Path(raw) / "tasks" / "001-review"
            notebook.mkdir(parents=True)
            command = self._codex_command("read-only", notebook)
        self.assertIn("sandbox_workspace_write.network_access=true", command)

    def test_codex_read_only_reviewer_is_launched_without_excluding_slash_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            notebook = Path(raw) / "tasks" / "001-review"
            notebook.mkdir(parents=True)
            command = self._codex_command("read-only", notebook)
        self.assertNotIn("sandbox_workspace_write.exclude_slash_tmp=true", command)

    def test_claude_restricted_children_are_launched_with_the_network_allow_list(self) -> None:
        for sandbox_mode in ("workspace-write", "read-only"):
            with self.subTest(sandbox_mode=sandbox_mode):
                command = task_runner.claude_access_arguments(
                    sandbox_mode, {"needs_weaker_nested_sandbox": False}
                )
                settings = json.loads(command[command.index("--settings") + 1])
                self.assertEqual(
                    settings["sandbox"]["network"],
                    task_runner.CLAUDE_SANDBOX_NETWORK,
                )

    def test_claude_full_access_child_carries_no_sandbox_settings(self) -> None:
        command = task_runner.claude_access_arguments("danger-full-access")
        self.assertNotIn("--settings", command)


class ApprovedFinalizationOwnerTests(unittest.TestCase):
    """After the bound review approves, what is left is still somebody's."""

    @staticmethod
    def _task_with_binding(root: Path, author_runner: str | None) -> Path:
        task = root / "tasks" / "001-example"
        (task / ".runner").mkdir(parents=True)
        if author_runner is not None:
            task_runner.write_json(
                task / ".runner" / "review-admission.json",
                {
                    "schema_version": 1,
                    "decision": "admitted",
                    "classification": {"work_class": "material"},
                    "pair": {
                        "author_runner": author_runner,
                        "reviewer_runner": "claude",
                        "bound": True,
                    },
                },
            )
        return task

    def test_completion_gate_names_the_bound_author(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task_with_binding(Path(raw), "codex")
            with mock.patch.object(
                task_runner, "independent_review_blocker", return_value=None
            ):
                transition = task_runner.confirmed_phase_transition(
                    task, producer=task_runner.PHASE_TRANSITION_COMPLETION_GATE
                )
        self.assertEqual(transition["next_phase"], "finalization")
        self.assertEqual(transition["owner_role"], "author")
        self.assertEqual(transition["owner_runner"], "codex")
        self.assertIs(transition["automatic"], False)

    def test_other_producers_still_name_nobody(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task_with_binding(Path(raw), "codex")
            with mock.patch.object(
                task_runner, "independent_review_blocker", return_value=None
            ):
                for producer in (None, task_runner.PHASE_TRANSITION_REVIEW_ROUND):
                    with self.subTest(producer=producer):
                        self.assertIsNone(
                            task_runner.confirmed_phase_transition(
                                task, status={}, producer=producer
                            )
                        )

    def test_a_number_with_no_bound_author_stays_a_real_stop(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task_with_binding(Path(raw), None)
            with mock.patch.object(
                task_runner, "independent_review_blocker", return_value=None
            ):
                self.assertIsNone(
                    task_runner.confirmed_phase_transition(
                        task, producer=task_runner.PHASE_TRANSITION_COMPLETION_GATE
                    )
                )

    def test_user_owned_refusal_after_approval_names_nobody(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task_with_binding(Path(raw), "codex")
            reason = task_runner.completion_failure(
                "required live evidence is not established: live-send",
                gate="required_live_evidence",
                owner=USER_OR_EXTERNAL_COMPLETION_GATE,
            )
            with mock.patch.object(
                task_runner, "independent_review_blocker", return_value=None
            ):
                refusal = task_runner.completion_refusal(task, reason)
        self.assertNotIn("phase_transition", refusal)


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
        command = task_runner.build_command(
            "codex", "test", task_runner.repo_root(), None, "workspace-write"
        )
        index = command.index("--ask-for-approval")
        self.assertEqual(command[index + 1], task_runner.CODEX_APPROVAL_MODE)

    def test_codex_read_only_keeps_only_the_task_notebook_writable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            notebook = Path(raw) / "tasks" / "001-review"
            notebook.mkdir(parents=True)
            command = task_runner.build_command(
                "codex", "review", task_runner.repo_root(), None, "read-only", notebook
            )
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(command[command.index("-C") + 1], str(notebook))
        writable = json.loads(
            next(
                value.split("=", 1)[1]
                for value in command
                if value.startswith("sandbox_workspace_write.writable_roots=")
            )
        )
        self.assertEqual(writable, [str(task_runner.repo_root() / ".state")])

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
            command = task_runner.build_command(
                "claude",
                "review",
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

    @staticmethod
    def _parsed_start(*arguments: str) -> argparse.Namespace:
        with mock.patch.object(
            sys, "argv", ["task_runner.py", "start", "/tmp/001-task", *arguments]
        ):
            return task_runner.parse_args()

    def test_dev_pipeline_hands_one_repository_path_across_both_boundaries(self) -> None:
        """`--repo` is repeatable, but both dev-pipeline consumers take one path.

        The watcher is reached as text and the adapter as a command list, so a
        list that survives to either boundary becomes `['/opt/...']`: a path the
        watcher cannot resolve, and a run record no reader can use.
        """
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "owner-workspace"
            repo.mkdir()
            parsed = self._parsed_start(
                "--workflow", "dev-pipeline", "--runner", "codex", "--repo", str(repo)
            )

            watcher_arguments: list[str] = []
            for name, value in task_runner.watcher_options(parsed).items():
                watcher_arguments.extend([f"--{name.replace('_', '-')}", str(value)])
            with mock.patch.object(
                sys,
                "argv",
                [
                    "task_runner.py",
                    "_run-child",
                    "/tmp/001-task",
                    "--runner",
                    "codex",
                    "--workflow",
                    "dev-pipeline",
                    *watcher_arguments,
                ],
            ):
                watcher_args = task_runner.parse_args()
            self.assertEqual(
                task_runner.resolve_access_directories("codex", watcher_args.repo),
                [repo.resolve()],
            )

            adapter_command = task_runner.build_workflow_command(
                "dev-pipeline",
                "codex",
                Path("/tmp/001-task"),
                "workspace-write",
                None,
                **task_runner.dev_pipeline_options(parsed),
            )
            self.assertEqual(
                adapter_command[adapter_command.index("--repo") + 1], str(repo)
            )

    def test_dev_pipeline_names_the_repositories_it_cannot_run_in_at_once(self) -> None:
        parsed = self._parsed_start(
            "--workflow",
            "dev-pipeline",
            "--repo",
            "/tmp/first-workspace",
            "--repo",
            "/tmp/second-workspace",
        )
        with self.assertRaisesRegex(SystemExit, "second-workspace"):
            task_runner.dev_pipeline_options(parsed)

    def test_standard_workflow_keeps_every_repeated_repository(self) -> None:
        parsed = self._parsed_start(
            "--repo", "/tmp/first-repo", "--repo", "/tmp/second-repo"
        )
        self.assertEqual(parsed.repo, ["/tmp/first-repo", "/tmp/second-repo"])
        self.assertEqual(task_runner.dev_pipeline_options(parsed), {})

    def test_build_codex_command_uses_current_approval_flag_without_full_auto(self) -> None:
        command = task_runner.build_command(
            runner="codex",
            prompt="test prompt",
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
            for runner in ("codex", "claude", "agent"):
                directories, grant = task_runner.prepare_access_grant(
                    runner, "workspace-write", target, require_git_worktree=True
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
                    "test",
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
                "test",
                task_runner.repo_root(),
                None,
                "read-only",
                access_directories=[target],
            )
            self.assertNotIn("--add-dir", read_only_codex)

    def test_author_write_profile_refuses_a_non_git_or_non_root_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plain = root / "plain"
            plain.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaisesRegex(SystemExit, "exact Git worktree"):
                    task_runner.prepare_access_grant(
                        "codex", "workspace-write", plain, require_git_worktree=True
                    )
            self.assertNotIn("fatal: not a git repository", stderr.getvalue())

            repository = root / "repository"
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            child = repository / "child"
            child.mkdir()
            with self.assertRaisesRegex(SystemExit, "exact Git worktree root"):
                task_runner.prepare_access_grant(
                    "codex", "workspace-write", child, require_git_worktree=True
                )

    def test_generic_write_profile_retains_plain_directory_targets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            plain = Path(raw).resolve()
            directories, grant = task_runner.prepare_access_grant(
                "codex", "workspace-write", plain
            )

        self.assertEqual(directories, [plain])
        self.assertEqual(grant["writable_directories"], [str(plain)])
        self.assertTrue(grant["grants_write"])

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


class _StartedWatcher:
    """A watcher that reports a healthy startup and does nothing else."""

    STARTUP = {
        "ok": True,
        "watcher_pid": 4242,
        "pid": 4243,
        "child_started_at": "2026-09-02T00:00:00+00:00",
        "process_identity": None,
        "watcher_process_identity": None,
    }

    def __init__(self, *args, **kwargs) -> None:
        self.stdout = io.StringIO(json.dumps(self.STARTUP) + "\n")


class _RecordingApplication:
    """An installation that keeps durable per-task state, as the real one does.

    It writes only when the launch says it is recording, so a test that asserts
    the file is absent is asserting the answer got here, not that applications
    happen to be inert.
    """

    api_version = 1

    def __init__(self) -> None:
        self.launch_requests: list[object] = []
        self.session_requests: list[object] = []

    def launch_policy(self, request):
        self.launch_requests.append(request)
        if request.committing:
            path = request.task_dir / ".runner" / "installation-policy.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"recorded": true}\n', encoding="utf-8")
        return application_adapter.LaunchPolicyV1(request.requested_memory_limit_bytes)

    def standard_session(self, request):
        self.session_requests.append(request)
        return application_adapter.StandardSessionV1()

    def standard_run_finished(self, result):
        return None

    def deliver_event(self, event):
        return application_adapter.DeliveryResultV1(True, "recorded")

    def recover_transport(self, request):
        return None

    def completion_problems(self, request):
        return []


class DryRunLeavesTheTaskAloneTests(unittest.TestCase):
    """A dry run reports a decision instead of becoming one of the task's runs.

    A dry run over task 1212 replaced the stored prompt and runner metadata of
    that task's real review round and moved its card from `completed` back to
    `ready`, seventeen minutes after a person had closed it. The probe is the
    one that found it: every byte of the task directory, before and after.
    """

    def _task(self, root: Path) -> Path:
        """A task carrying the records of a run that really happened."""
        task_dir = root / "tasks" / "001-subject"
        (task_dir / ".runner").mkdir(parents=True)
        (task_dir / "task.md").write_text(
            "---\n"
            'id: 1\nslug: "001-subject"\ntitle: "Subject"\ndate: 2026-09-01\n'
            'status: "completed"\nprojects: []\ntrips: []\n---\n# Subject\n',
            encoding="utf-8",
        )
        (task_dir / "plan.md").write_text("# Plan\n\n1. [done] Work\n", encoding="utf-8")
        (task_dir / "trace.md").write_text(
            "# Trace\n\n- 2026-09-01T00:00:00+00:00 the run that happened\n",
            encoding="utf-8",
        )
        task_runner.write_json(
            task_runner.status_path(task_dir),
            {"state": "completed", "current_step": "the run that happened"},
        )
        task_runner.runner_prompt_path(task_dir).write_text(
            "the prompt of the run that happened", encoding="utf-8"
        )
        task_runner.write_json(
            task_runner.runner_meta_path(task_dir),
            {"runner": "codex", "started_at": "2026-09-01T00:00:00+00:00"},
        )
        return task_dir

    @staticmethod
    def _fingerprint(task_dir: Path) -> dict[str, bytes | None]:
        return {
            str(path.relative_to(task_dir)): path.read_bytes() if path.is_file() else None
            for path in sorted(task_dir.rglob("*"))
        }

    def _args(
        self, task_dir: Path, *, dry_run: bool, application: str | None = None
    ) -> argparse.Namespace:
        return argparse.Namespace(
            task_dir=str(task_dir),
            runner="claude",
            workflow="standard",
            model=None,
            reviewer_runner=None,
            sandbox_mode=None,
            repo=None,
            dry_run=dry_run,
            foreground=False,
            application=application,
            destination=None,
            memory_limit=None,
        )

    def _start(
        self, task_dir: Path, *, dry_run: bool, application: str | None = None
    ) -> str:
        stdout = io.StringIO()
        with mock.patch.object(
            task_runner.review_admission, "reviewer_available", return_value=True
        ), mock.patch.object(
            task_runner.subprocess, "Popen", _StartedWatcher
        ), contextlib.redirect_stdout(stdout):
            task_runner.cmd_start(
                self._args(task_dir, dry_run=dry_run, application=application)
            )
        return stdout.getvalue()

    def test_a_dry_run_changes_no_byte_of_an_existing_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = self._task(Path(raw))
            before = self._fingerprint(task_dir)
            reported = json.loads(self._start(task_dir, dry_run=True))
            self.assertEqual(self._fingerprint(task_dir), before)

        # It still reports the prepared launch it was asked about: the admission
        # decision, and the prompt the child would have been given.
        self.assertTrue(reported["dry_run"])
        self.assertEqual(reported["review_admission"]["decision"], "admitted")
        self.assertIn("You are the child execution agent", reported["command"][-1])
        self.assertNotIn(
            "the prompt of the run that happened", reported["command"][-1]
        )

    def test_a_dry_run_creates_nothing_in_a_task_that_never_ran(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw) / "tasks" / "001-subject"
            task_dir.mkdir(parents=True)
            (task_dir / "task.md").write_text(
                "---\n"
                'id: 1\nslug: "001-subject"\ntitle: "Subject"\ndate: 2026-09-01\n'
                'status: "planned"\nprojects: []\ntrips: []\n---\n# Subject\n',
                encoding="utf-8",
            )
            (task_dir / "plan.md").write_text(
                "# Plan\n\n1. [pending] Work\n", encoding="utf-8"
            )
            self._start(task_dir, dry_run=True)
            left_behind = sorted(item.name for item in task_dir.iterdir())

        # Not the generated contract, not `.runner/`, not the ownership lock.
        self.assertEqual(left_behind, ["plan.md", "task.md"])

    def _registered_application(self) -> tuple[str, _RecordingApplication]:
        module = types.ModuleType("dry_run_probe_application")
        module.adapter = _RecordingApplication()
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        return f"{module.__name__}:adapter", module.adapter

    def test_a_dry_run_tells_the_installation_it_is_not_recording(self) -> None:
        # The engine cannot stop an application from writing into the task, and
        # should not try: only the application knows which of its facts are
        # durable. What it owes the application is the answer it already has.
        # Without it, Companion's adapter rewrote a policy file in every task a
        # dry run was ever pointed at, and the engine's own silence was worth
        # nothing on the path this installation actually launches from.
        spec, adapter = self._registered_application()
        with tempfile.TemporaryDirectory() as raw:
            task_dir = self._task(Path(raw))
            before = self._fingerprint(task_dir)
            self._start(task_dir, dry_run=True, application=spec)
            after = self._fingerprint(task_dir)

        self.assertEqual(after, before)
        self.assertIs(adapter.launch_requests[-1].committing, False)
        self.assertIs(adapter.session_requests[-1].committing, False)

    def test_a_real_start_tells_the_installation_to_record(self) -> None:
        spec, adapter = self._registered_application()
        with tempfile.TemporaryDirectory() as raw:
            task_dir = self._task(Path(raw))
            self._start(task_dir, dry_run=False, application=spec)
            recorded = (task_dir / ".runner" / "installation-policy.json").is_file()

        self.assertTrue(recorded)
        self.assertIs(adapter.launch_requests[-1].committing, True)
        self.assertIs(adapter.session_requests[-1].committing, True)

    def test_a_real_start_still_records_the_run_it_started(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = self._task(Path(raw))
            self._start(task_dir, dry_run=False)

            prompt = task_runner.runner_prompt_path(task_dir).read_text(encoding="utf-8")
            meta = task_runner.read_json(task_runner.runner_meta_path(task_dir))
            status = task_runner.read_json(task_runner.status_path(task_dir))
            trace = (task_dir / "trace.md").read_text(encoding="utf-8")

        self.assertIn("You are the child execution agent", prompt)
        self.assertEqual(meta["command"][-1], prompt)
        self.assertEqual(meta["pid"], _StartedWatcher.STARTUP["pid"])
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["review_admission"]["decision"], "admitted")
        self.assertIn("Parent agent prepared child run", trace)
        self.assertIn("Child process started with pid 4243", trace)


class PreparedAssuranceTests(unittest.TestCase):
    """An installation that builds this launch's assurance is not made to file it.

    A dev-pipeline launch is refused unless the assurance the core will run
    names the reviewer bound before the author starts, and the launcher used to
    be able to learn that assurance only by reading the file back. So the
    installation front end had to write the document into the task directory
    before the launcher had decided anything at all -- and a dry run over a task
    nobody meant to launch overwrote that task's real assurance records to be
    told what its own caller had just decided.
    """

    ASSURANCE = {
        "schema_version": "1.0",
        "strategy": "cross_provider",
        "owner_provider": "claude",
        "review_provider": "codex",
        "providers": {
            "claude": {"executable": "/usr/bin/env"},
            "codex": {"executable": "/usr/bin/env"},
        },
    }

    def _gated_task(self, root: Path) -> Path:
        task_dir = root / "tasks" / "001-subject"
        task_dir.mkdir(parents=True)
        (task_dir / "task.md").write_text(
            "---\n"
            'id: 1\nslug: "001-subject"\ntitle: "Subject"\ndate: 2026-09-01\n'
            'status: "planned"\nprojects: []\ntrips: []\n---\n# Subject\n',
            encoding="utf-8",
        )
        (task_dir / "plan.md").write_text("# Plan\n\n1. [pending] Work\n", encoding="utf-8")
        task_runner.write_json(
            task_dir / "task_contract.json",
            {
                "version": 1,
                "review_gates": ["An independent reviewer approves the candidate."],
            },
        )
        return task_dir

    def _dry_run(self, task_dir: Path, *, prepared: dict | None) -> dict:
        """The launch a gated dev-pipeline dry run would make, as reported."""
        repository = task_dir.parent.parent / "repository"
        repository.mkdir(exist_ok=True)
        argv = [
            "task_runner.py",
            "start",
            str(task_dir),
            "--workflow",
            "dev-pipeline",
            "--runner",
            "claude",
            "--repo",
            str(repository),
            "--assurance-config",
            str(task_dir / "dev-pipeline" / "assurance.json"),
            "--dry-run",
        ]
        stdout = io.StringIO()
        keywords = {} if prepared is None else {"prepared_assurance": prepared}
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            task_runner.review_admission, "reviewer_available", return_value=True
        ), contextlib.redirect_stdout(stdout):
            task_runner.main(**keywords)
        return json.loads(stdout.getvalue())

    def test_a_launch_is_evaluated_against_the_assurance_it_was_handed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = self._gated_task(Path(raw))
            reported = self._dry_run(task_dir, prepared=self.ASSURANCE)
            named_file = task_dir / "dev-pipeline" / "assurance.json"
            wrote_the_file = named_file.exists()

        admission = reported["review_admission"]
        self.assertEqual(admission["decision"], "admitted")
        self.assertEqual(admission["assurance_source"], "installation_config")
        self.assertEqual(admission["assurance_binding"]["assurance_review_provider"], "codex")
        self.assertTrue(admission["assurance_binding"]["bound"])
        # The launcher was told, not shown: nothing had to be written for it.
        self.assertFalse(wrote_the_file)
        self.assertEqual(
            reported["command"][reported["command"].index("--assurance-config") + 1],
            str(named_file),
        )

    def test_the_same_launch_without_it_is_refused_as_unassured(self) -> None:
        # The control for the test above: the admission really comes from the
        # handed-over document, not from a default that would have bound the
        # same reviewer anyway.
        with tempfile.TemporaryDirectory() as raw:
            task_dir = self._gated_task(Path(raw))
            with self.assertRaises(SystemExit) as refusal:
                self._dry_run(task_dir, prepared=None)

        self.assertIn("carries no assurance configuration", str(refusal.exception))

    def test_a_configured_file_still_answers_for_a_launcher_nobody_handed(self) -> None:
        # The CLI contract is unchanged: a launch that names a readable file is
        # evaluated against it exactly as before.
        with tempfile.TemporaryDirectory() as raw:
            configured = Path(raw) / "assurance.json"
            task_runner.write_json(configured, self.ASSURANCE)
            args = argparse.Namespace(assurance_config=str(configured))
            self.assertEqual(task_runner.configured_assurance(args), self.ASSURANCE)


if __name__ == "__main__":
    unittest.main()
