import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def _load_task_runner_module():
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    module_path = scripts_dir / "task_runner.py"
    sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location("task_runner_lifecycle_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


task_runner = _load_task_runner_module()


class ChildLifecycleTests(unittest.TestCase):
    def _task_dir(self, tmp: str, state: str | None) -> Path:
        task_dir = Path(tmp) / "001-example"
        task_dir.mkdir()
        if state is not None:
            task_runner.write_status(task_dir, state, "step")
        return task_dir

    def test_non_zero_exit_replaces_a_stale_running_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp, "running")
            task_runner.finalize_child_lifecycle(task_dir, "standard", "codex", 3)
            status = json.loads(task_runner.status_path(task_dir).read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["exit_code"], 3)
        self.assertEqual(status["runner"], "codex")

    def test_failure_is_recorded_in_the_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp, "running")
            task_runner.finalize_child_lifecycle(task_dir, "standard", "claude", 1)
            trace = task_runner.trace_path(task_dir).read_text(encoding="utf-8")
        self.assertIn("exited with code 1", trace)

    def test_missing_status_still_becomes_a_terminal_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp, None)
            task_runner.finalize_child_lifecycle(task_dir, "standard", "codex", 127)
            status = json.loads(task_runner.status_path(task_dir).read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "failed")

    def test_clean_exit_leaves_the_status_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp, "running")
            task_runner.finalize_child_lifecycle(task_dir, "standard", "codex", 0)
            status = json.loads(task_runner.status_path(task_dir).read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "running")

    def test_a_terminal_state_written_by_the_child_is_never_overwritten(self) -> None:
        for state in ("completed", "blocked", "failed"):
            with self.subTest(state=state):
                with tempfile.TemporaryDirectory() as tmp:
                    task_dir = self._task_dir(tmp, state)
                    task_runner.finalize_child_lifecycle(task_dir, "standard", "codex", 5)
                    status = json.loads(
                        task_runner.status_path(task_dir).read_text(encoding="utf-8")
                    )
                self.assertEqual(status["state"], state)


class StructuredProgressTests(unittest.TestCase):
    def _write(self, tmp: str, payload) -> Path:
        task_dir = Path(tmp) / "001-example"
        task_dir.mkdir()
        task_runner.progress_path(task_dir).write_text(json.dumps(payload), encoding="utf-8")
        return task_dir

    def test_missing_progress_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "001-example"
            task_dir.mkdir()
            self.assertIsNone(task_runner.structured_progress(task_dir))

    def test_wrong_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._write(tmp, {"version": 2, "activity": "working"})
            self.assertIsNone(task_runner.structured_progress(task_dir))

    def test_blank_activity_makes_the_file_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._write(tmp, {"version": 1, "activity": "   "})
            self.assertIsNone(task_runner.structured_progress(task_dir))

    def test_complete_counts_produce_a_percentage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._write(
                tmp,
                {
                    "version": 1,
                    "activity": "Migrating module 3",
                    "updated_at": "2026-07-27T14:00:00+00:00",
                    "recent_outcome": "Module 2 migrated",
                    "completed": 3,
                    "total": 8,
                    "unit": "modules",
                },
            )
            progress = task_runner.structured_progress(task_dir)
        self.assertEqual(progress["percent"], 38)
        self.assertEqual(progress["unit"], "modules")
        self.assertEqual(progress["recent_outcome"], "Module 2 migrated")

    def test_partial_counts_are_rejected_rather_than_completed_by_inference(self) -> None:
        for payload in (
            {"version": 1, "activity": "Working", "completed": 3},
            {"version": 1, "activity": "Working", "total": 8},
            {"version": 1, "activity": "Working", "completed": 3, "total": 8},
        ):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    task_dir = self._write(tmp, payload)
                    progress = task_runner.structured_progress(task_dir)
                self.assertEqual(progress["counts_rejected"], "incomplete completed/total/unit")
                self.assertNotIn("percent", progress)

    def test_incoherent_counts_are_rejected(self) -> None:
        for completed, total, unit in ((9, 8, "modules"), (-1, 8, "modules"), (1, 0, "modules"), (1, 8, "  ")):
            with self.subTest(completed=completed, total=total, unit=unit):
                with tempfile.TemporaryDirectory() as tmp:
                    task_dir = self._write(
                        tmp,
                        {
                            "version": 1,
                            "activity": "Working",
                            "completed": completed,
                            "total": total,
                            "unit": unit,
                        },
                    )
                    progress = task_runner.structured_progress(task_dir)
                self.assertEqual(progress["counts_rejected"], "incoherent completed/total/unit")
                self.assertNotIn("percent", progress)

    def test_activity_only_progress_is_still_useful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._write(
                tmp,
                {
                    "version": 1,
                    "activity": "Reading the migration plan",
                    "updated_at": "2026-07-27T14:00:00+00:00",
                },
            )
            progress = task_runner.structured_progress(task_dir)
        self.assertEqual(progress["activity"], "Reading the migration plan")
        self.assertNotIn("counts_rejected", progress)


class ChildPromptContractTests(unittest.TestCase):
    def _prompt(self, tmp: str) -> str:
        task_dir = Path(tmp) / "001-example"
        task_dir.mkdir()
        return task_runner.build_child_prompt(task_dir)

    def test_prompt_carries_the_deliverables_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._prompt(tmp)
        self.assertIn("deliverables/manifest.json", prompt)
        self.assertIn("under a `deliverables` array", prompt)

    def test_prompt_keeps_internal_records_out_of_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._prompt(tmp)
        self.assertIn("none of them substitutes for a", prompt)

    def test_prompt_requires_rendered_visual_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._prompt(tmp)
        self.assertIn("depends on visual rendering", prompt)
        self.assertIn("not sufficient", prompt)

    def test_prompt_points_at_durable_user_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._prompt(tmp)
        self.assertIn("USER_PREFERENCES.md", prompt)
        self.assertIn("override it", prompt)

    def test_prompt_describes_the_progress_contract_without_inventing_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._prompt(tmp)
        self.assertIn("progress.json", prompt)
        self.assertIn("never invent a total", prompt)

    def test_prompt_has_no_hardcoded_absolute_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._prompt(tmp)
        self.assertIn(str(task_runner.workspace_root()), prompt)


if __name__ == "__main__":
    unittest.main()
