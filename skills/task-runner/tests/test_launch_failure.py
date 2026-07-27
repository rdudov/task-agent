"""Regression tests for the fail-closed launch path.

A refusal to start is a feature; a refusal that leaves the task reading
`running` is a lie. These exercise the real `start` entrypoint as a subprocess,
because the defect these cover lived in the boundary between the parent and its
detached watcher, not inside either one.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER = REPO_ROOT / "skills" / "task-runner" / "scripts" / "task_runner.py"


def shadow_path_without(shadow: Path, *names: str) -> str:
    """Build a PATH that resolves everything except the named executables.

    The caller owns `shadow` so the directory is cleaned up with the test rather
    than accumulating under the system temp directory across runs.
    """
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(entry)
        if not directory.is_dir():
            continue
        try:
            candidates = list(directory.iterdir())
        except OSError:
            continue
        for item in candidates:
            if item.name in names or (shadow / item.name).exists():
                continue
            try:
                (shadow / item.name).symlink_to(item)
            except OSError:
                continue
    return str(shadow)


class ClaudePreflightLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks_root = REPO_ROOT / "tasks"
        self.created = Path(tempfile.mkdtemp(prefix="lifecycle-", dir=self.tasks_root))
        (self.created / "task.md").write_text("# Launch failure probe\n", encoding="utf-8")
        (self.created / "plan.md").write_text("# Plan\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.created, ignore_errors=True)

    def _status(self) -> dict:
        path = self.created / "status.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _runner_meta(self) -> dict:
        path = self.created / ".runner" / "runner.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    @unittest.skipUnless(
        shutil.which("bwrap") and shutil.which("socat"),
        "needs a host where the sandbox dependencies normally exist",
    )
    def test_missing_sandbox_dependency_ends_in_terminal_failed(self) -> None:
        env = dict(os.environ)
        shadow = Path(tempfile.mkdtemp(prefix="task-agent-shadow-"))
        self.addCleanup(shutil.rmtree, shadow, ignore_errors=True)
        env["PATH"] = shadow_path_without(shadow, "bwrap", "socat")
        completed = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "start",
                str(self.created),
                "--runner",
                "claude",
                "--sandbox-mode",
                "workspace-write",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("JSONDecodeError", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

        status = self._status()
        self.assertEqual(status.get("state"), "failed")
        self.assertNotEqual(status.get("state"), "running")

        meta = self._runner_meta()
        self.assertEqual(meta.get("outcome"), "failed_to_launch")
        self.assertIn("bwrap", meta.get("launch_error", "") + status.get("current_step", ""))

    def test_unparsable_startup_output_does_not_leave_running(self) -> None:
        # The watcher is replaced by a stand-in that prints noise instead of a
        # startup record, which is what a traceback looked like to the parent.
        import importlib.util

        sys.path.insert(0, str(RUNNER.parent))
        spec = importlib.util.spec_from_file_location("task_runner_launch_module", RUNNER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        import argparse
        from unittest import mock

        args = argparse.Namespace(
            task_dir=str(self.created),
            runner="codex",
            workflow="standard",
            model=None,
            sandbox_mode=None,
            agents_dir=None,
            agents_repo_url=None,
            artifacts_subdir=None,
            dry_run=False,
            resume=False,
        )

        class FakeProcess:
            def __init__(self) -> None:
                import io

                self.stdout = io.StringIO("Traceback (most recent call last):\n")

            def wait(self, timeout=None):
                return 1

        with mock.patch.object(module.subprocess, "Popen", return_value=FakeProcess()):
            with self.assertRaises(SystemExit) as caught:
                module.cmd_start(args)

        self.assertIn("unparsable startup output", str(caught.exception))
        self.assertEqual(self._status().get("state"), "failed")

    def test_watcher_spawn_failure_does_not_leave_running(self) -> None:
        # The parent's own boundary: if the watcher process cannot be created at
        # all, nothing downstream ever runs to record a terminal state.
        import argparse
        import importlib.util
        from unittest import mock

        sys.path.insert(0, str(RUNNER.parent))
        spec = importlib.util.spec_from_file_location("task_runner_watcher_module", RUNNER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        args = argparse.Namespace(
            task_dir=str(self.created),
            runner="codex",
            workflow="standard",
            model=None,
            sandbox_mode=None,
            agents_dir=None,
            agents_repo_url=None,
            artifacts_subdir=None,
            dry_run=False,
            resume=False,
        )

        with mock.patch.object(
            module.subprocess, "Popen", side_effect=OSError("cannot fork")
        ):
            with self.assertRaises(SystemExit) as caught:
                module.cmd_start(args)

        self.assertIn("could not start the task watcher", str(caught.exception))
        status = self._status()
        self.assertEqual(status.get("state"), "failed")
        self.assertIn("cannot fork", status.get("current_step", ""))

        meta = self._runner_meta()
        self.assertEqual(meta.get("outcome"), "watcher_failed_to_launch")
        self.assertEqual(meta.get("launch_error"), "cannot fork")
        self.assertTrue(meta.get("finished_at"))

    def test_watcher_spawn_failure_is_recorded_in_the_trace(self) -> None:
        import argparse
        import importlib.util
        from unittest import mock

        sys.path.insert(0, str(RUNNER.parent))
        spec = importlib.util.spec_from_file_location("task_runner_watcher_trace_module", RUNNER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        args = argparse.Namespace(
            task_dir=str(self.created),
            runner="claude",
            workflow="standard",
            model=None,
            sandbox_mode=None,
            agents_dir=None,
            agents_repo_url=None,
            artifacts_subdir=None,
            dry_run=False,
            resume=False,
        )

        with mock.patch.object(
            module.subprocess, "Popen", side_effect=OSError("permission denied")
        ):
            with self.assertRaises(SystemExit):
                module.cmd_start(args)

        trace = (self.created / "trace.md").read_text(encoding="utf-8")
        self.assertIn("could not start the task watcher", trace)

    def test_a_terminal_state_survives_a_watcher_spawn_failure(self) -> None:
        # A child that already reported a terminal state must not be relabelled
        # by a later parent-side failure.
        import argparse
        import importlib.util
        from unittest import mock

        sys.path.insert(0, str(RUNNER.parent))
        spec = importlib.util.spec_from_file_location("task_runner_watcher_keep_module", RUNNER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        args = argparse.Namespace(
            task_dir=str(self.created),
            runner="codex",
            workflow="standard",
            model=None,
            sandbox_mode=None,
            agents_dir=None,
            agents_repo_url=None,
            artifacts_subdir=None,
            dry_run=False,
            resume=False,
        )

        def keep_completed(*call_args, **call_kwargs):
            module.write_status(self.created, "completed", "already done")
            raise OSError("cannot fork")

        with mock.patch.object(module.subprocess, "Popen", side_effect=keep_completed):
            with self.assertRaises(SystemExit):
                module.cmd_start(args)

        self.assertEqual(self._status().get("state"), "completed")
        self.assertEqual(self._runner_meta().get("outcome"), "watcher_failed_to_launch")


if __name__ == "__main__":
    unittest.main()
