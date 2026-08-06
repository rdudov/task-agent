import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_task_runner_module():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    module_path = scripts_dir / "task_runner.py"
    sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location("task_runner_resolution_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


task_runner = _load_task_runner_module()


class RunnerResolutionTests(unittest.TestCase):
    def test_explicit_flag_wins_over_everything(self) -> None:
        with mock.patch.dict(os.environ, {task_runner.RUNNER_OVERRIDE_ENV: "agent"}):
            self.assertEqual(task_runner.resolve_runner("claude"), ("claude", "explicit_flag"))

    def test_env_override_wins_over_detection(self) -> None:
        with mock.patch.dict(os.environ, {task_runner.RUNNER_OVERRIDE_ENV: "agent"}):
            with mock.patch.object(
                task_runner, "detect_parent_runner", return_value=("codex", "parent_process:codex")
            ):
                self.assertEqual(task_runner.resolve_runner(None), ("agent", "env_override"))

    def test_env_override_rejects_unknown_runner(self) -> None:
        with mock.patch.dict(os.environ, {task_runner.RUNNER_OVERRIDE_ENV: "nonsense"}):
            with self.assertRaises(SystemExit):
                task_runner.resolve_runner(None)

    def test_ancestry_outranks_session_markers(self) -> None:
        # A nested chain shows both vendors' markers, so ancestry has to decide.
        env = {"CODEX_THREAD_ID": "t-1", "CLAUDECODE": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(
                task_runner, "process_ancestry", return_value=(["bash", "claude", "sshd"], "proc")
            ):
                self.assertEqual(
                    task_runner.detect_parent_runner(), ("claude", "parent_process:claude")
                )

    def test_single_session_marker_decides_without_ancestry(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=True):
            with mock.patch.object(task_runner, "process_ancestry", return_value=([], "unavailable")):
                self.assertEqual(
                    task_runner.detect_parent_runner(), ("claude", "parent_env:claude")
                )

    def test_ambiguous_session_markers_decide_nothing(self) -> None:
        env = {"CODEX_THREAD_ID": "t-1", "CLAUDECODE": "1"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(task_runner, "process_ancestry", return_value=([], "proc")):
                detected, reason = task_runner.detect_parent_runner()
        self.assertIsNone(detected)
        self.assertEqual(reason, "ambiguous_parent_env:claude+codex")

    def test_missing_ancestry_is_reported_not_hidden(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(task_runner, "process_ancestry", return_value=([], "unavailable")):
                detected, reason = task_runner.detect_parent_runner()
        self.assertIsNone(detected)
        self.assertEqual(reason, "no_parent_signal:no_process_ancestry")

    def test_falls_back_to_documented_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                task_runner, "detect_parent_runner", return_value=(None, "no_parent_signal")
            ):
                self.assertEqual(
                    task_runner.resolve_runner(None),
                    (task_runner.DEFAULT_RUNNER, "fallback_default:no_parent_signal"),
                )

    def test_ps_fallback_walks_the_tree_without_proc(self) -> None:
        table = "  10     1 /usr/bin/init\n  20    10 claude\n  30    20 python3\n"
        completed = mock.Mock(returncode=0, stdout=table)
        with mock.patch.object(task_runner.shutil, "which", return_value="/bin/ps"):
            with mock.patch.object(task_runner.subprocess, "run", return_value=completed):
                names = task_runner.process_ancestry_via_ps(30)
        self.assertEqual(names, ["python3", "claude", "init"])

    def test_ps_fallback_is_absent_when_ps_is_missing(self) -> None:
        with mock.patch.object(task_runner.shutil, "which", return_value=None):
            self.assertEqual(task_runner.process_ancestry_via_ps(1), [])


class ChildEnvironmentTests(unittest.TestCase):
    def test_every_vendor_marker_is_scrubbed(self) -> None:
        env = {"CODEX_THREAD_ID": "t-1", "CLAUDECODE": "1", "PATH": "/usr/bin"}
        with mock.patch.dict(os.environ, env, clear=True):
            child = task_runner.child_environment("codex")
        self.assertNotIn("CODEX_THREAD_ID", child)
        self.assertNotIn("CLAUDECODE", child)
        self.assertEqual(child["PATH"], "/usr/bin")

    def test_claude_child_receives_sandbox_marker(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(task_runner.child_environment("claude")["IS_SANDBOX"], "1")

    def test_codex_child_does_not_receive_sandbox_marker(self) -> None:
        with mock.patch.dict(os.environ, {"IS_SANDBOX": "1"}, clear=True):
            self.assertNotIn("IS_SANDBOX", task_runner.child_environment("codex"))


class WorkspaceRootTests(unittest.TestCase):
    def test_defaults_to_the_parent_of_the_checkout(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(task_runner.workspace_root(), task_runner.repo_root().parent)

    def test_environment_override_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {task_runner.WORKSPACE_ROOT_ENV: tmp}, clear=True):
                self.assertEqual(task_runner.workspace_root(), Path(tmp).resolve())

    def test_full_access_workdir_follows_the_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {task_runner.WORKSPACE_ROOT_ENV: tmp}, clear=True):
                self.assertEqual(
                    task_runner.codex_workdir("danger-full-access"), Path(tmp).resolve()
                )
                self.assertEqual(
                    task_runner.codex_workdir("workspace-write"), task_runner.repo_root()
                )


class ClaudeAccessTests(unittest.TestCase):
    def test_read_only_without_a_notebook_exposes_only_reading_tools(self) -> None:
        args = task_runner.claude_access_arguments(
            "read-only", {"needs_weaker_nested_sandbox": False}
        )
        self.assertIn("--tools", args)
        self.assertEqual(
            args[args.index("--tools") + 1], "Read,Grep,Glob,WebFetch,WebSearch"
        )
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--add-dir", args)

    def test_workspace_write_uses_accept_edits_without_added_directories(self) -> None:
        args = task_runner.claude_access_arguments(
            "workspace-write", {"needs_weaker_nested_sandbox": False}
        )
        self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")
        self.assertNotIn("--add-dir", args)

    def test_restricted_modes_pin_the_settings_source_and_fail_closed(self) -> None:
        for mode in ("read-only", "workspace-write"):
            args = task_runner.claude_access_arguments(mode, {"needs_weaker_nested_sandbox": False})
            self.assertEqual(args[args.index("--setting-sources") + 1], "project")
            settings = json.loads(args[args.index("--settings") + 1])["sandbox"]
            self.assertTrue(settings["failIfUnavailable"])
            self.assertFalse(settings["allowUnsandboxedCommands"])

    def test_weaker_nested_sandbox_is_only_for_root(self) -> None:
        as_root = json.loads(
            task_runner.claude_access_arguments(
                "workspace-write", {"needs_weaker_nested_sandbox": True}
            )[-1]
        )["sandbox"]
        as_user = json.loads(
            task_runner.claude_access_arguments(
                "workspace-write", {"needs_weaker_nested_sandbox": False}
            )[-1]
        )["sandbox"]
        self.assertTrue(as_root["enableWeakerNestedSandbox"])
        self.assertNotIn("enableWeakerNestedSandbox", as_user)

    def test_full_access_adds_the_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {task_runner.WORKSPACE_ROOT_ENV: tmp}, clear=True):
                args = task_runner.claude_access_arguments("danger-full-access")
        self.assertEqual(args[args.index("--add-dir") + 1], str(Path(tmp).resolve()))
        self.assertIn("--dangerously-skip-permissions", args)
        # --add-dir is variadic, so it must never be the last argument.
        self.assertNotEqual(args[-1], str(Path(tmp).resolve()))

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            task_runner.claude_access_arguments("sideways", {})


class ClaudeSandboxPreflightTests(unittest.TestCase):
    def test_full_access_needs_no_native_sandbox(self) -> None:
        task_runner.require_claude_sandbox_dependencies(
            "danger-full-access", {"native_sandbox": False, "platform": "darwin"}
        )

    def test_restricted_mode_passes_with_a_working_sandbox(self) -> None:
        task_runner.require_claude_sandbox_dependencies(
            "read-only", {"native_sandbox": True, "platform": "linux"}
        )

    def test_restricted_mode_fails_closed_on_a_missing_dependency(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            task_runner.require_claude_sandbox_dependencies(
                "workspace-write",
                {"native_sandbox": False, "platform": "linux", "missing_dependencies": ["bwrap"]},
            )
        self.assertIn("bwrap", str(caught.exception))

    def test_restricted_mode_fails_closed_off_linux(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            task_runner.require_claude_sandbox_dependencies(
                "read-only",
                {"native_sandbox": False, "platform": "darwin", "missing_dependencies": []},
            )
        self.assertIn("Linux", str(caught.exception))


class ClaudeProjectSettingsTests(unittest.TestCase):
    def _write_settings(self, root: Path, payload) -> None:
        (root / ".claude").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "settings.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_absent_settings_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_runner.require_safe_claude_project_settings(Path(tmp), "read-only")

    def test_read_only_permission_rules_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_settings(root, {"permissions": {"allow": ["Read", "Read(./src)"]}})
            task_runner.require_safe_claude_project_settings(root, "workspace-write")

    def test_widening_settings_are_rejected(self) -> None:
        cases = [
            {"hooks": {"Stop": []}},
            {"enabledPlugins": ["x"]},
            {"permissions": {"allow": ["Bash(rm:*)"]}},
            {"permissions": {"additionalDirectories": ["/"]}},
            {"sandbox": {"network": {"allowUnixSockets": ["/tmp/s"]}}},
            {"sandbox": {"filesystem": {"disabled": True}}},
            {"sandbox": {"network": {"httpProxyPort": 8080}}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self._write_settings(root, payload)
                    with self.assertRaises(RuntimeError):
                        task_runner.require_safe_claude_project_settings(root, "read-only")

    def test_full_access_skips_the_settings_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_settings(root, {"hooks": {"Stop": []}})
            task_runner.require_safe_claude_project_settings(root, "danger-full-access")

    def test_unreadable_settings_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".claude").mkdir()
            (root / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                task_runner.require_safe_claude_project_settings(root, "read-only")


class ClaudeCommandTests(unittest.TestCase):
    def _prompt_file(self, root: Path) -> Path:
        path = root / "prompt.txt"
        path.write_text("do the work", encoding="utf-8")
        return path

    def test_claude_command_keeps_the_prompt_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = task_runner.build_command(
                "claude", self._prompt_file(root), root, None, "danger-full-access"
            )
        self.assertEqual(command[0], "claude")
        self.assertIn("--print", command)
        self.assertEqual(command[-1], "do the work")

    def test_model_override_reaches_only_the_resolved_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = self._prompt_file(root)
            claude_command = task_runner.build_command(
                "claude", prompt, root, "claude-model-slug", "workspace-write"
            )
            codex_command = task_runner.build_command(
                "codex", prompt, root, "codex-model-slug", "workspace-write"
            )
        self.assertEqual(claude_command[claude_command.index("--model") + 1], "claude-model-slug")
        self.assertEqual(codex_command[codex_command.index("--model") + 1], "codex-model-slug")

    def test_claude_default_model_comes_from_its_own_variable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(
                os.environ, {"CLAUDE_CHILD_DEFAULT_MODEL": "pinned-model"}, clear=False
            ):
                command = task_runner.build_command(
                    "claude", self._prompt_file(root), root, None, "workspace-write"
                )
        self.assertEqual(command[command.index("--model") + 1], "pinned-model")

    def test_unsupported_runner_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SystemExit):
                task_runner.build_command(
                    "gemini", self._prompt_file(root), root, None, "workspace-write"
                )


if __name__ == "__main__":
    unittest.main()
