"""Material work gets an independent reviewer before its author starts."""

import argparse
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


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


def _launch(task_dir: Path, **admission) -> dict:
    """Admit a launch and start it, which is what binds the number.

    The launcher decides the pair first, commits it only where the author
    actually starts, and confirms the commitment once the author exists, so a
    test that needs the number bound has to do all three. Tests about launches
    that never start call `admit_launch` on its own, and tests about launches
    that committed and then started nobody stop after `commit_admission`.
    """
    record = review_admission.admit_launch(task_dir, **admission)
    review_admission.commit_admission(task_dir, record)
    review_admission.confirm_admission(task_dir)
    return record


WRITE_GRANT = {
    "sandbox_mode": "workspace-write",
    "granted_directories": ["/srv/target-repo"],
    "grants_write": True,
}
READ_ONLY_GRANT = {"sandbox_mode": "read-only", "granted_directories": [], "grants_write": False}
WORKSPACE_WRITE_WITHOUT_REPO = {
    "sandbox_mode": "workspace-write",
    "granted_directories": [],
    "grants_write": False,
}
UNGATED = {"gate_status": "ungated", "review_gates": []}

# What an installation hands the dev-pipeline core to have it review at all.
CODEX_ASSURANCE = {
    "schema_version": "1.0",
    "strategy": "cross_provider",
    "owner_provider": "claude",
    "review_provider": "codex",
    "providers": {"claude": {"executable": "claude"}, "codex": {"executable": "codex"}},
}
SAME_PROVIDER_ASSURANCE = {
    "schema_version": "1.0",
    "strategy": "isolated_same_provider",
    "owner_provider": "claude",
    "review_provider": "claude",
    "providers": {"claude": {"executable": "claude"}},
}
LIVE_ONLY_ASSURANCE = {
    "schema_version": "1.0",
    "strategy": "live_acceptance_only",
    "owner_provider": "claude",
    "review_provider": None,
    "providers": {"claude": {"executable": "claude"}},
    "live_scenarios": ["real_user_path"],
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
    def test_configured_provider_uses_the_contract_executable_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            executable = Path(raw) / "provider-cli"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            assurance = {
                "providers": {"codex": {"executable": str(executable)}}
            }
            self.assertTrue(
                review_admission.configured_provider_available(assurance, "codex")
            )
            executable.unlink()
            self.assertFalse(
                review_admission.configured_provider_available(assurance, "codex")
            )

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

    def test_isolated_same_provider_binds_a_fresh_session_on_one_provider(self) -> None:
        pair = review_admission.resolve_pair(
            author_runner="claude",
            assurance=SAME_PROVIDER_ASSURANCE,
            which=_installed("claude"),
            configured_resolver=_installed("claude"),
        )
        self.assertTrue(pair["bound"])
        self.assertEqual(pair["reviewer_runner"], "claude")
        self.assertEqual(pair["assurance_strategy"], "isolated_same_provider")

    def test_live_acceptance_only_binds_scenarios_not_a_model(self) -> None:
        pair = review_admission.resolve_pair(
            author_runner="claude",
            assurance=LIVE_ONLY_ASSURANCE,
            which=_installed("claude"),
            configured_resolver=_installed("claude"),
        )
        self.assertTrue(pair["bound"])
        self.assertIsNone(pair["reviewer_runner"])
        self.assertEqual(pair["live_scenarios"], ["real_user_path"])

    def test_unavailable_configured_reviewer_does_not_downgrade(self) -> None:
        pair = review_admission.resolve_pair(
            author_runner="claude",
            assurance=CODEX_ASSURANCE,
            which=_installed("claude"),
            configured_resolver=_installed("claude"),
        )
        self.assertFalse(pair["bound"])
        self.assertEqual(pair["outcome"], "configured_reviewer_unavailable")
        self.assertNotIn("downgraded", pair["detail"])

    def test_cursor_is_never_selected_as_an_independent_reviewer(self) -> None:
        pair = review_admission.resolve_pair(
            author_runner="claude", which=_installed("claude", "agent")
        )
        self.assertFalse(pair["bound"])
        self.assertEqual(pair["outcome"], "no_independent_runner_installed")

    def test_declared_cursor_reviewer_is_refused(self) -> None:
        pair = review_admission.resolve_pair(
            author_runner="claude",
            declared_reviewer="agent",
            which=_installed("claude", "agent"),
        )
        self.assertFalse(pair["bound"])
        self.assertEqual(pair["outcome"], "reviewer_not_supported")

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
            author_runner="claude", declared_reviewer="codex", which=_installed("claude")
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
                configured_resolver=_installed("claude", "codex"),
            )
        self.assertEqual(record["decision"], "admitted")
        self.assertEqual(record["pair"]["reviewer_family"], "Codex")
        self.assertEqual(record["rework_rounds"], "unlimited")
        self.assertTrue(record["assurance_binding"]["bound"])

    def test_an_admitted_launch_is_appended_to_the_numbers_ledger(self) -> None:
        """The binding outlives the launch record a later review overwrites."""
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            _launch(
                task_dir,
                workflow="standard",
                author_runner="claude",
                access_grant=WRITE_GRANT,
                contract=UNGATED,
                which=_installed("claude", "codex"),
            )
            _launch(
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

    def test_an_admitted_decision_binds_nothing_until_the_launch_starts(self) -> None:
        """Deciding is not binding, whether or not the launch means to run.

        A `--dry-run` gets the same answer and leaves no binding behind, and so
        does a real launch that is still being prepared: the pair becomes this
        number's binding at the moment its author starts, and not before.
        """
        for persist in (True, False):
            with self.subTest(persist=persist), tempfile.TemporaryDirectory() as raw:
                task_dir = Path(raw)
                record = review_admission.admit_launch(
                    task_dir,
                    workflow="standard",
                    author_runner="claude",
                    access_grant=WRITE_GRANT,
                    contract=UNGATED,
                    which=_installed("claude", "codex"),
                    persist=persist,
                )
                self.assertEqual(record["decision"], "admitted")
                self.assertEqual(record["pair"]["reviewer_family"], "Codex")
                self.assertEqual(review_admission.recorded_admission(task_dir), {})
                self.assertEqual(review_admission.admissions(task_dir), [])
                self.assertIsNone(review_admission.bound_author_admission(task_dir))

                # Committed, but with no author observed to start yet: the
                # commitment is outstanding, so it is still not the binding.
                review_admission.commit_admission(task_dir, record)
                self.assertIsNone(review_admission.bound_author_admission(task_dir))
                self.assertEqual(
                    review_admission.admission_commitment(task_dir)["admission_id"],
                    record["admission_id"],
                )

                review_admission.confirm_admission(task_dir)
                binding = review_admission.bound_author_admission(task_dir)
                self.assertEqual(binding["pair"]["reviewer_runner"], "codex")
                self.assertIsNone(review_admission.admission_commitment(task_dir))
                self.assertEqual(
                    review_admission.recorded_admission(task_dir)["admission_id"],
                    record["admission_id"],
                )

    def test_a_withdrawn_binding_leaves_the_previous_pair_in_charge(self) -> None:
        """A launch refused after it committed authored nothing either."""
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            _launch(
                task_dir,
                workflow="standard",
                author_runner="claude",
                access_grant=WRITE_GRANT,
                contract=UNGATED,
                which=_installed("claude", "codex"),
            )
            refused = review_admission.admit_launch(
                task_dir,
                workflow="standard",
                author_runner="codex",
                access_grant=WRITE_GRANT,
                contract=UNGATED,
                which=_installed("claude", "codex"),
            )
            receipt = review_admission.commit_admission(task_dir, refused)
            withdrawn = review_admission.annul_admission(
                task_dir, receipt, reason="the watcher could not be started."
            )

            binding = review_admission.bound_author_admission(task_dir)
            current = review_admission.recorded_admission(task_dir)
            bound_review = review_admission.resolve_review_launch_pair(
                task_dir, reviewer_runner="codex"
            )
            author_review = review_admission.resolve_review_launch_pair(
                task_dir, reviewer_runner="claude"
            )
            # The withdrawal is appended rather than erasing what happened.
            self.assertEqual(len(review_admission.admissions(task_dir)), 3)
        self.assertEqual(withdrawn["annuls"], refused["admission_id"])
        self.assertEqual(binding["pair"]["author_family"], "Claude")
        self.assertEqual(binding["pair"]["reviewer_family"], "Codex")
        self.assertEqual(current["pair"]["author_family"], "Claude")
        self.assertTrue(bound_review["bound"])
        self.assertEqual(author_review["outcome"], "review_by_author_family")

    def test_a_preparation_cannot_rebind_the_number_it_prepared_for(self) -> None:
        """The pair belongs to the launch that ran, not to one that was drafted.

        A `--dry-run` on the other family used to append its own binding, and the
        acceptance gate reads the last one: the bound reviewer was then refused as
        the author's own family, and the family that actually wrote the work was
        admitted to review and close it.
        """
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            _launch(
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
                access_grant=WRITE_GRANT,
                contract=UNGATED,
                which=_installed("claude", "codex"),
                persist=False,
            )
            binding = review_admission.bound_author_admission(task_dir)
            bound_review = review_admission.resolve_review_launch_pair(
                task_dir, reviewer_runner="codex"
            )
            author_review = review_admission.resolve_review_launch_pair(
                task_dir, reviewer_runner="claude"
            )
            self.assertEqual(len(review_admission.admissions(task_dir)), 1)
        self.assertEqual(binding["pair"]["author_family"], "Claude")
        self.assertEqual(binding["pair"]["reviewer_family"], "Codex")
        self.assertTrue(bound_review["bound"])
        self.assertEqual(author_review["outcome"], "review_by_author_family")

    def test_a_prepared_refusal_allocates_no_outage_number(self) -> None:
        """A launch that never starts does not file another number's defect."""
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
                    persist=False,
                )
            self.assertEqual(review_admission.infrastructure_obligations(task_dir), [])
            self.assertEqual(review_admission.recorded_admission(task_dir), {})
            self.assertEqual(
                sorted(item.name for item in (Path(raw) / "tasks").iterdir()),
                [task_dir.name],
            )
        # The caller is still told what it ran into, and told that this task is
        # not the defect -- only the number for it waits for a real start.
        self.assertIn("refuses to start the author", str(caught.exception))
        self.assertIn("a real start files it", str(caught.exception))

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


class _StopAfterSpawn(Exception):
    """Ends a launch at the point the child exists, before it is supervised."""


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


class ForegroundApplicationLifecycleTests(unittest.TestCase):
    def test_foreground_start_keeps_the_public_binding_and_child_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = _workspace_task(raw)
            assurance = Path(raw) / "assurance.json"
            assurance.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "strategy": "isolated_same_provider",
                        "owner_provider": "codex",
                        "review_provider": "codex",
                        "providers": {"codex": {"executable": "/bin/true"}},
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                task_dir=str(task_dir),
                runner="codex",
                reviewer_runner=None,
                workflow="standard",
                model=None,
                sandbox_mode="danger-full-access",
                repo=None,
                dry_run=False,
                foreground=True,
                application=None,
                destination=None,
                memory_limit=None,
                assurance_config=str(assurance),
                review_packet=None,
                operation="start",
            )
            observed: dict[str, object] = {}

            def foreground_child(child_args):
                observed["token"] = child_args.launch_token
                observed["commitment"] = review_admission.admission_commitment(task_dir)
                review_admission.confirm_admission(
                    task_dir, launch_token=child_args.launch_token
                )

            with mock.patch.object(task_runner, "cmd_run_child", side_effect=foreground_child):
                task_runner.cmd_start(args)

            metadata = task_runner.read_json(task_runner.runner_meta_path(task_dir))
            binding = review_admission.bound_author_admission(task_dir)

        self.assertIsNotNone(observed["commitment"])
        self.assertEqual(observed["token"], observed["commitment"]["launch_token"])
        self.assertEqual(binding["admission_id"], observed["commitment"]["admission_id"])
        self.assertEqual(binding["assurance_strategy"], "isolated_same_provider")
        self.assertEqual(metadata["supervision_boundary"]["mode"], "foreground_process")
        self.assertNotIn("launch_pending", metadata)


class OnlyAStartedLaunchAuthorsTheNumberTests(unittest.TestCase):
    """A launch refused before its author started has authored nothing.

    The dry-run repair stated that only a launch which runs may bind the pair,
    and then equated "not a dry run" with "ran". Every refusal between the
    admission and the child was still binding the number -- the application
    launch policy first among them -- which left a launch that wrote no line of
    work recorded as the latest author: enough to refuse the bound reviewer as
    "the author's own family" and admit the family that wrote the work to review
    and close it.
    """

    def _bound_number(self, root: str) -> tuple[Path, dict]:
        """A number whose Claude author started with Codex bound to review it."""
        task_dir = _workspace_task(root)
        author = _launch(
            task_dir,
            workflow="standard",
            author_runner="claude",
            access_grant=WRITE_GRANT,
            contract=UNGATED,
            which=_installed("claude", "codex"),
        )
        return task_dir, author

    def _start(self, task_dir: Path, runner: str, application: str) -> str:
        args = argparse.Namespace(
            task_dir=str(task_dir),
            runner=runner,
            workflow="standard",
            model=None,
            sandbox_mode=None,
            repo=None,
            dry_run=False,
            application=application,
            destination=None,
        )
        # Both families count as installed, so the launch is admitted here for
        # the same reason it is on a host that has them: what is under test is
        # what happens to the binding after that.
        with mock.patch.object(
            task_runner.review_admission,
            "reviewer_available",
            lambda runner, which=None: runner in {"claude", "codex"},
        ):
            with self.assertRaises(SystemExit) as caught:
                task_runner.cmd_start(args)
        return str(caught.exception)

    def test_an_application_policy_refusal_leaves_the_author_binding_alone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir, author = self._bound_number(raw)
            message = self._start(
                task_dir, "codex", "task_agent_absent_application_1094:adapter"
            )

            ledger = review_admission.admissions(task_dir)
            binding = review_admission.bound_author_admission(task_dir)
            bound_review = review_admission.resolve_review_launch_pair(
                task_dir, reviewer_runner="codex"
            )
            author_review = review_admission.resolve_review_launch_pair(
                task_dir, reviewer_runner="claude"
            )
            started = (task_dir / ".runner" / "runner.json").exists()

        self.assertIn("Application launch policy refused the run", message)
        self.assertFalse(started)
        # Nothing was appended: the refused launch is not a second author.
        self.assertEqual(
            [entry["admission_id"] for entry in ledger], [author["admission_id"]]
        )
        self.assertEqual(binding["pair"]["author_family"], "Claude")
        self.assertEqual(binding["pair"]["reviewer_family"], "Codex")
        # And the pair still works the way the number was promised it would.
        self.assertTrue(bound_review["bound"])
        self.assertEqual(author_review["outcome"], "review_by_author_family")

    def test_a_watcher_that_never_spawns_withdraws_the_binding_it_made(self) -> None:
        """The refusals that live past the commit put the pair back themselves."""
        adapter = _RecordingTransport()
        module = types.ModuleType("task_agent_transport_watcher_probe_1094")
        module.adapter = adapter
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)

        with tempfile.TemporaryDirectory() as raw:
            task_dir, author = self._bound_number(raw)
            prior_findings = "# Findings\n\nVerdict: approved\n"
            (task_dir / "findings.md").write_text(prior_findings, encoding="utf-8")
            with mock.patch.object(
                task_runner.subprocess,
                "Popen",
                side_effect=OSError("no process could be created"),
            ):
                message = self._start(task_dir, "codex", f"{module.__name__}:adapter")

            binding = review_admission.bound_author_admission(task_dir)
            current = review_admission.recorded_admission(task_dir)
            bound_review = review_admission.resolve_review_launch_pair(
                task_dir, reviewer_runner="codex"
            )
            trace = (task_dir / "trace.md").read_text(encoding="utf-8")
            status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
            findings = (task_dir / "findings.md").read_text(encoding="utf-8")

        self.assertIn("could not start the task watcher", message)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(binding["admission_id"], author["admission_id"])
        self.assertEqual(current["admission_id"], author["admission_id"])
        self.assertTrue(bound_review["bound"])
        self.assertIn("withdrew the review binding", trace)
        self.assertEqual(findings, prior_findings)


class TheWithdrawalOutlivesTheProcessThatCommittedTests(unittest.TestCase):
    """A launch that started no author binds nothing, parent alive or not.

    Making the withdrawal an act of the parent's own closure made it an act only
    a live parent could perform. The detached watcher is supervised
    independently: it can refuse before it spawns anything long after the parent
    is gone, release the pending launch claim, and leave the phantom author on
    record -- the same reversal, reached from the process the parent does not
    control. And a parent killed between committing and spawning leaves nobody
    to withdraw anything at all.
    """

    def _bound_number(self, root: str) -> tuple[Path, dict]:
        task_dir = _workspace_task(root)
        author = _launch(
            task_dir,
            workflow="standard",
            author_runner="claude",
            access_grant=WRITE_GRANT,
            contract=UNGATED,
            which=_installed("claude", "codex"),
        )
        return task_dir, author

    def _committed_launch(
        self, task_dir: Path, runner: str, token: str, application: str | None = None
    ) -> dict:
        """What a parent leaves behind after committing and before spawning."""
        proposed = review_admission.admit_launch(
            task_dir,
            workflow="standard",
            author_runner=runner,
            access_grant=WRITE_GRANT,
            contract=UNGATED,
            which=_installed("claude", "codex"),
        )
        review_admission.commit_admission(task_dir, proposed, launch_token=token)
        prompt = task_runner.runner_prompt_path(task_dir)
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("Do the work.\n", encoding="utf-8")
        prepared = task_runner.prepared_application_launch(
            argparse.Namespace(
                task_dir=str(task_dir),
                runner=runner,
                workflow="standard",
                application=application,
                destination=None,
                memory_limit=None,
                launch_token=None,
            ),
            task_dir,
        )
        task_runner.update_runner_meta(
            task_dir,
            {
                "runner": runner,
                "workflow": "standard",
                "application": prepared,
                "launch_pending": {"token": token, "started_at": task_runner.utc_now()},
            },
        )
        return proposed

    def test_a_watcher_preflight_failure_withdraws_what_its_parent_committed(self) -> None:
        """The watcher's own end of the launch owns the withdrawal.

        No parent runs here at all: the commitment is on disk, the pending claim
        is on disk, and the watcher refuses before it spawns a child. The pair
        has to be back before the claim is released, because releasing the claim
        is what makes the task startable again with the phantom author in place.
        """
        adapter = _RecordingTransport()
        module = types.ModuleType("task_agent_transport_orphan_watcher_1094")
        module.adapter = adapter
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)

        observed: dict = {}
        released = task_runner.finish_runner_meta

        def watch_release(task_dir, extra):
            observed["binding_when_released"] = review_admission.bound_author_admission(
                task_dir
            )
            return released(task_dir, extra)

        with tempfile.TemporaryDirectory() as raw:
            task_dir, author = self._bound_number(raw)
            proposed = self._committed_launch(
                task_dir, "codex", "orphaned-launch", f"{module.__name__}:adapter"
            )
            args = argparse.Namespace(
                task_dir=str(task_dir),
                runner="codex",
                runner_resolution="explicit_flag",
                workflow="standard",
                launch_token="orphaned-launch",
                model=None,
                sandbox_mode=None,
                repo=None,
                application=f"{module.__name__}:adapter",
                destination=None,
                memory_limit=None,
                require_review_verdict=False,
            )
            with mock.patch.object(
                task_runner, "prepare_access_grant", side_effect=SystemExit("no sandbox here")
            ), mock.patch.object(task_runner, "finish_runner_meta", watch_release):
                with self.assertRaises(SystemExit):
                    task_runner.cmd_run_child(args)

            binding = review_admission.bound_author_admission(task_dir)
            meta = json.loads(
                (task_dir / ".runner" / "runner.json").read_text(encoding="utf-8")
            )
            withdrawals = [
                entry
                for entry in review_admission.admissions(task_dir)
                if entry.get("kind") == review_admission.ANNULLED_ADMISSION
            ]
            bound_review = review_admission.resolve_review_launch_pair(
                task_dir, reviewer_runner="codex"
            )
            author_review = review_admission.resolve_review_launch_pair(
                task_dir, reviewer_runner="claude"
            )
            trace = (task_dir / "trace.md").read_text(encoding="utf-8")

        # The claim is released, which is the whole hazard: the number is
        # startable again, and it is startable with its real author's pair.
        self.assertNotIn("launch_pending", meta)
        self.assertEqual(observed["binding_when_released"]["admission_id"], author["admission_id"])
        self.assertEqual(binding["admission_id"], author["admission_id"])
        self.assertEqual(binding["pair"]["author_family"], "Claude")
        self.assertEqual(binding["pair"]["reviewer_family"], "Codex")
        self.assertEqual([entry["annuls"] for entry in withdrawals], [proposed["admission_id"]])
        self.assertIsNone(review_admission.admission_commitment(task_dir))
        self.assertTrue(bound_review["bound"])
        self.assertEqual(author_review["outcome"], "review_by_author_family")
        self.assertIn("withdrew the review binding", trace)

    def test_a_spawned_author_confirms_the_commitment_that_binds_it(self) -> None:
        """The commitment stops being outstanding where the author appears.

        Nothing else in a launch can say this. Without the confirmation at the
        spawn, every real launch would leave its commitment outstanding, and the
        number would keep answering with the pair of some earlier launch while
        its actual author worked.
        """
        adapter = _RecordingTransport()
        module = types.ModuleType("task_agent_transport_spawned_author_1094")
        module.adapter = adapter
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)

        class _Spawned:
            """A child that exists, which is all the confirmation is about."""

            pid = 4242

            def wait(self) -> int:
                raise _StopAfterSpawn()

        with tempfile.TemporaryDirectory() as raw:
            task_dir, author = self._bound_number(raw)
            spawned = self._committed_launch(
                task_dir, "codex", "spawning-launch", f"{module.__name__}:adapter"
            )
            args = argparse.Namespace(
                task_dir=str(task_dir),
                runner="codex",
                runner_resolution="explicit_flag",
                workflow="standard",
                launch_token="spawning-launch",
                model=None,
                sandbox_mode=None,
                repo=None,
                application=f"{module.__name__}:adapter",
                destination=None,
                memory_limit=None,
                require_review_verdict=False,
            )
            with mock.patch.object(
                task_runner.subprocess, "Popen", return_value=_Spawned()
            ):
                with self.assertRaises(_StopAfterSpawn):
                    task_runner.cmd_run_child(args)

            binding = review_admission.bound_author_admission(task_dir)
            outstanding = review_admission.admission_commitment(task_dir)

        self.assertIsNone(outstanding)
        self.assertEqual(binding["admission_id"], spawned["admission_id"])
        self.assertEqual(binding["pair"]["author_family"], "Codex")
        self.assertNotEqual(binding["admission_id"], author["admission_id"])

    def test_a_parent_lost_before_the_watcher_exists_binds_nothing(self) -> None:
        """Nobody is left to withdraw it, so nothing has to be.

        The commitment is outstanding and stays outstanding, and an outstanding
        commitment is not a binding. The number answers with the pair whose
        author actually ran, for reviewers and for acceptance alike.
        """
        with tempfile.TemporaryDirectory() as raw:
            task_dir, author = self._bound_number(raw)
            self._committed_launch(task_dir, "codex", "vanished-parent")

            binding = review_admission.bound_author_admission(task_dir)
            bound_review = review_admission.resolve_review_launch_pair(
                task_dir, reviewer_runner="codex"
            )
            author_review = review_admission.resolve_review_launch_pair(
                task_dir, reviewer_runner="claude"
            )
            status = review_admission.independent_review_status(task_dir)

        self.assertEqual(binding["admission_id"], author["admission_id"])
        self.assertEqual(binding["pair"]["author_family"], "Claude")
        self.assertTrue(bound_review["bound"])
        self.assertEqual(author_review["outcome"], "review_by_author_family")
        self.assertEqual(status["reviewer_family"], "Codex")
        self.assertEqual(status["author_family"], "Claude")

    def test_a_launch_that_started_its_author_keeps_the_binding(self) -> None:
        """The rule cuts both ways, and the other way is a reversal too.

        Once the author exists it may be writing work, and that work keeps the
        reviewer it was admitted with. A refusal arriving after the author
        started -- a parent that cannot read the startup record it was handed --
        withdraws nothing, because handing started work back to the pair the
        number had before it is the same substitution from the other side.
        """
        with tempfile.TemporaryDirectory() as raw:
            task_dir, author = self._bound_number(raw)
            started = self._committed_launch(task_dir, "codex", "started-launch")
            confirmed = review_admission.confirm_admission(
                task_dir, launch_token="started-launch"
            )
            late = review_admission.annul_admission(
                task_dir,
                reason="the parent could not read the startup record.",
                launch_token="started-launch",
            )

            binding = review_admission.bound_author_admission(task_dir)

        self.assertEqual(confirmed["admission_id"], started["admission_id"])
        self.assertIsNone(late)
        self.assertEqual(binding["admission_id"], started["admission_id"])
        self.assertNotEqual(binding["admission_id"], author["admission_id"])

    def test_only_the_launch_that_committed_may_settle_the_commitment(self) -> None:
        """A settlement is about one launch, and it says which one it is."""
        with tempfile.TemporaryDirectory() as raw:
            task_dir, _ = self._bound_number(raw)
            proposed = self._committed_launch(task_dir, "codex", "this-launch")

            self.assertIsNone(
                review_admission.confirm_admission(task_dir, launch_token="another-launch")
            )
            self.assertIsNone(
                review_admission.annul_admission(
                    task_dir, reason="an unrelated failure.", launch_token="another-launch"
                )
            )
            outstanding = review_admission.admission_commitment(task_dir)

        self.assertEqual(outstanding["admission_id"], proposed["admission_id"])
        self.assertEqual(outstanding["launch_token"], "this-launch")


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
            configured_resolver=_installed("claude", "codex"),
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

    def test_invalid_assurance_is_refused_before_pair_binding(self) -> None:
        assurance = {**CODEX_ASSURANCE, "review_provider": "claude"}
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._admit(Path(raw), assurance)
        record = caught.exception.record
        self.assertEqual(record["pair"]["outcome"], "invalid_assurance_configuration")
        self.assertEqual(record["assurance_strategy"], "cross_provider")

    def test_assurance_reviewing_with_another_family_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                review_admission.admit_launch(
                    Path(raw),
                    workflow="dev-pipeline",
                    author_runner="claude",
                    access_grant=WRITE_GRANT,
                    contract=UNGATED,
                    declared_reviewer="claude",
                    assurance=CODEX_ASSURANCE,
                    which=_installed("claude", "codex"),
                    configured_resolver=_installed("claude", "codex"),
                )
        self.assertEqual(
            caught.exception.record["pair"]["outcome"],
            "declared_reviewer_conflicts_with_assurance",
        )

    def test_assurance_that_reviews_with_nobody_is_refused(self) -> None:
        assurance = {**CODEX_ASSURANCE, "review_provider": None}
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._admit(Path(raw), assurance)
        self.assertEqual(
            caught.exception.record["pair"]["outcome"],
            "invalid_assurance_configuration",
        )

    def test_cross_provider_config_cannot_select_cursor_as_reviewer(self) -> None:
        assurance = {
            **CODEX_ASSURANCE,
            "review_provider": "cursor",
            "providers": {
                **CODEX_ASSURANCE["providers"],
                "cursor": {"executable": "cursor-agent"},
            },
        }
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                review_admission.admit_launch(
                    Path(raw), workflow="dev-pipeline", author_runner="claude",
                    access_grant=WRITE_GRANT, contract=UNGATED, assurance=assurance,
                    which=_installed("claude", "agent"),
                    configured_resolver=_installed("claude", "agent"),
                )
        self.assertEqual(caught.exception.record["pair"]["outcome"], "reviewer_not_supported")
        self.assertIn("never a reviewer", caught.exception.record["message"])
        self.assertIn("Choose Codex or Claude", caught.exception.record["refusal_action"])

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

    def test_standard_launch_uses_isolated_same_provider_assurance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            record = review_admission.admit_launch(
                Path(raw), workflow="standard", author_runner="claude",
                access_grant=WRITE_GRANT, contract=UNGATED,
                assurance=SAME_PROVIDER_ASSURANCE, which=_installed("claude"),
                configured_resolver=_installed("claude"),
            )
        self.assertEqual(record["decision"], "admitted")
        self.assertEqual(record["pair"]["reviewer_runner"], "claude")
        self.assertIn("isolated_same_provider", record["message"])


class ReviewLaunchPairingTests(unittest.TestCase):
    """The review of a number has to be the family that number was promised."""

    def _author(self, task_dir: Path, author: str = "claude") -> dict:
        return _launch(
            task_dir,
            workflow="standard",
            author_runner=author,
            access_grant=WRITE_GRANT,
            contract=UNGATED,
            which=_installed("claude", "codex"),
        )

    def _review(
        self,
        task_dir: Path,
        reviewer: str,
        access_grant: dict = READ_ONLY_GRANT,
    ) -> dict:
        return _launch(
            task_dir,
            workflow="standard",
            author_runner=reviewer,
            access_grant=access_grant,
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

    def test_cursor_cannot_review_the_number_even_when_not_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._author(task_dir)
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._review(task_dir, "agent")
        self.assertEqual(caught.exception.record["pair"]["outcome"], "reviewer_not_supported")
        self.assertIn("never a reviewer", caught.exception.record["message"])

    def test_cursor_cannot_review_a_number_without_a_local_author_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._review(Path(raw), "agent")
        self.assertEqual(caught.exception.record["pair"]["outcome"], "reviewer_not_supported")

    def test_same_provider_review_with_write_access_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            _launch(
                task_dir, workflow="standard", author_runner="claude",
                access_grant=WRITE_GRANT, contract=UNGATED,
                assurance=SAME_PROVIDER_ASSURANCE, which=_installed("claude"),
                configured_resolver=_installed("claude"),
            )
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._review(task_dir, "claude", WRITE_GRANT)
        record = caught.exception.record
        self.assertEqual(record["pair"]["outcome"], "same_provider_review_not_read_only")
        self.assertIn("workspace-write", record["message"])
        self.assertIn("task_runner.py review", record["refusal_action"])

    def test_same_provider_review_without_repo_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            _launch(
                task_dir, workflow="standard", author_runner="claude",
                access_grant=WRITE_GRANT, contract=UNGATED,
                assurance=SAME_PROVIDER_ASSURANCE, which=_installed("claude"),
                configured_resolver=_installed("claude"),
            )
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._review(task_dir, "claude", WORKSPACE_WRITE_WITHOUT_REPO)
        record = caught.exception.record
        self.assertEqual(record["pair"]["outcome"], "same_provider_review_not_read_only")
        self.assertIn("workspace-write", record["message"])

    def test_same_provider_review_with_a_contradictory_grant_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            _launch(
                task_dir, workflow="standard", author_runner="claude",
                access_grant=WRITE_GRANT, contract=UNGATED,
                assurance=SAME_PROVIDER_ASSURANCE, which=_installed("claude"),
                configured_resolver=_installed("claude"),
            )
            contradictory = {**READ_ONLY_GRANT, "grants_write": True}
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._review(task_dir, "claude", contradictory)
        self.assertEqual(
            caught.exception.record["pair"]["outcome"],
            "same_provider_review_not_read_only",
        )

    def test_a_family_that_was_not_bound_cannot_review_the_number(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            _launch(
                task_dir, workflow="standard", author_runner="claude",
                access_grant=WRITE_GRANT, contract=UNGATED,
                assurance=SAME_PROVIDER_ASSURANCE, which=_installed("claude"),
                configured_resolver=_installed("claude"),
            )
            with self.assertRaises(review_admission.ReviewAdmissionError) as caught:
                self._review(task_dir, "codex")
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


class ReviewCommandTests(unittest.TestCase):
    def test_review_command_uses_the_bound_family_and_read_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            _launch(
                task_dir,
                workflow="standard",
                author_runner="claude",
                access_grant=WRITE_GRANT,
                contract=UNGATED,
                which=_installed("claude", "codex"),
            )
            args = argparse.Namespace(
                task_dir=str(task_dir),
                repo="/srv/target-repo",
                model=None,
                application=None,
                destination=None,
                memory_limit=None,
                dry_run=True,
            )
            with mock.patch.object(task_runner, "cmd_start") as start:
                task_runner.cmd_review(args)

        start.assert_called_once_with(args)
        self.assertEqual(args.runner, "codex")
        self.assertEqual(args.workflow, "standard")
        self.assertEqual(args.sandbox_mode, "read-only")
        self.assertTrue(args.require_review_verdict)

    def test_review_command_refuses_a_legacy_cursor_binding(self) -> None:
        args = argparse.Namespace(task_dir="/tmp/task")
        admission = {"pair": {"reviewer_runner": "agent"}}
        with mock.patch.object(
            review_admission, "bound_author_admission", return_value=admission
        ), self.assertRaises(SystemExit, msg="Cursor cannot review"):
            task_runner.cmd_review(args)

    def test_review_command_uses_same_provider_only_for_isolated_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            _launch(
                task_dir, workflow="standard", author_runner="claude",
                access_grant=WRITE_GRANT, contract=UNGATED,
                assurance=SAME_PROVIDER_ASSURANCE, which=_installed("claude"),
                configured_resolver=_installed("claude"),
            )
            args = argparse.Namespace(
                task_dir=str(task_dir), repo="/srv/target-repo", model=None,
                application=None, destination=None, memory_limit=None, dry_run=True,
            )
            with mock.patch.object(task_runner, "cmd_start") as start:
                task_runner.cmd_review(args)
        start.assert_called_once_with(args)
        self.assertEqual(args.runner, "claude")
        self.assertEqual(args.sandbox_mode, "read-only")
        self.assertTrue(args.require_review_verdict)

    def test_review_command_refuses_live_only_binding(self) -> None:
        admission = {
            "pair": {
                "reviewer_runner": None,
                "assurance_strategy": "live_acceptance_only",
            }
        }
        with mock.patch.object(
            review_admission, "bound_author_admission", return_value=admission
        ), self.assertRaises(SystemExit):
            task_runner.cmd_review(argparse.Namespace(task_dir="/tmp/task"))

    def test_review_is_visible_in_top_level_help(self) -> None:
        with mock.patch.object(sys, "argv", ["task_runner.py", "--help"]):
            with self.assertRaises(SystemExit) as caught:
                with mock.patch("sys.stdout") as output:
                    task_runner.parse_args()
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("review", "".join(call.args[0] for call in output.write.call_args_list))


class IndependentReviewStatusTests(unittest.TestCase):
    """Acceptance asks whether the work as it stands carries that approval."""

    def _admitted(self, task_dir: Path) -> None:
        _launch(
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

    def _admitted_same_provider(self, task_dir: Path) -> None:
        _launch(
            task_dir,
            workflow="standard",
            author_runner="claude",
            access_grant=WRITE_GRANT,
            contract=UNGATED,
            assurance=SAME_PROVIDER_ASSURANCE,
            which=_installed("claude"),
            configured_resolver=_installed("claude"),
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
        self.assertIn("task_runner.py review", status["action"])

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

    def test_same_family_approval_satisfies_explicit_isolated_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            self._admitted_same_provider(task_dir)
            self._round(task_dir, "approved", provider="claude")
            status = review_admission.independent_review_status(task_dir)
        self.assertTrue(status["required"])
        self.assertTrue(status["satisfied"])
        self.assertIn("separate read-only session", status["reason"])

    def test_live_only_claims_no_model_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task_dir = Path(raw)
            _launch(
                task_dir, workflow="standard", author_runner="claude",
                access_grant=WRITE_GRANT, contract=UNGATED,
                assurance=LIVE_ONLY_ASSURANCE, which=_installed("claude"),
                configured_resolver=_installed("claude"),
            )
            status = review_admission.independent_review_status(task_dir)
        self.assertFalse(status["required"])
        self.assertTrue(status["satisfied"])
        self.assertEqual(status["assurance_strategy"], "live_acceptance_only")
        self.assertNotIn("last_round", status)

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
        _launch(
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
        self.assertIn("task_runner.py review", reason)

    def test_standard_live_only_requires_each_configured_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            _launch(
                task, workflow="standard", author_runner="claude",
                access_grant=WRITE_GRANT, contract=UNGATED,
                assurance=LIVE_ONLY_ASSURANCE, which=_installed("claude"),
                configured_resolver=_installed("claude"),
            )
            ready_before, reason = task_completion.completion_ready(
                task, workflow="standard"
            )
            (task / "verification.md").write_text(
                "## real_user_path\n\n- Result: **PASS**\n- Evidence: live path passed.\n",
                encoding="utf-8",
            )
            ready_after, _ = task_completion.completion_ready(task, workflow="standard")
        self.assertFalse(ready_before)
        self.assertIn("live_acceptance_only", reason)
        self.assertTrue(ready_after)

    def test_live_only_refuses_a_model_verdict_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            _launch(
                task, workflow="standard", author_runner="claude",
                access_grant=WRITE_GRANT, contract=UNGATED,
                assurance=LIVE_ONLY_ASSURANCE, which=_installed("claude"),
                configured_resolver=_installed("claude"),
            )
            (task / "verification.md").write_text(
                "## real_user_path\n\n- Result: **PASS**\n- Evidence: live path passed.\n",
                encoding="utf-8",
            )
            review_admission.record_review_round(
                task, event_id="model-verdict", decision={"decision": "approved"},
                review_provider="claude",
            )
            ready, reason = task_completion.completion_ready(task, workflow="standard")
        self.assertFalse(ready)
        self.assertIn("names no model reviewer", reason)

    def test_the_reviewers_published_verdict_becomes_the_round(self) -> None:
        """A standard review reaches the ledger through its own verdict line."""
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw))
            self._admit_author(task)
            _launch(
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
        self.assertIn("task_runner.py review", reason)

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

    def test_approved_standard_review_completes_canonical_metadata_itself(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw) / "tasks")
            task_md = (task / "task.md").read_text(encoding="utf-8")
            (task / "task.md").write_text(
                task_md.replace('status: "completed"', 'status: "blocked"'),
                encoding="utf-8",
            )
            self._admit_author(task)
            _launch(
                task,
                workflow="standard",
                author_runner="codex",
                access_grant=READ_ONLY_GRANT,
                contract=UNGATED,
                review_launch=True,
                which=_installed("claude", "codex"),
            )
            (task / "findings.md").write_text(
                "# Findings\n\nVerdict: approved\n", encoding="utf-8"
            )
            task_runner.write_json(
                task_runner.status_path(task),
                {"state": "blocked", "current_step": "waiting for review"},
            )

            task_runner.finalize_child_lifecycle(task, "standard", "codex", 0)

            status = task_runner.read_json(task_runner.status_path(task))
            self.assertEqual(task_completion.task_status(task), "completed")
            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["review_result"], "approved")
            self.assertEqual(review_admission.review_rounds(task)[-1]["decision"], "approved")

    def test_run_child_keeps_scope_cleanup_refusal_after_approved_review(self) -> None:
        class ApprovedReviewProcess:
            pid = 4242

            def __init__(self, task: Path):
                self.task = task

            def wait(self) -> int:
                (self.task / "findings.md").write_text(
                    "# Findings\n\nVerdict: approved\n", encoding="utf-8"
                )
                task_runner.write_json(
                    task_runner.status_path(self.task),
                    {"state": "completed", "current_step": "review child finished"},
                )
                return 0

        not_empty = {
            "outcome": "not_empty",
            "reason": "processes_survived",
            "initial_pids": [4243],
            "remaining_pids": [4243],
        }

        def complete_metadata(task: Path) -> None:
            task_md = (task / "task.md").read_text(encoding="utf-8")
            (task / "task.md").write_text(
                task_md.replace('status: "blocked"', 'status: "completed"'),
                encoding="utf-8",
            )

        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw) / "tasks")
            task_md = (task / "task.md").read_text(encoding="utf-8")
            (task / "task.md").write_text(
                task_md.replace('status: "completed"', 'status: "blocked"'),
                encoding="utf-8",
            )
            self._admit_author(task)
            _launch(
                task,
                workflow="standard",
                author_runner="codex",
                access_grant=READ_ONLY_GRANT,
                contract=UNGATED,
                review_launch=True,
                which=_installed("claude", "codex"),
            )
            args = argparse.Namespace(
                task_dir=str(task),
                runner="codex",
                runner_resolution="explicit_flag",
                workflow="standard",
                launch_token=None,
                model=None,
                sandbox_mode="read-only",
                repo=None,
                application=None,
                destination=None,
                memory_limit=None,
                state_dir=None,
                previous_state_dir=None,
                operation="start",
                require_review_verdict=True,
                dev_pipeline_bin=None,
                assurance_config=None,
                review_packet=None,
            )
            with mock.patch.object(
                task_runner, "prepare_access_grant", return_value=([], READ_ONLY_GRANT)
            ), mock.patch.object(
                task_runner, "build_command", return_value=["/usr/bin/codex"]
            ), mock.patch.object(
                task_runner.subprocess,
                "Popen",
                return_value=ApprovedReviewProcess(task),
            ), mock.patch.object(
                task_runner.task_workspace, "drain_task_scope", return_value=not_empty
            ), mock.patch.object(
                task_runner, "complete_task_metadata", side_effect=complete_metadata
            ):
                task_runner.cmd_run_child(args)

            status = task_runner.read_json(task_runner.status_path(task))
            metadata = task_runner.read_json(task_runner.runner_meta_path(task))
            rounds = review_admission.review_rounds(task)
            self.assertEqual(status["state"], "blocked")
            self.assertIn("task cgroup could not be proven empty", status["current_step"])
            self.assertEqual(task_completion.task_status(task), "blocked")
            self.assertEqual(rounds[-1]["decision"], "approved")
            self.assertEqual(metadata["scope_cleanup"], not_empty)
            self.assertNotIn("workspace_cleanup", metadata)

    def test_refused_completion_demotes_premature_completed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw) / "tasks")
            self._admit_author(task)
            task_runner.write_json(
                task_runner.status_path(task),
                {"state": "completed", "current_step": "child claimed completion"},
            )

            task_runner.finalize_child_lifecycle(task, "standard", "claude", 0)

            status = task_runner.read_json(task_runner.status_path(task))
            self.assertEqual(task_completion.task_status(task), "blocked")
            self.assertEqual(status["state"], "blocked")
            self.assertIn("independent review", status["current_step"])

    def test_child_written_blocked_state_is_canonical_for_observers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw) / "tasks")
            task_runner.write_json(
                task_runner.status_path(task),
                {"state": "blocked", "current_step": "waiting for review"},
            )

            task_runner.finalize_child_lifecycle(task, "standard", "claude", 0)

            self.assertEqual(task_completion.task_status(task), "blocked")
            self.assertEqual(
                task_runner.read_json(task_runner.status_path(task))["state"], "blocked"
            )

    def test_child_written_failed_state_is_canonical_for_observers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw) / "tasks")
            task_runner.write_json(
                task_runner.status_path(task),
                {"state": "failed", "current_step": "child reported failure"},
            )

            task_runner.finalize_child_lifecycle(task, "standard", "claude", 1)

            self.assertEqual(task_completion.task_status(task), "blocked")
            self.assertEqual(
                task_runner.read_json(task_runner.status_path(task))["state"], "failed"
            )

    def test_nonzero_exit_is_canonical_for_observers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            task = self._task(Path(raw) / "tasks")
            task_runner.write_json(
                task_runner.status_path(task),
                {"state": "running", "current_step": "child running"},
            )

            task_runner.finalize_child_lifecycle(task, "standard", "claude", 7)

            self.assertEqual(task_completion.task_status(task), "blocked")
            self.assertEqual(
                task_runner.read_json(task_runner.status_path(task))["state"], "failed"
            )


if __name__ == "__main__":
    unittest.main()
