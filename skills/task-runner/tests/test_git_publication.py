from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import git_publication
import task_completion
import task_runner
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


def commit(repository: Path, name: str, message: str = "seed") -> str:
    git(repository, "config", "user.email", "tests@example.invalid")
    git(repository, "config", "user.name", "Task Agent Tests")
    (repository / name).write_text(message, encoding="utf-8")
    git(repository, "add", name)
    git(repository, "commit", "-m", message)
    return git(repository, "rev-parse", "HEAD")


def treat_the_local_origin_as_offsite(test: unittest.TestCase) -> None:
    """Let a test push somewhere real while asking a question about something else.

    A test can only create a remote on the machine it runs on, and this gate
    refuses exactly that (see `RemotesOnThisMachineTests`, which uses real
    directories and no patch). So tests whose subject is dirty files, branch
    counting or deferrals stub the one answer they are not about: where the
    remote lives.
    """
    patch = unittest.mock.patch.object(
        git_publication, "_same_machine_directory", return_value=None
    )
    patch.start()
    test.addCleanup(patch.stop)


class SavedAndPublishedTests(unittest.TestCase):
    """What keeps a task's Git work on one disk, named one repository at a time."""

    def setUp(self) -> None:
        treat_the_local_origin_as_offsite(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.origin = self.root / "origin.git"
        git(self.root, "init", "--bare", "-b", "main", str(self.origin))
        self.seed = self.root / "seed"
        git(self.root, "clone", str(self.origin), str(self.seed))
        commit(self.seed, "tracked.txt")
        git(self.seed, "push", "-u", "origin", "main")

        self.project = self.root / "project"
        git(self.root, "clone", str(self.origin), str(self.project))
        self.task = self.root / "700-example"
        (self.task / ".runner").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _grant(self, *repositories: Path) -> None:
        (self.task / ".runner" / "runner.json").write_text(
            json.dumps(
                {
                    "access_grant": {
                        "granted_directories": [str(path) for path in repositories]
                    }
                }
            ),
            encoding="utf-8",
        )

    def _defer(self, repository: Path, **fields: object) -> None:
        entry: dict[str, object] = {
            "repository": str(repository),
            "reason": "the remote is unreachable from this host",
            "owner": "product owner",
        }
        entry.update(fields)
        (self.task / git_publication.PUBLICATION_RECORD_NAME).write_text(
            json.dumps({"schema_version": 1, "deferred": [entry]}),
            encoding="utf-8",
        )

    def test_a_clean_pushed_repository_raises_nothing(self) -> None:
        self._grant(self.project)

        self.assertEqual(git_publication.publication_problems(self.task), [])

    def test_a_dirty_working_tree_is_refused_and_its_files_are_named(self) -> None:
        self._grant(self.project)
        (self.project / "tracked.txt").write_text("edited", encoding="utf-8")
        (self.project / "unsaved.txt").write_text("only here", encoding="utf-8")

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1)
        self.assertIn("2 uncommitted paths", problems[0])
        self.assertIn("tracked.txt", problems[0])
        self.assertIn("unsaved.txt", problems[0])

    def test_only_the_first_named_paths_are_listed_with_a_remainder(self) -> None:
        self._grant(self.project)
        for index in range(git_publication.NAMED_PATHS_LIMIT + 3):
            (self.project / f"file{index:02d}.txt").write_text("x", encoding="utf-8")

        problems = git_publication.publication_problems(self.task)

        self.assertIn("11 uncommitted paths", problems[0])
        self.assertIn("file00.txt", problems[0])
        self.assertIn("and 3 more", problems[0])
        self.assertNotIn("file10.txt", problems[0])

    def test_commits_no_remote_has_are_refused_with_branch_and_count(self) -> None:
        self._grant(self.project)
        commit(self.project, "first.txt", "first")
        commit(self.project, "second.txt", "second")

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1)
        self.assertIn("2 commits on branch main", problems[0])
        self.assertIn("no remote off this machine has", problems[0])

    def test_pushing_those_commits_clears_the_refusal(self) -> None:
        self._grant(self.project)
        commit(self.project, "first.txt", "first")
        before = git_publication.publication_problems(self.task)

        git(self.project, "push", "origin", "main")

        self.assertEqual(len(before), 1)
        self.assertEqual(git_publication.publication_problems(self.task), [])

    def test_a_detached_head_names_its_commit_instead_of_a_branch(self) -> None:
        self._grant(self.project)
        head = commit(self.project, "first.txt", "first")
        git(self.project, "checkout", "--detach", head)

        problems = git_publication.publication_problems(self.task)

        self.assertIn(f"detached HEAD {head[:12]}", problems[0])

    def test_a_repository_with_no_remote_at_all_is_refused(self) -> None:
        standalone = self.root / "standalone"
        standalone.mkdir()
        git(standalone, "init", "-b", "main")
        commit(standalone, "tracked.txt")
        self._grant(standalone)

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1)
        self.assertIn("has no Git remote", problems[0])
        self.assertIn(str(standalone), problems[0])

    def test_an_empty_repository_with_a_remote_holds_nothing_back(self) -> None:
        empty = self.root / "empty"
        git(self.root, "clone", str(self.origin), str(empty))
        git(empty, "checkout", "--orphan", "unborn")
        git(empty, "rm", "-rf", ".")
        self._grant(empty)

        self.assertEqual(git_publication.publication_problems(self.task), [])

    def test_the_task_own_worktree_is_judged_not_only_the_main_repository(
        self,
    ) -> None:
        worktree = self.task / "checkout"
        git(self.project, "worktree", "add", "-b", "task/700", str(worktree))
        commit(worktree, "work.txt", "work that lives only here")
        git(worktree, "push", "-u", "origin", "task/700")
        (worktree / "unsaved.txt").write_text("only in this checkout", encoding="utf-8")
        self._grant(self.project)

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn(str(worktree), problems[0])
        self.assertIn("unsaved.txt", problems[0])

    def test_a_worktree_does_not_repeat_the_branches_it_shares(self) -> None:
        worktree = self.task / "checkout"
        git(self.project, "worktree", "add", "-b", "task/700", str(worktree))
        commit(worktree, "work.txt", "work that lives only here")
        self._grant(self.project)

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("1 commit on branch task/700", problems[0])

    def test_a_branch_nobody_has_checked_out_is_still_judged(self) -> None:
        self._grant(self.project)
        git(self.project, "checkout", "-b", "task/side")
        commit(self.project, "side.txt", "side")
        git(self.project, "checkout", "main")

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("1 commit on branch task/side", problems[0])
        self.assertIn("no remote off this machine has", problems[0])

    def test_every_unpublished_branch_is_named_in_one_refusal(self) -> None:
        self._grant(self.project)
        commit(self.project, "first.txt", "first")
        git(self.project, "checkout", "-b", "task/side")
        commit(self.project, "side.txt", "side")

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("1 commit on branch main", problems[0])
        self.assertIn("2 commits on branch task/side", problems[0])

    def test_a_local_remote_tracking_ref_is_not_a_remote(self) -> None:
        self._grant(self.project)
        commit(self.project, "first.txt", "first")
        git(self.project, "update-ref", "refs/remotes/origin/main", "HEAD")

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("1 commit on branch main", problems[0])
        self.assertIn("no remote off this machine has", problems[0])

    def _local_parent_of(self, published: str, unpublished: str) -> str:
        """A commit holding the published tree with unpublished work as parent."""
        return git(
            self.project,
            "commit-tree",
            f"{published}^{{tree}}",
            "-p",
            unpublished,
            "-m",
            "a local rewrite of what the remote tip contains",
        )

    def test_a_replace_ref_cannot_put_local_work_inside_a_remote_tip(self) -> None:
        self._grant(self.project)
        published = git(self.project, "rev-parse", "HEAD")
        git(self.project, "checkout", "-b", "task/side")
        unpublished = commit(self.project, "side.txt", "side")
        rewritten = self._local_parent_of(published, unpublished)
        git(self.project, "replace", published, rewritten)

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("1 commit on branch task/side", problems[0])

    def test_a_graft_file_cannot_put_local_work_inside_a_remote_tip(self) -> None:
        self._grant(self.project)
        published = git(self.project, "rev-parse", "HEAD")
        git(self.project, "checkout", "-b", "task/side")
        unpublished = commit(self.project, "side.txt", "side")
        info = Path(git(self.project, "rev-parse", "--absolute-git-dir")) / "info"
        info.mkdir(exist_ok=True)
        (info / "grafts").write_text(f"{published} {unpublished}\n", encoding="utf-8")

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("1 commit on branch task/side", problems[0])

    def test_a_remote_that_cannot_answer_is_refused_not_assumed(self) -> None:
        self._grant(self.project)
        commit(self.project, "first.txt", "first")
        git(self.project, "remote", "set-url", "origin", str(self.root / "gone.git"))
        git(self.project, "update-ref", "refs/remotes/origin/main", "HEAD")

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("cannot be compared with remote origin", problems[0])
        self.assertIn("ever left this disk is unknown", problems[0])

    def test_a_workspace_set_that_cannot_be_listed_is_refused(self) -> None:
        self._grant(self.project)

        with unittest.mock.patch.object(
            task_workspace, "_registered_worktrees", return_value=None
        ):
            problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("cannot list its Git worktrees", problems[0])
        self.assertIn("worktree_list_failed", problems[0])

    def test_a_deferral_clears_a_workspace_set_that_cannot_be_listed(self) -> None:
        self._grant(self.project)
        self._defer(self.project)

        with unittest.mock.patch.object(
            task_workspace, "_registered_worktrees", return_value=None
        ):
            problems = git_publication.publication_problems(self.task)

        self.assertEqual(problems, [])

    def test_a_granted_directory_that_is_not_a_repository_is_not_one(self) -> None:
        plain = self.root / "notes"
        plain.mkdir()
        self._grant(plain)

        self.assertEqual(git_publication.publication_problems(self.task), [])

    def test_a_recorded_deferral_with_a_reason_and_an_owner_clears_it(self) -> None:
        self._grant(self.project)
        commit(self.project, "first.txt", "first")
        self._defer(self.project)

        self.assertEqual(git_publication.publication_problems(self.task), [])

    def test_a_deferral_naming_another_repository_defers_nothing(self) -> None:
        self._grant(self.project)
        commit(self.project, "first.txt", "first")
        self._defer(self.root / "elsewhere")

        self.assertIn("no remote off this machine has", git_publication.publication_problems(self.task)[0])

    def test_a_deferral_without_an_owner_is_refused_by_name(self) -> None:
        self._grant(self.project)
        commit(self.project, "first.txt", "first")
        self._defer(self.project, owner="   ")

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 2)
        self.assertIn("`owner`", problems[0])
        self.assertIn("no remote off this machine has", problems[1])

    def test_an_unreadable_deferral_record_defers_nothing(self) -> None:
        self._grant(self.project)
        (self.task / git_publication.PUBLICATION_RECORD_NAME).write_text(
            "{not json", encoding="utf-8"
        )

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1)
        self.assertIn("cannot be read", problems[0])


class RemotesOnThisMachineTests(unittest.TestCase):
    """A remote that is a directory here does not get the work off this disk.

    Real directories and real pushes, no patch: this is the one question the
    other classes stub out. Seven of the twenty-five checkouts on the host this
    was written for are in exactly this state.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.mirror = self.root / "mirror.git"
        git(self.root, "init", "--bare", "-b", "main", str(self.mirror))
        self.project = self.root / "project"
        self.project.mkdir()
        git(self.project, "init", "-b", "main")
        commit(self.project, "tracked.txt")
        self.task = self.root / "700-example"
        (self.task / ".runner").mkdir(parents=True)
        (self.task / "task.md").write_text(
            '---\nid: 700\nslug: "example"\ntitle: "x"\ndate: 2026-08-27\n'
            'status: "completed"\n---\n# x\n',
            encoding="utf-8",
        )
        (self.task / "plan.md").write_text("# Plan\n", encoding="utf-8")
        (self.task / "task_contract.json").write_text(
            '{"version": 1}', encoding="utf-8"
        )
        (self.task / ".runner" / "runner.json").write_text(
            json.dumps(
                {
                    "workflow": "standard",
                    "access_grant": {"granted_directories": [str(self.project)]},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_the_close_is_refused_even_though_the_push_succeeded(self) -> None:
        git(self.project, "remote", "add", "origin", str(self.mirror))
        git(self.project, "push", "-u", "origin", "main")

        ready, reason = task_completion.completion_ready(self.task, workflow="standard")

        self.assertFalse(ready)
        self.assertEqual(reason.gate, "git_publication")
        self.assertIn("no remote off this machine", reason)

    def test_pushing_to_a_directory_beside_the_clone_is_still_one_disk(self) -> None:
        git(self.project, "remote", "add", "origin", str(self.mirror))
        git(self.project, "push", "-u", "origin", "main")

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("no remote off this machine", problems[0])
        self.assertIn("origin is the directory", problems[0])
        self.assertIn(str(self.mirror), problems[0])

    def test_the_file_url_spelling_of_that_directory_is_the_same_place(self) -> None:
        git(self.project, "remote", "add", "origin", f"file://{self.mirror}")
        git(self.project, "push", "-u", "origin", "main")

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("origin is the directory", problems[0])

    def test_a_remote_reached_by_transport_is_the_one_asked(self) -> None:
        """A local mirror holding everything does not answer for a real remote."""
        git(self.project, "remote", "add", "origin", str(self.mirror))
        git(self.project, "push", "-u", "origin", "main")
        git(
            self.project,
            "remote",
            "add",
            "publish",
            "git@github.invalid:nobody/nothing.git",
        )

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("cannot be compared with remote publish", problems[0])

    def test_a_relative_remote_path_is_resolved_against_the_repository(self) -> None:
        git(self.project, "remote", "add", "origin", "../mirror.git")
        git(self.project, "push", "-u", "origin", "main")

        problems = git_publication.publication_problems(self.task)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn(str(self.mirror), problems[0])

    def test_a_recorded_deferral_still_clears_it(self) -> None:
        git(self.project, "remote", "add", "origin", str(self.mirror))
        git(self.project, "push", "-u", "origin", "main")
        (self.task / git_publication.PUBLICATION_RECORD_NAME).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "deferred": [
                        {
                            "repository": str(self.project),
                            "reason": "this clone mirrors a repository that is "
                            "published from elsewhere",
                            "owner": "product owner",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(git_publication.publication_problems(self.task), [])


class CompletionRefusesUnpublishedWorkTests(unittest.TestCase):
    """The existing completion step is where the question is asked."""

    def setUp(self) -> None:
        treat_the_local_origin_as_offsite(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.origin = self.root / "origin.git"
        git(self.root, "init", "--bare", "-b", "main", str(self.origin))
        self.project = self.root / "project"
        self.project.mkdir()
        git(self.project, "init", "-b", "main")
        git(self.project, "remote", "add", "origin", str(self.origin))
        commit(self.project, "tracked.txt")

        self.task = self.root / "700-example"
        (self.task / ".runner").mkdir(parents=True)
        (self.task / "task.md").write_text(
            '---\nid: 700\nslug: "example"\ntitle: "x"\ndate: 2026-08-27\n'
            'status: "completed"\n---\n# x\n',
            encoding="utf-8",
        )
        (self.task / "plan.md").write_text("# Plan\n", encoding="utf-8")
        (self.task / "task_contract.json").write_text(
            '{"version": 1}', encoding="utf-8"
        )
        (self.task / ".runner" / "runner.json").write_text(
            json.dumps(
                {
                    "workflow": "standard",
                    "access_grant": {"granted_directories": [str(self.project)]},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_unpushed_work_refuses_the_close_and_says_how_to_clear_it(self) -> None:
        ready, reason = task_completion.completion_ready(self.task, workflow="standard")

        self.assertFalse(ready)
        self.assertEqual(reason.gate, "git_publication")
        self.assertEqual(reason.owner, task_completion.ENGINE_OWNED_COMPLETION_GATE)
        self.assertIn("1 commit on branch main", reason)
        self.assertIn(str(self.task / "publication.json"), reason)
        self.assertIn("who will send it", reason)

    def test_the_same_close_is_accepted_once_the_work_is_pushed(self) -> None:
        refused, _reason = task_completion.completion_ready(
            self.task, workflow="standard"
        )

        git(self.project, "push", "-u", "origin", "main")
        accepted, reason = task_completion.completion_ready(
            self.task, workflow="standard"
        )

        self.assertFalse(refused)
        self.assertTrue(accepted, reason)


class TheChildIsToldAboutTheGateTests(unittest.TestCase):
    """A gate a child only meets at the end is a gate it cannot satisfy."""

    def test_the_prompt_names_the_gate_and_the_shape_of_the_deferral(self) -> None:
        prompt = task_runner.build_child_prompt(
            Path("/tasks/700-example"), repository=Path("/repo")
        )

        self.assertIn("whose commits no remote off this machine has", prompt)
        self.assertIn("a directory on this machine does not count", prompt)
        self.assertIn("/tasks/700-example/publication.json", prompt)
        self.assertIn(
            '{"schema_version": 1, "deferred": [{"repository": "...", '
            '"reason": "why it stays here", "owner": "who will send it"}]}',
            prompt,
        )


if __name__ == "__main__":
    unittest.main()
