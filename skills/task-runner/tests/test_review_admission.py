"""Material work gets an independent reviewer before its author starts."""

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


review_admission = _load("review_admission")


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
