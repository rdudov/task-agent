import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_codex_multi_agent_module():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts_dir))
    module_path = scripts_dir / "codex_multi_agent.py"
    spec = importlib.util.spec_from_file_location("codex_multi_agent_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


codex_multi_agent = _load_codex_multi_agent_module()


class MultiAgentRunnerTests(unittest.TestCase):
    def test_run_agent_uses_cursor_cli_flags(self) -> None:
        workdir = Path("/tmp/work")
        with patch.object(codex_multi_agent.subprocess, "run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = "ok"
            run_mock.return_value.stderr = ""

            codex_multi_agent.run_agent("do work", "composer-2.5", "danger-full-access", workdir)

        command = run_mock.call_args.args[0]
        self.assertEqual(command[0:6], ["agent", "--print", "--trust", "--force", "--workspace", str(workdir)])
        self.assertIn("--model", command)
        self.assertIn("composer-2.5", command)
        self.assertIn("--sandbox", command)
        self.assertIn("disabled", command)

    def test_run_agent_uses_plan_mode_for_read_only(self) -> None:
        workdir = Path("/tmp/work")
        with patch.object(codex_multi_agent.subprocess, "run") as run_mock:
            run_mock.return_value.returncode = 0
            run_mock.return_value.stdout = "ok"
            run_mock.return_value.stderr = ""

            codex_multi_agent.run_agent("review only", None, "read-only", workdir)

        command = run_mock.call_args.args[0]
        self.assertIn("--mode", command)
        self.assertIn("plan", command)


if __name__ == "__main__":
    unittest.main()
