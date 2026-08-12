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
task_completion = _load("task_completion")
task_phases = _load("task_phases")

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

# What an installation hands the dev-pipeline core to have it review at all.
CODEX_ASSURANCE = {
    "schema_version": "1.0",
    "strategy": "cross_provider",
    "owner_provider": "claude",
    "review_provider": "codex",
    "providers": {"claude": {"executable": "claude"}, "codex": {"executable": "codex"}},
}


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
                assurance=CODEX_ASSURANCE,
                which=_installed("claude", "codex"),
            )
        self.assertEqual(record["decision"], "admitted")
        self.assertEqual(record["pair"]["reviewer_family"], "Codex")
        self.assertEqual(record["rework_rounds"], "unlimited")
        self.assertTrue(record["assurance_binding"]["bound"])

    def test_an_admitted_launch_is_appended_to_the_numbers_ledger(self) -> None:
        """The binding outlives the launch record a later review overwrites."""
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            review_admission.admit_launch(
                task_dir,
                workflow="standard",
                author_runner="claude",
                access_grant=WRITE_GRANT,
                contract=UNGATED,
                which=_installed("claude", "codex"),
            )
            review_admission.admit_launch(
                task_dir,
                workflow="standard",
                author_runner="codex",
                access_grant=READ_ONLY_GRANT,
                contract=UNGATED,
                review_launch=True,
                which=_installed("claude", "codex"),
            )
            ledger = review_admission.admissions(task_dir)
            binding = review_admission.bound_author_admission(task_dir)
        self.assertEqual(len(ledger), 2)
        self.assertEqual(ledger[-1]["classification"]["work_class"], "review")
        self.assertEqual(binding["pair"]["author_runner"], "claude")
        self.assertEqual(binding["pair"]["reviewer_runner"], "codex")

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

    def _refuse(
        self, adapter: _RecordingTransport, root: str, workflow: str = "dev-pipeline"
    ) -> tuple[Path, str]:
        task_dir = _workspace_task(root)
        args = argparse.Namespace(
            task_dir=str(task_dir),
            runner="claude",
            # Naming the author's own family refuses whatever is installed on
            # the host running this test.
            reviewer_runner="claude",
            workflow=workflow,
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

    def test_the_delivered_event_carries_the_workflow_that_was_refused(self) -> None:
        """An adapter that routes or audits by workflow must not be misinformed."""
        for workflow in ("dev-pipeline", "standard"):
            with self.subTest(workflow=workflow):
                adapter = _RecordingTransport()
                with tempfile.TemporaryDirectory() as raw:
                    self._refuse(adapter, raw, workflow=workflow)
                self.assertEqual([event.workflow for event in adapter.events], [workflow])

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


class AssuranceBindingTests(unittest.TestCase):
    """A dev-pipeline launch is reviewed by the pair it was admitted with."""

    def _admit(self, task_dir: Path, assurance):
        return review_admission.admit_launch(
            task_dir,
            workflow="dev-pipeline",
            author_runner="claude",
            access_grant=WRITE_GRANT,
            contract=UNGATED,
            assurance=assurance,
            which=_installed("claude", "codex"),
        )

    def test_a_material_launch_without_assurance_is_refused(self) -> None:
        """Nothing would ask the bound reviewer for anything."""
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._admit(Path(raw), None)
        record = caught.exception.record
        self.assertEqual(record["decision"], "refused")
        self.assertEqual(record["assurance_binding"]["outcome"], "assurance_missing")
        self.assertIn("never be asked to review", record["message"])

    def test_assurance_reviewing_with_another_family_is_refused(self) -> None:
        assurance = {**CODEX_ASSURANCE, "review_provider": "agent"}
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._admit(Path(raw), assurance)
        record = caught.exception.record
        self.assertEqual(
            record["assurance_binding"]["outcome"], "assurance_reviewer_mismatch"
        )

    def test_assurance_that_reviews_with_nobody_is_refused(self) -> None:
        assurance = {**CODEX_ASSURANCE, "review_provider": None}
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._admit(Path(raw), assurance)
        self.assertEqual(
            caught.exception.record["assurance_binding"]["outcome"],
            "assurance_reviews_nobody",
        )

    def test_a_standard_launch_needs_no_assurance_configuration(self) -> None:
        """Standard has no assurance seam; its review is a launch of its own."""
        with tempfile.TemporaryDirectory() as raw:
            record = review_admission.admit_launch(
                Path(raw),
                workflow="standard",
                author_runner="claude",
                access_grant=WRITE_GRANT,
                contract=UNGATED,
                which=_installed("claude", "codex"),
            )
        self.assertEqual(record["decision"], "admitted")
        self.assertNotIn("assurance_binding", record)


class ReviewLaunchPairingTests(unittest.TestCase):
    """The review of a number has to be the family that number was promised."""

    def _author(self, task_dir: Path, author: str = "claude") -> dict:
        return review_admission.admit_launch(
            task_dir,
            workflow="standard",
            author_runner=author,
            access_grant=WRITE_GRANT,
            contract=UNGATED,
            which=_installed("claude", "codex"),
        )

    def _review(self, task_dir: Path, reviewer: str) -> dict:
        return review_admission.admit_launch(
            task_dir,
            workflow="standard",
            author_runner=reviewer,
            access_grant=READ_ONLY_GRANT,
            contract=UNGATED,
            review_launch=True,
            which=_installed("claude", "codex"),
        )

    def test_the_bound_family_is_admitted_as_the_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._author(task_dir)
            record = self._review(task_dir, "codex")
        self.assertEqual(record["decision"], "admitted_review")
        self.assertEqual(record["classification"]["work_class"], "review")
        self.assertEqual(record["pair"]["author_family"], "Claude")

    def test_the_authors_own_family_cannot_review_the_number(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._author(task_dir)
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._review(task_dir, "claude")
        record = caught.exception.record
        self.assertEqual(record["pair"]["outcome"], "review_by_author_family")
        self.assertIn("does not review itself", record["message"])

    def test_a_family_that_was_not_bound_cannot_review_the_number(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._author(task_dir)
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._review(task_dir, "agent")
        self.assertEqual(
            caught.exception.record["pair"]["outcome"], "review_by_unbound_family"
        )

    def test_the_launch_that_is_the_review_is_the_one_admitted_as_one(self) -> None:
        """The contract keeps `require_review_verdict` forever; admission does not.

        Reading the contract instead would make the author's next run look like
        another review, and the rework phase the review asked for would never be
        recorded -- which is exactly the history a stale approval is measured
        against.
        """
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._author(task_dir)
            self.assertFalse(review_admission.launch_is_review(task_dir))
            self._review(task_dir, "codex")
            self.assertTrue(review_admission.launch_is_review(task_dir))
            self._author(task_dir)
            self.assertFalse(review_admission.launch_is_review(task_dir))

    def test_a_review_of_someone_elses_number_is_left_to_its_owner(self) -> None:
        """A review task whose subject is another number is not paired here."""
        with tempfile.TemporaryDirectory() as raw:
            record = self._review(Path(raw), "codex")
        self.assertEqual(record["decision"], "admitted_review")
        self.assertEqual(record["pair"]["outcome"], "no_bound_author_in_this_task")


class IndependentReviewStatusTests(unittest.TestCase):
    """Acceptance asks whether the work as it stands carries that approval."""

    def _admitted(self, task_dir: Path) -> None:
        review_admission.admit_launch(
            task_dir,
            workflow="standard",
            author_runner="claude",
            access_grant=WRITE_GRANT,
            contract=UNGATED,
            which=_installed("claude", "codex"),
        )

    def _round(self, task_dir: Path, decision: str, provider: str = "codex") -> dict:
        return review_admission.record_review_round(
            task_dir,
            event_id=f"event-{decision}-{provider}-{len(review_admission.review_rounds(task_dir))}",
            decision={"decision": decision},
            review_provider=provider,
        )

    def test_an_unadmitted_task_is_not_gated_on_a_review_it_never_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            status = review_admission.independent_review_status(Path(raw))
        self.assertFalse(status["required"])
        self.assertTrue(status["satisfied"])

    def test_an_admitted_task_with_no_round_is_not_satisfied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._admitted(task_dir)
            status = review_admission.independent_review_status(task_dir)
        self.assertTrue(status["required"])
        self.assertFalse(status["satisfied"])
        self.assertIn("no review round yet", status["reason"])
        self.assertIn("--require-review-verdict", status["action"])

    def test_a_rework_round_blocks_acceptance_and_allows_another_round(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._admitted(task_dir)
            self._round(task_dir, "rework")
            status = review_admission.independent_review_status(task_dir)
            # Nothing stops the next round: the ledger keeps appending.
            self._round(task_dir, "rework")
            after = review_admission.independent_review_status(task_dir)
        self.assertFalse(status["satisfied"])
        self.assertIn("no limit", status["reason"])
        self.assertEqual(after["rounds"], 2)

    def test_an_approval_by_the_bound_family_satisfies_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._admitted(task_dir)
            self._round(task_dir, "rework")
            self._round(task_dir, "approved")
            status = review_admission.independent_review_status(task_dir)
        self.assertTrue(status["satisfied"])
        self.assertEqual(status["last_round"]["reviewer_family"], "Codex")

    def test_an_approval_by_the_authors_own_family_does_not_satisfy_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._admitted(task_dir)
            self._round(task_dir, "approved", provider="claude")
            status = review_admission.independent_review_status(task_dir)
        self.assertFalse(status["satisfied"])
        self.assertIn("authored this work", status["reason"])

    def test_an_approval_by_a_third_independent_family_does_not_satisfy_it(self) -> None:
        """Independence is not enough: it has to be the family that was bound.

        Cursor is neither the Claude author nor the Codex reviewer this number
        bound, so its approval is of work nobody promised to review.
        """
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._admitted(task_dir)
            self._round(task_dir, "approved", provider="agent")
            status = review_admission.independent_review_status(task_dir)
        self.assertEqual(status["reviewer_family"], "Codex")
        self.assertEqual(status["last_round"]["reviewer_family"], "Cursor")
        self.assertFalse(status["satisfied"])
        self.assertIn("not the review this work was admitted with", status["reason"])

    def test_author_work_after_an_approval_needs_another_review(self) -> None:
        """An approval is of what was there, not of whatever replaced it."""
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._admitted(task_dir)
            approved = self._round(task_dir, "approved")
            later = [{"phase": "rework", "entered_at": "2999-01-01T00:00:00+00:00"}]
            status = review_admission.independent_review_status(
                task_dir, author_phases=later
            )
            earlier = [{"phase": "implementation", "entered_at": "1999-01-01T00:00:00+00:00"}]
            unaffected = review_admission.independent_review_status(
                task_dir, author_phases=earlier
            )
        self.assertTrue(approved["decision"] == "approved")
        self.assertFalse(status["satisfied"])
        self.assertIn("has not been reviewed", status["reason"])
        self.assertTrue(unaffected["satisfied"])


class AcceptanceIsBoundToTheReviewTests(unittest.TestCase):
    """The shared completion decision refuses work the bound reviewer never saw."""

    def _task(self, root: Path) -> Path:
        task = root / "001-example"
        (task / ".runner").mkdir(parents=True)
        (task / "task.md").write_text(
            '---\nid: 1\nslug: "example"\ntitle: "x"\ndate: 2026-08-11\n'
            'status: "completed"\n---\n# x\n',
            encoding="utf-8",
        )
        (task / "plan.md").write_text("# Plan\n", encoding="utf-8")
        (task / "task_contract.json").write_text('{"version": 1}', encoding="utf-8")
        return task

    def _admit_author(self, task: Path) -> None:
        review_admission.admit_launch(
            task,
            workflow="standard",
            author_runner="claude",
            access_grant=WRITE_GRANT,
            contract=UNGATED,
            which=_installed("claude", "codex"),
        )
        task_phases.record_phase(task, "implementation")

    def test_an_admitted_author_cannot_close_without_the_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            self._admit_author(task)
            ready, reason = task_completion.completion_ready(task, workflow="standard")
        self.assertFalse(ready)
        self.assertIn("independent review", reason)
        self.assertIn("--require-review-verdict", reason)

    def test_the_reviewers_published_verdict_becomes_the_round(self) -> None:
        """A standard review reaches the ledger through its own verdict line."""
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            self._admit_author(task)
            review_admission.admit_launch(
                task,
                workflow="standard",
                author_runner="codex",
                access_grant=READ_ONLY_GRANT,
                contract=UNGATED,
                review_launch=True,
                which=_installed("claude", "codex"),
            )
            (task / "findings.md").write_text(
                "# Findings\n\nThe live evidence is missing.\n\nVerdict: rework\n",
                encoding="utf-8",
            )
            entry = task_runner.record_standard_review_round(task, "codex")
            rework_ready, rework_reason = task_completion.completion_ready(
                task, workflow="standard"
            )
            # The author fixes it and the same reviewer approves the new state.
            task_phases.record_phase(task, "rework")
            (task / "findings.md").write_text(
                "# Findings\n\nThe live evidence is there now.\n\nVerdict: approved\n",
                encoding="utf-8",
            )
            task_runner.write_json(
                task_runner.runner_meta_path(task), {"child_started_at": "second-run"}
            )
            task_runner.record_standard_review_round(task, "codex")
            ready, _ = task_completion.completion_ready(task, workflow="standard")
        self.assertEqual(entry["decision"], "rework")
        self.assertFalse(rework_ready)
        self.assertIn("did not approve", rework_reason)
        self.assertTrue(ready)

    def test_an_unbound_familys_approval_does_not_close_the_task(self) -> None:
        """The gate reads the binding, not merely "somebody else approved".

        A dev-pipeline installation whose assurance reviews with a third family
        projects that family's round into this ledger, and before this it closed
        a task whose bound Codex reviewer had seen nothing.
        """
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            self._admit_author(task)
            review_admission.record_review_round(
                task,
                event_id="event-cursor-approved",
                decision={"decision": "approved"},
                review_provider="agent",
            )
            ready, reason = task_completion.completion_ready(task, workflow="standard")
        self.assertFalse(ready)
        self.assertIn("Codex was bound", reason)
        self.assertIn("--require-review-verdict", reason)

    def test_the_author_cannot_record_the_round_that_accepts_its_own_work(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            self._admit_author(task)
            (task / "findings.md").write_text("Verdict: approved\n", encoding="utf-8")
            # The author's own launch is not classified as the review, so its
            # verdict never becomes a round at all.
            self.assertIsNone(task_runner.record_standard_review_round(task, "claude"))
            ready, reason = task_completion.completion_ready(task, workflow="standard")
        self.assertFalse(ready)
        self.assertIn("no review round yet", reason)


if __name__ == "__main__":
    unittest.main()
