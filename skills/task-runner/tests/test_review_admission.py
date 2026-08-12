"""Material work gets an independent reviewer before its author starts."""

import argparse
import importlib.util
import json
import sys
import tempfile
import types
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


review_admission = _load("review_admission")
task_runner = _load("task_runner")

import application_adapter  # noqa: E402  (`_load` put the scripts directory on the path)


def _installed(*runners: str):
    executables = {review_admission.RUNNER_EXECUTABLES[runner] for runner in runners}
    return lambda name: f"/usr/bin/{name}" if name in executables else None


WRITE_GRANT = {
    "sandbox_mode": "workspace-write",
    "granted_directories": ["/srv/target-repo"],
    "grants_write": True,
}
READ_ONLY_GRANT = {"sandbox_mode": "read-only", "granted_directories": [], "grants_write": False}
UNGATED = {"gate_status": "ungated", "review_gates": []}


class ClassificationTests(unittest.TestCase):
    def test_a_write_grant_makes_the_launch_material(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            classification = review_admission.classify_work(
                Path(raw), workflow="standard", access_grant=WRITE_GRANT, contract=UNGATED
            )
        self.assertEqual(classification["work_class"], "material")
        self.assertTrue(classification["material_effects"])

    def test_an_undeclared_launch_is_material_even_with_nothing_observed(self) -> None:
        """Silence is not the exception. The exception has to be claimed."""
        with tempfile.TemporaryDirectory() as raw:
            classification = review_admission.classify_work(
                Path(raw), workflow="standard", access_grant=READ_ONLY_GRANT, contract=UNGATED
            )
        self.assertEqual(classification["work_class"], "material")
        self.assertEqual(
            classification["classified_by"], "undeclared_launch_defaults_to_material"
        )

    def test_the_read_only_exception_needs_a_structured_declaration(self) -> None:
        contract = {**UNGATED, "review_policy": {"work_class": "read_only_lookup"}}
        with tempfile.TemporaryDirectory() as raw:
            classification = review_admission.classify_work(
                Path(raw), workflow="standard", access_grant=READ_ONLY_GRANT, contract=contract
            )
        self.assertEqual(classification["work_class"], "read_only_lookup")

    def test_calling_it_trivial_in_prose_does_not_classify_anything(self) -> None:
        """Prose is not a classification, whatever adjective it uses."""
        contract = {
            **UNGATED,
            "non_negotiable_constraints": ["This is a trivial read-only lookup."],
        }
        with tempfile.TemporaryDirectory() as raw:
            classification = review_admission.classify_work(
                Path(raw), workflow="standard", access_grant=READ_ONLY_GRANT, contract=contract
            )
        self.assertEqual(classification["work_class"], "material")

    def test_a_declaration_contradicted_by_a_write_grant_is_not_an_exception(self) -> None:
        contract = {**UNGATED, "review_policy": {"work_class": "read_only_lookup"}}
        with tempfile.TemporaryDirectory() as raw:
            classification = review_admission.classify_work(
                Path(raw), workflow="standard", access_grant=WRITE_GRANT, contract=contract
            )
        self.assertEqual(classification["work_class"], "material")
        self.assertEqual(
            classification["classified_by"],
            "declared_read_only_lookup_contradicted_by_observation",
        )

    def test_registered_deliverables_are_a_material_effect(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            (task_dir / "deliverables").mkdir()
            (task_dir / "deliverables" / "manifest.json").write_text("{}", encoding="utf-8")
            contract = {**UNGATED, "review_policy": {"work_class": "read_only_lookup"}}
            classification = review_admission.classify_work(
                task_dir, workflow="standard", access_grant=READ_ONLY_GRANT, contract=contract
            )
        self.assertEqual(classification["work_class"], "material")


class PairingTests(unittest.TestCase):
    def test_an_independent_family_binds_automatically(self) -> None:
        pair = review_admission.resolve_pair(
            author_runner="claude", which=_installed("claude", "codex")
        )
        self.assertTrue(pair["bound"])
        self.assertEqual(pair["reviewer_runner"], "codex")

    def test_the_author_never_reviews_itself_when_nobody_else_is_installed(self) -> None:
        pair = review_admission.resolve_pair(
            author_runner="claude", which=_installed("claude")
        )
        self.assertFalse(pair["bound"])
        self.assertEqual(pair["outcome"], "no_independent_runner_installed")

    def test_a_declared_reviewer_of_the_authors_family_is_refused(self) -> None:
        pair = review_admission.resolve_pair(
            author_runner="claude",
            declared_reviewer="claude",
            which=_installed("claude", "codex"),
        )
        self.assertFalse(pair["bound"])
        self.assertEqual(pair["outcome"], "reviewer_is_author_family")

    def test_a_declared_reviewer_that_is_not_installed_is_refused(self) -> None:
        pair = review_admission.resolve_pair(
            author_runner="claude", declared_reviewer="agent", which=_installed("claude", "codex")
        )
        self.assertFalse(pair["bound"])
        self.assertEqual(pair["outcome"], "reviewer_unavailable")


class AdmissionTests(unittest.TestCase):
    def test_material_work_without_a_pair_refuses_and_records_why(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                review_admission.admit_launch(
                    task_dir,
                    workflow="dev-pipeline",
                    author_runner="claude",
                    access_grant=WRITE_GRANT,
                    contract=UNGATED,
                    which=_installed("claude"),
                )
            record = review_admission.recorded_admission(task_dir)
        self.assertEqual(record["decision"], "refused")
        self.assertIn("refuses to start the author", str(caught.exception))
        self.assertEqual(record["message"], str(caught.exception))

    def test_an_admitted_launch_records_the_bound_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            record = review_admission.admit_launch(
                task_dir,
                workflow="dev-pipeline",
                author_runner="claude",
                access_grant=WRITE_GRANT,
                contract=UNGATED,
                which=_installed("claude", "codex"),
            )
        self.assertEqual(record["decision"], "admitted")
        self.assertEqual(record["pair"]["reviewer_family"], "Codex")
        self.assertEqual(record["rework_rounds"], "unlimited")

    def test_the_read_only_exception_starts_without_a_reviewer(self) -> None:
        contract = {**UNGATED, "review_policy": {"work_class": "read_only_lookup"}}
        with tempfile.TemporaryDirectory() as raw:
            record = review_admission.admit_launch(
                Path(raw),
                workflow="standard",
                author_runner="claude",
                access_grant=READ_ONLY_GRANT,
                contract=contract,
                which=_installed("claude"),
            )
        self.assertEqual(record["decision"], "exempt")


def _workspace_task(root: str, name: str = "001-subject") -> Path:
    """A task directory in a real workspace layout, so numbers can be allocated."""
    task_dir = Path(root) / "tasks" / name
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text(
        "---\n"
        'id: 1\nslug: "001-subject"\ntitle: "Subject"\ndate: 2026-08-12\n'
        'status: "planned"\nprojects: []\ntrips: []\n---\n# Subject\n',
        encoding="utf-8",
    )
    (task_dir / "plan.md").write_text("# Plan\n\n1. [pending] Work\n", encoding="utf-8")
    return task_dir


class InfrastructureObligationTests(unittest.TestCase):
    def test_a_missing_reviewer_is_filed_under_its_own_number(self) -> None:
        """The outage gets a number of its own; the subject task keeps its scope."""
        with tempfile.TemporaryDirectory() as raw:
            task_dir = _workspace_task(raw)
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                review_admission.admit_launch(
                    task_dir,
                    workflow="dev-pipeline",
                    author_runner="claude",
                    access_grant=WRITE_GRANT,
                    contract=UNGATED,
                    which=_installed("claude"),
                )
            obligations = review_admission.infrastructure_obligations(task_dir)
            filed = obligations[0]["recorded_as"]
            self.assertTrue((Path(raw) / filed / "task.md").is_file())
        self.assertEqual(len(obligations), 1)
        self.assertEqual(obligations[0]["kind"], "review_infrastructure_defect")
        self.assertEqual(obligations[0]["subject_scope"], "unchanged")
        self.assertNotEqual(filed, task_dir.name)
        self.assertIn(filed, str(caught.exception))

    def test_the_same_outage_does_not_keep_allocating_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = _workspace_task(raw)
            first = review_admission.record_infrastructure_obligation(
                task_dir,
                event_id="event-1",
                source="dev-pipeline:review_refused",
                reason="the reviewer sandbox could not execute any command",
            )
            second = review_admission.record_infrastructure_obligation(
                task_dir,
                event_id="event-2",
                source="dev-pipeline:review_refused",
                reason="the reviewer sandbox could not execute any command",
            )
        self.assertEqual(first["recorded_as"], second["recorded_as"])
        self.assertTrue(second["reused_existing_number"])

    def test_an_incoherent_launch_is_not_an_infrastructure_defect(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = _workspace_task(raw)
            with self.assertRaises(review_admission.ReviewAdmissionError):
                review_admission.admit_launch(
                    task_dir,
                    workflow="dev-pipeline",
                    author_runner="claude",
                    declared_reviewer="claude",
                    access_grant=WRITE_GRANT,
                    contract=UNGATED,
                    which=_installed("claude", "codex"),
                )
            self.assertEqual(review_admission.infrastructure_obligations(task_dir), [])

    def test_the_obligation_is_recorded_once_per_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = _workspace_task(raw)
            for _ in range(2):
                review_admission.record_infrastructure_obligation(
                    task_dir,
                    event_id="event-1",
                    source="dev-pipeline:review_refused",
                    reason="reviewer sandbox cannot execute",
                )
            self.assertEqual(len(review_admission.infrastructure_obligations(task_dir)), 1)

    def test_an_unfileable_defect_says_so_instead_of_pretending(self) -> None:
        """No workspace to file into is stated, not silently dropped."""
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            entry = review_admission.record_infrastructure_obligation(
                task_dir,
                event_id="event-1",
                source="dev-pipeline:review_waiting",
                reason="no reviewer answered",
            )
        self.assertIsNone(entry["recorded_as"])
        self.assertEqual(entry["separate_task_number"], "unfiled")
        self.assertIn("could not be allocated", entry["statement"])


class RefusalNotificationTests(unittest.TestCase):
    """A refusal nobody hears is indistinguishable from a launch that never ran."""

    def _refused_record(self, task_dir: Path) -> dict:
        with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
            review_admission.admit_launch(
                task_dir,
                workflow="dev-pipeline",
                author_runner="claude",
                access_grant=WRITE_GRANT,
                contract=UNGATED,
                declared_reviewer="claude",
                which=_installed("claude"),
            )
        return caught.exception.record

    def test_the_notification_says_what_happened_and_what_to_do(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record = self._refused_record(Path(raw))
        parts = review_admission.refusal_notification(record)
        self.assertIn("refuses to start the author", parts["summary"])
        self.assertIn("author reviewing itself", parts["summary"])
        self.assertNotIn(review_admission.REFUSAL_ACTION, parts["summary"])
        self.assertEqual(parts["requested_action"], review_admission.REFUSAL_ACTION)
        # The two halves are the message the task's own state records, so the
        # caller cannot be told a different decision from the one that was made.
        self.assertEqual(
            f"{parts['summary']} {parts['requested_action']}", record["message"]
        )

    def test_a_filed_review_outage_travels_with_the_notification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = _workspace_task(raw)
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                review_admission.admit_launch(
                    task_dir,
                    workflow="dev-pipeline",
                    author_runner="claude",
                    access_grant=WRITE_GRANT,
                    contract=UNGATED,
                    which=_installed("claude"),
                )
            parts = review_admission.refusal_notification(caught.exception.record)
        self.assertIn("defect in the review machinery", parts["summary"])

    def test_an_old_record_without_the_split_still_notifies(self) -> None:
        parts = review_admission.refusal_notification({"message": "refused: no reviewer"})
        self.assertEqual(parts["summary"], "refused: no reviewer")
        self.assertEqual(parts["requested_action"], review_admission.REFUSAL_ACTION)


class _RecordingTransport:
    """An installation that keeps what the engine handed to it."""

    api_version = 1

    def __init__(self, fail: bool = False) -> None:
        self.events: list = []
        self.fail = fail

    def launch_policy(self, request):
        return application_adapter.LaunchPolicyV1(None)

    def standard_session(self, request):
        return application_adapter.StandardSessionV1()

    def standard_run_finished(self, result):
        return None

    def deliver_event(self, event):
        if self.fail:
            raise RuntimeError("transport is down")
        self.events.append(event)
        return application_adapter.DeliveryResultV1(True, "recorded")

    def recover_transport(self, request):
        return None

    def completion_problems(self, request):
        return []


class RefusalReachesTheCallerTests(unittest.TestCase):
    """The refusal path of the real `start` entrypoint, end to end."""

    def _register(self, adapter: _RecordingTransport) -> str:
        module = types.ModuleType(f"task_agent_transport_{id(adapter)}")
        module.adapter = adapter
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        return f"{module.__name__}:adapter"

    def _refuse(self, adapter: _RecordingTransport, root: str) -> tuple[Path, str]:
        task_dir = _workspace_task(root)
        args = argparse.Namespace(
            task_dir=str(task_dir),
            runner="claude",
            # Naming the author's own family refuses whatever is installed on
            # the host running this test.
            reviewer_runner="claude",
            workflow="dev-pipeline",
            model=None,
            sandbox_mode=None,
            repo=None,
            dry_run=True,
            application=self._register(adapter),
            destination=None,
        )
        with self.assertRaises(SystemExit) as caught:
            task_runner.cmd_start(args)
        return task_dir, str(caught.exception)

    def test_a_refused_launch_is_delivered_and_not_only_filed(self) -> None:
        adapter = _RecordingTransport()
        with tempfile.TemporaryDirectory() as raw:
            task_dir, message = self._refuse(adapter, raw)
            status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
            trace = (task_dir / "trace.md").read_text(encoding="utf-8")
            started = (task_dir / ".runner" / "runner.json").exists()

        self.assertEqual([event.kind for event in adapter.events], ["pipeline_stopped"])
        delivered = adapter.events[0].payload["message"]
        self.assertIn("refuses to start the author", delivered)
        self.assertIn(review_admission.REFUSAL_ACTION, delivered)
        self.assertIn("refuses to start the author", message)
        # The author never ran: the refusal is what the caller was told about.
        self.assertFalse(started)
        self.assertEqual(status["state"], "blocked")
        self.assertTrue(status["review_admission"]["notification"]["delivered"])
        self.assertIn("Delivered the review-admission refusal", trace)

    def test_a_broken_transport_does_not_turn_a_refusal_into_a_launch(self) -> None:
        adapter = _RecordingTransport(fail=True)
        with tempfile.TemporaryDirectory() as raw:
            task_dir, message = self._refuse(adapter, raw)
            status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
            started = (task_dir / ".runner" / "runner.json").exists()

        self.assertIn("refuses to start the author", message)
        self.assertFalse(started)
        self.assertEqual(status["state"], "blocked")
        notification = status["review_admission"]["notification"]
        self.assertFalse(notification["delivered"])
        self.assertIn("transport is down", notification["detail"])


class RoundLedgerTests(unittest.TestCase):
    def _record(self, task_dir: Path, event_id: str, *findings: str):
        return review_admission.record_review_round(
            task_dir,
            event_id=event_id,
            decision={
                "decision": "rework_required",
                "findings": [{"id": name, "severity": "critical"} for name in findings],
            },
            review_provider="codex",
        )

    def test_rounds_are_never_capped(self) -> None:
        """Twenty rounds is not an error state; it is twenty rounds."""
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            for index in range(20):
                entry = self._record(task_dir, f"event-{index}", f"finding-{index}")
                self.assertIsNone(entry["warning"])
            self.assertEqual(entry["round"], 20)
            self.assertEqual(len(review_admission.review_rounds(task_dir)), 20)

    def test_a_repeated_finding_warns_without_stopping_rework(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._record(task_dir, "event-1", "missing-live-evidence")
            entry = self._record(task_dir, "event-2", "missing-live-evidence", "new-depth")
        self.assertEqual(entry["repeated_finding_ids"], ["missing-live-evidence"])
        self.assertIn("quality warning", entry["warning"])
        self.assertIn("Rework continues", entry["warning"])

    def test_a_newly_exposed_finding_is_not_a_quality_warning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._record(task_dir, "event-1", "shallow")
            entry = self._record(task_dir, "event-2", "deeper")
        self.assertEqual(entry["repeated_finding_ids"], [])
        self.assertIsNone(entry["warning"])

    def test_replaying_the_same_event_does_not_invent_a_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            first = self._record(task_dir, "event-1", "same")
            replay = self._record(task_dir, "event-1", "same")
        self.assertEqual(first, replay)
        self.assertIsNone(replay["warning"])

    def test_free_text_findings_still_recognise_a_literal_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            for event_id in ("event-1", "event-2"):
                entry = review_admission.record_review_round(
                    task_dir,
                    event_id=event_id,
                    decision={"decision": "rework_required", "findings": ["No live evidence"]},
                )
        self.assertTrue(entry["repeated_finding_ids"])
        self.assertIsNotNone(entry["warning"])


if __name__ == "__main__":
    unittest.main()
