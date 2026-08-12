#!/usr/bin/env python3
"""Task phases: one user goal, one task number, review and rework as phases.

A user goal used to need a second task number the moment it reached review, and
a third if the review asked for changes. Nothing about the goal changed; only
the machinery did, and the machinery's shape leaked into the way the work was
named. Externally that reads as three unrelated pieces of work.

A phase is the answer to "what is this one task doing right now". It belongs to
the task, so it is recorded in the task directory and nowhere else, and the
sequence `implementation -> review -> rework -> review -> completed` lives in a
single `phases.json` under a single number.

The phase vocabulary is this project's, not a provider's. `dev-pipeline` owns
the neutral lifecycle events and validates them; this module decides what those
events mean for a task, and derives the same phases for a `standard` run that
emits no events at all. That is why both profiles present one vocabulary.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PHASES_FILE = "phases.json"

PLANNED = "planned"
IMPLEMENTATION = "implementation"
REVIEW = "review"
REWORK = "rework"
LIVE_ACCEPTANCE = "live_acceptance"
BLOCKED = "blocked"
COMPLETED = "completed"
FAILED = "failed"

PHASES = (
    PLANNED,
    IMPLEMENTATION,
    REVIEW,
    REWORK,
    LIVE_ACCEPTANCE,
    BLOCKED,
    COMPLETED,
    FAILED,
)

TERMINAL_PHASES = frozenset({COMPLETED, FAILED})

# Which phase a neutral lifecycle event puts the task in. Kinds absent from this
# table leave the phase alone: a run failing, a quota wait or a session
# discovery says something about the machinery, not about which stage of the
# goal the task is at.
#
# `review_approved` deliberately stays in `review`. A phase ends when the next
# one begins, and an approval is the review phase's outcome, not a new stage.
#
# The kinds in `WORK_EVENT_KINDS` are not in this table because they do not name
# a stage on their own. They say the owner is building something, and what that
# is depends on where the task already is: the same checkpoint event means
# implementation the first time around and rework after a review sent the
# candidate back. Reading it as implementation both times would erase the rework
# phase exactly when it is happening.
WORK_EVENT_KINDS = frozenset(
    {"attempt_started", "checkpoint_completed", "increment_completed"}
)

EVENT_PHASES = {
    "increment_ready_for_review": REVIEW,
    "assurance_pending": REVIEW,
    "assurance_bound": REVIEW,
    "review_started": REVIEW,
    "review_process_started": REVIEW,
    "review_session_discovered": REVIEW,
    "review_approved": REVIEW,
    "review_rework_required": REWORK,
    "live_acceptance_waiting": LIVE_ACCEPTANCE,
    "live_acceptance_completed": LIVE_ACCEPTANCE,
    # Neither of these is a rework instruction. `review_waiting` means the
    # review could not be obtained — the reviewer was unavailable, its preflight
    # failed, or it asked for an external decision — and `review_refused` means
    # the review that ran cannot be trusted. Both stop the work for a human
    # rather than sending it back around the loop, so neither may read as a
    # stage the task is making progress through.
    "review_waiting": BLOCKED,
    "review_refused": BLOCKED,
    "blocked_on_user_decision": BLOCKED,
    "attempt_failed": FAILED,
}

# Terminal task states, as `status.json` spells them, mapped to a phase. The
# adapter and the standard runner both already decide these; this only gives the
# decision a phase name.
STATE_PHASES = {
    "completed": COMPLETED,
    "failed": FAILED,
    "blocked": BLOCKED,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def phases_path(task_dir: Path) -> Path:
    return task_dir / PHASES_FILE


def read_phases(task_dir: Path) -> dict[str, Any]:
    """Read the durable phase record, treating anything unreadable as absent."""
    try:
        value = json.loads(phases_path(task_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def current_phase(task_dir: Path) -> str:
    """The phase a task is in, or `planned` before anything has run."""
    phase = read_phases(task_dir).get("phase")
    return phase if isinstance(phase, str) and phase in PHASES else PLANNED


def phase_history(task_dir: Path) -> list[dict[str, Any]]:
    history = read_phases(task_dir).get("history")
    return [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []


def phase_sequence(task_dir: Path) -> list[str]:
    """The phases this one task went through, in order."""
    return [str(item["phase"]) for item in phase_history(task_dir) if "phase" in item]


# The phases in which the author is changing the work. A review approves what
# these produced, so an entry into one of them after an approval means the
# approved thing is not what is there now.
AUTHOR_WORK_PHASES = frozenset({IMPLEMENTATION, REWORK})


def author_work_entries(task_dir: Path) -> list[dict[str, Any]]:
    """Every recorded entry into a phase where the author changed the work.

    This vocabulary belongs here, so the review gate asks for the entries rather
    than deciding for itself which phase names mean "the author worked".
    """
    return [
        item
        for item in phase_history(task_dir)
        if str(item.get("phase")) in AUTHOR_WORK_PHASES
    ]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace the record atomically and durably.

    A watcher can be killed between two phases. A half-written record would make
    the task's own history unreadable, and the history is the thing that shows a
    review and a rework belong to this task rather than to two others.
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


def record_phase(
    task_dir: Path,
    phase: str,
    *,
    cause: dict[str, Any] | None = None,
    entered_at: str | None = None,
) -> dict[str, Any]:
    """Enter a phase, appending to this task's own history.

    Re-entering the phase the task is already in appends nothing, so a run that
    emits twenty implementation events does not produce twenty entries. Entering
    a phase the task held earlier *does* append: `review -> rework -> review` is
    three entries, because the second review is a second review.
    """
    if phase not in PHASES:
        raise ValueError(f"Unknown task phase: {phase}")
    record = read_phases(task_dir)
    history = [item for item in record.get("history", []) if isinstance(item, dict)]
    timestamp = entered_at or utc_now()
    if record.get("phase") != phase:
        entry: dict[str, Any] = {"phase": phase, "entered_at": timestamp}
        if cause:
            entry["cause"] = cause
        history.append(entry)
    payload = {
        "schema_version": 1,
        "task_ref": task_dir.name,
        "phase": phase,
        "entered_at": history[-1]["entered_at"] if history else timestamp,
        "history": history,
        "updated_at": timestamp,
    }
    _write_json(phases_path(task_dir), payload)
    return payload


def phase_for_event(event: dict[str, Any], current: str = PLANNED) -> str | None:
    """The phase a neutral lifecycle event puts the task in, if any.

    `current` is where the task already is, because a work event's meaning
    depends on it: building after a review asked for changes is rework, and the
    core emits the same checkpoint event for both.

    Returning None means "this event says nothing about the stage of the goal".
    An unknown kind lands here too: a newer core may emit a kind this project
    has never heard of, and inventing a phase for it would be worse than leaving
    the task in the phase it is demonstrably still in.
    """
    kind = event.get("kind")
    if not isinstance(kind, str):
        return None
    if kind in WORK_EVENT_KINDS:
        return REWORK if current == REWORK else IMPLEMENTATION
    return EVENT_PHASES.get(kind)


def phase_for_state(state: str) -> str | None:
    """The phase a terminal `status.json` state corresponds to, if any."""
    return STATE_PHASES.get(state)


def phase_for_standard_start(task_dir: Path, *, require_review_verdict: bool) -> str:
    """The phase a `standard` run is entering, so both profiles agree.

    A standard run publishes no lifecycle events, so the phase comes from what
    the run was asked to do and from what this task has already been through.
    A review is a review under either profile; and work that starts after a
    review is rework, not a fresh implementation, which is exactly the
    distinction that used to require a new task number.
    """
    if require_review_verdict:
        return REVIEW
    return REWORK if REVIEW in phase_sequence(task_dir) else IMPLEMENTATION
