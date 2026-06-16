#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path


def repo_root() -> Path:
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
) -> tuple[bool, str]:
    format_pipeline_stop_message(task_dir, summary, requested_action, artifact_paths)
    return False, "no notification transport configured"


def try_send_pipeline_status_message(
    task_dir: Path,
    status: str,
    artifact_paths: list[Path] | None = None,
) -> tuple[bool, str]:
    format_pipeline_status_message(task_dir, status, artifact_paths)
    return False, "no notification transport configured"
