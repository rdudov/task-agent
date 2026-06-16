import importlib.util
import sys
import unittest
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
    def test_resolve_sandbox_mode_defaults_multi_agent_codex_to_danger_full_access(self) -> None:
        self.assertEqual(
            task_runner.resolve_sandbox_mode(
                runner="codex",
                workflow="multi-agent-dev",
                sandbox_mode=None,
            ),
            "danger-full-access",
        )

    def test_resolve_sandbox_mode_defaults_multi_agent_agent_to_danger_full_access(self) -> None:
        self.assertEqual(
            task_runner.resolve_sandbox_mode(
                runner="agent",
                workflow="multi-agent-dev",
                sandbox_mode=None,
            ),
            "danger-full-access",
        )

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
                workflow="multi-agent-dev",
                sandbox_mode="workspace-write",
            ),
            "workspace-write",
        )

    def test_build_workflow_command_passes_resolved_sandbox_mode(self) -> None:
        command = task_runner.build_workflow_command(
            workflow="multi-agent-dev",
            runner="codex",
            task_dir=Path("/tmp/example-task"),
            agents_dir=None,
            agents_repo_url=None,
            artifacts_subdir=None,
            sandbox_mode="danger-full-access",
            resume=True,
            model=None,
        )

        self.assertIsNotNone(command)
        self.assertIn("--sandbox-mode", command)
        self.assertIn("danger-full-access", command)
        self.assertIn("--resume", command)

    def test_build_workflow_command_passes_model_override(self) -> None:
        command = task_runner.build_workflow_command(
            workflow="multi-agent-dev",
            runner="codex",
            task_dir=Path("/tmp/example-task"),
            agents_dir=None,
            agents_repo_url=None,
            artifacts_subdir=None,
            sandbox_mode="danger-full-access",
            resume=True,
            model="gpt-5.4",
        )

        self.assertIsNotNone(command)
        self.assertIn("--model", command)
        self.assertIn("gpt-5.4", command)

    def test_build_workflow_command_passes_agents_repo_url(self) -> None:
        command = task_runner.build_workflow_command(
            workflow="multi-agent-dev",
            runner="codex",
            task_dir=Path("/tmp/example-task"),
            agents_dir="/tmp/agents",
            agents_repo_url="https://example.test/agents.git",
            artifacts_subdir=None,
            sandbox_mode=None,
            resume=False,
            model=None,
        )

        self.assertIsNotNone(command)
        self.assertIn("--agents-dir", command)
        self.assertIn("/tmp/agents", command)
        self.assertIn("--agents-repo-url", command)
        self.assertIn("https://example.test/agents.git", command)

    def test_build_workflow_command_passes_agent_runner(self) -> None:
        command = task_runner.build_workflow_command(
            workflow="multi-agent-dev",
            runner="agent",
            task_dir=Path("/tmp/example-task"),
            agents_dir=None,
            agents_repo_url=None,
            artifacts_subdir=None,
            sandbox_mode="danger-full-access",
            resume=False,
        )

        self.assertIsNotNone(command)
        self.assertIn("--runner", command)
        self.assertIn("agent", command)

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
