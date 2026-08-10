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
nothing to overwrite. An abandoned scope is resolved by measuring the repository
now, and where it cannot be resolved it blocks only the task that abandoned it.

Whether an outstanding change has been reviewed is not decided here. This module
reports the outstanding change; the completion owner decides whether the task's
own gates — contract review verdict, live evidence, policy families — are
satisfied. Pairing policy belongs to whichever installation defines it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from task_completion import completion_ready
from task_contract import (
    capture_preexisting_tracked_dirty_baseline,
    git_repository_identity,
)


LEDGER_NAME = "write-admission.jsonl"

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
    """Tracked-byte identity of one repository right now.

    HEAD alone would miss uncommitted work and the dirty baseline alone would
    miss a commit, so identity is both. Both come from the existing Git-state
    owner rather than a second parser.
    """
    identity = git_repository_identity(repository)
    root = Path(identity["worktree"])
    head = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    baseline = capture_preexisting_tracked_dirty_baseline(root)
    return {
        "repository": str(root),
        "common_dir": identity["common_dir"],
        "head": head,
        "tracked_content_digest": baseline["digest"],
    }


def state_digest(state: dict[str, Any]) -> str:
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


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


def open_write_scope(task_dir: Path, repository: Path, run_id: str) -> dict[str, Any]:
    """Record that this task is about to write `repository` under `run_id`."""
    return _append(
        task_dir,
        {
            "schema_version": 1,
            "record": OPENED,
            "run_id": run_id,
            "opened_at": utc_now(),
            "before": git_write_state(repository),
        },
    )


def close_write_scope(task_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Record what the scope opened under `run_id` actually did.

    Returns None when there is no matching open scope, because closing one that
    was never opened would invent a result. A repository whose identity changed
    under the run is a refusal for the same reason.
    """
    scope = _open_scope_for_run(task_dir, run_id)
    if scope is None:
        return None
    before = scope["before"]
    after = git_write_state(Path(before["repository"]))
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
    return (
        before.get("head") != after.get("head")
        or before.get("tracked_content_digest") != after.get("tracked_content_digest")
    )


def _open_scope_for_run(task_dir: Path, run_id: str) -> dict[str, Any] | None:
    for record in reversed(read_ledger(task_dir)):
        if record.get("run_id") != run_id:
            continue
        if record.get("record") == CLOSED:
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
    """Decide what an abandoned scope did by measuring the repository now.

    A run that died without closing its scope still either changed the
    repository or did not, and the repository can still be asked. Only where it
    cannot be asked at all — the worktree is gone, or Git refuses — does the
    scope stay unresolved, and then it constrains its own task and no other.
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
    return {
        "resolved": True,
        "changed": _changed(before, after),
        "before": before,
        "after": after,
        "measured_at": utc_now(),
    }


def write_results(task_dir: Path) -> list[dict[str, Any]]:
    """Every determinate result this task produced, abandoned scopes included."""
    results = [record for record in read_ledger(task_dir) if record.get("record") == CLOSED]
    for scope in unclosed_scopes(task_dir):
        resolution = resolve_abandoned_scope(scope)
        if resolution.get("resolved"):
            results.append(
                {
                    "schema_version": 1,
                    "record": CLOSED,
                    "run_id": scope.get("run_id"),
                    "closed_at": resolution["measured_at"],
                    "changed": resolution["changed"],
                    "before": resolution["before"],
                    "after": resolution["after"],
                    "resolution": "measured_after_abandonment",
                }
            )
    return results


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
    is_live: Callable[[Path], bool],
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
        if in_repository and is_live(task) and task != requesting:
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
