"""Whether the Git work a task did is saved and published.

`task_completion` owns the completion decision; this module owns one of its
gates. Work that exists only in one working tree, or only in one clone, is work
nobody but its author can see, and finding it means walking every repository on
a disk by hand. So the question is asked once, at the moment a task tries to
close, about the repositories that task was admitted to write and the Git
worktrees it created inside its own task directory.

Two things are asked, because they fail separately. Every working tree is asked
what it holds that no commit does: uncommitted bytes live in exactly one
directory, and a task's own worktree hides them from the main checkout. Every
distinct history is asked what its local refs hold that its remotes do not, and
the remotes are asked directly rather than through `refs/remotes/*`, which is a
local file any command can write without a byte leaving the disk. Deciding that
a remote's tip already contains a branch is still a walk over parents in this
clone, which `refs/replace/*` and the graft file rewrite just as cheaply, so
those are off here too.

A remote is only worth asking if answering yes means something. Seven of the
twenty-five checkouts on the host this was written for have a single remote that
is a directory beside them, so a push there copies the work from one place on
the disk to another and the disk still loses all of it. Such a remote is not
storage this gate accepts; it is named, and a repository that has only remotes
like it is refused exactly as a repository with no remote is. What passing means
is therefore bounded and worth stating plainly: some remote that is not a
directory on this machine reported these commits. Where its bytes actually sit
is its transport's business and is not claimed here.

A repository whose state is deliberately left alone is named in the task's own
`publication.json`, with a reason and the person or role who will send it. That
record is the only way past this gate other than pushing: the decision to leave
work unsent stays with a human, and it stays legible afterwards.
"""

from __future__ import annotations

import json
import os
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

# Asking a remote what it holds is a network call, and a hung one would hang the
# close it is part of. An installation whose remotes are slow raises this.
REMOTE_QUERY_TIMEOUT_ENV = "TASK_AGENT_REMOTE_QUERY_TIMEOUT_SECONDS"
DEFAULT_REMOTE_QUERY_TIMEOUT_SECONDS = 30


def remote_query_timeout_seconds() -> int:
    raw = os.environ.get(REMOTE_QUERY_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_REMOTE_QUERY_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{REMOTE_QUERY_TIMEOUT_ENV} must be an integer: {raw!r}") from exc
    if value <= 0:
        raise SystemExit(f"{REMOTE_QUERY_TIMEOUT_ENV} must be positive: {value}")
    return value


def _git(
    repository: Path,
    *arguments: str,
    stdin: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env={
                **os.environ,
                # A gate must never stop to ask a person for a password.
                "GIT_TERMINAL_PROMPT": "0",
                # Whether a remote's tip already contains a local commit is
                # decided by walking parents, and Git lets two local files
                # rewrite that walk: `refs/replace/*` and the graft file. Either
                # one can make unpublished work look reachable from a genuine
                # remote tip without a byte leaving the disk, so both are turned
                # off for every command asked here.
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_GRAFT_FILE": os.devnull,
            },
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=list(arguments),
            returncode=1,
            stdout="",
            stderr=f"git {' '.join(arguments)} did not answer within {timeout}s",
        )


def _dirty_path(entry: str) -> str:
    """Return the worktree path a `status --porcelain=v1` line is about."""
    path = entry[3:] if len(entry) > 3 else entry
    _original, separator, renamed = path.partition(" -> ")
    return (renamed if separator else path).strip('"')


def _named(items: list[str]) -> str:
    shown = ", ".join(items[:NAMED_PATHS_LIMIT])
    remainder = len(items) - NAMED_PATHS_LIMIT
    return f"{shown} and {remainder} more" if remainder > 0 else shown


def _unsaved_work(repository: Path) -> str | None:
    """Say what this working tree holds that no commit does, or nothing."""
    status = _git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode:
        return (
            f"{repository} cannot be read by git status, so whether its work is "
            f"saved is unknown: {status.stderr.strip() or 'git status failed'}"
        )
    dirty = [_dirty_path(line) for line in status.stdout.splitlines() if line.strip()]
    if not dirty:
        return None
    return (
        f"{repository} has {len(dirty)} uncommitted "
        f"{'path' if len(dirty) == 1 else 'paths'}: {_named(dirty)}"
    )


def _same_machine_directory(repository: Path, url: str) -> Path | None:
    """The directory on this machine a remote URL names, if it names one.

    Git reaches a plain path, and the `file://` spelling of one, by opening it
    here; anything else it hands to a transport. Only the first kind can be
    shown to keep the work on this disk, and showing that is all this decides.
    """
    candidate = url
    if candidate.startswith("file://"):
        host, _slash, path = candidate[len("file://") :].partition("/")
        if host not in ("", "localhost"):
            return None
        candidate = f"/{path}"
    location = Path(candidate).expanduser()
    if not location.is_absolute():
        # Git resolves a relative remote against the repository itself.
        location = repository / location
    try:
        resolved = location.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _offsite_remotes(repository: Path) -> tuple[list[str], list[str]]:
    """Remotes that could hold the work elsewhere, and the ones that cannot.

    `git ls-remote --get-url` is asked rather than the configured string,
    because that is the address the query below will really use, rewrites and
    all.
    """
    remotes = _git(repository, "remote")
    if remotes.returncode:
        return [], []
    offsite: list[str] = []
    on_this_machine: list[str] = []
    for remote in remotes.stdout.split():
        url = _git(repository, "ls-remote", "--get-url", remote).stdout.strip()
        directory = _same_machine_directory(repository, url) if url else None
        if directory is None:
            offsite.append(remote)
        else:
            on_this_machine.append(f"{remote} is the directory {directory}")
    return offsite, on_this_machine


def _remote_commits(repository: Path) -> tuple[list[str], int, str | None]:
    """Ask every remote what it holds: its tips known here, its tips unknown, why not.

    `refs/remotes/*` is not asked. It is an ordinary local ref namespace, so a
    single `git update-ref` makes a repository look published while nothing left
    the disk; the whole point of this gate is that the bytes are somewhere else.
    A remote that cannot answer leaves the question open, which is a refusal.
    """
    offsite, on_this_machine = _offsite_remotes(repository)
    if not offsite:
        if not on_this_machine:
            return [], 0, (
                f"{repository} has no Git remote, so nothing committed there can "
                "leave this disk"
            )
        return [], 0, (
            f"{repository} has no remote off this machine — {_named(on_this_machine)} "
            "— so a push there leaves every copy on this disk"
        )

    reported: list[str] = []
    for remote in offsite:
        listed = _git(
            repository,
            "ls-remote",
            "--quiet",
            remote,
            timeout=remote_query_timeout_seconds(),
        )
        if listed.returncode:
            # Git says why on its first line and then explains how to fix it;
            # the cause is what a refusal has to carry.
            cause = next(
                (line for line in listed.stderr.splitlines() if line.strip()),
                "git ls-remote failed",
            )
            return [], 0, (
                f"{repository} cannot be compared with remote {remote}, so "
                f"whether its commits ever left this disk is unknown: {cause.strip()}"
            )
        for line in listed.stdout.splitlines():
            object_id = line.split("\t", 1)[0].strip()
            if object_id and object_id not in reported:
                reported.append(object_id)

    if not reported:
        return [], 0, None
    # Containment can only be computed against objects this repository has. A
    # tip it has never fetched is left out, which can only make the answer more
    # cautious, never falsely clean.
    probe = _git(
        repository,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype)",
        "--buffer",
        stdin="\n".join(reported) + "\n",
    )
    known = []
    for line in probe.stdout.splitlines():
        object_id, _space, kind = line.partition(" ")
        if kind.strip() == "commit":
            known.append(object_id.strip())
    return known, len(reported) - len(known), None


def _local_history(repository: Path) -> list[tuple[str, str]]:
    """Every local ref that can hold commits, named the way a person names it.

    Every branch, not only the checked-out one: a branch nobody has checked out
    since committing to it is exactly the case this gate exists for.
    """
    history: list[tuple[str, str]] = []
    branches = _git(
        repository, "for-each-ref", "--format=%(objectname) %(refname:short)", "refs/heads"
    )
    for line in branches.stdout.splitlines():
        object_id, _space, branch = line.partition(" ")
        if object_id.strip() and branch.strip():
            history.append((f"branch {branch.strip()}", object_id.strip()))

    head = _git(repository, "rev-parse", "--verify", "HEAD")
    if head.returncode:
        # An unborn HEAD has no commits of its own to publish.
        return history
    commit = head.stdout.strip()
    if _git(repository, "symbolic-ref", "--quiet", "HEAD").returncode:
        history.append((f"detached HEAD {commit[:12]}", commit))
    return history


def _unpublished_history(repository: Path) -> str | None:
    """Say what this repository's refs hold that its remotes do not, or nothing."""
    published, unknown_tips, refusal = _remote_commits(repository)
    if refusal is not None:
        return refusal

    reported = set(published)
    clauses: list[str] = []
    for label, commit in _local_history(repository):
        if commit in reported:
            continue
        counted = _git(
            repository,
            "rev-list",
            "--count",
            "--stdin",
            stdin="\n".join([commit, *(f"^{tip}" for tip in published)]) + "\n",
        )
        if counted.returncode:
            return (
                f"{repository} cannot be compared with its remotes on {label}: "
                f"{counted.stderr.strip() or 'git rev-list failed'}"
            )
        count = int(counted.stdout.strip() or "0")
        if count:
            clauses.append(
                f"{count} {'commit' if count == 1 else 'commits'} on {label}"
            )
    if not clauses:
        return None
    unfetched = (
        f", and {unknown_tips} of its remotes' refs are not present here, so fetch "
        "first if a remote is ahead"
        if unknown_tips
        else ""
    )
    return (
        f"{repository} has {_named(clauses)} that no remote off this machine "
        f"has{unfetched}"
    )


def _shared_history(repository: Path) -> str:
    """The Git directory whose refs this working tree shares.

    A worktree keeps its own index and files but the same branches as the
    repository it was added from, so its history is judged once for all of them.
    """
    common = _git(repository, "rev-parse", "--git-common-dir")
    if common.returncode or not common.stdout.strip():
        return str(repository)
    return str((repository / common.stdout.strip()).resolve())


def _deferrals(task_dir: Path) -> tuple[set[Path], str | None]:
    """Read the task's own deferral record, or say why it cannot be trusted."""
    path = task_dir / PUBLICATION_RECORD_NAME
    if not path.is_file():
        return set(), None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return set(), f"{path} cannot be read: {exc}"
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        return set(), f"{path} is not a version 1 publication record"
    entries = record.get("deferred")
    if not isinstance(entries, list) or not entries:
        return set(), f"{path} names no deferred repository under `deferred`"
    deferred: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return set(), f"{path} has a `deferred` entry that is not an object"
        if not all(
            isinstance(entry.get(field), str) and entry[field].strip()
            for field in ("repository", "reason", "owner")
        ):
            return set(), (
                f"{path} has a `deferred` entry without a non-empty `repository`, "
                "`reason` and `owner`"
            )
        deferred.add(Path(entry["repository"]).expanduser().resolve())
    return deferred, None


def task_repositories(
    task_dir: Path, runner_meta: dict[str, Any]
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Every Git repository this task holds, and why the set may be incomplete.

    A granted directory that is not a Git repository is not one of them; this
    gate is about published Git history, not about every path a run could reach.
    But a path that carries a `.git` this Git cannot read, and a repository whose
    worktrees the enumeration owner could not list, are returned as named
    unknowns rather than dropped: a set nobody could compute is not an empty set.
    """
    workspaces, failures = task_workspace.task_git_workspaces(task_dir, runner_meta)
    repositories: list[Path] = []
    unknown: dict[Path, str] = {}
    for path, _ownership_proven_by_git in workspaces:
        if path.is_dir() and not _git(path, "rev-parse", "--git-dir").returncode:
            repositories.append(path)
        elif (path / ".git").exists():
            unknown[path] = (
                f"{path} carries a .git that git cannot read, so whether its work "
                "is saved and published is unknown"
            )
    for failure in failures:
        path = Path(str(failure.get("path")))
        if path in repositories:
            unknown[path] = (
                f"{path} cannot list its Git worktrees ({failure.get('reason')}), "
                "so which working copies this task holds is unknown"
            )
    return repositories, list(unknown.items())


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

    repositories, unknown = task_repositories(task_dir, runner_meta)
    problems.extend(problem for path, problem in unknown if path not in deferred)

    judged: set[str] = set()
    for repository in repositories:
        history = _shared_history(repository)
        if repository in deferred:
            # The person took responsibility for this repository, and a worktree
            # sharing its branches is the same decision.
            judged.add(history)
            continue
        unsaved = _unsaved_work(repository)
        if unsaved is not None:
            problems.append(unsaved)
        if history in judged:
            continue
        judged.add(history)
        unpublished = _unpublished_history(repository)
        if unpublished is not None:
            problems.append(unpublished)
    return problems
