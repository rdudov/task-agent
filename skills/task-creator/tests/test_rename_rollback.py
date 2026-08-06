"""A rename that stops halfway ends where it started, and says so if it cannot.

`cmd_rename` writes the new `slug` into `task.md` and then moves the directory.
The two cannot share a transaction, so a system error on the move used to leave
the file claiming the new name while the directory kept the old one -- and
`check` compared nothing, so the tree read as healthy.

The fault is injected rather than simulated: these tests run the real script in a
subprocess with `os.rename` replaced by one that raises, which is the same
deterministic reproduction that produced the report. Both of the first two tests
fail against the implementation as it stood at the end of task 531.

`read_record` cannot be used to observe any of this. Its `slug` is derived from
the directory name, so it agrees with the directory by construction and reports
the file as fine no matter what the file says. Every assertion below reads the
raw `task.md`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from test_rebuild_contract import DB, SCRIPTS, make_task, repo, rows, run  # noqa: F401
from test_review_findings import frontmatter_of, write_task

sys.path.insert(0, str(SCRIPTS))

# Injected into the child before `rename` runs. `os.rename` moves the directory;
# `replace_text` writes task.md, once for the rename's own write and a second
# time only if the rollback is attempted, which is where `--fail-rollback` bites.
DRIVER = textwrap.dedent("""\
    import errno, os, sys
    sys.path.insert(0, {scripts!r})
    import tasks_index

    def refuse(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    os.rename = refuse

    if {fail_rollback!r}:
        writes = []
        original = tasks_index.replace_text

        def flaky(path, text):
            writes.append(path)
            if len(writes) > 1:
                raise OSError(errno.EROFS, "Read-only file system")
            return original(path, text)

        tasks_index.replace_text = flaky

    sys.exit(tasks_index.main({argv!r}))
    """)


def rename_with_a_broken_move(repo: Path, *argv: str, fail_rollback: bool = False):
    """Run the real `rename` against `repo` with the directory move guaranteed to fail."""
    program = DRIVER.format(scripts=str(SCRIPTS), argv=list(argv), fail_rollback=fail_rollback)
    return subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, cwd=repo, timeout=180,
        env={**os.environ, "TASKS_INDEX_ROOT": str(repo)},
    )


def raw(directory: Path) -> str:
    return (directory / "task.md").read_text(encoding="utf-8")


# --- the rollback ---------------------------------------------------------------

def test_a_failed_move_restores_the_frontmatter_byte_for_byte(repo: Path) -> None:
    """The reported defect: `slug: "129-beta"` in a directory still named `129-alpha`.

    The move is the second of two operations and the file is the only thing
    already changed when it fails, so the command has everything it needs to end
    where it started -- and must, because nothing else will notice.
    """
    directory = make_task(repo, 129, "alpha")
    run(repo, "reindex")
    before = raw(directory)

    result = rename_with_a_broken_move(repo, "rename", "129", "beta")

    assert result.returncode != 0, f"the broken move was reported as success: {result.stdout}"
    assert "No space left on device" in result.stderr
    assert raw(directory) == before, "the frontmatter kept the name the move never applied"
    assert frontmatter_of(directory)["slug"] == "129-alpha"
    assert directory.is_dir() and not (repo / "tasks" / "129-beta").exists()
    assert [r["path"] for r in rows(repo)] == ["tasks/129-alpha"]

    report = run(repo, "check", check=False)
    assert report.returncode == 0, report.stdout + report.stderr


def test_a_failed_move_does_not_apply_a_title_given_alongside_the_slug(repo: Path) -> None:
    """`--title` rides on the same frontmatter write, so it rolls back with it.

    Leaving the title behind would be the same defect wearing a different key:
    half of one write surviving an operation that did not happen.
    """
    directory = make_task(repo, 129, "alpha", title="Alpha")
    run(repo, "reindex")

    result = rename_with_a_broken_move(repo, "rename", "129", "beta", "--title", "Beta")

    assert result.returncode != 0
    fields = frontmatter_of(directory)
    assert fields["title"] == "Alpha" and fields["slug"] == "129-alpha"


def test_a_rollback_that_fails_reports_both_errors_and_the_state_it_leaves(repo: Path) -> None:
    """A restore can fail too, and then the divergence is real. Say all of it.

    Exiting on the move's errno alone would hide the fact that `task.md` was
    changed and not changed back. The message has to carry the original failure,
    the failed restore, and which of the two names now sits where.
    """
    directory = make_task(repo, 129, "alpha")
    run(repo, "reindex")

    result = rename_with_a_broken_move(repo, "rename", "129", "beta", fail_rollback=True)

    assert result.returncode != 0
    assert "No space left on device" in result.stderr, "the original failure was dropped"
    assert "Read-only file system" in result.stderr, "the failed restore was silent"
    assert "129-alpha" in result.stderr and "129-beta" in result.stderr
    # The state it says it left is the state it left.
    assert directory.is_dir() and frontmatter_of(directory)["slug"] == "129-beta"

    # And what it could not repair, `check` names.
    report = run(repo, "check", check=False)
    assert report.returncode != 0
    assert "frontmatter `slug`" in report.stderr


# --- the diagnosis ---------------------------------------------------------------

def test_check_reports_a_frontmatter_slug_that_disagrees_with_its_directory(repo: Path) -> None:
    """Constructed directly: the divergence is a state, however it was reached.

    It is an issue, not a note. A note is for something that is merely untidy;
    this is two records of one name that no longer agree, and every reader of the
    file gets a different answer from every reader of the directory.
    """
    make_task(repo, 129, "healthy")
    write_task(repo / "tasks" / "130-alpha", textwrap.dedent("""\
        id: 130
        slug: "130-beta"
        title: "Renamed in the file only"
        date: 2026-07-29
        status: "completed"
        projects: []
        trips: []
        """))

    report = run(repo, "check", check=False)

    assert report.returncode != 0, report.stdout
    assert "tasks/130-alpha" in report.stderr
    assert "frontmatter `slug`" in report.stderr and "130-beta" in report.stderr
    assert "issue:" in report.stderr
    assert "note:" not in report.stdout
    # The healthy task is not swept up in it.
    assert "129-healthy" not in report.stderr


def test_check_stays_quiet_when_the_frontmatter_carries_no_slug_at_all(repo: Path) -> None:
    """A missing key is not a disagreement. There is nothing to compare."""
    write_task(repo / "tasks" / "131-no-slug", textwrap.dedent("""\
        id: 131
        title: "No slug key"
        date: 2026-07-29
        status: "completed"
        projects: []
        trips: []
        """))

    report = run(repo, "check", check=False)
    assert report.returncode == 0, report.stdout + report.stderr


# --- what the repair must not have cost -------------------------------------------

def test_a_successful_rename_still_agrees_with_itself(repo: Path) -> None:
    """The invariant `check` now enforces has to hold after an ordinary rename."""
    make_task(repo, 129, "before")
    run(repo, "reindex")
    run(repo, "rename", "129", "after", "--title", "After")

    moved = repo / "tasks" / "129-after"
    assert frontmatter_of(moved)["slug"] == "129-after"
    assert frontmatter_of(moved)["title"] == "After"
    assert not (repo / "tasks" / "129-before").exists()
    assert run(repo, "check").returncode == 0


@pytest.mark.parametrize("number, new_slug, guard", [
    ("130", "taken", "no-clobber"),      # the target is a directory already on disk
    ("129", "Ещё Одна", "empty-slug"),   # normalizes to nothing
])
def test_rename_still_refuses_what_it_refused_before(repo: Path, number: str,
                                                     new_slug: str, guard: str) -> None:
    """The rollback sits after both guards and must not have moved either one.

    Both refusals happen before a single byte is written, so neither one can
    reach the restore path -- which is precisely the claim worth holding onto.
    """
    make_task(repo, 129, "source")
    occupied = make_task(repo, 130, "taken")
    (occupied / "marker").write_text("keep me", encoding="utf-8")
    run(repo, "reindex")

    result = run(repo, "rename", number, new_slug, check=False)

    assert result.returncode != 0, f"{guard} guard let it through: {result.stdout}"
    assert (occupied / "marker").read_text(encoding="utf-8") == "keep me"
    for directory in (repo / "tasks" / "129-source", occupied):
        assert directory.is_dir()
        assert frontmatter_of(directory)["slug"] == directory.name
    assert json.loads(run(repo, "query", "--number", number,
                          "--format", "json").stdout)[0]["slug"] == \
        {"129": "129-source", "130": "130-taken"}[number]
    assert run(repo, "check").returncode == 0
