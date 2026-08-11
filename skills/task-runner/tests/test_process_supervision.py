"""Process supervision exercised against real processes, not mocked pids."""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


def _load_task_runner_module():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    module_path = scripts_dir / "task_runner.py"
    sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location("task_runner_supervision_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


task_runner = _load_task_runner_module()

RUNNER_SCRIPT = Path(task_runner.__file__).resolve()

IDENTITY_REQUIRED = unittest.skipUnless(
    task_runner.process_identity_available(),
    "kernel process identity needs a readable /proc",
)


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class ProcessIdentityTests(unittest.TestCase):
    def test_identity_is_stable_for_one_live_process(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        first = task_runner.process_identity(process.pid)
        time.sleep(0.2)
        self.assertEqual(first, task_runner.process_identity(process.pid))

    def test_two_live_processes_have_distinct_identities(self) -> None:
        # Start times are recorded in clock ticks, so two processes can share a
        # tick. Retrying keeps the assertion about identity, not about timing.
        for _ in range(20):
            first = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            time.sleep(0.05)
            second = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            identities = (
                task_runner.process_identity(first.pid),
                task_runner.process_identity(second.pid),
            )
            for process in (first, second):
                process.kill()
                process.wait()
            if identities[0] != identities[1]:
                return
        self.fail("two processes started apart never produced distinct identities")

    def test_identity_of_a_reaped_process_is_none(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", ""])
        process.wait()
        self.assertIsNone(task_runner.process_identity(process.pid))

    def test_recorded_instance_reports_how_it_decided(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        identity = task_runner.process_identity(process.pid)

        self.assertEqual(
            task_runner.process_is_recorded_instance(process.pid, identity),
            (True, "identity_match"),
        )
        self.assertEqual(
            task_runner.process_is_recorded_instance(process.pid, "not-this-process"),
            (False, "identity_mismatch"),
        )
        self.assertEqual(
            task_runner.process_is_recorded_instance("not-a-pid", identity),
            (False, "no_recorded_pid"),
        )

    def test_a_run_without_recorded_identity_degrades_to_pid_liveness(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        alive, source = task_runner.process_is_recorded_instance(process.pid, None)
        self.assertTrue(alive)
        self.assertTrue(source.startswith("pid_only"))


class DetachedWatcherTests(unittest.TestCase):
    """The watcher must outlive the process that asked for the run."""

    def _prepare(self, tmp: str) -> tuple[Path, dict, Path]:
        """Put a real, scriptable executable named `claude` first on PATH.

        The supervision path under test is the OS one: spawn, detach, wait,
        finalize. What the child computes is irrelevant to it, so a shell script
        exercises the same launch path a CLI would without needing a model.
        """
        task_dir = Path(tmp) / "001-detached"
        task_dir.mkdir(parents=True)
        (task_dir / "task.md").write_text("# Detached\n", encoding="utf-8")
        (task_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")

        stub_dir = Path(tmp) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "claude"

        environment = dict(os.environ)
        environment["PATH"] = f"{stub_dir}{os.pathsep}{environment['PATH']}"
        return task_dir, environment, stub

    def _write_child(self, stub: Path, script: str) -> None:
        stub.write_text(script, encoding="utf-8")
        stub.chmod(0o755)

    def _start(self, task_dir: Path, environment: dict) -> dict:
        completed = subprocess.run(
            [
                sys.executable,
                str(RUNNER_SCRIPT),
                "start",
                str(task_dir),
                "--runner",
                "claude",
                "--sandbox-mode",
                "danger-full-access",
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def test_child_survives_the_initiating_process_and_records_its_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir, environment, stub = self._prepare(tmp)
            # The child writes its own terminal state, exactly as the standard
            # workflow prompt instructs a real CLI child to do.
            terminal_status = json.dumps(
                {
                    "state": "completed",
                    "current_step": "child finished",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            )
            self._write_child(
                stub,
                "#!/bin/sh\n"
                "sleep 3\n"
                f"cat > '{task_dir / 'status.json'}' <<'JSON'\n{terminal_status}\nJSON\n"
                "exit 0\n",
            )

            meta = self._start(task_dir, environment)

            # `start` has already returned, so the initiating process is gone.
            self.assertTrue(task_runner.pid_is_running(meta["watcher_pid"]))
            self.assertTrue(task_runner.pid_is_running(meta["pid"]))
            if task_runner.process_identity_available():
                self.assertIsNotNone(meta["process_identity"])
                self.assertEqual(
                    task_runner.process_identity(meta["pid"]), meta["process_identity"]
                )

            self.assertTrue(
                _wait_until(lambda: not task_runner.pid_is_running(meta["watcher_pid"]), 30),
                "the detached watcher never finished",
            )
            status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "blocked")
            self.assertIn("completion_refusal", status)
            runner_meta = json.loads(
                (task_dir / ".runner" / "runner.json").read_text(encoding="utf-8")
            )
            self.assertEqual(runner_meta["outcome"], "rejected_completion_contract")
            self.assertEqual(runner_meta["exit_code"], 0)

    def test_a_child_that_dies_without_a_terminal_state_is_recorded_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir, environment, stub = self._prepare(tmp)
            self._write_child(stub, "#!/bin/sh\nsleep 1\nexit 9\n")
            meta = self._start(task_dir, environment)
            self.assertTrue(
                _wait_until(lambda: not task_runner.pid_is_running(meta["watcher_pid"]), 30),
                "the detached watcher never finished",
            )
            status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["exit_code"], 9)
            trace = (task_dir / "trace.md").read_text(encoding="utf-8")
            self.assertIn("exited with code 9", trace)


@IDENTITY_REQUIRED
class ReattachTests(unittest.TestCase):
    def _task_dir(self, tmp: str) -> Path:
        task_dir = Path(tmp) / "001-reattach"
        task_dir.mkdir(parents=True)
        (task_dir / "task.md").write_text("# Reattach\n", encoding="utf-8")
        (task_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
        (task_dir / ".runner").mkdir()
        return task_dir

    def _live_child(self) -> subprocess.Popen:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def _reattach(self, task_dir: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER_SCRIPT),
                "reattach",
                str(task_dir),
                "--poll-interval",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_reattach_refuses_a_reused_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp)
            child = self._live_child()
            # The pid is genuinely alive but is not the recorded incarnation:
            # this is what a caller sees after the kernel recycles a pid.
            task_runner.write_json(
                task_dir / ".runner" / "runner.json",
                {
                    "pid": child.pid,
                    "process_identity": "identity-of-a-process-that-exited",
                    "runner": "claude",
                    "workflow": "standard",
                },
            )
            result = self._reattach(task_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("identity is not running", result.stdout + result.stderr)

    def test_reattach_refuses_when_a_watcher_is_already_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp)
            child = self._live_child()
            watcher = self._live_child()
            task_runner.write_json(
                task_dir / ".runner" / "runner.json",
                {
                    "pid": child.pid,
                    "process_identity": task_runner.process_identity(child.pid),
                    "watcher_pid": watcher.pid,
                    "watcher_process_identity": task_runner.process_identity(watcher.pid),
                    "runner": "claude",
                    "workflow": "standard",
                },
            )
            result = self._reattach(task_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("watcher is already monitoring", result.stdout + result.stderr)

    def test_reattach_refuses_a_run_without_recorded_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp)
            child = self._live_child()
            task_runner.write_json(
                task_dir / ".runner" / "runner.json",
                {"pid": child.pid, "runner": "claude", "workflow": "standard"},
            )
            result = self._reattach(task_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no process identity", result.stdout + result.stderr)

    def test_reattach_supervises_a_live_child_through_its_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp)
            child = self._live_child()
            # A dead watcher pid: the recorded identity cannot match anything,
            # which is the state left behind when a watcher is killed.
            task_runner.write_json(
                task_dir / ".runner" / "runner.json",
                {
                    "pid": child.pid,
                    "process_identity": task_runner.process_identity(child.pid),
                    "watcher_pid": child.pid,
                    "watcher_process_identity": "identity-of-a-watcher-that-died",
                    "runner": "claude",
                    "workflow": "standard",
                },
            )
            task_runner.write_status(task_dir, "running", "child work")

            result = self._reattach(task_dir)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            startup = json.loads(result.stdout)
            self.assertTrue(startup["ok"])
            self.assertEqual(startup["pid"], child.pid)
            recovered_watcher = startup["watcher_pid"]
            self.assertTrue(task_runner.pid_is_running(recovered_watcher))

            child.send_signal(signal.SIGKILL)
            child.wait()

            self.assertTrue(
                _wait_until(lambda: not task_runner.pid_is_running(recovered_watcher), 30),
                "the recovered watcher never observed the child exit",
            )
            status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "failed")
            runner_meta = json.loads(
                (task_dir / ".runner" / "runner.json").read_text(encoding="utf-8")
            )
            self.assertEqual(runner_meta["outcome"], "recovered_terminal_state_unknown")

    def test_reattach_keeps_a_terminal_state_the_child_already_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp)
            child = self._live_child()
            task_runner.write_json(
                task_dir / ".runner" / "runner.json",
                {
                    "pid": child.pid,
                    "process_identity": task_runner.process_identity(child.pid),
                    "runner": "claude",
                    "workflow": "standard",
                },
            )
            task_runner.write_status(task_dir, "running", "child work")
            result = self._reattach(task_dir)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            recovered_watcher = json.loads(result.stdout)["watcher_pid"]

            task_runner.write_status(task_dir, "completed", "child finished its own work")
            child.kill()
            child.wait()

            self.assertTrue(
                _wait_until(lambda: not task_runner.pid_is_running(recovered_watcher), 30)
            )
            status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "completed")
            runner_meta = json.loads(
                (task_dir / ".runner" / "runner.json").read_text(encoding="utf-8")
            )
            self.assertEqual(runner_meta["outcome"], "recovered_completed")


class StopRefusesRecycledPidTests(unittest.TestCase):
    @IDENTITY_REQUIRED
    def test_stop_refuses_a_pid_that_is_no_longer_the_recorded_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "001-stop"
            task_dir.mkdir(parents=True)
            (task_dir / "task.md").write_text("# Stop\n", encoding="utf-8")
            (task_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
            (task_dir / ".runner").mkdir()
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                start_new_session=True,
            )
            self.addCleanup(process.wait)
            self.addCleanup(process.kill)
            task_runner.write_json(
                task_dir / ".runner" / "runner.json",
                {"pid": process.pid, "process_identity": "identity-of-a-process-that-exited"},
            )
            result = subprocess.run(
                [sys.executable, str(RUNNER_SCRIPT), "stop", str(task_dir)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("identity is no longer running", result.stdout + result.stderr)
            # The refusal must be total: the live process is untouched.
            self.assertIsNone(process.poll())


if __name__ == "__main__":
    unittest.main()
