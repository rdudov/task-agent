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

Here a read-only or dry run opens no scope and appends nothing, so it has
nothing to overwrite. Before a successor is admitted, an abandoned scope is
durably closed under the repository lock only when the repository still equals
its opening fingerprint, which proves that scope was a no-op. Unknown liveness
or a divergent repository is ambiguous, not evidence to freeze as a verdict;
such a scope blocks only the task that abandoned it.

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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from task_completion import completion_ready
from task_contract import (
    capture_preexisting_tracked_dirty_baseline,
    git_repository_identity,
)


LEDGER_NAME = "write-admission.jsonl"
REPOSITORY_LOCK_NAME = "task-agent-write-admission.lock"

OPENED = "opened"
CLOSED = "closed"

LIVE_OVERLAPPING_WRITE = "live_overlapping_write"
UNREVIEWED_OVERLAPPING_WRITE = "unreviewed_overlapping_write"
UNRESOLVED_OWN_WRITE_SCOPE = "unresolved_own_write_scope"


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
        if isinstance(value, dict) and value.get("record") in {OPENED, CLOSED}:
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


def close_write_scope(task_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Record what the scope opened under `run_id` actually did.

    Returns None when there is no matching open scope, because closing one that
    was never opened would invent a result. A repository whose identity changed
    under the run is a refusal for the same reason.
    """
    scope = _open_scope_for_close(task_dir, run_id)
    if scope is None:
        return None
    before = scope["before"]
    repository = Path(before["repository"])
    with repository_lock(repository):
        scope = _open_scope_for_close(task_dir, run_id)
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


def _open_scope_for_close(task_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Find the real opening even after an observer's synthetic settlement.

    A real close is terminal. A `measured_after_abandonment` close is another
    observer's inference; if the original writer later closes honestly, that
    arrival proves the inference was premature and its result must be appended.
    """
    for record in reversed(read_ledger(task_dir)):
        if record.get("run_id") != run_id:
            continue
        if record.get("record") == CLOSED:
            if record.get("resolution") == "measured_after_abandonment":
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


def _claimant_liveness(scope: dict[str, Any]) -> bool | None:
    """True/False when observable; None when a negative is not evidence."""
    pid = scope.get("claimant_pid")
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
        return (
            open_write_scope(
                task_dir,
                repository,
                run_id,
                claimant_pid=os.getpid(),
            ),
            [],
        )


def outstanding_write_results(task_dir: Path) -> list[dict[str, Any]]:
    """Changes this task made to a repository that its own gates have not closed.

    The completion owner is asked, not re-implemented: a task whose durable
    state authorizes completion has satisfied whatever review its contract
    requires, and its change is no longer outstanding.
    """
    changes = [result for result in write_results(task_dir) if result.get("changed") is True]
    if not changes:
        return []
    ready, _reason = completion_ready(task_dir)
    return [] if ready else changes


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
        if (
            in_repository
            and any(_scope_liveness(task, scope, is_live) is True for scope in in_repository)
            and task != requesting
        ):
            blockers.append(
                {
                    "task": str(task),
                    "reason": LIVE_OVERLAPPING_WRITE,
                    "detail": "another task is writing this repository right now",
                }
            )
            continue
        if task == requesting:
            for scope in in_repository:
                resolution = resolve_abandoned_scope(scope)
                if not resolution.get("resolved"):
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
            if not resolution.get("resolved") or not resolution.get("changed"):
                continue
            ready, _reason = completion_ready(task)
            if not ready:
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
    task_liveness = is_live(task)
    if task_liveness is True:
        return True
    claimant_liveness = _claimant_liveness(scope)
    if claimant_liveness is True:
        return True
    if task_liveness is None or claimant_liveness is None:
        return None
    return False
