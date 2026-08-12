#!/usr/bin/env python3
"""Run a task through the standalone `dev-pipeline` CLI and project its events.

This adapter is deliberately transport-neutral. It turns a task directory into
an owner instruction, runs the public `dev-pipeline owner` command, validates
the neutral lifecycle events it emits, and projects them into the task's own
`status.json`, `trace.md`, and `progress.json`.

It carries no delivery policy: no recipient binding, message deduplication, or
at-most-once claim. Those belong to the registered application API v1, because
only that owner knows whether its transport can be replayed safely. The default
application is intentionally inert.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from dev_pipeline.events import validate_event

try:
    from .application_adapter import (
        ApplicationEventV1,
        CompletionPreparationRequestV1,
        TransportRecoveryV1,
        completion_preparation_evidence_ids,
        load_application,
    )
    from .task_completion import (
        TASKS_INDEX_PATH,
        complete_task_metadata,
        completion_ready,
        task_reference,
    )
    from .task_contract import (
        PASSING_EVIDENCE_RESULTS,
        enforced_live_evidence,
        enforced_policy_families,
        enforced_review_verdict,
        load_task_contract,
    )
    from .task_runner import completion_refusal, resolve_dev_pipeline_bin
    from . import review_admission, task_phases
except ImportError:
    from application_adapter import (
        ApplicationEventV1,
        CompletionPreparationRequestV1,
        TransportRecoveryV1,
        completion_preparation_evidence_ids,
        load_application,
    )
    from task_completion import (
        TASKS_INDEX_PATH,
        complete_task_metadata,
        completion_ready,
        task_reference,
    )
    from task_contract import (
        PASSING_EVIDENCE_RESULTS,
        enforced_live_evidence,
        enforced_policy_families,
        enforced_review_verdict,
        load_task_contract,
    )
    from task_runner import completion_refusal, resolve_dev_pipeline_bin
    import review_admission
    import task_phases


# Lifecycle kinds worth telling a human about. The template has no transport, so
# this only decides what reaches the (inert) notification seam.
NOTIFIABLE = frozenset(
    {
        "attempt_started",
        "checkpoint_completed",
        "increment_ready_for_review",
        "increment_completed",
        "review_started",
        "review_rework_required",
        "review_waiting",
        "review_refused",
        "run_waiting_for_quota",
        "blocked_on_user_decision",
        "attempt_failed",
        "attempt_completed",
    }
)

# Startup bookkeeping. These say the machinery began, not that anything was
# achieved, so they never become a `recent_outcome`.
BOOKKEEPING = frozenset(
    {"attempt_started", "run_started", "process_started", "native_session_discovered"}
)

# Events the public core emits first under a fresh run identity. Review,
# rework, live acceptance, and an escalated phase-claim failure are runs within
# the same attempt even though they are not owner-session `run_started` events.
# Keeping this list explicit preserves the refusal for arbitrary foreign-run
# events such as a stray `process_started`.
RUN_BOUNDARY_KINDS = frozenset(
    {
        "run_started",
        "review_started",
        "rework_started",
        "live_acceptance_waiting",
        "live_acceptance_completed",
        "blocked_on_user_decision",
    }
)

# Events that carry a reviewer's decision about the candidate. Each one is a
# review round of this task number; there is no limit on how many there may be.
REVIEW_DECISION_KINDS = frozenset({"review_approved", "review_rework_required"})

# Events that say the review machinery failed rather than that the candidate
# did. They belong to another task number; this task waits.
REVIEW_UNAVAILABLE_KINDS = frozenset({"review_waiting", "review_refused"})

TERMINAL_TASK_STATES = frozenset({"completed", "failed", "blocked"})

PROGRESS_SOURCE = "dev-pipeline-adapter"

STATE_DIR_NAME = "dev-pipeline"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    """Replace a file atomically and durably.

    A watcher may be killed at any moment. A half-written cursor would make the
    next run either replay work or skip it, so the rename is the commit point.
    """
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_jsonl(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def state_dir(task_dir: Path) -> Path:
    return task_dir / STATE_DIR_NAME


def core_state_dir(task_dir: Path, value: Path | None, default: str = "core") -> Path:
    """Keep the core's own lifecycle state inside the task that owns the run."""
    resolved = (value or state_dir(task_dir) / default).resolve()
    root = state_dir(task_dir).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(
            "Core lifecycle state must stay inside the task's dev-pipeline state directory"
        )
    return resolved


def prepare_owner_instruction(task_dir: Path) -> Path:
    """Build the exact instruction artifact handed to the dev-pipeline owner."""
    task_text = (task_dir / "task.md").read_text(encoding="utf-8")
    instruction_path = state_dir(task_dir) / "owner-instruction.md"
    instruction_path.parent.mkdir(parents=True, exist_ok=True)
    write_text(
        instruction_path,
        "# Dev-pipeline owner contract\n\n"
        "Treat the canonical task request below and every continuation as one ordered "
        "semantic contract. Keep the canonical task artifacts current while working.\n\n"
        f"Publish substantive live progress to `{task_dir / 'progress.json'}` using this "
        "version 1 contract:\n\n"
        "- Write `schema_version: 1`, a concrete `activity`, and `updated_at`.\n"
        "- Add `recent_outcome` after a meaningful checkpoint.\n"
        "- When measurable bounds are actually known, write non-negative `completed`, "
        "positive `total`, and a plural-capable `unit` together.\n"
        "- Never infer or invent a total. When no total is known, omit `completed`, "
        "`total`, and `unit` together and describe the concrete current operation and "
        "meaningful completed work instead.\n"
        "- Keep `status.json` and `trace.md` current as required by the task, but do not "
        "use runner startup bookkeeping as a meaningful outcome.\n\n"
        f"Follow `{task_dir / 'task_contract.json'}` when present and "
        f"`{task_dir / 'plan.md'}` for the execution plan.\n\n"
        f"{completion_contract_instruction(task_dir)}\n\n"
        "## Canonical task request\n\n"
        f"{task_text.rstrip()}\n",
    )
    return instruction_path


def completion_contract_instruction(task_dir: Path) -> str:
    """Tell the owner how to satisfy the exact shared completion gate."""
    contract = load_task_contract(task_dir)
    reference = task_reference(task_dir)
    lines = [
        "## Closing this task",
        "",
        "The attempt is accepted only when all durable gates pass:",
        f"1. Set task frontmatter through `{TASKS_INDEX_PATH} set-status {reference} completed`.",
        f"2. Remove every `[pending]` and `[in_progress]` marker from `{task_dir / 'plan.md'}`.",
    ]
    gates = [str(item["id"]) for item in enforced_live_evidence(contract)]
    if gates:
        allowed = "|".join(sorted(PASSING_EVIDENCE_RESULTS))
        lines.append(
            "3. Record a passing `- Result:` (`"
            + allowed
            + "`) in `verification.md` for: "
            + ", ".join(gates)
            + ". The last section for a repeated gate id wins."
        )
    verdict = enforced_review_verdict(contract)
    if verdict:
        lines.append(
            f"4. `{verdict['path']}` must contain exactly one standalone `Verdict: "
            + "|".join(verdict["allowed"])
            + "` line written by the review owner."
        )
    families = enforced_policy_families(contract)
    if families:
        lines.append(
            "Run the bounded contract-policy review after the candidate is final: "
            f"`{Path(__file__).with_name('task_runner.py')} review-candidate {task_dir} "
            "--repo <target-repository>`. It must approve the effective contract and "
            "exact committed candidate for: " + ", ".join(families) + "."
        )
    lines.append(
        "If completion cannot be established, set the task to `blocked`, record the reason, "
        "and leave remaining plan work explicit."
    )
    return "\n".join(lines)


def contract_completion_ready(task_dir: Path) -> tuple[bool, str]:
    """Reject a completion the task's own durable artifacts contradict.

    The owner process exiting cleanly says the process ended, not that the task
    was finished. Where the task states its own completion conditions, those
    decide.
    """
    return completion_ready(task_dir)


def task_status_value(task_text: str) -> str:
    """Read the value under the task's `## Status` heading."""
    lines = task_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() != "## status":
            continue
        for candidate in lines[index + 1:]:
            if candidate.strip():
                return candidate.strip().lower()
        return ""
    return ""


def status_projection(event: dict, task_dir: Path) -> tuple[str, str]:
    """Map a lifecycle event to the task's own status vocabulary."""
    kind = event["kind"]
    payload = event["payload"]
    if kind == "attempt_started":
        return "running", "Dev-pipeline owner attempt started"
    if kind == "checkpoint_completed":
        return (
            "running",
            f"Checkpoint completed: {payload['checkpoint']}; next: {payload['next_step']}",
        )
    if kind == "increment_ready_for_review":
        return "running", f"Increment ready for review: {payload['increment']}"
    if kind == "increment_completed":
        return (
            "running",
            f"Increment completed: {payload['increment']}; next: {payload['next_step']}",
        )
    if kind == "review_started":
        return (
            "running",
            f"Independent review started by {payload['review_provider']} "
            f"({payload['strategy']})",
        )
    if kind == "review_approved":
        return "running", f"Review approved by {payload['review_provider']}"
    if kind == "review_rework_required":
        return "running", f"Review requires rework: {payload['review_provider']} did not approve"
    if kind in {"review_waiting", "review_refused"}:
        # Neither is a verdict this task may proceed on. The configured review
        # did not produce a usable one, and substituting a different reviewer or
        # counting the work as reviewed is exactly what must not happen here.
        return "blocked", f"Review unavailable ({kind}): {payload['reason']}"
    if kind == "live_acceptance_waiting":
        return "running", f"Waiting on live acceptance: {payload['reason']}"
    if kind == "live_acceptance_completed":
        return "running", f"Live acceptance completed ({payload['strategy']})"
    if kind == "run_waiting_for_quota":
        return (
            "waiting",
            f"Claude usage limit exhausted; native resume waits until {payload['resets_at']}",
        )
    if kind == "blocked_on_user_decision":
        return "blocked", f"Waiting for user decision: {payload['question']}"
    if kind == "run_failed":
        return "running", f"Dev-pipeline run failed: {payload['reason']}"
    if kind == "attempt_failed":
        return "failed", f"Dev-pipeline failed: {payload['reason']}"
    if kind == "attempt_completed":
        ready, reason = contract_completion_ready(task_dir)
        if ready:
            return "completed", "Dev-pipeline owner attempt completed"
        return "blocked", completion_refusal(task_dir, reason)["summary"]
    return "running", f"Dev-pipeline event: {kind}"


def progress_projection(event: dict, current_step: str) -> tuple[str, str | None]:
    """Describe the concrete current operation, and any real outcome behind it.

    Counts are never derived here. The lifecycle vocabulary carries no notion of
    how many checkpoints or increments a task has, and a total nobody knows is
    worse than no total because it reads as a measurement.
    """
    kind = event["kind"]
    payload = event["payload"]
    activities = {
        "attempt_started": "Dev-pipeline owner attempt starting",
        "process_started": "Owner runtime process running",
        "native_session_discovered": (
            "Owner runtime session established; owner work is active"
        ),
        "run_completed": "Owner run finished",
    }
    if kind == "run_started":
        activity = f"Owner run starting ({payload['run_operation']})"
    elif kind == "native_resume_unavailable":
        activity = f"Owner session cannot be resumed: {payload['reason']}"
    elif kind == "checkpoint_completed":
        activity = f"Working past checkpoint {payload['checkpoint']}; next: {payload['next_step']}"
    elif kind == "increment_ready_for_review":
        activity = f"Increment {payload['increment']} is waiting for review"
    elif kind == "increment_completed":
        activity = f"Working past increment {payload['increment']}; next: {payload['next_step']}"
    elif kind == "review_started":
        activity = f"{payload['review_provider']} is reviewing the bound candidate"
    elif kind == "review_approved":
        activity = f"Review approved by {payload['review_provider']}"
    elif kind == "review_rework_required":
        activity = "Reworking the candidate after review"
    elif kind in {"review_waiting", "review_refused"}:
        activity = f"Review could not be obtained: {payload['reason']}"
    elif kind == "live_acceptance_waiting":
        activity = f"Waiting on live acceptance: {payload['reason']}"
    elif kind == "live_acceptance_completed":
        activity = f"Live acceptance completed ({payload['strategy']})"
    elif kind == "blocked_on_user_decision":
        activity = f"Waiting for a user decision: {payload['question']}"
    elif kind in {"run_failed", "attempt_failed"}:
        activity = f"Owner work stopped: {payload['reason']}"
    elif kind == "attempt_completed":
        activity = current_step
    else:
        activity = activities.get(kind, f"Dev-pipeline event: {kind}")

    if kind in BOOKKEEPING:
        return activity, None
    outcomes = {
        "checkpoint_completed": lambda: f"Checkpoint {payload['checkpoint']} completed",
        "increment_ready_for_review": lambda: (
            f"Increment {payload['increment']} reached review"
        ),
        "increment_completed": lambda: f"Increment {payload['increment']} completed",
        "review_approved": lambda: f"Review approved by {payload['review_provider']}",
        "review_rework_required": lambda: (
            f"Review returned the candidate for rework ({payload['review_provider']})"
        ),
        "review_waiting": lambda: f"Review unavailable: {payload['reason']}",
        "review_refused": lambda: f"Review refused: {payload['reason']}",
        "live_acceptance_completed": lambda: (
            f"Live acceptance completed ({payload['strategy']})"
        ),
        "blocked_on_user_decision": lambda: f"Blocked on: {payload['question']}",
        "run_failed": lambda: f"Run failed: {payload['reason']}",
        "attempt_failed": lambda: f"Attempt failed: {payload['reason']}",
        "run_completed": lambda: f"Owner run exited with code {payload['exit_code']}",
        "attempt_completed": lambda: current_step,
        "native_resume_unavailable": lambda: (
            f"Native session unavailable: {payload['reason']}"
        ),
    }
    outcome = outcomes.get(kind)
    return activity, outcome() if outcome else None


class TaskArtifactProjector:
    """Project validated lifecycle events into one task's durable artifacts.

    Ordering is enforced rather than assumed: an event that skips a sequence
    number, belongs to a different task, or belongs to a run this projector is
    not following is a refusal, not a gap to paper over.
    """

    def __init__(
        self,
        task_dir: Path,
        application: str | None = None,
        destination: str | None = None,
    ) -> None:
        self.task_dir = task_dir.resolve()
        self.task_ref = self.task_dir.name
        self.state_dir = state_dir(self.task_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cursor_path = self.state_dir / "adapter-cursor.json"
        self.event_path = self.state_dir / "projected-events.jsonl"
        self.projection_cursor_path = self.state_dir / "projection-cursor.json"
        self.application_spec = application
        self.application = load_application(application)
        self.destination = destination
        self.recover_projection()
        cursor = read_json(self.cursor_path)
        self.application.recover_transport(
            TransportRecoveryV1(
                task_dir=self.task_dir,
                workflow="dev-pipeline",
                event_log_path=self.event_path,
                destination=self.destination,
                active_attempt_id=cursor.get("attempt_id"),
            )
        )

    def recover_projection(self) -> None:
        """Replay any event that was durably recorded but not yet projected."""
        projection = read_json(self.projection_cursor_path)
        projected = set(projection.get("projected_event_ids", []))
        if not self.event_path.exists():
            return
        with self.event_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = validate_event(
                        json.loads(line), allow_legacy_unclassified_resume=True
                    )
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ValueError(
                        f"Projected lifecycle event log is invalid at line {line_number}"
                    ) from exc
                if event["event_id"] in projected:
                    continue
                preparation_refusal = self.prepare_completion(event)
                self.project(event, preparation_refusal=preparation_refusal)
                projected.add(event["event_id"])
                write_json(
                    self.projection_cursor_path,
                    {
                        "schema_version": 1,
                        "last_event_id": event["event_id"],
                        "last_sequence": event["sequence"],
                        "attempt_id": event["attempt_id"],
                        "run_id": event["run_id"],
                        "projected_event_ids": sorted(projected),
                        "updated_at": utc_now(),
                    },
                )

    def prepare_completion(self, event: dict) -> str | None:
        """Let an installation establish declared terminal evidence first.

        The hook is reached only when the ordinary completion predicate would
        pass with exactly the application's declared evidence and terminal task
        status deferred.  Successful preparation is followed by the canonical
        metadata transition, and both durable effects are checked by the full
        predicate during normal projection.  Failed preparation therefore
        leaves metadata non-complete and remains blocked.
        """
        if event["kind"] != "attempt_completed":
            return None
        declared_ids = completion_preparation_evidence_ids(self.application)
        if not declared_ids:
            return None
        contract = load_task_contract(self.task_dir)
        enforced_ids = {
            str(item["id"]).strip()
            for item in enforced_live_evidence(contract)
            if isinstance(item.get("id"), str) and str(item["id"]).strip()
        }
        evidence_ids = tuple(
            evidence_id for evidence_id in declared_ids if evidence_id in enforced_ids
        )
        if not evidence_ids:
            return None
        ready, reason = completion_ready(
            self.task_dir,
            workflow="dev-pipeline",
            application=self.application_spec,
            deferred_live_evidence_ids=frozenset(evidence_ids),
            defer_task_status=True,
        )
        if not ready:
            return reason
        try:
            result = self.application.prepare_completion(
                CompletionPreparationRequestV1(
                    task_dir=self.task_dir,
                    workflow="dev-pipeline",
                    event_id=event["event_id"],
                    destination=self.destination,
                    evidence_ids=evidence_ids,
                )
            )
            if result.delivered:
                complete_task_metadata(self.task_dir)
                self.append_trace(
                    f"Prepared completion evidence before finalization: {result.detail}"
                )
                return None
            return "application completion preparation refused: " + result.detail
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            self.append_trace(
                "Automatic completion finalization failed after preparation: " + detail
            )
            return "automatic completion finalization failed: " + detail

    def consume(self, raw_event: dict) -> bool:
        """Record and project one event. Returns False for a repeat."""
        event = validate_event(raw_event)
        if event["task_ref"] != self.task_ref:
            raise ValueError(
                "Lifecycle event task_ref does not match the canonical task directory"
            )
        cursor = read_json(self.cursor_path)
        starts_new_attempt = bool(
            cursor
            and event["kind"] == "attempt_started"
            and event["sequence"] == 1
            and (
                cursor.get("attempt_id") != event["attempt_id"]
                or cursor.get("run_id") != event["run_id"]
            )
        )
        starts_new_run = bool(
            cursor
            and event["kind"] in RUN_BOUNDARY_KINDS
            and event["attempt_id"] == cursor.get("attempt_id")
            and event["run_id"] != cursor.get("run_id")
            and event["sequence"] == cursor.get("last_sequence", 0) + 1
        )
        consumed = set() if starts_new_attempt else set(cursor.get("consumed_event_ids", []))
        if cursor and not starts_new_attempt and not starts_new_run:
            if (
                cursor.get("attempt_id") != event["attempt_id"]
                or cursor.get("run_id") != event["run_id"]
            ):
                raise ValueError(
                    "Lifecycle event identity differs from the active adapter cursor"
                )
            if event["event_id"] in consumed:
                return False
            if event["sequence"] != cursor["last_sequence"] + 1:
                raise ValueError("Lifecycle event sequence is stale or out of order")

        consumed.add(event["event_id"])
        append_jsonl(self.event_path, event)
        write_json(
            self.cursor_path,
            {
                "schema_version": 1,
                "attempt_id": event["attempt_id"],
                "run_id": event["run_id"],
                "last_sequence": event["sequence"],
                "consumed_event_ids": sorted(consumed),
                "updated_at": utc_now(),
            },
        )
        self.recover_projection()
        self.offer_to_delivery(event)
        return True

    def offer_to_delivery(self, event: dict) -> None:
        """Hand a notable event to the delivery seam.

        The default application reports that no transport is configured, so in
        this template nothing is delivered and nothing is recorded. A
        registered application owns recipient binding and replay rules.
        """
        if event["kind"] not in NOTIFIABLE:
            return
        status = read_json(self.task_dir / "status.json").get("current_step", event["kind"])
        result = self.application.deliver_event(
            ApplicationEventV1(
                task_dir=self.task_dir,
                kind=event["kind"],
                workflow="dev-pipeline",
                payload={
                    "status": str(status),
                    "event": event,
                    "artifact_paths": ["status.json", "trace.md"],
                },
                destination=self.destination,
                event_id=event.get("event_id"),
            )
        )
        if result.delivered:
            self.append_trace(
                f"Delivered dev-pipeline `{event['kind']}` notification: {result.detail}"
            )

    def project(self, event: dict, *, preparation_refusal: str | None = None) -> None:
        state, step = status_projection(event, self.task_dir)
        if event["kind"] == "attempt_completed" and preparation_refusal is not None:
            refusal = completion_refusal(self.task_dir, preparation_refusal)
            state, step = "blocked", refusal["summary"]
        warning = self.record_review_round(event) or self.record_review_obligation(event)
        if warning:
            step = f"{step}; {warning}"
        self.project_phase(event, state)
        self.project_status(
            event, state, step, preparation_refusal=preparation_refusal
        )
        self.project_trace(event, step)
        self.project_progress(event, step, warning=warning)

    def record_review_round(self, event: dict) -> str | None:
        """Log this review round and say whether it repeated a known finding.

        The round is recorded for the user's benefit, not as a budget: nothing
        here can stop rework, and a repeat only adds a sentence to what the task
        is already reporting. A finding that appears for the first time -- the
        honest deeper problem a fix exposes -- says nothing about quality and
        produces no warning.
        """
        if event["kind"] not in REVIEW_DECISION_KINDS:
            return None
        payload = event["payload"]
        decision: dict = {}
        artifact = payload.get("decision_artifact")
        if artifact:
            try:
                candidate = read_json(Path(str(artifact))).get("decision")
            except (OSError, ValueError):
                # An unreadable decision artifact is the reviewer's problem to
                # report; it must not stop the task's own projection.
                candidate = None
            if isinstance(candidate, dict):
                decision = candidate
        if not decision and isinstance(payload.get("decision"), str):
            decision = {"decision": payload["decision"]}
        entry = review_admission.record_review_round(
            self.task_dir,
            event_id=event["event_id"],
            decision=decision,
            review_provider=payload.get("review_provider"),
            recorded_at=event["timestamp"],
        )
        return entry.get("warning")

    def record_review_obligation(self, event: dict) -> str | None:
        """Keep a broken reviewer from becoming this task's subject.

        A review that could not be obtained is a defect in the review machinery.
        It gets its own task number, allocated by `task-creator`; recording the
        obligation here is what makes that durable rather than prose. This task
        keeps the work it has and waits -- the phase is already `blocked`, and
        nothing about the candidate is rewritten to accommodate the outage.
        """
        if event["kind"] not in REVIEW_UNAVAILABLE_KINDS:
            return None
        entry = review_admission.record_infrastructure_obligation(
            self.task_dir,
            event_id=event["event_id"],
            source=f"dev-pipeline:{event['kind']}",
            reason=str(event["payload"].get("reason", "")),
            reference=event["payload"].get("review_provider"),
            recorded_at=event["timestamp"],
        )
        return entry["statement"]

    def project_phase(self, event: dict, state: str) -> None:
        """Record which phase of this one task the event belongs to.

        The neutral event says what the machinery did; the phase says what the
        goal is doing. A review and the rework it asks for are phases of this
        task directory, which is the whole reason neither of them needs a task
        number of its own.

        A terminal state wins over the event's own phase, because
        `attempt_completed` is a completion only if the task's durable gates say
        so, and that decision has already been made by the time this runs.
        """
        phase = task_phases.phase_for_state(state) or task_phases.phase_for_event(
            event, task_phases.current_phase(self.task_dir)
        )
        if not phase:
            return
        task_phases.record_phase(
            self.task_dir,
            phase,
            cause={
                "source": "dev-pipeline",
                "kind": event["kind"],
                "event_id": event["event_id"],
            },
            entered_at=event["timestamp"],
        )

    def project_status(
        self,
        event: dict,
        state: str,
        step: str,
        *,
        preparation_refusal: str | None = None,
    ) -> None:
        status_path = self.task_dir / "status.json"
        status = read_json(status_path)
        status.update(
            {
                "state": state,
                "phase": task_phases.current_phase(self.task_dir),
                "current_step": step,
                "updated_at": event["timestamp"],
                "dev_pipeline": {
                    "attempt_id": event["attempt_id"],
                    "run_id": event["run_id"],
                    "last_event_id": event["event_id"],
                    "last_sequence": event["sequence"],
                },
            }
        )
        if event["kind"] == "attempt_completed" and state == "blocked":
            if preparation_refusal is None:
                _ready, reason = contract_completion_ready(self.task_dir)
            else:
                reason = preparation_refusal
            refusal = completion_refusal(self.task_dir, reason)
            if preparation_refusal is not None:
                refusal["automatic_finalization"] = True
            status["completion_refusal"] = refusal
        else:
            status.pop("completion_refusal", None)
        write_json(status_path, status)

    def project_trace(self, event: dict, step: str) -> None:
        trace_path = self.task_dir / "trace.md"
        marker = f"<!-- dev-pipeline-event:{event['event_id']} -->"
        trace = trace_path.read_text(encoding="utf-8") if trace_path.exists() else ""
        if marker in trace:
            return
        if not trace:
            trace_path.write_text("# Trace\n\n", encoding="utf-8")
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"- {event['timestamp']} dev-pipeline `{event['kind']}`: {step} {marker}\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    def project_progress(
        self, event: dict, step: str, *, warning: str | None = None
    ) -> None:
        """Publish progress from lifecycle events without overwriting the owner.

        The owner agent knows far more about its own work than the lifecycle
        vocabulary does, so anything it publishes wins. This fills the gap
        before and between those publications, and marks its own writes so it
        can tell them apart later.
        """
        progress_path = self.task_dir / "progress.json"
        existing: dict = {}
        if progress_path.exists():
            try:
                candidate = json.loads(progress_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                candidate = None
            if isinstance(candidate, dict):
                if (
                    candidate.get("schema_version") == 1
                    and candidate.get("source") != PROGRESS_SOURCE
                ):
                    return
                existing = candidate

        activity, outcome = progress_projection(event, step)
        if warning:
            outcome = f"{outcome}; {warning}" if outcome else warning
        progress = {
            "schema_version": 1,
            "source": PROGRESS_SOURCE,
            "activity": activity,
            "updated_at": event["timestamp"],
        }
        # Carry the last real outcome forward: bookkeeping events must not erase
        # the most recent thing that was actually achieved.
        carried = outcome or existing.get("recent_outcome")
        if isinstance(carried, str) and carried.strip():
            progress["recent_outcome"] = carried.strip()
        write_json(progress_path, progress)

    def append_trace(self, message: str) -> None:
        trace_path = self.task_dir / "trace.md"
        if not trace_path.exists():
            trace_path.write_text("# Trace\n\n", encoding="utf-8")
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {utc_now()} {message}\n")


def consume_lines(projector: TaskArtifactProjector, lines: Iterable[str]) -> None:
    for line in lines:
        if line.strip():
            projector.consume(json.loads(line))


def recover_core_ledger(projector: TaskArtifactProjector, core_state: Path) -> None:
    """Project authoritative core events missed by an interrupted adapter."""
    ledger = core_state / "events.jsonl"
    if not ledger.exists():
        return
    with ledger.open(encoding="utf-8") as handle:
        consume_lines(projector, handle)


def build_core_command(args: argparse.Namespace, task_dir: Path, instruction: Path) -> list[str]:
    core_state = core_state_dir(task_dir, args.state_dir)
    command = [
        args.dev_pipeline_bin,
        "owner",
        args.operation,
        "--task-ref",
        task_dir.name,
        "--instruction-file",
        str(instruction),
        "--repo",
        str(args.repo.resolve()),
        "--state-dir",
        str(core_state),
        "--owner-runtime",
        args.owner_runtime,
        "--sandbox",
        args.sandbox,
    ]
    if args.operation in {"start", "retry"} and (task_dir / "task_contract.json").exists():
        command.extend(["--artifact", str(task_dir / "task_contract.json")])
    if args.operation == "retry":
        command.extend(
            ["--previous-state-dir", str(core_state_dir(task_dir, args.previous_state_dir))]
        )
        if args.retry_reason:
            command.extend(["--retry-reason", args.retry_reason])
    if args.model:
        command.extend(["--model", args.model])
    if args.assurance_config:
        command.extend(["--assurance-config", str(args.assurance_config.resolve())])
    if args.review_packet:
        command.extend(["--review-packet", str(args.review_packet.resolve())])
    return command


def run(args: argparse.Namespace) -> int:
    task_dir = args.task_dir.resolve()
    instruction = prepare_owner_instruction(task_dir)
    core_state = core_state_dir(task_dir, args.state_dir)
    command = build_core_command(args, task_dir, instruction)
    projector = TaskArtifactProjector(
        task_dir, application=args.application, destination=args.destination
    )
    recover_core_ledger(projector, core_state)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None
    consume_lines(projector, process.stdout)
    return process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--repo", required=True, type=Path, help="Target repository for the run.")
    parser.add_argument("--dev-pipeline-bin")
    parser.add_argument("--operation", choices=("start", "resume", "retry"), default="start")
    parser.add_argument(
        "--state-dir", type=Path, help="Task-local core lifecycle state directory."
    )
    parser.add_argument(
        "--previous-state-dir", type=Path, help="Prior task-local lifecycle state for a retry."
    )
    parser.add_argument(
        "--retry-reason", choices=("native_unavailable", "intentional_replacement")
    )
    parser.add_argument("--model")
    parser.add_argument("--assurance-config", type=Path)
    parser.add_argument("--review-packet", type=Path)
    parser.add_argument("--application", help="Versioned installation adapter module:attribute.")
    parser.add_argument("--destination", help="Opaque installation-owned delivery destination.")
    parser.add_argument(
        "--owner-runtime",
        choices=("codex", "claude", "cursor"),
        default="codex",
        help="CLI runtime that owns the pipeline session.",
    )
    parser.add_argument(
        "--sandbox",
        choices=("read-only", "workspace-write", "danger-full-access"),
        default="workspace-write",
    )
    args = parser.parse_args(argv)
    args.dev_pipeline_bin = resolve_dev_pipeline_bin(args.dev_pipeline_bin)
    if args.operation == "retry" and args.state_dir is None:
        parser.error("Retry requires an explicit new --state-dir")
    if args.operation != "retry" and (args.previous_state_dir or args.retry_reason):
        parser.error("Retry state and reason options require --operation retry")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
