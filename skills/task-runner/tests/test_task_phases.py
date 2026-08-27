"""One user goal keeps one task number, and review and rework are its phases."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def _load(name: str):
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(f"{name}_module", scripts_dir / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


task_phases = _load("task_phases")


def _event(kind: str, **payload):
    return {
        "schema_version": "1.0",
        "event_id": f"event-{kind}",
        "sequence": 1,
        "timestamp": "2026-08-10T20:00:00+00:00",
        "task_ref": "1063-single-engine",
        "attempt_id": "attempt-1",
        "run_id": "run-1",
        "kind": kind,
        "payload": payload,
    }


class PhaseRecordTests(unittest.TestCase):
    def test_the_accepted_sequence_lives_in_one_task_directory(self) -> None:
        """implementation -> review -> rework -> review -> completed, one number.

        This is the user-visible effect the whole change exists for: the goal
        never acquires a second task number when it reaches review, and never a
        third when the review sends it back.
        """
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            for phase in (
                task_phases.IMPLEMENTATION,
                task_phases.REVIEW,
                task_phases.REWORK,
                task_phases.REVIEW,
                task_phases.COMPLETED,
            ):
                task_phases.record_phase(task_dir, phase)
            self.assertEqual(
                task_phases.phase_sequence(task_dir),
                ["implementation", "review", "rework", "review", "completed"],
            )
            self.assertEqual(task_phases.current_phase(task_dir), "completed")
            self.assertEqual(len(list(task_dir.iterdir())), 1)

    def test_re_entering_the_same_phase_does_not_append(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            for _ in range(5):
                task_phases.record_phase(task_dir, task_phases.IMPLEMENTATION)
            self.assertEqual(task_phases.phase_sequence(task_dir), ["implementation"])

    def test_an_absent_record_reads_as_planned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(task_phases.current_phase(Path(raw)), "planned")

    def test_an_unreadable_record_does_not_invent_a_phase(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            task_phases.phases_path(task_dir).write_text("{ truncated", encoding="utf-8")
            self.assertEqual(task_phases.current_phase(task_dir), "planned")

    def test_an_unknown_phase_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ValueError):
                task_phases.record_phase(Path(raw), "almost_done")

    def test_the_cause_of_each_transition_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            task_phases.record_phase(
                task_dir, task_phases.REVIEW, cause={"source": "dev-pipeline", "kind": "review_started"}
            )
            history = task_phases.phase_history(task_dir)
            self.assertEqual(history[0]["cause"]["kind"], "review_started")


class EventPhaseTests(unittest.TestCase):
    def test_review_events_are_the_review_phase(self) -> None:
        for kind in ("increment_ready_for_review", "review_started", "review_approved"):
            with self.subTest(kind=kind):
                self.assertEqual(task_phases.phase_for_event(_event(kind)), "review")

    def test_rework_required_is_the_rework_phase(self) -> None:
        self.assertEqual(
            task_phases.phase_for_event(_event("review_rework_required")), "rework"
        )

    def test_an_unobtainable_review_stops_rather_than_looping(self) -> None:
        """A reviewer that is unavailable or refused is not a rework instruction.

        Reading either as rework would send the work back around the loop and
        let it close having never been reviewed.
        """
        for kind in ("review_waiting", "review_refused"):
            with self.subTest(kind=kind):
                self.assertEqual(task_phases.phase_for_event(_event(kind)), "blocked")

    def test_machinery_events_leave_the_phase_alone(self) -> None:
        for kind in ("run_started", "process_started", "run_waiting_for_quota", "run_failed"):
            with self.subTest(kind=kind):
                self.assertIsNone(task_phases.phase_for_event(_event(kind)))

    def test_work_during_rework_stays_rework(self) -> None:
        """The core emits the same checkpoint event before and after a review.

        Reading it as implementation both times would erase the rework phase at
        exactly the moment the rework is being done.
        """
        for kind in ("attempt_started", "checkpoint_completed", "increment_completed"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    task_phases.phase_for_event(_event(kind), task_phases.REWORK), "rework"
                )
                self.assertEqual(
                    task_phases.phase_for_event(_event(kind), task_phases.REVIEW),
                    "implementation",
                )

    def test_an_unknown_future_event_kind_does_not_invent_a_phase(self) -> None:
        self.assertIsNone(task_phases.phase_for_event(_event("teleported")))


class StandardParityTests(unittest.TestCase):
    """A `standard` run emits no events and must still show the same phases."""

    def test_a_review_run_enters_the_review_phase(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(
                task_phases.phase_for_standard_start(Path(raw), require_review_verdict=True),
                "review",
            )

    def test_statement_review_does_not_enter_engineering_review_phase(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self.assertIsNone(
                task_phases.phase_for_standard_start(
                    task_dir,
                    require_review_verdict=True,
                    review_kind="statement",
                )
            )
            self.assertEqual(task_phases.current_phase(task_dir), "planned")

    def test_the_first_run_is_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(
                task_phases.phase_for_standard_start(Path(raw), require_review_verdict=False),
                "implementation",
            )

    def test_work_after_a_review_is_rework_under_the_same_number(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            task_phases.record_phase(task_dir, task_phases.IMPLEMENTATION)
            task_phases.record_phase(task_dir, task_phases.REVIEW)
            task_phases.record_phase(task_dir, task_phases.BLOCKED)
            self.assertEqual(
                task_phases.phase_for_standard_start(task_dir, require_review_verdict=False),
                "rework",
            )

    def test_a_terminal_state_maps_to_a_phase(self) -> None:
        self.assertEqual(task_phases.phase_for_state("completed"), "completed")
        self.assertEqual(task_phases.phase_for_state("failed"), "failed")
        self.assertEqual(task_phases.phase_for_state("blocked"), "blocked")
        self.assertIsNone(task_phases.phase_for_state("running"))

    def test_statement_review_finish_does_not_end_the_task_phase(self) -> None:
        self.assertIsNone(task_phases.phase_for_state("statement_review_finished"))

    def test_the_record_is_replaced_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            task_phases.record_phase(task_dir, task_phases.REVIEW)
            payload = json.loads(task_phases.phases_path(task_dir).read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertFalse(list(task_dir.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
