#!/usr/bin/env python3
"""One durable completion-readiness decision for task-runner profiles."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from task_contract import (
    load_task_contract,
    unsatisfied_live_evidence,
    unsatisfied_policy_families,
    unsatisfied_review_verdict,
)


TASKS_INDEX_PATH = (
    Path(__file__).resolve().parents[2] / "task-creator" / "scripts" / "tasks_index.py"
)
_tasks_index_module = None


def load_tasks_index():
    """Load the canonical task metadata reader without inventing a second parser."""
    global _tasks_index_module
    if _tasks_index_module is None:
        spec = importlib.util.spec_from_file_location("tasks_index", TASKS_INDEX_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load task metadata owner: {TASKS_INDEX_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _tasks_index_module = module
    return _tasks_index_module


def task_reference(task_dir: Path) -> str:
    """Return the stable task number, falling back only for diagnostic text."""
    try:
        number = load_tasks_index().read_record(task_dir)["id"]
    except Exception:
        return task_dir.name
    return str(number) if number is not None else task_dir.name


def task_status(task_dir: Path) -> str:
    """Read status through ``tasks_index.py`` and fail closed on any error."""
    try:
        return load_tasks_index().read_record(task_dir)["status"]
    except Exception:
        return "unknown"


def completion_ready(task_dir: Path) -> tuple[bool, str]:
    """Decide whether durable task state authorizes successful completion.

    This composes existing semantic owners: ``tasks_index.py`` owns metadata and
    ``task_contract.py`` owns effective-contract, evidence, and policy-family
    semantics. Standard and dev-pipeline finalizers consume this function rather
    than maintaining profile-specific copies of the same decision.
    """
    contract_path = task_dir / "task_contract.json"
    if not contract_path.exists():
        return False, "task_contract.json is absent; no contract can authorize completion"

    status = task_status(task_dir)
    if status != "completed":
        return False, f"task.md frontmatter status is {status!r}, not 'completed'"

    plan_path = task_dir / "plan.md"
    if not plan_path.is_file():
        return False, "plan.md is absent; completion readiness cannot be established"
    try:
        plan_text = plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return False, f"plan.md is unreadable: {exc}"
    if "[pending]" in plan_text or "[in_progress]" in plan_text:
        return False, "plan.md still has unfinished steps"

    contract = load_task_contract(task_dir)
    verification_path = task_dir / "verification.md"
    verification = (
        verification_path.read_text(encoding="utf-8")
        if verification_path.exists()
        else ""
    )
    unsatisfied = unsatisfied_live_evidence(contract, verification)
    if unsatisfied:
        return False, f"required live evidence is not established: {'; '.join(unsatisfied)}"

    verdict_problems = unsatisfied_review_verdict(contract, task_dir)
    if verdict_problems:
        return False, f"required review verdict is not established: {'; '.join(verdict_problems)}"

    unestablished = unsatisfied_policy_families(contract, task_dir)
    if unestablished:
        return False, f"contract policy families are not established: {'; '.join(unestablished)}"
    return True, ""
