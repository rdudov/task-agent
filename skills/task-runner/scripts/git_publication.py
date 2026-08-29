"""Whether the Git work a task did is saved and published.

`task_completion` owns the completion decision; this module owns one of its
gates. Work that exists only in one working tree, or only in one clone, is work
nobody but its author can see, and finding it means walking every repository on
a disk by hand. So the question is asked once, at the moment a task tries to
close, about the repositories that task was admitted to write and the Git
worktrees it created inside its own task directory.

A repository whose state is deliberately left alone is named in the task's own
`publication.json`, with a reason and the person or role who will send it. That
record is the only way past this gate other than pushing: the decision to leave
work unsent stays with a human, and it stays legible afterwards.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

try:
    from . import task_workspace
except ImportError:  # Standalone source-module execution in repository tests.
    import task_workspace  # type: ignore[no-redef]


PUBLICATION_RECORD_NAME = "publication.json"

# A refusal names what to fix. Past a certain length it stops being a list of
# files and becomes a wall of text nobody reads the end of.
NAMED_PATHS_LIMIT = 8


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _dirty_path(entry: str) -> str:
    """Return the worktree path a `status --porcelain=v1` line is about."""
    path = entry[3:] if len(entry) > 3 else entry
    _original, separator, renamed = path.partition(" -> ")
    return (renamed if separator else path).strip('"')


def _named(paths: list[str]) -> str:
    shown = ", ".join(paths[:NAMED_PATHS_LIMIT])
    remainder = len(paths) - NAMED_PATHS_LIMIT
    return f"{shown} and {remainder} more" if remainder > 0 else shown


def _repository_problem(repository: Path) -> str | None:
    """Say what keeps this repository's current state on one disk, or nothing."""
    status = _git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode:
        return (
            f"{repository} cannot be read by git status, so whether its work is "
            f"saved is unknown: {status.stderr.strip() or 'git status failed'}"
        )
    dirty = [_dirty_path(line) for line in status.stdout.splitlines() if line.strip()]
    if dirty:
        return (
            f"{repository} has {len(dirty)} uncommitted "
            f"{'path' if len(dirty) == 1 else 'paths'}: {_named(dirty)}"
        )

    remotes = _git(repository, "remote")
    if remotes.returncode or not remotes.stdout.split():
        return (
            f"{repository} has no Git remote, so nothing committed there can "
            "leave this disk"
        )

    head = _git(repository, "rev-parse", "--verify", "HEAD")
    if head.returncode:
        # An unborn HEAD has no commits to publish, and a clean tree above means
        # there is nothing here to lose.
        return None
    unpushed = _git(repository, "rev-list", "--count", "HEAD", "--not", "--remotes")
    if unpushed.returncode:
        return (
            f"{repository} cannot be compared with its remotes: "
            f"{unpushed.stderr.strip() or 'git rev-list failed'}"
        )
    count = int(unpushed.stdout.strip() or "0")
    if count:
        branch = _git(repository, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        where = (
            f"branch {branch}"
            if branch and branch != "HEAD"
            else f"detached HEAD {head.stdout.strip()[:12]}"
        )
        return (
            f"{repository} has {count} {'commit' if count == 1 else 'commits'} on "
            f"{where} that no remote has"
        )
    return None


def _deferrals(task_dir: Path) -> tuple[dict[Path, str], str | None]:
    """Read the task's own deferral record, or say why it cannot be trusted."""
    path = task_dir / PUBLICATION_RECORD_NAME
    if not path.is_file():
        return {}, None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, f"{path} cannot be read: {exc}"
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        return {}, f"{path} is not a version 1 publication record"
    entries = record.get("deferred")
    if not isinstance(entries, list) or not entries:
        return {}, f"{path} names no deferred repository under `deferred`"
    deferred: dict[Path, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return {}, f"{path} has a `deferred` entry that is not an object"
        repository = entry.get("repository")
        reason = entry.get("reason")
        owner = entry.get("owner")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (repository, reason, owner)
        ):
            return {}, (
                f"{path} has a `deferred` entry without a non-empty `repository`, "
                "`reason` and `owner`"
            )
        deferred[Path(repository).expanduser().resolve()] = f"{reason} ({owner})"
    return deferred, None


def task_repositories(task_dir: Path, runner_meta: dict[str, Any]) -> list[Path]:
    """Every Git repository this task holds, in stable order.

    A granted directory that is not a Git repository is not one of them; this
    gate is about published Git history, not about every path a run could reach.
    """
    workspaces, _failures = task_workspace.task_git_workspaces(task_dir, runner_meta)
    return [
        path
        for path, _ownership_proven_by_git in workspaces
        if path.is_dir() and not _git(path, "rev-parse", "--git-dir").returncode
    ]


def publication_problems(
    task_dir: Path, runner_meta: dict[str, Any] | None = None
) -> list[str]:
    """Why this task's Git work is not saved and published. Empty means it is."""
    if runner_meta is None:
        try:
            runner_meta = json.loads(
                (task_dir / ".runner" / "runner.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            runner_meta = {}
    if not isinstance(runner_meta, dict):
        runner_meta = {}

    deferred, malformed = _deferrals(task_dir)
    problems = []
    if malformed is not None:
        # A record nobody can read is not a decision anybody made, so it defers
        # nothing and is itself named.
        problems.append(malformed)
    for repository in task_repositories(task_dir, runner_meta):
        if repository in deferred:
            continue
        problem = _repository_problem(repository)
        if problem is not None:
            problems.append(problem)
    return problems
