"""Projection of neutral dev-pipeline lifecycle events into task artifacts."""

import json
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip(
    "dev_pipeline",
    reason="the dev-pipeline workflow needs the standalone dev-pipeline package",
)

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import dev_pipeline_adapter  # noqa: E402
import application_adapter  # noqa: E402

from dev_pipeline.events import EVENT_KINDS  # noqa: E402

# The review and rework phases exist only where the core announces them. An
# older pinned core emits no such event, and the projection degrades to the
# phases it can see rather than breaking — but a test cannot synthesize an event
# the installed core refuses to validate, so it skips instead of failing.
CORE_HAS_REVIEW_EVENTS = "review_started" in EVENT_KINDS
requires_review_events = unittest.skipUnless(
    CORE_HAS_REVIEW_EVENTS,
    "the installed dev-pipeline core emits no provider-neutral review events",
)


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
    frontmatter_status = "completed" if status == "done" else status
    (task_dir / "task.md").write_text(
        "---\n"
        f"id: 1\nslug: example\ntitle: Example\ndate: 2026-07-27\nstatus: {frontmatter_status}\n"
        "projects: []\ntrips: []\n---\n# Example\n\n## Summary\nWork\n",
        encoding="utf-8",
    )
    (task_dir / "plan.md").write_text("# Plan\n\n1. [completed] Work\n", encoding="utf-8")
    (task_dir / "task_contract.json").write_text(
        json.dumps({"version": 1}), encoding="utf-8"
    )
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
        self.assertIn("frontmatter status", status["current_step"])

    def test_completion_is_rejected_while_criteria_are_unchecked(self) -> None:
        task_dir = make_task(self.tmp, status="done")
        (task_dir / "plan.md").write_text(
            "# Plan\n\n1. [completed] first\n2. [pending] second\n", encoding="utf-8"
        )
        status = self._complete(task_dir)
        self.assertEqual(status["state"], "blocked")
        self.assertIn("unfinished steps", status["current_step"])

    def test_completion_is_rejected_while_a_required_gate_is_unrecorded(self) -> None:
        task_dir = make_task(self.tmp, status="done")
        (task_dir / "task_contract.json").write_text(
            json.dumps(
                {"required_live_evidence": [{"id": "live-smoke"}, {"id": "render-check"}]}
            ),
            encoding="utf-8",
        )
        (task_dir / "verification.md").write_text(
            "# Verification\n\n## live-smoke\n- Result: **OK**\n- Evidence: ran\n",
            encoding="utf-8",
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
            "# Verification\n\n## live-smoke\n- Result: **OK**\n"
            "- Evidence: ran against the real entrypoint\n",
            encoding="utf-8",
        )
        status = self._complete(task_dir)
        self.assertEqual(status["state"], "completed")

    def test_declared_application_evidence_is_prepared_before_full_predicate(self) -> None:
        task_dir = make_task(self.tmp, status="done")
        (task_dir / "task_contract.json").write_text(
            json.dumps({"required_live_evidence": [{"id": "delivery-receipt"}]}),
            encoding="utf-8",
        )

        class PreparingApplication(application_adapter.DefaultApplicationV1):
            completion_preparation_evidence_ids = ("delivery-receipt",)

            def prepare_completion(self, request):
                self.request = request
                with (request.task_dir / "verification.md").open("a", encoding="utf-8") as handle:
                    handle.write(
                        "# Verification\n\n## delivery-receipt\n"
                        "- Result: **PASS**\n- Evidence: delivered first\n"
                    )
                return application_adapter.DeliveryResultV1(True, "receipt persisted")

        module = types.ModuleType("preparing_application")
        module.adapter = PreparingApplication()
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        projector = dev_pipeline_adapter.TaskArtifactProjector(
            task_dir, application=f"{module.__name__}:adapter", destination="bound"
        )
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("attempt_completed", 2))
        self.assertEqual(status_of(task_dir)["state"], "completed")
        self.assertIn("receipt persisted", trace_of(task_dir))
        self.assertEqual(module.adapter.request.evidence_ids, ("delivery-receipt",))

    def test_successful_preparation_closes_in_progress_task_through_metadata_owner(self) -> None:
        task_dir = make_task(self.tmp, status="in_progress")
        (task_dir / "task_contract.json").write_text(
            json.dumps({"required_live_evidence": [{"id": "delivery-receipt"}]}),
            encoding="utf-8",
        )

        class PreparingApplication(application_adapter.DefaultApplicationV1):
            completion_preparation_evidence_ids = ("delivery-receipt",)

            def prepare_completion(self, request):
                with (request.task_dir / "verification.md").open("a", encoding="utf-8") as handle:
                    handle.write(
                        "# Verification\n\n## delivery-receipt\n"
                        "- Result: **PASS**\n- Evidence: delivered first\n"
                    )
                return application_adapter.DeliveryResultV1(True, "receipt persisted")

        module = types.ModuleType("closing_preparing_application")
        module.adapter = PreparingApplication()
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)

        def close_metadata(path):
            task_md = path / "task.md"
            task_md.write_text(
                task_md.read_text(encoding="utf-8").replace(
                    "status: in_progress", "status: completed"
                ),
                encoding="utf-8",
            )

        projector = dev_pipeline_adapter.TaskArtifactProjector(
            task_dir, application=f"{module.__name__}:adapter", destination="bound"
        )
        with mock.patch.object(
            dev_pipeline_adapter,
            "complete_task_metadata",
            side_effect=close_metadata,
        ) as complete:
            projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
            projector.consume(event("attempt_completed", 2))
        complete.assert_called_once_with(task_dir)
        self.assertEqual(status_of(task_dir)["state"], "completed")

    def test_metadata_owner_failure_is_projected_instead_of_crashing(self) -> None:
        task_dir = make_task(self.tmp, status="in_progress")
        (task_dir / "task_contract.json").write_text(
            json.dumps({"required_live_evidence": [{"id": "delivery-receipt"}]}),
            encoding="utf-8",
        )

        class PreparingApplication(application_adapter.DefaultApplicationV1):
            completion_preparation_evidence_ids = ("delivery-receipt",)

            def prepare_completion(self, request):
                with (request.task_dir / "verification.md").open(
                    "a", encoding="utf-8"
                ) as handle:
                    handle.write(
                        "# Verification\n\n## delivery-receipt\n"
                        "- Result: **PASS**\n- Evidence: delivered first\n"
                    )
                return application_adapter.DeliveryResultV1(
                    True, "receipt persisted"
                )

        module = types.ModuleType("failing_metadata_owner_application")
        module.adapter = PreparingApplication()
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        projector = dev_pipeline_adapter.TaskArtifactProjector(
            task_dir, application=f"{module.__name__}:adapter", destination="bound"
        )
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        with mock.patch.object(
            dev_pipeline_adapter,
            "complete_task_metadata",
            side_effect=RuntimeError("canonical owner unavailable"),
        ):
            projector.consume(event("attempt_completed", 2))

        status = status_of(task_dir)
        self.assertEqual(status["state"], "blocked")
        self.assertEqual(
            status["completion_refusal"]["reason"],
            "automatic completion finalization failed: canonical owner unavailable",
        )
        self.assertIs(
            status["completion_refusal"]["automatic_finalization"], True
        )
        projection = json.loads(
            (task_dir / "dev-pipeline" / "projection-cursor.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("event_attempt_completed_2", projection["projected_event_ids"])
        self.assertIn("canonical owner unavailable", trace_of(task_dir))

    def test_failed_preparation_leaves_in_progress_task_open(self) -> None:
        task_dir = make_task(self.tmp, status="in_progress")
        (task_dir / "task_contract.json").write_text(
            json.dumps({"required_live_evidence": [{"id": "delivery-receipt"}]}),
            encoding="utf-8",
        )

        class RefusingApplication(application_adapter.DefaultApplicationV1):
            completion_preparation_evidence_ids = ("delivery-receipt",)

            def prepare_completion(self, request):
                return application_adapter.DeliveryResultV1(False, "transport refused")

        module = types.ModuleType("refusing_preparing_application")
        module.adapter = RefusingApplication()
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        projector = dev_pipeline_adapter.TaskArtifactProjector(
            task_dir, application=f"{module.__name__}:adapter", destination="bound"
        )
        with mock.patch.object(dev_pipeline_adapter, "complete_task_metadata") as complete:
            projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
            projector.consume(event("attempt_completed", 2))
        complete.assert_not_called()
        self.assertEqual(status_of(task_dir)["state"], "blocked")
        self.assertEqual(
            status_of(task_dir)["completion_refusal"]["reason"],
            "application completion preparation refused: transport refused",
        )
        self.assertIs(
            status_of(task_dir)["completion_refusal"]["automatic_finalization"],
            True,
        )
        self.assertEqual(dev_pipeline_adapter.task_reference(task_dir), "1")
        self.assertIn("status: in_progress", (task_dir / "task.md").read_text(encoding="utf-8"))

    def test_preparation_precondition_reason_survives_full_status_projection(self) -> None:
        task_dir = make_task(self.tmp, status="in_progress")
        (task_dir / "plan.md").write_text(
            "# Plan\n\n1. [in_progress] Finish preparation\n", encoding="utf-8"
        )
        (task_dir / "task_contract.json").write_text(
            json.dumps({"required_live_evidence": [{"id": "delivery-receipt"}]}),
            encoding="utf-8",
        )

        class PreparingApplication(application_adapter.DefaultApplicationV1):
            completion_preparation_evidence_ids = ("delivery-receipt",)

            def prepare_completion(self, request):
                raise AssertionError("preparation must not run before the plan is ready")

        module = types.ModuleType("precondition_refusing_application")
        module.adapter = PreparingApplication()
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        projector = dev_pipeline_adapter.TaskArtifactProjector(
            task_dir, application=f"{module.__name__}:adapter", destination="bound"
        )

        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("attempt_completed", 2))

        refusal = status_of(task_dir)["completion_refusal"]
        self.assertEqual(refusal["reason"], "plan.md still has unfinished steps")
        self.assertIs(refusal["automatic_finalization"], True)
        self.assertIn("plan.md still has unfinished steps", status_of(task_dir)["current_step"])
        self.assertNotIn("frontmatter status", status_of(task_dir)["current_step"])

    def test_completion_preparation_does_not_run_while_other_gates_fail(self) -> None:
        task_dir = make_task(self.tmp, status="done")
        (task_dir / "task_contract.json").write_text(
            json.dumps(
                {"required_live_evidence": [
                    {"id": "delivery-receipt"}, {"id": "other-live-gate"}
                ]}
            ),
            encoding="utf-8",
        )

        class PreparingApplication(application_adapter.DefaultApplicationV1):
            completion_preparation_evidence_ids = ("delivery-receipt",)

            def prepare_completion(self, request):
                raise AssertionError("preparation must wait for every other completion gate")

        module = types.ModuleType("guarded_preparing_application")
        module.adapter = PreparingApplication()
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        projector = dev_pipeline_adapter.TaskArtifactProjector(
            task_dir, application=f"{module.__name__}:adapter", destination="bound"
        )
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("attempt_completed", 2))
        self.assertEqual(status_of(task_dir)["state"], "blocked")
        self.assertIn("other-live-gate", status_of(task_dir)["current_step"])

    def test_completion_preparation_uses_only_ids_enforced_by_this_contract(self) -> None:
        task_dir = make_task(self.tmp, status="done")
        (task_dir / "task_contract.json").write_text(
            json.dumps({"required_live_evidence": [{"id": "delivery-receipt"}]}),
            encoding="utf-8",
        )

        class PreparingApplication(application_adapter.DefaultApplicationV1):
            completion_preparation_evidence_ids = (
                "review-transition", "delivery-receipt"
            )

            def prepare_completion(self, request):
                with (request.task_dir / "verification.md").open("a", encoding="utf-8") as handle:
                    handle.write(
                        "# Verification\n\n## delivery-receipt\n"
                        "- Result: **PASS**\n- Evidence: delivered first\n"
                    )
                return application_adapter.DeliveryResultV1(True, "receipt persisted")

        module = types.ModuleType("multi_capability_preparing_application")
        module.adapter = PreparingApplication()
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        projector = dev_pipeline_adapter.TaskArtifactProjector(
            task_dir, application=f"{module.__name__}:adapter", destination="bound"
        )
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("attempt_completed", 2))
        self.assertEqual(status_of(task_dir)["state"], "completed")



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

    @requires_review_events
    def test_an_unobtainable_review_becomes_another_numbers_work(self) -> None:
        """The outage gets its own number; this task keeps its scope and waits."""
        task_dir = make_task(self.tmp / "workspace" / "tasks")
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("run_started", 2, RUN_STARTED))
        projector.consume(
            event(
                "review_refused",
                3,
                {
                    "strategy": "cross_provider",
                    "review_provider": "codex",
                    "reason": "the reviewer sandbox could not execute any command",
                    "artifact_digest": "sha256:abc",
                },
            )
        )

        status = status_of(task_dir)
        self.assertEqual(status["state"], "blocked")
        self.assertIn("own task number", status["current_step"])
        self.assertIn("waits for a reviewer", status["current_step"])
        obligations = [
            json.loads(line)
            for line in (task_dir / "reviews" / "infrastructure-obligations.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(len(obligations), 1)
        self.assertEqual(obligations[0]["kind"], "review_infrastructure_defect")
        self.assertEqual(obligations[0]["source"], "dev-pipeline:review_refused")
        self.assertEqual(obligations[0]["subject_scope"], "unchanged")
        filed = obligations[0]["recorded_as"]
        self.assertTrue((self.tmp / "workspace" / filed / "task.md").is_file())
        self.assertNotIn(task_dir.name, filed)

    @requires_review_events
    def test_a_finding_that_comes_back_is_reported_without_stopping_rework(self) -> None:
        """Two rounds of the same task number, second one repeating a finding.

        The repeat is a statement about execution quality, so it reaches the
        user through the task's own state. It is not a stop: the task stays
        `running` in its rework phase, and no round is refused.
        """
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        decision_path = task_dir / "decision.json"

        def review_round(sequence: int, run: str, findings: list[str]) -> None:
            decision_path.write_text(
                json.dumps(
                    {
                        "decision": {
                            "decision": "rework_required",
                            "findings": [
                                {"id": name, "severity": "critical"} for name in findings
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            projector.consume(
                event(
                    "review_started",
                    sequence,
                    {
                        "strategy": "cross_provider",
                        "review_provider": "codex",
                        "artifact_digest": "sha256:abc",
                        "author_session_id": "session-author",
                    },
                    run_id=run,
                )
            )
            projector.consume(
                event(
                    "review_rework_required",
                    sequence + 1,
                    {
                        "strategy": "cross_provider",
                        "review_provider": "codex",
                        "artifact_digest": "sha256:abc",
                        "decision_artifact": str(decision_path),
                    },
                    run_id=run,
                )
            )

        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("run_started", 2, RUN_STARTED))
        review_round(3, "run_review_1", ["no-live-evidence"])
        self.assertNotIn("quality warning", status_of(task_dir)["current_step"])

        review_round(5, "run_review_2", ["no-live-evidence", "newly-exposed"])

        status = status_of(task_dir)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["phase"], "rework")
        self.assertIn("quality warning", status["current_step"])
        self.assertIn("no-live-evidence", status["current_step"])
        self.assertNotIn("newly-exposed", status["current_step"])
        self.assertIn("quality warning", progress_of(task_dir)["recent_outcome"])
        self.assertIn("quality warning", trace_of(task_dir))

        rounds = [
            json.loads(line)
            for line in (task_dir / "reviews" / "rounds.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual([entry["round"] for entry in rounds], [1, 2])

    @requires_review_events
    def test_review_and_rework_phase_claims_rotate_the_run_identity(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("run_started", 2, RUN_STARTED))
        projector.consume(
            event(
                "review_started",
                3,
                {
                    "strategy": "cross_provider",
                    "review_provider": "claude",
                    "artifact_digest": "sha256:abc",
                    "author_session_id": "session-author",
                },
                run_id="run_review",
            )
        )
        projector.consume(
            event(
                "review_rework_required",
                4,
                {
                    "strategy": "cross_provider",
                    "review_provider": "claude",
                    "artifact_digest": "sha256:abc",
                },
                run_id="run_review",
            )
        )
        projector.consume(
            event(
                "rework_started",
                5,
                {
                    "strategy": "cross_provider",
                    "author_provider": "codex",
                    "author_session_id": "session-author",
                    "artifact_digest": "sha256:abc",
                    "decision_digest": "sha256:def",
                    "decision_artifact": "/decision.json",
                },
                run_id="run_rework",
            )
        )

        cursor = json.loads(projector.cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(cursor["attempt_id"], "attempt_a")
        self.assertEqual(cursor["run_id"], "run_rework")
        self.assertEqual(cursor["last_sequence"], 5)

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

    @requires_review_events
    def test_restart_replays_core_events_missed_after_adapter_failure(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("run_started", 2, RUN_STARTED))

        core_state = task_dir / "dev-pipeline" / "core-state"
        core_state.mkdir()
        missing = [
            event("attempt_started", 1, ATTEMPT_STARTED),
            event("run_started", 2, RUN_STARTED),
            event(
                "review_started",
                3,
                {
                    "strategy": "cross_provider",
                    "review_provider": "claude",
                    "artifact_digest": "sha256:abc",
                    "author_session_id": "session-author",
                },
                run_id="run_review",
            ),
            event(
                "review_waiting",
                4,
                {
                    "strategy": "cross_provider",
                    "review_provider": "claude",
                    "reason": "provider pipe closed",
                },
                run_id="run_review",
            ),
        ]
        (core_state / "events.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in missing), encoding="utf-8"
        )

        dev_pipeline_adapter.recover_core_ledger(projector, core_state)

        cursor = json.loads(projector.cursor_path.read_text(encoding="utf-8"))
        self.assertEqual(cursor["last_sequence"], 4)
        self.assertEqual(cursor["run_id"], "run_review")
        self.assertEqual(trace_of(task_dir).count("`attempt_started`"), 1)
        self.assertEqual(trace_of(task_dir).count("`review_started`"), 1)
        self.assertIn("provider pipe closed", status_of(task_dir)["current_step"])


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
            assurance_config=None,
            review_packet=None,
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

    def test_assurance_inputs_reach_the_public_owner_command(self) -> None:
        task_dir = make_task(self.tmp)
        assurance = task_dir / "assurance.json"
        packet = task_dir / "review-packet.json"
        assurance.write_text("{}", encoding="utf-8")
        packet.write_text("{}", encoding="utf-8")
        instruction = dev_pipeline_adapter.prepare_owner_instruction(task_dir)
        command = dev_pipeline_adapter.build_core_command(
            self._args(task_dir, assurance_config=assurance, review_packet=packet),
            task_dir,
            instruction,
        )
        self.assertEqual(
            command[command.index("--assurance-config") + 1], str(assurance.resolve())
        )
        self.assertEqual(
            command[command.index("--review-packet") + 1], str(packet.resolve())
        )

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
        self.assertIn("Before writing code, use this order", text)
        self.assertIn("only then add the smallest necessary code", text)

    def test_the_adapter_accepts_only_the_versioned_delivery_configuration(self) -> None:
        task_dir = make_task(self.tmp)
        with mock.patch.object(
            dev_pipeline_adapter, "resolve_dev_pipeline_bin", return_value="dev-pipeline"
        ), mock.patch.object(dev_pipeline_adapter, "run", return_value=0) as run:
            self.assertEqual(
                dev_pipeline_adapter.main(
                    [str(task_dir), "--repo", str(task_dir.parent), "--destination", "opaque"]
                ),
                0,
            )
        self.assertEqual(run.call_args.args[0].destination, "opaque")

        for option in ("--chat-id", "--notification-test-context"):
            with self.subTest(option=option):
                with self.assertRaises(SystemExit):
                    dev_pipeline_adapter.main(
                        [str(task_dir), "--repo", str(task_dir.parent), option, "value"]
                    )

    def test_direct_adapter_invocation_uses_the_shared_pipeline_resolver(self) -> None:
        task_dir = make_task(self.tmp)
        with mock.patch.object(
            dev_pipeline_adapter,
            "resolve_dev_pipeline_bin",
            side_effect=SystemExit("shared-resolver"),
        ) as resolver:
            with self.assertRaisesRegex(SystemExit, "shared-resolver"):
                dev_pipeline_adapter.main(
                    [str(task_dir), "--repo", str(task_dir.parent)]
                )
        resolver.assert_called_once_with(None)

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


class PhaseProjectionTests(unittest.TestCase):
    """Review and rework are phases of one task, not tasks of their own."""

    def setUp(self) -> None:
        self.tmp = Path(__file__).resolve().parent / "_tmp_phases"
        if self.tmp.exists():
            subprocess.run(["rm", "-rf", str(self.tmp)], check=True)
        self.tmp.mkdir()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])

    @requires_review_events
    def test_the_accepted_sequence_is_one_task_directory(self) -> None:
        """implementation -> review -> rework -> review -> completed.

        Every event below is validated by the core's own `validate_event`, so
        this is the neutral vocabulary as published rather than a local
        rehearsal of it.
        """
        task_dir = make_task(self.tmp, status="done")
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)

        review_payload = {
            "strategy": "cross_provider",
            "review_provider": "claude",
            "artifact_digest": "sha256:abc",
            "author_session_id": "session-author",
        }
        for sequence, (kind, payload) in enumerate(
            [
                ("attempt_started", ATTEMPT_STARTED),
                ("run_started", RUN_STARTED),
                ("checkpoint_completed", {"checkpoint": "build", "next_step": "review"}),
                (
                    "increment_ready_for_review",
                    {"increment": "candidate", "artifact": "/c.json", "artifact_digest": "sha256:abc"},
                ),
                ("review_started", review_payload),
                (
                    "review_rework_required",
                    {"strategy": "cross_provider", "review_provider": "claude", "artifact_digest": "sha256:abc"},
                ),
                ("checkpoint_completed", {"checkpoint": "repair", "next_step": "re-review"}),
                ("review_started", review_payload),
                (
                    "review_approved",
                    {
                        "strategy": "cross_provider",
                        "review_provider": "claude",
                        "reviewer_session_id": "session-reviewer",
                        "artifact_digest": "sha256:def",
                    },
                ),
                ("attempt_completed", {}),
            ],
            start=1,
        ):
            projector.consume(event(kind, sequence, payload))

        phases = json.loads((task_dir / "phases.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [item["phase"] for item in phases["history"]],
            ["implementation", "review", "rework", "review", "completed"],
        )
        self.assertEqual(phases["phase"], "completed")
        self.assertEqual(phases["task_ref"], task_dir.name)
        self.assertEqual(status_of(task_dir)["phase"], "completed")
        # One goal, one number: no second task directory appeared for the review.
        self.assertEqual([p.name for p in self.tmp.iterdir()], [task_dir.name])

    def test_each_phase_records_the_event_that_caused_it(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(
            event(
                "increment_ready_for_review",
                2,
                {"increment": "candidate", "artifact": "/c.json", "artifact_digest": "sha256:abc"},
            )
        )
        phases = json.loads((task_dir / "phases.json").read_text(encoding="utf-8"))
        self.assertEqual(phases["history"][1]["cause"]["kind"], "increment_ready_for_review")
        self.assertEqual(phases["history"][1]["cause"]["source"], "dev-pipeline")
        # The phase is timestamped by the event, not by when projection ran.
        self.assertEqual(phases["history"][1]["entered_at"], "2026-07-27T12:02:00+00:00")

    @requires_review_events
    def test_an_unobtainable_review_blocks_instead_of_asking_for_rework(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(
            event(
                "review_waiting",
                2,
                {
                    "strategy": "cross_provider",
                    "review_provider": "claude",
                    "reason": "reviewer runtime is unavailable",
                },
            )
        )
        status = status_of(task_dir)
        self.assertEqual(status["state"], "blocked")
        self.assertEqual(status["phase"], "blocked")
        self.assertIn("reviewer runtime is unavailable", status["current_step"])

    def test_a_machinery_event_does_not_move_the_phase(self) -> None:
        task_dir = make_task(self.tmp)
        projector = dev_pipeline_adapter.TaskArtifactProjector(task_dir)
        projector.consume(event("attempt_started", 1, ATTEMPT_STARTED))
        projector.consume(event("run_started", 2, RUN_STARTED))
        projector.consume(event("process_started", 3, {"pid": 11}))
        phases = json.loads((task_dir / "phases.json").read_text(encoding="utf-8"))
        self.assertEqual([item["phase"] for item in phases["history"]], ["implementation"])


if __name__ == "__main__":
    unittest.main()
