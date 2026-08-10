#!/usr/bin/env python3
"""The public surface for one task's identity, phase, observation and state.

Everything here already had an owner: `tasks_index.py` owns task metadata,
`task_contract.py` owns the effective contract, `task_completion.py` owns whether
a completion may be accepted, `task_phases.py` owns phases, `write_admission.py`
owns Git write scopes, and `task_runner.py` owns supervision. What did not exist
was a way to *ask* — so consumers reached into those modules, imported private
helpers, and broke whenever one was renamed.

This composes them into one JSON answer and adds nothing of its own except
actuality: how long ago the task was observably touched. Actuality is measured
from the filesystem, never from a timestamp a child wrote about itself, because
a stalled child can keep writing a fresh `updated_at` and a stopped one cannot
correct the last it wrote.

    task_engine.py state <task>      one document: identity, phase, actuality,
                                     supervision, completion readiness
    task_engine.py phases <task>     the phase this task is in and how it got there
    task_engine.py actuality <task>  observed freshness alone
    task_engine.py admission <task> --repo R   whether R may be written now
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import task_phases
import write_admission
from task_completion import completion_ready, task_reference, task_status
from task_contract import contract_gate_status, load_task_contract
from task_runner import (
    admission_liveness,
    live_run_processes,
    read_json,
    resolve_task_dir,
    runner_meta_path,
    status_path,
    structured_progress,
)


# How long a task may go untouched before its observed state is called stale.
# A run that publishes every few minutes and one that compiles for half an hour
# are both healthy, so this is a reporting threshold rather than a control, and
# an installation that knows its own cadence sets it.
STALE_AFTER_SECONDS_ENV = "TASK_AGENT_ACTUALITY_STALE_SECONDS"
DEFAULT_STALE_AFTER_SECONDS = 900

# Files whose modification time means somebody actually did something to this
# task. `.runner/` metadata is deliberately excluded: the supervisor touches it
# on its own schedule, so it would report freshness the work does not have.
OBSERVED_FILES = ("progress.json", "status.json", "trace.md", "phases.json", "verification.md")


def stale_after_seconds() -> int:
    raw = os.environ.get(STALE_AFTER_SECONDS_ENV)
    if raw is None:
        return DEFAULT_STALE_AFTER_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{STALE_AFTER_SECONDS_ENV} must be an integer: {raw!r}") from exc
    if value <= 0:
        raise SystemExit(f"{STALE_AFTER_SECONDS_ENV} must be positive: {value}")
    return value


def actuality(task_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    """How recently this task was observably touched, and by which file.

    The answer comes from `stat`, so it says what happened rather than what a
    child claimed. A child that has hung still has a real last-write time; a
    child that reports its own `updated_at` can hang after writing one.
    """
    moment = time.time() if now is None else now
    observed: list[dict[str, Any]] = []
    for name in OBSERVED_FILES:
        try:
            stat = (task_dir / name).stat()
        except OSError:
            continue
        observed.append(
            {"file": name, "modified_at": stat.st_mtime, "age_seconds": max(0.0, moment - stat.st_mtime)}
        )
    threshold = stale_after_seconds()
    if not observed:
        return {
            "schema_version": 1,
            "observed": [],
            "stale": True,
            "reason": "no observable task artifact has ever been written",
            "stale_after_seconds": threshold,
        }
    freshest = min(observed, key=lambda item: item["age_seconds"])
    return {
        "schema_version": 1,
        "observed": sorted(observed, key=lambda item: item["file"]),
        "freshest_file": freshest["file"],
        "age_seconds": round(freshest["age_seconds"], 3),
        "stale": freshest["age_seconds"] > threshold,
        "stale_after_seconds": threshold,
        "measured_from": "filesystem",
    }


def supervision(task_dir: Path) -> dict[str, Any]:
    """What is observably running for this task, and on what evidence."""
    meta = read_json(runner_meta_path(task_dir))
    live = live_run_processes(task_dir)
    return {
        "live": bool(live),
        "processes": live,
        "runner": meta.get("runner"),
        "workflow": meta.get("workflow"),
        "started_at": meta.get("started_at"),
        "finished_at": meta.get("finished_at"),
        "exit_code": meta.get("exit_code"),
        "supervision_boundary": meta.get("supervision_boundary"),
        "dry_run": bool(meta.get("dry_run")),
    }


def state(task_dir: Path) -> dict[str, Any]:
    """One document describing this task, composed from its existing owners."""
    ready, reason = completion_ready(task_dir)
    contract = load_task_contract(task_dir)
    document: dict[str, Any] = {
        "schema_version": 1,
        "task_dir": str(task_dir),
        "task_ref": task_reference(task_dir),
        "status": task_status(task_dir),
        "phase": task_phases.current_phase(task_dir),
        "phase_sequence": task_phases.phase_sequence(task_dir),
        "contract_gate_status": contract_gate_status(contract),
        "completion": {"ready": ready, **({} if ready else {"reason": reason})},
        "actuality": actuality(task_dir),
        "supervision": supervision(task_dir),
        "run_status": read_json(status_path(task_dir)),
        "outstanding_write_results": [
            {
                "repository": result["before"].get("repository"),
                "run_id": result.get("run_id"),
                "digest": write_admission.state_digest(result["after"]),
            }
            for result in write_admission.outstanding_write_results(task_dir)
        ],
    }
    progress = structured_progress(task_dir)
    if progress:
        document["progress"] = progress
    return document


def phases(task_dir: Path) -> dict[str, Any]:
    record = task_phases.read_phases(task_dir)
    return {
        "schema_version": 1,
        "task_dir": str(task_dir),
        "phase": task_phases.current_phase(task_dir),
        "sequence": task_phases.phase_sequence(task_dir),
        "history": task_phases.phase_history(task_dir),
        "updated_at": record.get("updated_at"),
    }


def admission(task_dir: Path, repository: Path) -> dict[str, Any]:
    blockers = write_admission.admission_blockers(
        tasks_root=task_dir.parent,
        repository=repository,
        requesting_task=task_dir,
        is_live=admission_liveness,
    )
    return {
        "schema_version": 1,
        "task_dir": str(task_dir),
        "repository": str(repository),
        "admitted": not blockers,
        "blockers": blockers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observe one task's public state.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("state", "Print the composed public state document for a task."),
        ("phases", "Print the phase this task is in and how it got there."),
        ("actuality", "Print observed freshness for a task."),
        ("admission", "Print whether this task may write a Git repository now."),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("task_dir")
        if name == "admission":
            sub.add_argument("--repo", required=True)

    args = parser.parse_args(argv)
    task_dir = resolve_task_dir(args.task_dir)
    if not task_dir.is_dir():
        raise SystemExit(f"Task directory does not exist: {task_dir}")

    if args.command == "state":
        payload = state(task_dir)
    elif args.command == "phases":
        payload = phases(task_dir)
    elif args.command == "actuality":
        payload = actuality(task_dir)
    else:
        repository = Path(args.repo).expanduser()
        if not repository.is_dir():
            raise SystemExit(f"--repo must be a directory: {repository}")
        payload = admission(task_dir, repository.resolve())

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
