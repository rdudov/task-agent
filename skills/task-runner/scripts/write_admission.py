#!/usr/bin/env python3
"""Admission for two tasks that would write the same Git repository.

Two write-mode runs in one repository at the same time do not produce two
reviewable candidates; they produce one working tree nobody can attribute. This
module decides whether a task may open a write scope there, and it records what
that scope actually did so a later reader can tell a run that changed the
repository from one that only looked at it.

The record is an append-only ledger, and that shape is the point. An earlier
design kept one mutable `write_result` field per task, which had two failures
that independent review reproduced:

- a read-only or dry run wrote its own `changed: false` over an outstanding
  `changed: true`, and the obligation to review that earlier change vanished;
- a run that opened a scope and never closed it became an indeterminate result
  that blocked *every* task in the repository, so one no-op could stop the
  repository for everyone.

Here a read-only or dry run opens no scope and appends no result of its own, so
it has nothing to overwrite. A dry run may transfer an exact previous run's
terminal evidence before replacing current-run metadata. Before a successor is
admitted, an abandoned scope is
durably closed under the repository lock only when the repository still equals
its opening fingerprint, which proves that scope was a no-op. Unknown liveness
is not permission for a second writer. A matching terminal runner record can
settle its own scope even when a later observer cannot see the claimant's PID
namespace; this is run-owned evidence, not a negative PID lookup. A divergent
abandoned scope remains a recomputed obligation for other tasks, so restoring
the opening fingerprint or satisfying the owning task's gates clears it. The
owning task may enter rework under the same number while that old scope remains
recomputed rather than freezing ambiguous attribution.

An obligation whose owning task was cancelled clears as well, because the gates
that would have discharged it will never be asked and the repository would
otherwise stay closed to every later task forever. That release is a decision,
so it is written down: the ledger gets its own `scope_released` record naming
the released run ids, the state digest each one left behind, and the reason.
Liveness still comes first, and cancellation never reaches a task whose writer
cannot be proven absent.

Whether an outstanding change has been reviewed is not decided here. This module
reports the outstanding change; the completion owner decides whether the task's
own gates — contract review verdict, live evidence, policy families — are
satisfied. Pairing policy belongs to whichever installation defines it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

try:
    from .task_completion import completion_ready, task_status
    from .task_contract import (
        capture_preexisting_tracked_dirty_baseline,
        git_repository_identity,
        recorded_completion_candidate_head,
    )
    from . import task_phases
except ImportError:
    from task_completion import completion_ready, task_status
    from task_contract import (
        capture_preexisting_tracked_dirty_baseline,
        git_repository_identity,
        recorded_completion_candidate_head,
    )
    import task_phases


LEDGER_NAME = "write-admission.jsonl"
REPOSITORY_LOCK_NAME = "task-agent-write-admission.lock"

OPENED = "opened"
CLOSED = "closed"
CLAIMANT_TERMINAL = "claimant_terminal"
COMPLETION_ACCEPTED = "completion_accepted"
SCOPE_RELEASED = "scope_released"

CANCELLED_STATUS = "cancelled"
CANCELLED_RELEASE_REASON = "owning_task_cancelled"

LIVE_OVERLAPPING_WRITE = "live_overlapping_write"
UNREVIEWED_OVERLAPPING_WRITE = "unreviewed_overlapping_write"
UNRESOLVED_OWN_WRITE_SCOPE = "unresolved_own_write_scope"

SYNTHETIC_RESOLUTIONS = {
    "measured_after_abandonment",
    # Read compatibility for e054b03 ledgers. New code no longer writes this
    # frozen attribution, but a late real close must still supersede one.
    "adopted_by_owner_rework",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ledger_path(task_dir: Path) -> Path:
    return task_dir / ".runner" / LEDGER_NAME


def git_write_state(repository: Path) -> dict[str, Any]:
    """Publishable Git-visible identity of one repository right now.

    HEAD alone would miss uncommitted work and the dirty baseline alone would
    miss a commit. The index binds staged bytes, while non-ignored untracked
    paths bind their path, type, executable bit and content digest. Ignored
    host/runtime files are deliberately outside the publishable state.
    """
    identity = git_repository_identity(repository)
    root = Path(identity["worktree"])
    head = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    baseline = capture_preexisting_tracked_dirty_baseline(root)
    index = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "--stage", "-z", "--"]
    )
    untracked = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
        ]
    )
    untracked_entries = [
        _worktree_entry(root, raw.decode("utf-8", errors="surrogateescape"))
        for raw in sorted(set(part for part in untracked.split(b"\0") if part))
    ]
    return {
        "repository": str(root),
        "common_dir": identity["common_dir"],
        "head": head,
        "preexisting_tracked_dirty_baseline": baseline,
        "tracked_content_digest": baseline["digest"],
        "index_digest": "sha256:" + hashlib.sha256(index).hexdigest(),
        "untracked_entries_digest": state_digest({"entries": untracked_entries}),
    }


def _worktree_entry(repository: Path, relative: str) -> dict[str, Any]:
    path = repository / relative
    if path.is_symlink():
        content = os.readlink(path).encode("utf-8", errors="surrogateescape")
        state = "symlink"
        executable = False
    elif path.is_file():
        content = path.read_bytes()
        state = "present"
        executable = bool(path.stat().st_mode & 0o111)
    else:
        content = b""
        state = "missing"
        executable = False
    return {
        "path": relative,
        "state": state,
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "executable": executable,
    }


def state_digest(state: dict[str, Any]) -> str:
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


@contextmanager
def repository_lock(repository: Path):
    """Serialize claims and closes for one Git common directory."""
    identity = git_repository_identity(repository)
    path = Path(identity["common_dir"]) / REPOSITORY_LOCK_NAME
    handle = path.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


@contextmanager
def repository_locks(repositories: list[Path]):
    """Lock distinct Git repositories in stable common-directory order."""
    by_common_dir: dict[str, Path] = {}
    for repository in repositories:
        identity = git_repository_identity(repository)
        common_dir = identity["common_dir"]
        if common_dir in by_common_dir:
            raise ValueError(
                "repository set names the same Git common directory twice: "
                f"{by_common_dir[common_dir]} and {repository.resolve()}"
            )
        by_common_dir[common_dir] = repository.resolve()
    identities = sorted(by_common_dir.items())
    with ExitStack() as stack:
        for _common_dir, repository in identities:
            stack.enter_context(repository_lock(repository))
        yield


def _append(task_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    path = ledger_path(task_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return record


def read_ledger(task_dir: Path) -> list[dict[str, Any]]:
    """Every record this task appended, skipping anything unreadable.

    A truncated final line is the normal shape of a process killed mid-append.
    Dropping it loses at most the record that was never completed; refusing to
    read the file at all would lose every obligation before it.
    """
    try:
        raw = ledger_path(task_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    records = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("record") in {
            OPENED,
            CLOSED,
            CLAIMANT_TERMINAL,
            COMPLETION_ACCEPTED,
            SCOPE_RELEASED,
        }:
            records.append(value)
    return records


def open_write_scope(
    task_dir: Path,
    repository: Path,
    run_id: str,
    *,
    claimant_pid: int | None = None,
) -> dict[str, Any]:
    """Record that this task is about to write `repository` under `run_id`."""
    return _append(
        task_dir,
        {
            "schema_version": 1,
            "record": OPENED,
            "run_id": run_id,
            "opened_at": utc_now(),
            "before": git_write_state(repository),
            **_claimant_fields(claimant_pid),
        },
    )


def close_write_scope(
    task_dir: Path, run_id: str, *, repository: Path | None = None
) -> dict[str, Any] | None:
    """Record what the scope opened under `run_id` actually did.

    Returns None when there is no matching open scope, because closing one that
    was never opened would invent a result. A repository whose identity changed
    under the run is a refusal for the same reason.
    """
    scope = _open_scope_for_close(task_dir, run_id, repository=repository)
    if scope is None:
        return None
    before = scope["before"]
    repository = Path(before["repository"])
    with repository_lock(repository):
        scope = _open_scope_for_close(task_dir, run_id, repository=repository)
        if scope is None:
            return None
        before = scope["before"]
        after = git_write_state(repository)
        if before.get("common_dir") != after.get("common_dir"):
            raise ValueError("write scope repository identity changed during the run")
        return _append(
            task_dir,
            {
                "schema_version": 1,
                "record": CLOSED,
                "run_id": run_id,
                "closed_at": utc_now(),
                "changed": _changed(before, after),
                "before": before,
                "after": after,
            },
        )


def preserve_terminal_scope_evidence(
    task_dir: Path, runner_meta: dict[str, Any]
) -> dict[str, Any] | None:
    """Transfer an exact prior run's terminal evidence before metadata replacement.

    `runner.json` describes only the current run and is replaced by `start`.
    The write-admission ledger is append-only and keyed by run id, so it is the
    durable home for the previous run's evidence once a successor launch begins.
    Nothing is appended unless the metadata names an actually open scope and a
    terminal outcome for that same run.
    """
    if not isinstance(runner_meta, dict):
        return None
    run_id = runner_meta.get("write_scope_run_id")
    finished_at = runner_meta.get("finished_at")
    outcome = runner_meta.get("outcome")
    if not all(
        isinstance(value, str) and bool(value.strip())
        for value in (run_id, finished_at, outcome)
    ):
        return None
    if _open_scope_for_run(task_dir, run_id) is None:
        return None
    if any(
        record.get("record") == CLAIMANT_TERMINAL
        and record.get("run_id") == run_id
        for record in read_ledger(task_dir)
    ):
        return None
    return _append(
        task_dir,
        {
            "schema_version": 1,
            "record": CLAIMANT_TERMINAL,
            "run_id": run_id,
            "recorded_at": utc_now(),
            "finished_at": finished_at,
            "outcome": outcome,
        },
    )


def _changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    compared = (
        "head",
        "tracked_content_digest",
        "index_digest",
        "untracked_entries_digest",
    )
    return any(before.get(key) != after.get(key) for key in compared if key in before)


def _open_scope_for_run(task_dir: Path, run_id: str) -> dict[str, Any] | None:
    for record in reversed(read_ledger(task_dir)):
        if record.get("run_id") != run_id:
            continue
        if record.get("record") == CLOSED:
            return None
        if record.get("record") == OPENED and isinstance(record.get("before"), dict):
            return record
    return None


def _open_scope_for_close(
    task_dir: Path, run_id: str, *, repository: Path | None = None
) -> dict[str, Any] | None:
    """Find the real opening even after an observer's synthetic settlement.

    A real close is terminal. A `measured_after_abandonment` close is another
    observer's inference; if the original writer later closes honestly, that
    arrival proves the inference was premature and its result must be appended.
    """
    expected = str(repository.resolve()) if repository is not None else None
    for record in reversed(read_ledger(task_dir)):
        if record.get("run_id") != run_id:
            continue
        before = record.get("before") if isinstance(record.get("before"), dict) else {}
        if expected is not None and before.get("repository") != expected:
            continue
        if record.get("record") == CLOSED:
            if record.get("resolution") in SYNTHETIC_RESOLUTIONS:
                continue
            return None
        if record.get("record") == OPENED and isinstance(record.get("before"), dict):
            return record
    return None


def unclosed_scopes(task_dir: Path) -> list[dict[str, Any]]:
    """Scopes this task opened and never closed, newest last."""
    closed = {
        record["run_id"]
        for record in read_ledger(task_dir)
        if record.get("record") == CLOSED and isinstance(record.get("run_id"), str)
    }
    return [
        record
        for record in read_ledger(task_dir)
        if record.get("record") == OPENED
        and record.get("run_id") not in closed
        and isinstance(record.get("before"), dict)
    ]


def resolve_abandoned_scope(scope: dict[str, Any]) -> dict[str, Any]:
    """Prove an abandoned scope was a no-op, or leave attribution unresolved.

    The current tree can prove a no-op only while it still equals the opening
    fingerprint. Once it differs, this module cannot tell whether the abandoned
    run or an unrelated writer caused the difference, so freezing either answer
    in the durable ledger would manufacture attribution.
    """
    before = scope["before"]
    try:
        after = git_write_state(Path(before["repository"]))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"resolved": False, "reason": str(exc), "before": before}
    if before.get("common_dir") != after.get("common_dir"):
        return {
            "resolved": False,
            "reason": "repository identity changed since the scope was opened",
            "before": before,
        }
    if _changed(before, after):
        return {
            "resolved": False,
            "ambiguous": True,
            "reason": "repository diverged after the scope opened; change attribution is unknown",
            "before": before,
            "after": after,
        }
    return {
        "resolved": True,
        "changed": False,
        "before": before,
        "after": after,
        "measured_at": utc_now(),
    }


def write_results(task_dir: Path) -> list[dict[str, Any]]:
    """Every durable determinate result this task produced."""
    return [record for record in read_ledger(task_dir) if record.get("record") == CLOSED]


def accepted_write_run_ids(task_dir: Path) -> set[str]:
    """Write-scope runs covered by a durable successful completion decision."""
    accepted: set[str] = set()
    for record in read_ledger(task_dir):
        if record.get("record") != COMPLETION_ACCEPTED:
            continue
        if record.get("source") == "terminal_completion_backfill" or (
            record.get("source") == "historical_completion_ready"
            and not record.get("candidate_head")
        ):
            # ad94867 briefly emitted this weak legacy receipt from terminal
            # markers alone. Re-evaluate it through the canonical historical
            # completion predicate and replace it with a strong receipt.
            continue
        run_ids = record.get("accepted_run_ids")
        if isinstance(run_ids, list):
            accepted.update(
                run_id for run_id in run_ids if isinstance(run_id, str) and run_id
            )
    return accepted


def record_completion_acceptance(
    task_dir: Path,
    *,
    source: str = "completion_ready",
    additional_scope_evidence: dict[str, str] | None = None,
    candidate_head: str | None = None,
) -> dict[str, Any] | None:
    """Bind the task's accepted completion to its existing write scopes.

    A completion review binds the candidate that existed when the task closed.
    Re-evaluating that review against a later task's HEAD would turn legitimate
    repository history into a retroactive refusal. The append-only receipt
    records only the run IDs already covered at acceptance; later same-task
    rework therefore creates a new obligation instead of inheriting approval.
    ``additional_scope_evidence`` is reserved for a terminal abandoned scope
    whose repository change can no longer be measured after a successor
    advances HEAD; a live or incomplete scope never reaches that compatibility
    path, and the digest binds the durable completion evidence rather than
    misattributing the successor's current repository state to the old task.
    """
    accepted = accepted_write_run_ids(task_dir)
    results = [
        result
        for result in write_results(task_dir)
        if result.get("changed") is True
        and isinstance(result.get("run_id"), str)
        and result["run_id"] not in accepted
        and (
            candidate_head is None
            or _state_head_is_covered(result.get("after"), candidate_head)
        )
    ]
    run_ids = [result["run_id"] for result in results]
    scope_evidence = additional_scope_evidence or {}
    run_ids.extend(
        run_id
        for run_id in scope_evidence
        if run_id and run_id not in accepted and run_id not in run_ids
    )
    if not run_ids:
        return None
    return _append(
        task_dir,
        {
            "schema_version": 1,
            "record": COMPLETION_ACCEPTED,
            "accepted_at": utc_now(),
            "source": source,
            "accepted_run_ids": run_ids,
            **({"candidate_head": candidate_head} if candidate_head else {}),
            "write_result_digests": [state_digest(result["after"]) for result in results],
            "additional_scope_evidence": [
                {
                    "run_id": run_id,
                    "completion_evidence_digest": scope_evidence[run_id],
                }
                for run_id in run_ids
                if run_id in scope_evidence
            ],
        },
    )


def owner_is_cancelled(task_dir: Path) -> bool:
    """Whether the canonical metadata owner says this task was withdrawn.

    A cancelled task will never reach its own gates, so the obligation to review
    its change can never be discharged and would hold the repository against
    every later task forever. Cancellation is read from the canonical status
    owner rather than from a second marker, so the ordinary way a product
    withdraws a task is the way its write scopes are released. The status is read
    live: a task moved back out of ``cancelled`` owes its review again.
    """
    return task_status(task_dir) == CANCELLED_STATUS


def _obligation_key(obligation: dict[str, Any]) -> tuple[str, str, str] | None:
    """What identifies one released obligation, or None when it cannot be named.

    One run writes every repository of an exact set under a single run id, so the
    run alone does not identify what was let go. The repository and the kind of
    obligation complete it, which is also what a later reader needs to find the
    change that was never reviewed.
    """
    parts = (
        obligation.get("run_id"),
        obligation.get("repository"),
        obligation.get("kind"),
    )
    if not all(isinstance(part, str) and part for part in parts):
        return None
    return cast(tuple[str, str, str], parts)


def released_obligation_keys(task_dir: Path) -> set[tuple[str, str, str]]:
    """Obligations a recorded cancellation already released, by run and repository."""
    released: set[tuple[str, str, str]] = set()
    for record in read_ledger(task_dir):
        if record.get("record") != SCOPE_RELEASED:
            continue
        obligations = record.get("released_obligations")
        if not isinstance(obligations, list):
            continue
        for obligation in obligations:
            if not isinstance(obligation, dict):
                continue
            key = _obligation_key(obligation)
            if key is not None:
                released.add(key)
    return released


def record_cancellation_release(
    task_dir: Path, obligations: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Say in the ledger which unreviewed changes a cancellation let go, and why.

    Releasing an unreviewed change is not the same event as accepting a reviewed
    one, so it gets its own record rather than a completion receipt that nobody
    earned. The record keeps each released run id and the digest of the state it
    left behind, so a later reader of this repository can still find the change
    and see that it was never reviewed. Nothing earlier is rewritten; only run
    obligations not already recorded produce a new record. An obligation is
    identified by its run and repository together, so releasing one repository of
    an exact set never silently stands in for the rest of it.
    """
    already = released_obligation_keys(task_dir)
    pending: list[dict[str, Any]] = []
    for obligation in obligations:
        key = _obligation_key(obligation)
        if key is None or key in already:
            continue
        already.add(key)
        pending.append(obligation)
    if not pending:
        return None
    return _append(
        task_dir,
        {
            "schema_version": 1,
            "record": SCOPE_RELEASED,
            "released_at": utc_now(),
            "reason": CANCELLED_RELEASE_REASON,
            "owner_status": CANCELLED_STATUS,
            "detail": (
                "the owning task was cancelled, so its change can never reach that "
                "task's own gates; the obligation is released unreviewed"
            ),
            "released_run_ids": sorted(
                {obligation["run_id"] for obligation in pending}
            ),
            "released_obligations": sorted(pending, key=_obligation_key),
        },
    )


def _released_obligation(
    run_id: Any, repository: Any, state: dict[str, Any], kind: str
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "repository": repository,
        "kind": kind,
        "write_result_digest": state_digest(state),
    }


def _durable_terminal_completion(task_dir: Path) -> bool:
    """Whether the pre-receipt runtime already durably accepted completion.

    Older ledgers predate ``completion_accepted``. Backfill is deliberately
    narrower than completed frontmatter: both canonical runtime projections
    and the matching controller attempt must say completion. The canonical
    completion owner then revalidates every current gate against the recorded
    exact Git candidate and reviewer envelope; this module never reimplements
    review pairing or decision semantics.
    """
    try:
        status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not (
        task_status(task_dir) == "completed"
        and isinstance(status, dict)
        and status.get("state") == "completed"
        and task_phases.current_phase(task_dir) == task_phases.COMPLETED
    ):
        return False
    projection = status.get("dev_pipeline")
    attempt_id = projection.get("attempt_id") if isinstance(projection, dict) else None
    if not isinstance(attempt_id, str) or not attempt_id:
        return False
    events: list[dict[str, Any]] = []
    for path in sorted((task_dir / "dev-pipeline").glob("*/events.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("attempt_id") == attempt_id:
                events.append(event)
    if not any(event.get("kind") == "attempt_completed" for event in events):
        return False
    ready, _reason = completion_ready(task_dir, allow_historical_candidate=True)
    return ready


def _historical_completion_head(task_dir: Path) -> str | None:
    if not _durable_terminal_completion(task_dir):
        return None
    return recorded_completion_candidate_head(task_dir)


def _git_is_ancestor(repository: str, ancestor: str, descendant: str) -> bool:
    if not all(isinstance(value, str) and value for value in (repository, ancestor, descendant)):
        return False
    completed = subprocess.run(
        ["git", "-C", repository, "merge-base", "--is-ancestor", ancestor, descendant],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _state_head_is_covered(state: Any, candidate_head: str) -> bool:
    if not isinstance(state, dict):
        return False
    return _git_is_ancestor(
        str(state.get("repository", "")),
        str(state.get("head", "")),
        candidate_head,
    )


def _ambiguous_scope_is_covered(
    scope: dict[str, Any], resolution: dict[str, Any], candidate_head: str
) -> bool:
    before = scope.get("before")
    after = resolution.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    repository = str(before.get("repository", ""))
    if repository != str(after.get("repository", "")):
        return False
    return _git_is_ancestor(
        repository, str(before.get("head", "")), candidate_head
    ) and _git_is_ancestor(
        repository, candidate_head, str(after.get("head", ""))
    )


def _completion_evidence_digest(task_dir: Path) -> str:
    """Bind the durable acceptance evidence available for a legacy scope."""
    paths = [
        task_dir / "task.md",
        task_dir / "status.json",
        task_dir / task_phases.PHASES_FILE,
        task_dir / "dev-pipeline" / "contract-review" / "completion-review-subject.json",
    ]
    evidence = []
    for path in paths:
        try:
            content = path.read_bytes()
        except OSError:
            continue
        evidence.append(
            {
                "path": str(path.relative_to(task_dir)),
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        )
    return state_digest({"completion_evidence": evidence})


def _claimant_liveness(scope: dict[str, Any]) -> bool | None:
    """True/False when observable; None when a negative is not evidence."""
    pid = scope.get("claimant_pid")
    # Production claims always record a pid. Treating an absent pid as dead
    # preserves settlement of ledgers written by older or synthetic callers.
    if not isinstance(pid, int) or pid <= 0:
        return False
    recorded_namespace = scope.get("claimant_pid_namespace")
    current_namespace = _pid_namespace_identity()
    if recorded_namespace and recorded_namespace != current_namespace:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    recorded = scope.get("claimant_process_marker")
    if recorded is None:
        return True
    current = _process_marker(pid)
    if current is None:
        return None if _process_marker(os.getpid()) is not None else True
    return recorded == current


def _scope_claimant_liveness(task: Path, scope: dict[str, Any]) -> bool | None:
    """Use matching run-owned terminal evidence before observer liveness.

    A foreign PID namespace makes an observer's negative lookup inconclusive.
    The runner that owned this exact write scope can nevertheless record its
    own terminal outcome durably. Requiring the matching run id prevents a
    later run's terminal metadata from settling an older claimant.
    """
    if _scope_has_terminal_evidence(task, scope):
        return False
    return _claimant_liveness(scope)


def _scope_has_terminal_evidence(task: Path, scope: dict[str, Any]) -> bool:
    run_id = scope.get("run_id")
    if any(
        record.get("record") == CLAIMANT_TERMINAL
        and record.get("run_id") == run_id
        and isinstance(record.get("finished_at"), str)
        and bool(record["finished_at"].strip())
        and isinstance(record.get("outcome"), str)
        and bool(record["outcome"].strip())
        for record in read_ledger(task)
    ):
        return True
    try:
        meta = json.loads((task / ".runner" / "runner.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(meta, dict)
        and meta.get("write_scope_run_id") == run_id
        and isinstance(meta.get("finished_at"), str)
        and bool(meta["finished_at"].strip())
        and isinstance(meta.get("outcome"), str)
        and bool(meta["outcome"].strip())
    )


def _claimant_fields(pid: int | None) -> dict[str, Any]:
    if pid is None:
        return {}
    marker = _process_marker(pid)
    return {
        "claimant_pid": pid,
        **(
            {"claimant_pid_namespace": namespace}
            if (namespace := _pid_namespace_identity()) is not None
            else {}
        ),
        **({"claimant_process_marker": marker} if marker is not None else {}),
    }


def _pid_namespace_identity() -> str | None:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def _process_marker(pid: int) -> str | None:
    """Best available process-birth marker; pid-only remains the portable floor."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw.rsplit(")", 1)[1].split()
        return f"proc-start:{fields[19]}"
    except (OSError, UnicodeError, IndexError):
        pass
    try:
        started = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"ps-start:{started}" if started else None


def settle_abandoned_scopes(
    *,
    tasks_root: Path,
    common_dir: str,
    is_live: Callable[[Path], bool | None],
) -> None:
    """Durably close only provable abandoned no-ops before a successor claims."""
    for task in sorted(p for p in tasks_root.iterdir() if p.is_dir()):
        task = task.resolve()
        for scope in unclosed_scopes(task):
            if scope["before"].get("common_dir") != common_dir:
                continue
            if _scope_liveness(task, scope, is_live) is not False:
                continue
            resolution = resolve_abandoned_scope(scope)
            if not resolution.get("resolved"):
                continue
            _append(
                task,
                {
                    "schema_version": 1,
                    "record": CLOSED,
                    "run_id": scope.get("run_id"),
                    "closed_at": resolution["measured_at"],
                    "changed": resolution["changed"],
                    "before": resolution["before"],
                    "after": resolution["after"],
                    "resolution": "measured_after_abandonment",
                },
            )


def reconcile_owner_scopes(
    *,
    task_dir: Path,
    common_dir: str,
) -> None:
    """Close dead scopes so their owner can enter same-number rework.

    Task-level liveness describes the new supervised run and cannot identify an
    older scope under the same task number. The claimant identity recorded on
    that scope can. Unknown or live claimants stay open and refuse admission; a
    dead claimant is durably closed only when the current fingerprint proves a
    no-op. Divergent attribution stays open and recomputed for other tasks while
    its owner enters rework under the same number.
    """
    for scope in unclosed_scopes(task_dir):
        if scope["before"].get("common_dir") != common_dir:
            continue
        if _scope_claimant_liveness(task_dir, scope) is not False:
            continue
        resolution = resolve_abandoned_scope(scope)
        if not resolution.get("resolved"):
            continue
        _append(
            task_dir,
            {
                "schema_version": 1,
                "record": CLOSED,
                "run_id": scope.get("run_id"),
                "closed_at": resolution.get("measured_at", utc_now()),
                "changed": False,
                "before": resolution["before"],
                "after": resolution["after"],
                "resolution": "measured_after_abandonment",
            },
        )


def claim_write_scope(
    *,
    tasks_root: Path,
    task_dir: Path,
    repository: Path,
    run_id: str,
    is_live: Callable[[Path], bool | None],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Atomically settle, recheck and claim one repository write scope."""
    with repository_lock(repository):
        common_dir = git_write_state(repository)["common_dir"]
        settle_abandoned_scopes(
            tasks_root=tasks_root,
            common_dir=common_dir,
            is_live=is_live,
        )
        blockers = admission_blockers(
            tasks_root=tasks_root,
            repository=repository,
            requesting_task=task_dir,
            is_live=is_live,
        )
        if blockers:
            return None, blockers
        reconcile_owner_scopes(task_dir=task_dir, common_dir=common_dir)
        return (
            open_write_scope(
                task_dir,
                repository,
                run_id,
                claimant_pid=os.getpid(),
            ),
            [],
        )


def claim_write_scopes(
    *,
    tasks_root: Path,
    task_dir: Path,
    repositories: list[Path],
    run_id: str,
    is_live: Callable[[Path], bool | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Check and claim an exact repository set without a partial open scope."""
    ordered = [Path(path).resolve() for path in repositories]
    with repository_locks(ordered):
        blockers: list[dict[str, Any]] = []
        for repository in ordered:
            common_dir = git_write_state(repository)["common_dir"]
            settle_abandoned_scopes(
                tasks_root=tasks_root, common_dir=common_dir, is_live=is_live
            )
            blockers.extend(
                admission_blockers(
                    tasks_root=tasks_root,
                    repository=repository,
                    requesting_task=task_dir,
                    is_live=is_live,
                )
            )
        if blockers:
            return [], blockers
        for repository in ordered:
            reconcile_owner_scopes(
                task_dir=task_dir,
                common_dir=git_write_state(repository)["common_dir"],
            )
        return [
            open_write_scope(
                task_dir, repository, run_id, claimant_pid=os.getpid()
            )
            for repository in ordered
        ], []


def outstanding_write_results(task_dir: Path) -> list[dict[str, Any]]:
    """Changes this task made to a repository that its own gates have not closed.

    The completion owner is asked, not re-implemented: a task whose durable
    state authorizes completion has satisfied whatever review its contract
    requires, and its change is no longer outstanding. A cancelled task is the
    other terminal answer: its gates will never be asked, so the obligation is
    released with a recorded reason instead of outliving the task that owed it.
    """
    accepted = accepted_write_run_ids(task_dir)
    changes = [
        result
        for result in write_results(task_dir)
        if result.get("changed") is True and result.get("run_id") not in accepted
    ]
    if not changes:
        return []
    if owner_is_cancelled(task_dir):
        record_cancellation_release(
            task_dir,
            [
                _released_obligation(
                    result.get("run_id"),
                    result["before"].get("repository"),
                    result["after"],
                    "write_result",
                )
                for result in changes
            ],
        )
        return []
    ready, _reason = completion_ready(task_dir)
    if ready:
        record_completion_acceptance(task_dir)
        return []
    historical_head = _historical_completion_head(task_dir)
    if historical_head:
        record_completion_acceptance(
            task_dir,
            source="historical_completion_ready",
            candidate_head=historical_head,
        )
        accepted = accepted_write_run_ids(task_dir)
        return [result for result in changes if result.get("run_id") not in accepted]
    return changes


def admission_blockers(
    *,
    tasks_root: Path,
    repository: Path,
    requesting_task: Path,
    is_live: Callable[[Path], bool | None],
) -> list[dict[str, Any]]:
    """Why this task may not open a write scope in this repository right now.

    An empty list is admission. Each blocker names the task that holds the
    repository and why, so the answer is actionable rather than a refusal.
    """
    common_dir = git_write_state(repository)["common_dir"]
    requesting = requesting_task.resolve()
    blockers: list[dict[str, Any]] = []
    for task in sorted(p for p in tasks_root.iterdir() if p.is_dir()):
        task = task.resolve()
        scopes = unclosed_scopes(task)
        in_repository = [
            scope for scope in scopes if scope["before"].get("common_dir") == common_dir
        ]
        liveness = [
            (scope, _scope_liveness(task, scope, is_live))
            for scope in in_repository
        ]
        foreign_live = [state for _scope, state in liveness if state is not False]
        if in_repository and foreign_live and task != requesting:
            unknown = any(state is None for state in foreign_live)
            blockers.append(
                {
                    "task": str(task),
                    "reason": LIVE_OVERLAPPING_WRITE,
                    "detail": (
                        "another task's writer cannot be proven absent"
                        if unknown
                        else "another task is writing this repository right now"
                    ),
                }
            )
            continue
        if task == requesting:
            for scope in in_repository:
                claimant_liveness = _scope_claimant_liveness(task, scope)
                if claimant_liveness is not False:
                    blockers.append(
                        {
                            "task": str(task),
                            "reason": UNRESOLVED_OWN_WRITE_SCOPE,
                            "detail": (
                                "the older scope's claimant cannot be proven absent"
                                if claimant_liveness is None
                                else "the older scope's claimant is still live"
                            ),
                        }
                    )
                    continue
                resolution = resolve_abandoned_scope(scope)
                if not resolution.get("resolved") and not resolution.get("ambiguous"):
                    blockers.append(
                        {
                            "task": str(task),
                            "reason": UNRESOLVED_OWN_WRITE_SCOPE,
                            "detail": resolution.get("reason", "scope cannot be measured"),
                        }
                    )
        if task == requesting:
            # A task's own unreviewed change must not lock it out of the
            # repository: repairing that change is what the rework phase is,
            # and it happens under this same number.
            continue
        for scope in in_repository:
            if _scope_liveness(task, scope, is_live) is not False:
                continue
            resolution = resolve_abandoned_scope(scope)
            if not resolution.get("ambiguous"):
                continue
            run_id = scope.get("run_id")
            if isinstance(run_id, str) and run_id in accepted_write_run_ids(task):
                continue
            if owner_is_cancelled(task):
                # The claimant is provably gone and its owner was withdrawn, so
                # this divergence has nobody left to attribute or review it.
                record_cancellation_release(
                    task,
                    [
                        _released_obligation(
                            run_id,
                            scope["before"].get("repository"),
                            resolution["after"],
                            "abandoned_scope",
                        )
                    ],
                )
                continue
            ready, _reason = completion_ready(task)
            historical_head = None if ready else _historical_completion_head(task)
            if ready or (
                historical_head
                and _ambiguous_scope_is_covered(scope, resolution, historical_head)
            ):
                record_completion_acceptance(
                    task,
                    source=(
                        "completion_ready"
                        if ready
                        else "historical_completion_ready"
                    ),
                    candidate_head=historical_head,
                    additional_scope_evidence=(
                        {run_id: _completion_evidence_digest(task)}
                        if isinstance(run_id, str)
                        else {}
                    ),
                )
                continue
            blockers.append(
                {
                    "task": str(task),
                    "reason": UNREVIEWED_OVERLAPPING_WRITE,
                    "detail": (
                        "this abandoned scope changed the repository and has not "
                        "closed its own gates"
                    ),
                    "write_result_digest": state_digest(resolution["after"]),
                }
            )
        for result in outstanding_write_results(task):
            if result["before"].get("common_dir") != common_dir:
                continue
            blockers.append(
                {
                    "task": str(task),
                    "reason": UNREVIEWED_OVERLAPPING_WRITE,
                    "detail": "this task changed the repository and has not closed its own gates",
                    "write_result_digest": state_digest(result["after"]),
                }
            )
    return blockers


def _scope_liveness(
    task: Path,
    scope: dict[str, Any],
    is_live: Callable[[Path], bool | None],
) -> bool | None:
    if _scope_has_terminal_evidence(task, scope):
        return False
    task_liveness = is_live(task)
    if task_liveness is True:
        return True
    claimant_liveness = _claimant_liveness(scope)
    if claimant_liveness is True:
        return True
    if task_liveness is None or claimant_liveness is None:
        return None
    return False
