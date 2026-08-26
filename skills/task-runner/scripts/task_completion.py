#!/usr/bin/env python3
"""One durable completion-readiness decision for task-runner profiles."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from . import review_admission, task_phases
    from .application_adapter import (
        ApplicationAdapterError,
        CompletionRequestV1,
        load_application,
    )
    from .task_contract import (
        enforced_live_evidence,
        load_task_contract,
        unsatisfied_live_evidence,
        unsatisfied_policy_families,
        unsatisfied_review_verdict,
        verification_gate_result,
    )
except ImportError:
    import review_admission
    import task_phases
    from application_adapter import (
        ApplicationAdapterError,
        CompletionRequestV1,
        load_application,
    )
    from task_contract import (
        enforced_live_evidence,
        load_task_contract,
        unsatisfied_live_evidence,
        unsatisfied_policy_families,
        unsatisfied_review_verdict,
        verification_gate_result,
    )


def resolve_tasks_index_path(
    module_file: str | Path = __file__, executable: str | Path = sys.executable
) -> Path:
    """Find the metadata CLI from either a source tree or an installed runtime."""
    source_path = (
        Path(module_file).resolve().parents[2]
        / "task-creator"
        / "scripts"
        / "tasks_index.py"
    )
    if source_path.is_file():
        return source_path

    interpreter_entrypoint = Path(executable).parent / "task-agent-tasks-index"
    if interpreter_entrypoint.is_file():
        return interpreter_entrypoint

    discovered_entrypoint = shutil.which("task-agent-tasks-index")
    return (
        Path(discovered_entrypoint)
        if discovered_entrypoint is not None
        else interpreter_entrypoint
    )


TASKS_INDEX_PATH = resolve_tasks_index_path()
_tasks_index_module = None

ENGINE_OWNED_COMPLETION_GATE = "engine"
USER_OR_EXTERNAL_COMPLETION_GATE = "user_or_external"
ENGINE_EVIDENCE_OWNERS = frozenset(
    {"author", "engine", "executor", "pipeline", "reviewer"}
)


class CompletionFailure(str):
    """Human-readable refusal text with a machine-owned gate identity."""

    gate: str
    owner: str

    def __new__(cls, reason: str, *, gate: str, owner: str):
        value = super().__new__(cls, reason)
        value.gate = gate
        value.owner = owner
        return value


def completion_failure(reason: str, *, gate: str, owner: str) -> CompletionFailure:
    return CompletionFailure(reason, gate=gate, owner=owner)


def load_tasks_index():
    """Load the canonical task metadata reader without inventing a second parser."""
    global _tasks_index_module
    if _tasks_index_module is None:
        try:
            from task_agent_task_creator import tasks_index as packaged_tasks_index
        except ImportError:
            packaged_tasks_index = None
        if packaged_tasks_index is not None:
            _tasks_index_module = packaged_tasks_index
            return _tasks_index_module
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


def independent_review_blocker(task_dir: Path) -> dict | None:
    """Return the structured next role while admitted review is outstanding.

    The review owner already knows whether this number needs its reviewer or
    needs to return to its author. Persist that decision with a refused close,
    independently of which completion gate supplied the displayed reason, so
    installation adapters do not infer ownership from a launch admission or
    parse human wording.
    """
    author_phases = task_phases.author_work_entries(task_dir)
    status = review_admission.independent_review_status(
        task_dir, author_phases=author_phases
    )
    if status["satisfied"]:
        return None
    reason = (
        "the independent review this task was admitted with is not established: "
        + str(status["reason"])
    )
    if not status.get("required"):
        return {"reason": reason + ". " + str(status.get("action", ""))}

    binding = review_admission.bound_author_admission(task_dir)
    pair = binding.get("pair") if isinstance(binding, dict) else {}
    last_round = status.get("last_round")
    latest_author_at = max(
        (str(entry.get("entered_at", "")) for entry in author_phases),
        default="",
    )
    last_round_at = (
        str(last_round.get("recorded_at", ""))
        if isinstance(last_round, dict)
        else ""
    )
    needs_rework = (
        isinstance(last_round, dict)
        and str(last_round.get("decision", "")).strip().lower() == "rework"
        and latest_author_at <= last_round_at
    )
    owner_role = "author" if needs_rework else "reviewer"
    owner_runner = pair.get(f"{owner_role}_runner") if isinstance(pair, dict) else None
    if owner_runner not in review_admission.RUNNER_FAMILIES:
        return {"reason": reason + ". " + str(status.get("action", ""))}
    return {
        "reason": reason,
        "phase_transition": {
            "schema_version": 1,
            "next_phase": "rework" if needs_rework else "review",
            "owner_role": owner_role,
            "owner_runner": owner_runner,
            "automatic": False,
        },
    }


def set_task_metadata_status(task_dir: Path, status: str) -> None:
    """Write task state through the canonical metadata command or fail visibly."""
    if task_status(task_dir) == status:
        return
    command = (
        [sys.executable, str(TASKS_INDEX_PATH)]
        if TASKS_INDEX_PATH.suffix == ".py"
        else [str(TASKS_INDEX_PATH)]
    )
    env = os.environ.copy()
    env["TASKS_INDEX_ROOT"] = str(task_dir.resolve().parents[1])
    result = subprocess.run(
        [*command, "set-status", task_reference(task_dir), status],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"canonical task metadata owner refused status {status!r}: "
            + (detail or f"exit {result.returncode}")
        )
    if task_status(task_dir) != status:
        raise RuntimeError(
            "canonical task metadata owner returned success without persisting "
            f"status {status!r}"
        )


def complete_task_metadata(task_dir: Path) -> None:
    """Close a task through the canonical metadata owner."""
    set_task_metadata_status(task_dir, "completed")


def block_task_metadata(task_dir: Path) -> None:
    """Keep a refused close visible through the canonical metadata owner."""
    set_task_metadata_status(task_dir, "blocked")


def completion_workflow(task_dir: Path, explicit: str | None = None) -> str | None:
    """Resolve which profile owns the caller's completion policy boundary."""
    if explicit in {"standard", "dev-pipeline"}:
        return explicit
    try:
        metadata = json.loads(
            (task_dir / ".runner" / "runner.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    workflow = metadata.get("workflow") if isinstance(metadata, dict) else None
    return workflow if workflow in {"standard", "dev-pipeline"} else None


def _application_registration(task_dir: Path) -> tuple[str | None, str | None]:
    try:
        metadata = json.loads(
            (task_dir / ".runner" / "runner.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, None
    application = metadata.get("application") if isinstance(metadata, dict) else None
    spec = application.get("spec") if isinstance(application, dict) else None
    workflow = metadata.get("workflow") if isinstance(metadata, dict) else None
    return (spec if isinstance(spec, str) and spec else None,
            workflow if workflow in {"standard", "dev-pipeline"} else None)


def application_completion_ready(
    task_dir: Path, workflow: str | None = None, application: str | None = None
) -> tuple[bool, str]:
    recorded_application, recorded_workflow = _application_registration(task_dir)
    try:
        problems = load_application(application or recorded_application).completion_problems(
            CompletionRequestV1(task_dir=task_dir, workflow=workflow or recorded_workflow)
        )
    except (ApplicationAdapterError, OSError, TypeError, ValueError) as exc:
        return False, f"application completion policy cannot be evaluated: {exc}"
    if not isinstance(problems, list) or not all(
        isinstance(problem, str) and problem.strip() for problem in problems
    ):
        return False, "application completion policy must return a list of non-empty strings"
    if problems:
        return False, "application completion policy refused completion: " + "; ".join(problems)
    return True, ""


def completion_ready(
    task_dir: Path,
    workflow: str | None = None,
    application: str | None = None,
    deferred_live_evidence_ids: frozenset[str] = frozenset(),
    defer_task_status: bool = False,
    allow_historical_candidate: bool = False,
) -> tuple[bool, str]:
    """Decide whether durable task state authorizes successful completion.

    This composes existing semantic owners: ``tasks_index.py`` owns metadata and
    ``task_contract.py`` owns effective-contract, evidence, and policy-family
    semantics. Standard and dev-pipeline finalizers consume this function rather
    than maintaining profile-specific copies of the same decision.
    """
    contract_path = task_dir / "task_contract.json"
    if not contract_path.exists():
        return False, completion_failure(
            "task_contract.json is absent; no contract can authorize completion",
            gate="task_contract",
            owner=ENGINE_OWNED_COMPLETION_GATE,
        )

    status = task_status(task_dir)
    if status != "completed" and not (
        defer_task_status and status in {"in_progress", "blocked"}
    ):
        return False, completion_failure(
            f"task.md frontmatter status is {status!r}, not 'completed'",
            gate="task_status",
            owner=ENGINE_OWNED_COMPLETION_GATE,
        )

    plan_path = task_dir / "plan.md"
    if not plan_path.is_file():
        return False, completion_failure(
            "plan.md is absent; completion readiness cannot be established",
            gate="task_plan",
            owner=ENGINE_OWNED_COMPLETION_GATE,
        )
    try:
        plan_text = plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return False, completion_failure(
            f"plan.md is unreadable: {exc}",
            gate="task_plan",
            owner=ENGINE_OWNED_COMPLETION_GATE,
        )
    if "[pending]" in plan_text or "[in_progress]" in plan_text:
        return False, completion_failure(
            "plan.md still has unfinished steps",
            gate="task_plan",
            owner=ENGINE_OWNED_COMPLETION_GATE,
        )

    contract = load_task_contract(task_dir)
    verification_path = task_dir / "verification.md"
    verification = (
        verification_path.read_text(encoding="utf-8")
        if verification_path.exists()
        else ""
    )
    enforced_ids = {
        str(item.get("id", "")).strip()
        for item in contract.get("required_live_evidence", [])
        if isinstance(item, dict)
    }
    unknown_deferred = deferred_live_evidence_ids - enforced_ids
    if unknown_deferred:
        return False, completion_failure(
            "application completion preparation names evidence not enforced by the contract: "
            + ", ".join(sorted(unknown_deferred)),
            gate="application_evidence_contract",
            owner=ENGINE_OWNED_COMPLETION_GATE,
        )
    unsatisfied = unsatisfied_live_evidence(
        contract, verification, deferred_live_evidence_ids
    )
    if unsatisfied:
        unsatisfied_items = [
            item
            for item in enforced_live_evidence(contract)
            if str(item.get("id", "")).strip() not in deferred_live_evidence_ids
            and verification_gate_result(
                verification, str(item.get("id", "")).strip()
            )
            not in {"OK", "PASS", "PASSED"}
        ]
        evidence_owner = (
            ENGINE_OWNED_COMPLETION_GATE
            if unsatisfied_items
            and all(
                str(item.get("owner", "")).strip().lower()
                in ENGINE_EVIDENCE_OWNERS
                for item in unsatisfied_items
            )
            else USER_OR_EXTERNAL_COMPLETION_GATE
        )
        return False, completion_failure(
            f"required live evidence is not established: {'; '.join(unsatisfied)}",
            gate="required_live_evidence",
            owner=evidence_owner,
        )

    verdict_problems = unsatisfied_review_verdict(contract, task_dir)
    if verdict_problems:
        return False, completion_failure(
            f"required review verdict is not established: {'; '.join(verdict_problems)}",
            gate="review_verdict_record",
            owner=ENGINE_OWNED_COMPLETION_GATE,
        )

    # The reviewer bound before the author started has to have approved what is
    # here now. Without this, review admission would be a record of an intention:
    # the launcher would name an independent reviewer and the author could finish
    # and be accepted without that reviewer ever seeing the work. There is no
    # round budget in it -- an unapproved round refuses this acceptance and
    # authorizes the next round, which is the loop the task is supposed to have.
    review_blocker = independent_review_blocker(task_dir)
    if review_blocker is not None:
        return False, completion_failure(
            str(review_blocker["reason"]),
            gate="bound_independent_review",
            owner=ENGINE_OWNED_COMPLETION_GATE,
        )

    # The public dev-pipeline core enforces its own live-only scenario set before
    # it emits completion. A standard launch has no such core lifecycle, so the
    # same installation strategy is closed from the task's ordinary append-only
    # verification evidence instead of being treated as "no review required".
    binding = review_admission.bound_author_admission(task_dir)
    pair = binding.get("pair") if isinstance(binding, dict) else {}
    strategy = (
        binding.get("assurance_strategy") if isinstance(binding, dict) else None
    ) or (pair.get("assurance_strategy") if isinstance(pair, dict) else None)
    if strategy == review_admission.LIVE_ACCEPTANCE_ONLY and completion_workflow(
        task_dir, workflow
    ) == "standard":
        scenarios = pair.get("live_scenarios") if isinstance(pair, dict) else []
        missing = [
            str(scenario)
            for scenario in scenarios
            if verification_gate_result(verification, str(scenario))
            not in {"OK", "PASS", "PASSED"}
        ]
        if missing:
            return False, completion_failure(
                "assurance strategy `live_acceptance_only` requires passing "
                "verification for configured live scenarios: " + "; ".join(missing),
                gate="live_acceptance_scenarios",
                owner=USER_OR_EXTERNAL_COMPLETION_GATE,
            )

    # The delivered-candidate policy-family review is a dev-pipeline surface.
    # Standard tasks use their authored live-evidence and verdict gates and do
    # not manufacture another workflow's review directory.
    if completion_workflow(task_dir, workflow) != "standard":
        unestablished = unsatisfied_policy_families(
            contract,
            task_dir,
            allow_historical_candidate=allow_historical_candidate,
        )
        if unestablished:
            return False, completion_failure(
                "contract policy families are not established: "
                + "; ".join(unestablished),
                gate="policy_family_review",
                owner=ENGINE_OWNED_COMPLETION_GATE,
            )
    ready, reason = application_completion_ready(task_dir, workflow, application)
    if ready:
        return True, reason
    return False, completion_failure(
        reason,
        gate="application_completion_policy",
        owner=USER_OR_EXTERNAL_COMPLETION_GATE,
    )
