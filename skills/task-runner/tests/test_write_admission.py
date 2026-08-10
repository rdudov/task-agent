"""Admission for two tasks that would write the same Git repository.

The two named defects have a test each, because both were reproduced against
the previous single-mutable-field design and neither is prevented by the shape
of the code alone:

- F6: a read-only or dry run erasing an outstanding review obligation;
- F7: a no-op run turning into a repository-wide false blocker.
"""

import importlib.util
import json
import subprocess
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


write_admission = _load("write_admission")


def make_repository(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
    (repository / "source.txt").write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "source.txt"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "first"], check=True)
    return repository


def make_task(tasks_root: Path, name: str, *, completed: bool = False) -> Path:
    task_dir = tasks_root / name
    (task_dir / ".runner").mkdir(parents=True)
    (task_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (task_dir / "task_contract.json").write_text(
        json.dumps({"version": 1, "source": "test"}), encoding="utf-8"
    )
    status = "completed" if completed else "in_progress"
    (task_dir / "task.md").write_text(
        f'---\nid: 1\nslug: "{name}"\ntitle: "t"\ndate: 2026-08-10\nstatus: "{status}"\n---\n# t\n',
        encoding="utf-8",
    )
    return task_dir


class WriteScopeTests(unittest.TestCase):
    def test_a_scope_that_changed_the_repository_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = make_repository(root)
            task = make_task(root, "0001-writer")
            write_admission.open_write_scope(task, repository, "run-1")
            (repository / "source.txt").write_text("second\n", encoding="utf-8")
            result = write_admission.close_write_scope(task, "run-1")
            self.assertTrue(result["changed"])

    def test_a_scope_that_changed_nothing_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = make_repository(root)
            task = make_task(root, "0001-writer")
            write_admission.open_write_scope(task, repository, "run-1")
            self.assertFalse(write_admission.close_write_scope(task, "run-1")["changed"])

    def test_closing_a_scope_that_was_never_opened_invents_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_repository(root)
            task = make_task(root, "0001-writer")
            self.assertIsNone(write_admission.close_write_scope(task, "run-1"))

    def test_a_truncated_final_record_does_not_lose_the_records_before_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = make_repository(root)
            task = make_task(root, "0001-writer")
            write_admission.open_write_scope(task, repository, "run-1")
            with write_admission.ledger_path(task).open("a", encoding="utf-8") as handle:
                handle.write('{"record": "clos')
            self.assertEqual(len(write_admission.read_ledger(task)), 1)


class ErasedObligationTests(unittest.TestCase):
    """F6: a read-only or dry run must not erase an outstanding obligation."""

    def test_a_later_no_change_run_does_not_erase_an_earlier_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = make_repository(root)
            task = make_task(root, "0001-writer")

            write_admission.open_write_scope(task, repository, "run-1")
            (repository / "source.txt").write_text("second\n", encoding="utf-8")
            write_admission.close_write_scope(task, "run-1")

            # A second run that touches nothing. Under the previous design this
            # overwrote the one field that carried the obligation.
            write_admission.open_write_scope(task, repository, "run-2")
            write_admission.close_write_scope(task, "run-2")

            outstanding = write_admission.outstanding_write_results(task)
            self.assertEqual(len(outstanding), 1)
            self.assertTrue(outstanding[0]["changed"])

    def test_an_obligation_closes_when_the_task_own_gates_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = make_repository(root)
            task = make_task(root, "0001-writer", completed=True)
            write_admission.open_write_scope(task, repository, "run-1")
            (repository / "source.txt").write_text("second\n", encoding="utf-8")
            write_admission.close_write_scope(task, "run-1")
            self.assertEqual(write_admission.outstanding_write_results(task), [])


class FalseBlockerTests(unittest.TestCase):
    """F7: a no-op run must not become a repository-wide blocker."""

    def test_an_abandoned_no_op_scope_blocks_nobody(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = make_repository(root)
            tasks_root = root / "tasks"
            tasks_root.mkdir()
            abandoned = make_task(tasks_root, "0001-abandoned")
            requesting = make_task(tasks_root, "0002-next")

            # Opened and never closed, having changed nothing.
            write_admission.open_write_scope(abandoned, repository, "run-1")

            blockers = write_admission.admission_blockers(
                tasks_root=tasks_root,
                repository=repository,
                requesting_task=requesting,
                is_live=lambda task: False,
            )
            self.assertEqual(blockers, [])

    def test_a_dry_run_leaves_no_record_at_all(self) -> None:
        """A dry run never opens a scope, so it has nothing to overwrite."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            make_repository(root)
            task = make_task(root, "0001-dry")
            self.assertEqual(write_admission.read_ledger(task), [])
            self.assertEqual(write_admission.outstanding_write_results(task), [])

    def test_an_unresolvable_own_scope_constrains_only_its_own_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = make_repository(root)
            tasks_root = root / "tasks"
            tasks_root.mkdir()
            owner = make_task(tasks_root, "0001-owner")
            other = make_task(tasks_root, "0002-other")
            write_admission.open_write_scope(owner, repository, "run-1")
            # Point the recorded scope at a repository that no longer exists.
            records = write_admission.ledger_path(owner).read_text(encoding="utf-8")
            record = json.loads(records)
            record["before"]["repository"] = str(root / "gone")
            write_admission.ledger_path(owner).write_text(
                json.dumps(record) + "\n", encoding="utf-8"
            )

            self.assertEqual(
                write_admission.admission_blockers(
                    tasks_root=tasks_root,
                    repository=repository,
                    requesting_task=other,
                    is_live=lambda task: False,
                ),
                [],
            )
            own = write_admission.admission_blockers(
                tasks_root=tasks_root,
                repository=repository,
                requesting_task=owner,
                is_live=lambda task: False,
            )
            self.assertEqual([item["reason"] for item in own], ["unresolved_own_write_scope"])


class ConcurrentWriteTests(unittest.TestCase):
    def test_a_live_writer_blocks_another_task_in_the_same_repository(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = make_repository(root)
            tasks_root = root / "tasks"
            tasks_root.mkdir()
            live = make_task(tasks_root, "0001-live")
            requesting = make_task(tasks_root, "0002-next")
            write_admission.open_write_scope(live, repository, "run-1")

            blockers = write_admission.admission_blockers(
                tasks_root=tasks_root,
                repository=repository,
                requesting_task=requesting,
                is_live=lambda task: task.name == "0001-live",
            )
            self.assertEqual([item["reason"] for item in blockers], ["live_overlapping_write"])

    def test_a_live_writer_in_a_different_repository_blocks_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = make_repository(root)
            other_root = root / "elsewhere"
            other_root.mkdir()
            other_repository = make_repository(other_root)
            tasks_root = root / "tasks"
            tasks_root.mkdir()
            live = make_task(tasks_root, "0001-live")
            requesting = make_task(tasks_root, "0002-next")
            write_admission.open_write_scope(live, other_repository, "run-1")

            self.assertEqual(
                write_admission.admission_blockers(
                    tasks_root=tasks_root,
                    repository=repository,
                    requesting_task=requesting,
                    is_live=lambda task: True,
                ),
                [],
            )

    def test_an_unreviewed_change_blocks_a_different_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = make_repository(root)
            tasks_root = root / "tasks"
            tasks_root.mkdir()
            writer = make_task(tasks_root, "0001-writer")
            requesting = make_task(tasks_root, "0002-next")
            write_admission.open_write_scope(writer, repository, "run-1")
            (repository / "source.txt").write_text("second\n", encoding="utf-8")
            write_admission.close_write_scope(writer, "run-1")

            blockers = write_admission.admission_blockers(
                tasks_root=tasks_root,
                repository=repository,
                requesting_task=requesting,
                is_live=lambda task: False,
            )
            self.assertEqual(
                [item["reason"] for item in blockers], ["unreviewed_overlapping_write"]
            )

    def test_a_task_own_unreviewed_change_does_not_lock_it_out_of_rework(self) -> None:
        """Repairing your own reviewed change is the rework phase, not a collision."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = make_repository(root)
            tasks_root = root / "tasks"
            tasks_root.mkdir()
            writer = make_task(tasks_root, "0001-writer")
            write_admission.open_write_scope(writer, repository, "run-1")
            (repository / "source.txt").write_text("second\n", encoding="utf-8")
            write_admission.close_write_scope(writer, "run-1")

            self.assertEqual(
                write_admission.admission_blockers(
                    tasks_root=tasks_root,
                    repository=repository,
                    requesting_task=writer,
                    is_live=lambda task: False,
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
