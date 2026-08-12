#!/usr/bin/env python3
from __future__ import annotations

import os
import json
import re
from pathlib import Path

try:
    from .application_adapter import ApplicationEventV1, load_application
except ImportError:
    from application_adapter import ApplicationEventV1, load_application


# Shared machine vocabulary for the branch where an owner process ended after
# publishing an incomplete bound. The name reports an observation, not an
# inferred stopping point. Older task artifacts remain readable.
INTERRUPTED_COMPLETION_KIND = "owner_ended_after_incomplete_published_progress"
LEGACY_INTERRUPTED_COMPLETION_KINDS = frozenset(
    {"owner_stopped_with_work_outstanding"}
)


def is_interrupted_completion_kind(kind: object) -> bool:
    return (
        kind == INTERRUPTED_COMPLETION_KIND
        or kind in LEGACY_INTERRUPTED_COMPLETION_KINDS
    )


def repo_root() -> Path:
    configured = os.environ.get("TASK_AGENT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if __package__:
        return Path.cwd().resolve()
    return Path(__file__).resolve().parents[3]


def _task_title(task_dir: Path) -> str:
    task_file = task_dir / "task.md"
    if not task_file.exists():
        return task_dir.name
    for line in task_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return task_dir.name


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root()))
    except ValueError:
        return str(path.resolve())


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def format_pipeline_stop_message(
    task_dir: Path,
    summary: str,
    requested_action: str,
    artifact_paths: list[Path] | None = None,
) -> str:
    lines = [
        "task-agent: pipeline stopped",
        f"Task: {_task_title(task_dir)}",
        f"Dir: {_rel(task_dir)}",
        f"What happened: {_compact(summary)}",
        f"What you need to do: {_compact(requested_action)}",
    ]
    if artifact_paths:
        rendered = ", ".join(_rel(path) for path in artifact_paths[:3])
        lines.append(f"Artifacts: {rendered}")
    return "\n".join(lines)


def format_pipeline_status_message(
    task_dir: Path,
    status: str,
    artifact_paths: list[Path] | None = None,
) -> str:
    lines = [
        "task-agent: pipeline status",
        f"Task: {_task_title(task_dir)}",
        f"Dir: {_rel(task_dir)}",
        f"Now: {_compact(status)}",
    ]
    if artifact_paths:
        rendered = ", ".join(_rel(path) for path in artifact_paths[:3])
        lines.append(f"Artifacts: {rendered}")
    return "\n".join(lines)


def try_send_pipeline_stop_message(
    task_dir: Path,
    summary: str,
    requested_action: str,
    artifact_paths: list[Path] | None = None,
    destination: str | None = None,
    application: str | None = None,
    workflow: str = "standard",
) -> tuple[bool, str]:
    """Deliver a stop message, labelled with the workflow it stopped.

    An installation may route or audit by `workflow`, and nothing in the message
    text tells it which one this was. Only the caller knows, so it says.
    """
    text = format_pipeline_stop_message(task_dir, summary, requested_action, artifact_paths)
    if application is None:
        try:
            metadata = json.loads(
                (task_dir / ".runner" / "runner.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            metadata = {}
        registration = metadata.get("application") if isinstance(metadata, dict) else None
        application = registration.get("spec") if isinstance(registration, dict) else None
    result = load_application(application).deliver_event(
        ApplicationEventV1(
            task_dir=task_dir,
            kind="pipeline_stopped",
            workflow=workflow,
            payload={"message": text},
            destination=destination,
        )
    )
    return result.delivered, result.detail


def try_send_pipeline_status_message(
    task_dir: Path,
    status: str,
    artifact_paths: list[Path] | None = None,
    destination: str | None = None,
    application: str | None = None,
) -> tuple[bool, str]:
    text = format_pipeline_status_message(task_dir, status, artifact_paths)
    result = load_application(application).deliver_event(
        ApplicationEventV1(
            task_dir=task_dir,
            kind="pipeline_status",
            workflow="standard",
            payload={"message": text},
            destination=destination,
        )
    )
    return result.delivered, result.detail
