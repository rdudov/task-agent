from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import task_workspace


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def commit(repository: Path, message: str = "seed") -> str:
    git(repository, "config", "user.email", "tests@example.invalid")
    git(repository, "config", "user.name", "Task Agent Tests")
    (repository / "tracked.txt").write_text(message, encoding="utf-8")
    git(repository, "add", "tracked.txt")
    git(repository, "commit", "-m", message)
    return git(repository, "rev-parse", "HEAD")


class WorkspaceCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.canonical = self.root / "project"
        self.canonical.mkdir()
        git(self.canonical, "init", "-b", "main")
        self.head = commit(self.canonical)
        self.task = self.root / "700-example"
        self.task.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def meta(repository: Path) -> dict:
        return {"access_grant": {"granted_directories": [str(repository)]}}

    def test_clean_reachable_task_clone_is_removed(self) -> None:
        clone = self.root / "project-700"
        git(self.root, "clone", str(self.canonical), str(clone))

        result = task_workspace.cleanup_workspace(self.task, self.meta(clone))

        self.assertEqual(result["outcome"], "removed")
        self.assertEqual(result["head"], self.head)
        self.assertFalse(clone.exists())

    def test_dirty_clone_is_retained_with_one_reason(self) -> None:
        clone = self.root / "project-700"
        git(self.root, "clone", str(self.canonical), str(clone))
        (clone / "untracked.txt").write_text("preserve me", encoding="utf-8")

        result = task_workspace.cleanup_workspace(self.task, self.meta(clone))

        self.assertEqual(result["outcome"], "retained")
        self.assertEqual(result["reason"], "dirty")
        self.assertTrue(clone.exists())

    def test_unique_clone_head_is_retained(self) -> None:
        clone = self.root / "project-700"
        git(self.root, "clone", str(self.canonical), str(clone))
        commit(clone, "unique")

        result = task_workspace.cleanup_workspace(self.task, self.meta(clone))

        self.assertEqual(result["reason"], "head_unreachable")
        self.assertTrue(clone.exists())

    def test_stale_tracking_ref_does_not_survive_failed_remote_refresh(self) -> None:
        clone = self.root / "project-700"
        git(self.root, "clone", str(self.canonical), str(clone))
        git(clone, "remote", "set-url", "origin", "missing-origin")

        result = task_workspace.cleanup_workspace(self.task, self.meta(clone))

        self.assertEqual(result["reason"], "canonical_fetch_failed")
        self.assertTrue(clone.exists())

    def test_linked_worktree_removal_keeps_its_reachable_branch(self) -> None:
        worktree = self.root / "project-700"
        git(self.canonical, "worktree", "add", "-b", "task/700", str(worktree))

        result = task_workspace.cleanup_workspace(self.task, self.meta(worktree))

        self.assertEqual(result["outcome"], "removed")
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.canonical, "rev-parse", "task/700"), self.head)

    def test_exact_granted_path_does_not_need_a_task_number_in_its_name(self) -> None:
        clone = self.root / "portfolio-workspace"
        git(self.root, "clone", str(self.canonical), str(clone))

        result = task_workspace.cleanup_workspace(self.task, self.meta(clone))

        self.assertEqual(result["outcome"], "removed")
        self.assertFalse(clone.exists())

    def test_foreign_task_number_in_clone_name_is_never_removed(self) -> None:
        clone = self.root / "project-701"
        git(self.root, "clone", str(self.canonical), str(clone))

        result = task_workspace.cleanup_workspace(self.task, self.meta(clone))

        self.assertEqual(result["reason"], "path_not_task_owned")
        self.assertTrue(clone.exists())

    def test_ignored_task_data_is_never_removed(self) -> None:
        clone = self.root / "project-700"
        git(self.root, "clone", str(self.canonical), str(clone))
        (clone / ".gitignore").write_text("data/\n", encoding="utf-8")
        git(clone, "config", "user.email", "tests@example.invalid")
        git(clone, "config", "user.name", "Task Agent Tests")
        git(clone, "add", ".gitignore")
        git(clone, "commit", "-m", "ignore runtime data")
        git(self.canonical, "fetch", str(clone), "HEAD:refs/heads/with-ignore")
        (clone / "data").mkdir()
        (clone / "data" / "replay.json").write_text("{}\n", encoding="utf-8")

        result = task_workspace.cleanup_workspace(self.task, self.meta(clone))

        self.assertEqual(result["reason"], "protected_ignored_paths")
        self.assertEqual(result["protected_ignored_paths"], ["data/replay.json"])
        self.assertTrue(clone.exists())

    def test_ignored_path_inspection_failure_retains_clone(self) -> None:
        clone = self.root / "project-700"
        git(self.root, "clone", str(self.canonical), str(clone))

        with mock.patch.object(
            task_workspace,
            "_protected_ignored_paths",
            return_value=None,
        ):
            result = task_workspace.cleanup_workspace(self.task, self.meta(clone))

        self.assertEqual(result["reason"], "ignored_paths_unreadable")
        self.assertTrue(clone.exists())

    def test_unnumbered_canonical_repository_is_never_removed(self) -> None:
        result = task_workspace.cleanup_workspace(
            self.task, self.meta(self.canonical)
        )

        self.assertEqual(result["reason"], "path_not_task_owned")
        self.assertTrue(self.canonical.exists())

    def test_clone_removal_error_is_a_retained_outcome(self) -> None:
        clone = self.root / "project-700"
        git(self.root, "clone", str(self.canonical), str(clone))

        with mock.patch.object(
            task_workspace.shutil,
            "rmtree",
            side_effect=OSError("read-only boundary"),
        ):
            result = task_workspace.cleanup_workspace(self.task, self.meta(clone))

        self.assertEqual(result["outcome"], "retained")
        self.assertEqual(result["reason"], "workspace_remove_failed")
        self.assertIn("read-only boundary", result["detail"])
        self.assertTrue(clone.exists())

    def test_mounted_clone_is_retained_before_removal_starts(self) -> None:
        clone = self.root / "project-700"
        git(self.root, "clone", str(self.canonical), str(clone))

        with mock.patch.object(
            task_workspace,
            "_is_mountpoint",
            return_value=True,
        ), mock.patch.object(task_workspace.shutil, "rmtree") as rmtree:
            result = task_workspace.cleanup_workspace(self.task, self.meta(clone))

        self.assertEqual(result["outcome"], "retained")
        self.assertEqual(result["reason"], "workspace_is_mountpoint")
        rmtree.assert_not_called()
        self.assertEqual(git(clone, "status", "--porcelain=v1"), "")

    def test_process_with_workspace_cwd_retains_the_clone(self) -> None:
        clone = self.root / "project-700"
        git(self.root, "clone", str(self.canonical), str(clone))
        process = subprocess.Popen(["sleep", "30"], cwd=clone)
        try:
            result = task_workspace.cleanup_workspace(self.task, self.meta(clone))
        finally:
            process.terminate()
            process.wait(timeout=5)

        self.assertEqual(result["reason"], "live_processes")
        self.assertIn(process.pid, result["live_pids"])
        self.assertTrue(clone.exists())


class ScopeCleanupTests(unittest.TestCase):
    def test_scope_peers_receive_term_and_scope_is_rechecked(self) -> None:
        meta = {
            "supervision_boundary": {
                "mode": "systemd_scope",
                "unit": "task-agent-700-example.scope",
            }
        }
        snapshots = [[os.getpid(), 111, 222], [os.getpid()]]
        with mock.patch.object(
            task_workspace, "_task_cgroup", return_value=Path("/cgroup")
        ), mock.patch.object(
            task_workspace, "_cgroup_pids", side_effect=snapshots
        ), mock.patch.object(task_workspace.os, "kill") as kill:
            result = task_workspace.drain_task_scope(meta, grace_seconds=0)

        self.assertEqual(result["outcome"], "cleared")
        self.assertEqual(result["terminated_pids"], [111, 222])
        kill.assert_has_calls(
            [
                mock.call(111, task_workspace.signal.SIGTERM),
                mock.call(222, task_workspace.signal.SIGTERM),
            ]
        )

    def test_unprovable_scope_is_not_reported_empty(self) -> None:
        meta = {
            "supervision_boundary": {
                "mode": "systemd_scope",
                "unit": "task-agent-700-example.scope",
            }
        }
        with mock.patch.object(
            task_workspace, "_task_cgroup", return_value=None
        ), mock.patch.object(task_workspace, "_scope_is_collected", return_value=False):
            result = task_workspace.drain_task_scope(meta)
        self.assertEqual(
            result,
            {"outcome": "unverified", "reason": "scope_identity_mismatch"},
        )

    def test_a_collected_scope_is_proof_that_no_process_survived(self) -> None:
        meta = {
            "supervision_boundary": {
                "mode": "systemd_scope",
                "unit": "task-agent-700-example.scope",
            }
        }
        with mock.patch.object(
            task_workspace, "_task_cgroup", return_value=None
        ), mock.patch.object(task_workspace, "_scope_is_collected", return_value=True):
            result = task_workspace.drain_task_scope(meta)
        self.assertEqual(result["outcome"], "cleared")
        self.assertEqual(result["reason"], "scope_collected")


if __name__ == "__main__":
    unittest.main()
