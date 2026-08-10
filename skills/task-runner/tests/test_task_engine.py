"""The public observation surface, and where its answers come from."""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


def _load(name: str):
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(f"{name}_module", scripts_dir / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


task_engine = _load("task_engine")
task_phases = _load("task_phases")
task_runner = _load("task_runner")


def make_task(root: Path, name: str = "0001-goal") -> Path:
    task_dir = root / name
    (task_dir / ".runner").mkdir(parents=True)
    (task_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (task_dir / "task_contract.json").write_text(
        json.dumps({"version": 1, "source": "test"}), encoding="utf-8"
    )
    (task_dir / "task.md").write_text(
        f'---\nid: 1\nslug: "{name}"\ntitle: "t"\ndate: 2026-08-10\nstatus: "in_progress"\n---\n# t\n',
        encoding="utf-8",
    )
    return task_dir


class ActualityTests(unittest.TestCase):
    def test_freshness_is_measured_from_the_filesystem(self) -> None:
        """A child's own `updated_at` is a claim; the mtime is an observation.

        A stalled child can keep a fresh `updated_at` in a file it wrote before
        it stalled, so the timestamp inside the file cannot decide freshness.
        """
        with tempfile.TemporaryDirectory() as raw:
            task_dir = make_task(Path(raw))
            (task_dir / "progress.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "activity": "working",
                        "updated_at": "2099-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            old = time.time() - 10_000
            os.utime(task_dir / "progress.json", (old, old))

            report = task_engine.actuality(task_dir)
            self.assertEqual(report["measured_from"], "filesystem")
            self.assertTrue(report["stale"])
            self.assertGreater(report["age_seconds"], 9_000)

    def test_a_recently_touched_task_is_not_stale(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = make_task(Path(raw))
            (task_dir / "trace.md").write_text("# Trace\n", encoding="utf-8")
            self.assertFalse(task_engine.actuality(task_dir)["stale"])

    def test_a_task_nothing_has_ever_written_is_stale_and_says_why(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw) / "empty"
            task_dir.mkdir()
            report = task_engine.actuality(task_dir)
            self.assertTrue(report["stale"])
            self.assertIn("has ever been written", report["reason"])

    def test_the_staleness_threshold_comes_from_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = make_task(Path(raw))
            (task_dir / "trace.md").write_text("# Trace\n", encoding="utf-8")
            old = time.time() - 60
            os.utime(task_dir / "trace.md", (old, old))
            with mock.patch.dict(
                os.environ, {task_engine.STALE_AFTER_SECONDS_ENV: "30"}
            ):
                self.assertTrue(task_engine.actuality(task_dir)["stale"])
            with mock.patch.dict(
                os.environ, {task_engine.STALE_AFTER_SECONDS_ENV: "600"}
            ):
                self.assertFalse(task_engine.actuality(task_dir)["stale"])

    def test_a_nonsense_threshold_is_refused_rather_than_ignored(self) -> None:
        with mock.patch.dict(os.environ, {task_engine.STALE_AFTER_SECONDS_ENV: "soon"}):
            with self.assertRaises(SystemExit):
                task_engine.stale_after_seconds()
        with mock.patch.dict(os.environ, {task_engine.STALE_AFTER_SECONDS_ENV: "0"}):
            with self.assertRaises(SystemExit):
                task_engine.stale_after_seconds()

    def test_the_default_applies_when_nothing_is_configured(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                task_engine.stale_after_seconds(), task_engine.DEFAULT_STALE_AFTER_SECONDS
            )


class StateDocumentTests(unittest.TestCase):
    def test_one_document_carries_identity_phase_and_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = make_task(Path(raw))
            task_phases.record_phase(task_dir, task_phases.IMPLEMENTATION)
            task_phases.record_phase(task_dir, task_phases.REVIEW)
            document = task_engine.state(task_dir)
            self.assertEqual(document["phase"], "review")
            self.assertEqual(document["phase_sequence"], ["implementation", "review"])
            self.assertFalse(document["completion"]["ready"])
            self.assertIn("reason", document["completion"])
            self.assertFalse(document["supervision"]["live"])
            self.assertIn("actuality", document)

    def test_the_phase_view_shows_how_the_task_got_here(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = make_task(Path(raw))
            for phase in ("implementation", "review", "rework", "review", "completed"):
                task_phases.record_phase(task_dir, phase)
            view = task_engine.phases(task_dir)
            self.assertEqual(
                view["sequence"], ["implementation", "review", "rework", "review", "completed"]
            )
            self.assertEqual(len(view["history"]), 5)


class SupervisionEvidenceTests(unittest.TestCase):
    """A host that cannot produce a kernel process identity still has live runs.

    `process_identity` reads `/proc`, which macOS does not have. Requiring proof
    of identity there reported every live run as dead, and the caller acting on
    that is the one refusing a second run for the same task — so the hosts that
    cannot detect a concurrent run were the hosts that admitted one.
    """

    def test_a_live_run_stays_visible_without_process_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = make_task(Path(raw))
            task_runner.write_json(
                task_runner.runner_meta_path(task_dir),
                {"pid": os.getpid(), "watcher_pid": os.getpid()},
            )
            with mock.patch.object(task_runner, "process_identity", return_value=None):
                live = task_runner.live_run_processes(task_dir)
            self.assertEqual(
                [item["evidence"] for item in live],
                ["pid_only_no_process_identity", "pid_only_no_process_identity"],
            )
            with mock.patch.object(task_runner, "process_identity", return_value=None):
                with self.assertRaises(SystemExit):
                    task_runner.require_no_live_run(task_dir)

    def test_an_identity_mismatch_is_still_a_dead_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = make_task(Path(raw))
            task_runner.write_json(
                task_runner.runner_meta_path(task_dir),
                {"pid": os.getpid(), "process_identity": "recorded-elsewhere"},
            )
            self.assertEqual(task_runner.live_run_processes(task_dir), [])

    def test_a_child_that_wrote_its_own_terminal_status_still_closes_the_phase(self) -> None:
        """A standard child maintains `status.json` and knows nothing of phases.

        A live run found this: the task reached `completed` and the phase stayed
        at `implementation`, so the task's own history never showed it finished.
        """
        with tempfile.TemporaryDirectory() as raw:
            task_dir = make_task(Path(raw))
            task_phases.record_phase(task_dir, task_phases.IMPLEMENTATION)
            task_runner.write_json(
                task_runner.status_path(task_dir),
                {"state": "completed", "current_step": "child wrote this itself"},
            )
            task_runner.finalize_child_lifecycle(task_dir, "standard", "claude", 0)
            self.assertEqual(task_phases.current_phase(task_dir), "completed")
            self.assertEqual(
                task_phases.phase_sequence(task_dir), ["implementation", "completed"]
            )
            status = json.loads(
                task_runner.status_path(task_dir).read_text(encoding="utf-8")
            )
            self.assertEqual(status["phase"], "completed")
            # The state the child recorded is its own; nothing here rewrites it.
            self.assertEqual(status["current_step"], "child wrote this itself")

    def test_a_dry_run_holds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = make_task(Path(raw))
            task_runner.write_json(
                task_runner.runner_meta_path(task_dir),
                {"pid": os.getpid(), "dry_run": True},
            )
            self.assertEqual(task_runner.live_run_processes(task_dir), [])


if __name__ == "__main__":
    unittest.main()
