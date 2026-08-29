import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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

    def test_a_clean_dev_pipeline_exit_is_not_a_completion(self) -> None:
        # The workflow reports its outcome through lifecycle events. A quiet
        # exit means the adapter stopped reading, not that the work finished.
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp, "running")
            task_runner.finalize_child_lifecycle(task_dir, "dev-pipeline", "codex", 0)
            status = json.loads(task_runner.status_path(task_dir).read_text(encoding="utf-8"))
            trace = task_runner.trace_path(task_dir).read_text(encoding="utf-8")
        self.assertEqual(status["state"], "failed")
        self.assertIn("without a terminal lifecycle event", status["current_step"])
        self.assertIn("Rejected the dev-pipeline process exit", trace)

    def test_a_dev_pipeline_terminal_refusal_or_failure_is_kept(self) -> None:
        for state in ("blocked", "failed"):
            with self.subTest(state=state):
                with tempfile.TemporaryDirectory() as tmp:
                    task_dir = self._task_dir(tmp, state)
                    task_runner.finalize_child_lifecycle(task_dir, "dev-pipeline", "codex", 0)
                    status = json.loads(
                        task_runner.status_path(task_dir).read_text(encoding="utf-8")
                    )
                self.assertEqual(status["state"], state)

    def test_clean_exit_without_durable_completion_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp, "running")
            task_runner.finalize_child_lifecycle(task_dir, "standard", "codex", 0)
            status = json.loads(task_runner.status_path(task_dir).read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "blocked")
        self.assertIn("completion_refusal", status)

    def test_clean_standard_completion_records_write_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp, "running")
            with (
                mock.patch.object(
                    task_runner, "completion_ready", return_value=(True, "")
                ),
                mock.patch.object(
                    task_runner.write_admission, "record_completion_acceptance"
                ) as record,
            ):
                task_runner.finalize_child_lifecycle(
                    task_dir, "standard", "codex", 0
                )
            record.assert_called_once_with(task_dir)

    def test_successful_statement_review_uses_nonterminal_run_state_after_application_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp, "running")
            task_runner.runner_dir(task_dir).mkdir()
            task_runner.write_json(
                task_runner.runner_meta_path(task_dir), {"review_kind": "statement"}
            )
            application = mock.Mock()
            application.standard_run_finished.return_value = None
            with (
                mock.patch.object(task_runner, "load_application", return_value=application),
                mock.patch.object(
                    task_runner.product_review,
                    "validate_result",
                    return_value=(True, "statement review passed", {}),
                ),
            ):
                task_runner.finalize_child_lifecycle(
                    task_dir, "standard", "claude", 0
                )
            status = task_runner.read_json(task_runner.status_path(task_dir))
        application.standard_run_finished.assert_called_once()
        self.assertEqual(status["state"], "statement_review_finished")
        self.assertTrue(status["statement_review_passed"])
        self.assertEqual(status["current_step"], "statement review passed")
        self.assertEqual(task_runner.task_phases.current_phase(task_dir), "planned")

    def test_original_watcher_records_statement_review_as_its_own_run_outcome(self) -> None:
        self.assertEqual(
            task_runner.run_outcome_for_state("statement_review_finished", 0),
            "statement_review_finished",
        )
        self.assertEqual(
            task_runner.run_outcome_for_state("statement_review_finished", 9),
            "failed",
        )

    def test_recovered_watcher_uses_the_same_statement_review_vocabulary(self) -> None:
        self.assertEqual(
            task_runner.run_outcome_for_state(
                "statement_review_finished", None, recovered=True
            ),
            "recovered_statement_review_finished",
        )

    def test_statement_review_quota_pause_is_owned_before_result_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp, "running")
            task_runner.runner_dir(task_dir).mkdir()
            task_runner.write_json(
                task_runner.runner_meta_path(task_dir), {"review_kind": "statement"}
            )
            application = mock.Mock()
            application.standard_run_finished.return_value = mock.Mock(
                state="waiting_for_quota",
                current_step="resume after reset",
                metadata={"quota_wait": {"runner": "claude"}},
            )
            with (
                mock.patch.object(task_runner, "load_application", return_value=application),
                mock.patch.object(task_runner.product_review, "validate_result") as validate,
                mock.patch.object(task_runner, "block_task_metadata"),
            ):
                task_runner.finalize_child_lifecycle(
                    task_dir, "standard", "claude", 1
                )
            status = task_runner.read_json(task_runner.status_path(task_dir))
        validate.assert_not_called()
        self.assertEqual(status["state"], "waiting_for_quota")

    def test_a_child_written_completed_state_still_needs_durable_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp, "completed")
            task_runner.finalize_child_lifecycle(task_dir, "standard", "codex", 5)
            status = json.loads(
                task_runner.status_path(task_dir).read_text(encoding="utf-8")
            )
        self.assertEqual(status["state"], "blocked")
        self.assertIn("completion_refusal", status)

    def test_child_written_completed_state_records_write_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task_dir(tmp, "completed")
            with (
                mock.patch.object(
                    task_runner, "completion_ready", return_value=(True, "")
                ),
                mock.patch.object(
                    task_runner.write_admission, "record_completion_acceptance"
                ) as record,
            ):
                task_runner.finalize_child_lifecycle(
                    task_dir, "dev-pipeline", "codex", 0
                )
            record.assert_called_once_with(task_dir)
            self.assertEqual(task_runner.task_phases.current_phase(task_dir), "completed")

    def test_a_child_written_refusal_or_failure_is_never_overwritten(self) -> None:
        for state in ("blocked", "failed"):
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
            task_dir = self._write(tmp, {"schema_version": 1, "activity": "   ", "updated_at": "2026-07-27T00:00:00Z"})
            self.assertIsNone(task_runner.structured_progress(task_dir))

    def test_complete_counts_produce_a_percentage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._write(
                tmp,
                {
                    "schema_version": 1,
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
            {"schema_version": 1, "activity": "Working", "updated_at": "2026-07-27T00:00:00Z", "completed": 3},
            {"schema_version": 1, "activity": "Working", "updated_at": "2026-07-27T00:00:00Z", "total": 8},
            {"schema_version": 1, "activity": "Working", "updated_at": "2026-07-27T00:00:00Z", "completed": 3, "total": 8},
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
                            "schema_version": 1,
                            "activity": "Working",
                            "updated_at": "2026-07-27T00:00:00Z",
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
                    "schema_version": 1,
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

    def test_prompt_carries_the_no_code_first_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._prompt(tmp)
        self.assertIn("Before writing code, try in order", prompt)
        self.assertIn("only then add the smallest necessary code", prompt)

    def test_prompt_declares_only_a_safe_bound_review_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._prompt(tmp)
        self.assertIn('"phase_handoff": {"kind": "bound_independent_review"}', prompt)
        self.assertIn("Do not declare that handoff when user action", prompt)
        self.assertIn("only the user or an external environment can supply", prompt)
        self.assertIn("executor or existing pipeline does not prevent", prompt)
        self.assertIn("records `completed` does not need to predict", prompt)
        self.assertIn("validates the declaration against the review ledger", prompt)

    def test_review_prompt_names_role_subject_author_repository_and_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "001-review"
            task_dir.mkdir()
            repository = Path(tmp) / "subject"
            repository.mkdir()
            prompt = task_runner.build_child_prompt(
                task_dir,
                repository=repository,
                review_subject="009-subject",
                review_subject_author="codex",
                require_review_verdict=True,
            )
        self.assertIn("Role: independent reviewer", prompt)
        self.assertIn("`009-subject`", prompt)
        self.assertIn("`codex`", prompt)
        self.assertIn(str(repository), prompt)
        self.assertIn("Verdict: approved", prompt)
        self.assertIn("do not re-execute or repair", prompt)

    def test_review_prompt_identity_reads_author_from_admitted_pair(self) -> None:
        task_dir = Path("/tmp/009-subject")
        subject, author = task_runner.review_prompt_identity(
            task_dir, {"pair": {"author_runner": "codex"}}, True
        )
        self.assertEqual(subject, str(task_dir))
        self.assertEqual(author, "codex")

    def test_product_review_prompt_stages_product_evidence_before_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "001-review"
            task_dir.mkdir()
            packet = task_dir / "product-review-packet.md"
            packet.write_text("# Product review packet\n", encoding="utf-8")
            (task_dir / "user-verbatim.json").write_text(
                '{"schema_version":1,"messages":[{"channel":"cli","source_id":"m1",'
                '"occurred_at":"2026-08-26T00:00:00Z","text":"do the exact job"}]}\n',
                encoding="utf-8",
            )
            packet_sha256 = hashlib.sha256(packet.read_bytes()).hexdigest()
            subject = Path(tmp) / "subject"
            subject.mkdir()
            subprocess.run(["git", "init", "-q", str(subject)], check=True)
            subprocess.run(["git", "-C", str(subject), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(subject), "config", "user.name", "Test"], check=True)
            (subject / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(subject), "add", "candidate.txt"], check=True)
            subprocess.run(["git", "-C", str(subject), "commit", "-qm", "candidate"], check=True)
            prompt = task_runner.build_child_prompt(
                task_dir,
                repository=subject,
                review_subject="001-review",
                review_subject_author="codex",
                require_review_verdict=True,
                product_review_packet=packet,
                review_admission_record={"admission_id": "review-1"},
            )
        self.assertIn("Role: fresh independent product and technical reviewer", prompt)
        self.assertIn("1. Read only", prompt)
        self.assertIn("User job:", prompt)
        self.assertIn("Required actor:", prompt)
        self.assertIn("Observable result:", prompt)
        self.assertIn("Strongest false proxy:", prompt)
        self.assertIn("Product verdict: not established", prompt)
        self.assertIn("Neither verdict substitutes for the other", prompt)
        self.assertIn("a concrete next-step", prompt)
        self.assertIn("Cover every source_id from both `messages` and `excluded_messages`", prompt)
        self.assertIn("`not_a_requirement`", prompt)
        self.assertIn("`out_of_scope`", prompt)
        self.assertIn(packet_sha256, prompt)
        self.assertNotIn("1. Read `", prompt)
        for domain_hint in ("MOEX", "trading", "replay", "121:121"):
            self.assertNotIn(domain_hint, prompt)

    def test_statement_review_prompt_never_requests_implementation_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "001-review"
            task_dir.mkdir()
            (task_dir / "task.md").write_text("# Exact statement\n", encoding="utf-8")
            (task_dir / "task_contract.json").write_text('{"version": 1}\n', encoding="utf-8")
            packet = task_dir / "statement-packet.json"
            packet.write_text('{"verbatim_user_intent":"do the exact job"}\n', encoding="utf-8")
            (task_dir / "user-verbatim.json").write_text(
                '{"schema_version":1,"messages":[{"channel":"cli","source_id":"m1",'
                '"occurred_at":"2026-08-26T00:00:00Z","text":"do the exact job"}]}\n',
                encoding="utf-8",
            )
            prompt = task_runner.build_child_prompt(
                task_dir,
                statement_review_packet=packet,
                statement_author_runner="codex",
                review_admission_record={"admission_id": "statement-review-1"},
            )
        self.assertIn("Role: fresh independent statement product reviewer", prompt)
        self.assertIn("Do not inspect implementation code, diffs, tests", prompt)
        self.assertIn("complete readable statement", prompt)
        self.assertIn("do not embed a second raw Markdown", prompt)
        self.assertIn("review_admission_id` as `statement-review-1`", prompt)
        self.assertIn("Cover every source_id from both `messages` and `excluded_messages`", prompt)
        self.assertIn("Do not set the task itself to `completed` or `blocked`", prompt)
        self.assertNotIn("Product verdict: not established", prompt)
        self.assertNotIn("Verdict: approved", prompt)
        self.assertNotIn("has `state` set to `completed` or `blocked`", prompt)

    def test_prompt_has_no_hardcoded_absolute_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = self._prompt(tmp)
        self.assertIn(str(task_runner.workspace_root()), prompt)


class ChildInstructionOwnerTests(unittest.TestCase):
    """`build_child_prompt` is the only executor procedure this repository has.

    A published `skills/task-executor/SKILL.md` once told readers that a standard
    child follows an ordered document. Nothing loaded it: the launcher names no
    skill, and the child receives the text built here. The document was true of
    nothing and could drift from the prompt without any test noticing, so it was
    removed rather than wired up.

    These checks hold that decision from both sides. The prompt must still carry
    the procedure, and no document may restate it or send a child to a file for
    it.
    """

    REPO = Path(__file__).resolve().parents[3]

    # Verbatim from the built prompt. A document that contains one of these has
    # started keeping a second copy of the procedure.
    PROMPT_SENTENCES = (
        "Before doing substantial work:",
        "Append a final trace entry summarizing what was done.",
        "Do not store task outputs inside",
    )

    def _tracked(self, suffixes: tuple[str, ...]) -> list[str]:
        listing = subprocess.run(
            ["git", "-C", str(self.REPO), "ls-files", "-z"],
            capture_output=True, text=True, check=True).stdout
        return [name for name in listing.split("\0")
                if name and name.endswith(suffixes)]

    def _prompt(self) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "001-example"
            task_dir.mkdir()
            return task_runner.build_child_prompt(task_dir)

    def test_the_generated_prompt_states_the_ordered_procedure(self) -> None:
        prompt = self._prompt()
        self.assertIn("Before doing substantial work:", prompt)
        self.assertIn("While working:", prompt)
        self.assertIn("Before finishing:", prompt)
        for sentence in self.PROMPT_SENTENCES:
            self.assertIn(sentence, prompt)

    def test_standard_prompt_activates_context_discovery_before_work(self) -> None:
        prompt = self._prompt()
        activation = "Invoke `context-discovery` by reading and following"
        self.assertIn(activation, prompt)
        self.assertIn("skills/context-discovery/SKILL.md", prompt)
        self.assertIn(
            "Do not continue until the skill's own completion condition is satisfied",
            " ".join(prompt.split()),
        )
        self.assertIn("every result it requires is recorded", " ".join(prompt.split()))
        self.assertLess(prompt.index(activation), prompt.index("While working:"))

    def test_no_document_keeps_a_second_copy_of_the_procedure(self) -> None:
        for name in self._tracked((".md", ".mdc")):
            text = (self.REPO / name).read_text(encoding="utf-8")
            for sentence in self.PROMPT_SENTENCES:
                self.assertNotIn(sentence, text, (
                    f"{name} restates the generated child instruction. The prompt in "
                    f"task_runner.py owns it; a document copy drifts unnoticed because "
                    f"no child reads it"))

    def test_nothing_sends_a_child_to_an_executor_document(self) -> None:
        self.assertFalse(
            (self.REPO / "skills" / "task-executor").exists(),
            "skills/task-executor/ is back; the launcher loads no skill, so such a "
            "file is a second, dormant statement of the author procedure")
        this_file = str(Path(__file__).resolve().relative_to(self.REPO))
        offenders = [name for name in self._tracked((".md", ".mdc", ".py", ".sh", ".json"))
                     if name != this_file
                     and "task-executor" in (self.REPO / name).read_text(encoding="utf-8")]
        self.assertEqual(offenders, [], (
            "these files name an executor skill that nothing activates: "
            f"{', '.join(offenders)}"))

    def test_the_entry_document_names_the_generator(self) -> None:
        agents = (self.REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("build_child_prompt", agents)
        self.assertIn("skills/task-runner/scripts/task_runner.py", agents)
        self.assertIn(".runner/prompt.txt", agents)


class ProgressRobustnessTests(unittest.TestCase):
    """Regressions for review findings: a status reader must not be fragile."""

    def _task(self, tmp: str, raw: str) -> Path:
        task_dir = Path(tmp) / "001-example"
        task_dir.mkdir()
        task_runner.progress_path(task_dir).write_text(raw, encoding="utf-8")
        return task_dir

    def test_malformed_json_yields_none_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, "{not json")
            self.assertIsNone(task_runner.structured_progress(task_dir))

    def test_non_object_payload_yields_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, '["a", "list"]')
            self.assertIsNone(task_runner.structured_progress(task_dir))

    def test_missing_updated_at_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(tmp, json.dumps({"schema_version": 1, "activity": "Working"}))
            self.assertIsNone(task_runner.structured_progress(task_dir))

    def test_blank_updated_at_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(
                tmp,
                json.dumps({"schema_version": 1, "activity": "Working", "updated_at": "  "}),
            )
            self.assertIsNone(task_runner.structured_progress(task_dir))

    def test_legacy_version_key_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(
                tmp,
                json.dumps(
                    {"version": 1, "activity": "Working", "updated_at": "2026-07-27T00:00:00Z"}
                ),
            )
            self.assertIsNone(task_runner.structured_progress(task_dir))

    def test_booleans_are_not_counts(self) -> None:
        # bool is a subclass of int, so a naive isinstance check accepts True.
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(
                tmp,
                json.dumps(
                    {
                        "schema_version": 1,
                        "activity": "Working",
                        "updated_at": "2026-07-27T00:00:00Z",
                        "completed": True,
                        "total": 8,
                        "unit": "modules",
                    }
                ),
            )
            progress = task_runner.structured_progress(task_dir)
        self.assertEqual(progress["counts_rejected"], "incoherent completed/total/unit")
        self.assertNotIn("percent", progress)

    def test_non_finite_counts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = self._task(
                tmp,
                '{"schema_version": 1, "activity": "Working", '
                '"updated_at": "2026-07-27T00:00:00Z", '
                '"completed": NaN, "total": 8, "unit": "modules"}',
            )
            progress = task_runner.structured_progress(task_dir)
        self.assertEqual(progress["counts_rejected"], "incoherent completed/total/unit")

    def test_status_command_survives_malformed_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "001-example"
            task_dir.mkdir()
            (task_dir / "task.md").write_text("# t", encoding="utf-8")
            (task_dir / "plan.md").write_text("# p", encoding="utf-8")
            task_runner.progress_path(task_dir).write_text("{broken", encoding="utf-8")
            args = argparse.Namespace(task_dir=str(task_dir))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                task_runner.cmd_status(args)
            payload = json.loads(buffer.getvalue())
        self.assertNotIn("progress", payload)


if __name__ == "__main__":
    unittest.main()
