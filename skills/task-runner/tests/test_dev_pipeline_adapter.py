"""Projection of neutral dev-pipeline lifecycle events into task artifacts."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

import pytest

pytest.importorskip(
    "dev_pipeline",
    reason="the dev-pipeline workflow needs the standalone dev-pipeline package",
)

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import dev_pipeline_adapter  # noqa: E402


def event(
    kind: str,
    sequence: int,
    payload: dict | None = None,
    *,
    task_ref: str = "001-example",
    attempt_id: str = "attempt_a",
    run_id: str = "run_a",
    event_id: str | None = None,
    timestamp: str | None = None,
) -> dict:
    return {
        "schema_version": "1.0",
        "event_id": event_id or f"event_{kind}_{sequence}",
        "sequence": sequence,
        "timestamp": timestamp or f"2026-07-27T12:{sequence:02d}:00+00:00",
        "task_ref": task_ref,
        "attempt_id": attempt_id,
        "run_id": run_id,
        "kind": kind,
        "payload": payload or {},
    }


ATTEMPT_STARTED = {"attempt_origin": "new_owner_session", "repository": "/repo"}
RUN_STARTED = {"run_operation": "native_session_start"}


def make_task(tmp_path: Path, name: str = "001-example", status: str = "planned") -> Path:
    task_dir = tmp_path / name
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text(
        f"# Example\n\n## Summary\nWork\n\n## Status\n{status}\n", encoding="utf-8"
    )
    (task_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
    return task_dir


def status_of(task_dir: Path) -> dict:
    return json.loads((task_dir / "status.json").read_text(encoding="utf-8"))


def progress_of(task_dir: Path) -> dict:
    return json.loads((task_dir / "progress.json").read_text(encoding="utf-8"))


def trace_of(task_dir: Path) -> str:
    return (task_dir / "trace.md").read_text(encoding="utf-8")


class ProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(__file__).resolve().parent / "_tmp_projection"
        if self.tmp.exists():
            subprocess.run(["rm", "-rf", str(self.tmp)], check=True)
        self.tmp.mkdir()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])

    def test_a_full_lifecycle_lands_in_all_three_artifacts(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)

        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        self.assertEqual(status_of(task_dir)["state"], "running")
        self.assertEqual(
            status_of(task_dir)["current_step"], "Dev-pipeline owner attempt started"
        )
        # Startup is not an outcome, so nothing is claimed as achieved yet.
        self.assertNotIn("recent_outcome", progress_of(task_dir))
        self.assertEqual(progress_of(task_dir)["schema_version"], 1)

        projector.consume(event("run_started", 2, RUN_STARTED))
        projector.consume(event("process_started", 3, {"pid": 4242}))
        projector.consume(
            event("checkpoint_completed", 4, {"checkpoint": "scenario", "next_step": "architecture"})
        )

        status = status_of(task_dir)
        self.assertEqual(status["state"], "running")
        self.assertIn("scenario", status["current_step"])
        self.assertEqual(status["dev_pipeline"]["last_sequence"], 4)

        progress = progress_of(task_dir)
        self.assertIn("scenario", progress["activity"])
        self.assertEqual(progress["recent_outcome"], "Checkpoint scenario completed")
        # A lifecycle event knows of no bounds, so it must never publish counts.
        for field in ("completed", "total", "unit"):
            self.assertNotIn(field, progress)

        trace = trace_of(task_dir)
        for kind in ("attempt_started", "run_started", "process_started", "checkpoint_completed"):
            self.assertIn(f"`{kind}`", trace)

    def test_a_bookkeeping_event_keeps_the_last_real_outcome(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("run_started", 2, RUN_STARTED))
        projector.consume(
            event("increment_completed", 3, {"increment": "walking_skeleton", "next_step": "api"})
        )
        self.assertEqual(
            progress_of(task_dir)["recent_outcome"], "Increment walking_skeleton completed"
        )

        projector.consume(event("process_started", 4, {"pid": 11}))
        progress = progress_of(task_dir)
        self.assertEqual(progress["activity"], "Owner runtime process running")
        self.assertEqual(progress["recent_outcome"], "Increment walking_skeleton completed")

    def test_owner_published_progress_is_never_overwritten(self) -> None:
        task_dir = make_task(self.tmp)
        owner_progress = {
            "schema_version": 1,
            "activity": "Migrating call sites in module 3",
            "updated_at": "2026-07-27T13:00:00+00:00",
            "completed": 3,
            "total": 8,
            "unit": "modules",
        }
        (task_dir / "progress.json").write_text(json.dumps(owner_progress), encoding="utf-8")

        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))

        self.assertEqual(progress_of(task_dir), owner_progress)
        # The other two artifacts are the adapter's own, so they still move.
        self.assertEqual(status_of(task_dir)["state"], "running")

    def test_the_adapter_replaces_its_own_earlier_progress(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        self.assertEqual(progress_of(task_dir)["source"], "dev-pipeline-adapter")
        projector.consume(event("run_started", 2, RUN_STARTED))
        self.assertIn("native_session_start", progress_of(task_dir)["activity"])

    def test_a_blocker_marks_the_task_blocked_with_its_question(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(
            event(
                "blocked_on_user_decision",
                2,
                {
                    "question": "Which destination receives lifecycle notifications?",
                    "artifact": "task_contract.json",
                    "options": [{"label": "configured id", "consequence": "delivery is bound"}],
                },
            )
        )
        status = status_of(task_dir)
        self.assertEqual(status["state"], "blocked")
        self.assertIn("Which destination", status["current_step"])
        self.assertIn("Blocked on:", progress_of(task_dir)["recent_outcome"])

    def test_a_failure_is_terminal(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("attempt_failed", 2, {"reason": "Codex exited with code 1"}))
        self.assertEqual(status_of(task_dir)["state"], "failed")


class CompletionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(__file__).resolve().parent / "_tmp_completion"
        if self.tmp.exists():
            subprocess.run(["rm", "-rf", str(self.tmp)], check=True)
        self.tmp.mkdir()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])

    def _complete(self, task_dir: Path) -> dict:
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("attempt_completed", 2))
        return status_of(task_dir)

    def test_completion_is_rejected_while_the_task_is_not_done(self) -> None:
        status = self._complete(make_task(self.tmp, status="planned"))
        self.assertEqual(status["state"], "blocked")
        self.assertIn("status is not done", status["current_step"])

    def test_completion_is_rejected_while_criteria_are_unchecked(self) -> None:
        task_dir = make_task(self.tmp, status="done")
        (task_dir / "task.md").write_text(
            "# Example\n\n## Acceptance Criteria\n- [x] first\n- [ ] second\n\n## Status\ndone\n",
            encoding="utf-8",
        )
        status = self._complete(task_dir)
        self.assertEqual(status["state"], "blocked")
        self.assertIn("unchecked acceptance criteria", status["current_step"])

    def test_completion_is_rejected_while_a_required_gate_is_unrecorded(self) -> None:
        task_dir = make_task(self.tmp, status="done")
        (task_dir / "task_contract.json").write_text(
            json.dumps(
                {"required_live_evidence": [{"id": "live-smoke"}, {"id": "render-check"}]}
            ),
            encoding="utf-8",
        )
        (task_dir / "verification.md").write_text(
            "# Verification\n\n## live-smoke\nran\n", encoding="utf-8"
        )
        status = self._complete(task_dir)
        self.assertEqual(status["state"], "blocked")
        self.assertIn("render-check", status["current_step"])

    def test_completion_is_accepted_once_the_contract_is_satisfied(self) -> None:
        task_dir = make_task(self.tmp, status="done")
        (task_dir / "task_contract.json").write_text(
            json.dumps({"required_live_evidence": [{"id": "live-smoke"}]}), encoding="utf-8"
        )
        (task_dir / "verification.md").write_text(
            "# Verification\n\n## live-smoke\nran against the real entrypoint\n", encoding="utf-8"
        )
        status = self._complete(task_dir)
        self.assertEqual(status["state"], "completed")


class EventOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(__file__).resolve().parent / "_tmp_ordering"
        if self.tmp.exists():
            subprocess.run(["rm", "-rf", str(self.tmp)], check=True)
        self.tmp.mkdir()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])

    def test_a_repeated_event_is_not_projected_twice(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        self.assertFalse(projector.consume(event("attempt_started", 1, ATTEMPT_STARTED)))
        self.assertEqual(trace_of(task_dir).count("`attempt_started`"), 1)

    def test_a_gap_in_the_sequence_is_refused(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        with self.assertRaises(ValueError):
            projector.consume(event("process_started", 3, {"pid": 1}))

    def test_an_event_for_another_task_is_refused(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        with self.assertRaises(ValueError):
            projector.consume(event("attempt_started", 1, ATTEMPT_STARTED, task_ref="002-other"))

    def test_an_event_from_a_foreign_run_is_refused(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        with self.assertRaises(ValueError):
            projector.consume(event("process_started", 2, {"pid": 1}, run_id="run_b"))

    def test_a_new_attempt_starts_a_fresh_cursor(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("run_started", 2, RUN_STARTED))
        self.assertTrue(
            projector.consume(
                event(
                    "attempt_started",
                    1,
                    ATTEMPT_STARTED,
                    attempt_id="attempt_b",
                    run_id="run_b",
                    event_id="event_retry_1",
                )
            )
        )
        self.assertEqual(status_of(task_dir)["dev_pipeline"]["attempt_id"], "attempt_b")

    def test_projection_resumes_after_an_interrupted_write(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        # Simulate a crash between recording an event and projecting it.
        dev_pipeline_adapter.append_jsonl(
            projector.event_path, event("run_started", 2, RUN_STARTED)
        )
        recovered = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        self.assertIsNotNone(recovered)
        self.assertIn("`run_started`", trace_of(task_dir))
        self.assertEqual(status_of(task_dir)["dev_pipeline"]["last_sequence"], 2)


class CoreCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(__file__).resolve().parent / "_tmp_command"
        if self.tmp.exists():
            subprocess.run(["rm", "-rf", str(self.tmp)], check=True)
        self.tmp.mkdir()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])

    def _args(self, task_dir: Path, **overrides) -> object:
        import argparse

        defaults = dict(
            task_dir=task_dir,
            repo=task_dir.parent,
            dev_pipeline_bin="dev-pipeline",
            operation="start",
            state_dir=None,
            previous_state_dir=None,
            retry_reason=None,
            model=None,
            owner_runtime="codex",
            sandbox="workspace-write",
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_the_command_uses_the_public_owner_interface(self) -> None:
        task_dir = make_task(self.tmp)
        instruction = dev_pipeline_adapter.prepare_owner_instruction(task_dir)
        command = dev_pipeline_adapter.build_core_command(
            self._args(task_dir), task_dir, instruction
        )
        self.assertEqual(command[:3], ["dev-pipeline", "owner", "start"])
        self.assertIn("--task-ref", command)
        self.assertIn(task_dir.name, command)
        self.assertIn("--owner-runtime", command)
        self.assertIn("--instruction-file", command)
        self.assertIn(str(instruction), command)

    def test_a_task_contract_is_passed_as_an_artifact(self) -> None:
        task_dir = make_task(self.tmp)
        (task_dir / "task_contract.json").write_text("{}", encoding="utf-8")
        instruction = dev_pipeline_adapter.prepare_owner_instruction(task_dir)
        command = dev_pipeline_adapter.build_core_command(
            self._args(task_dir), task_dir, instruction
        )
        self.assertIn("--artifact", command)
        self.assertIn(str(task_dir / "task_contract.json"), command)

    def test_core_state_must_stay_inside_the_task(self) -> None:
        task_dir = make_task(self.tmp)
        with self.assertRaises(ValueError):
            dev_pipeline_adapter.core_state_dir(task_dir, Path("/tmp/elsewhere"))

    def test_the_owner_instruction_carries_the_canonical_request(self) -> None:
        task_dir = make_task(self.tmp)
        instruction = dev_pipeline_adapter.prepare_owner_instruction(task_dir)
        text = instruction.read_text(encoding="utf-8")
        self.assertIn("## Canonical task request", text)
        self.assertIn("# Example", text)
        self.assertIn("schema_version: 1", text)
        self.assertIn("Never infer or invent a total", text)

    def test_the_adapter_accepts_no_delivery_configuration(self) -> None:
        # Delivery belongs to whoever owns a transport. The template cannot bind
        # a recipient or promise at-most-once delivery, so it refuses to be told
        # about one at all.
        task_dir = make_task(self.tmp)
        for option in ("--destination", "--chat-id", "--notification-test-context"):
            with self.subTest(option=option):
                with self.assertRaises(SystemExit):
                    dev_pipeline_adapter.main(
                        [str(task_dir), "--repo", str(task_dir.parent), option, "value"]
                    )

    def test_the_delivery_seam_is_inert_and_records_nothing(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        trace = trace_of(task_dir)
        self.assertIn("`attempt_started`", trace)
        self.assertNotIn("notification", trace.lower())
        self.assertEqual(
            sorted(path.name for path in (task_dir / "dev-pipeline").iterdir()),
            ["adapter-cursor.json", "projected-events.jsonl", "projection-cursor.json"],
        )


if __name__ == "__main__":
    unittest.main()
