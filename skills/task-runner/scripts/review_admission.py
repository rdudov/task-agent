#!/usr/bin/env python3
"""Review admission: who checks this work, decided before the author starts.

Independent review used to be something a launch could simply not have. The
runner would start an author, the author would finish, and only then would
anyone notice that no second family was ever going to look at the result -- or
that the work had quietly been treated as too small to review because a human
called it trivial in prose. Both failures cost the whole attempt.

This module answers two questions at launch time, from observable inputs:

1. Does this launch do something a review has to cover? The answer comes from
   what the launch is actually granted and gated on -- a writable repository,
   a delivery workflow, a gated contract, registered deliverables -- never from
   an adjective in prose. The narrow read-only exception exists, but it has to
   be declared in the structured contract *and* survive those observations; a
   declaration contradicted by a write grant is not an exception, it is a
   mislabel, and the launch stays reviewable.

2. Can an independent reviewer be bound to it? A reviewer is independent when
   it is a different provider family from the author and its CLI is actually
   installed here. The author is never its own reviewer, no matter what is
   unavailable.

Material work with no bindable reviewer refuses before the author starts, which
is the only moment where refusing is still cheap.

Binding a reviewer at launch is worth nothing on its own, so the binding is
carried through to the two places that can still let the work out unreviewed:

- the admitted pair governs the run. A dev-pipeline launch has to hand the same
  reviewer to the assurance the core will actually run (`assurance_binding`),
  and a review launch into this number has to be the family that was bound
  (`evaluate(review_launch=True)`).
- the admitted pair governs acceptance. `independent_review_status` answers, from
  this task's own append-only ledgers, whether the work as it now stands has an
  approval from that family; the shared completion owner refuses a standard
  completion that does not.

What this module deliberately does not have is a limit. Review and rework are
phases of one task (`task_phases`), and `record_review_round` counts rounds
without ever capping them: the round number is for telling the user that the
same demonstrated finding came back, not for deciding when to stop fixing it.
An unapproved round therefore blocks acceptance and nothing else -- the next
round is always allowed, and it is the approval, never the count, that ends the
loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1

ADMISSION_RECORD = ".runner/review-admission.json"
ROUNDS_LEDGER = "reviews/rounds.jsonl"

# Every admission this task number ever made, oldest first. The single-record
# file above is the current launch; a review launch overwrites it, and the
# author binding it replaced is exactly what acceptance has to check against.
ADMISSIONS_LEDGER = "reviews/admissions.jsonl"

# Provider families. Two runners of the same family are the same reviewer for
# admission purposes: independence is about the provider that judges the work,
# not about which process invoked it.
RUNNER_FAMILIES = {"codex": "Codex", "claude": "Claude", "agent": "Cursor"}

# What has to be on PATH for a family to be a reviewer we can actually bind.
RUNNER_EXECUTABLES = {"codex": "codex", "claude": "claude", "agent": "cursor-agent"}

# Preference order when nobody named a reviewer. Deterministic so the recorded
# decision is reproducible from the same host.
REVIEWER_PREFERENCE = ("codex", "claude", "agent")

MATERIAL = "material"
READ_ONLY_LOOKUP = "read_only_lookup"

# A launch that is itself the review of this number's work. It is not a third
# kind of exception: it is the other half of the pair, and what it is checked
# for is being the family that was bound rather than having a reviewer of its
# own.
REVIEW = "review"

# What a reviewer's decision has to say for the work to be accepted, and what it
# says when it does not. Both vocabularies exist because the decision reaches
# this module from two directions: a dev-pipeline decision artifact and a
# `Verdict:` line in a reviewer-authored `findings.md`.
APPROVED_DECISIONS = frozenset({"approved", "approve", "accept", "accepted"})
REWORK_DECISIONS = frozenset(
    {"rework", "rework_required", "changes_requested", "rejected"}
)

OBLIGATIONS_LEDGER = "reviews/infrastructure-obligations.jsonl"

# Pairing outcomes that say the review machinery is missing rather than that the
# caller asked for something incoherent. A host with no second provider family
# installed, or a named reviewer that is not there, is a defect in the review
# infrastructure; a caller naming the author's own family as reviewer is not.
INFRASTRUCTURE_OUTCOMES = frozenset(
    {"reviewer_unavailable", "no_independent_runner_installed"}
)

# What a caller can do about a refusal. It is the closing sentence of the
# refusal message and the requested action of the refusal notification, so the
# two cannot drift into different instructions.
REFUSAL_ACTION = (
    "Install a reviewer from another provider family, name one in the task "
    "contract's `review_policy.reviewer_runner`, or declare an observably "
    "read-only launch that grants no write access and delivers nothing."
)


class ReviewAdmissionError(RuntimeError):
    """A material launch that has no independent reviewer to bind."""

    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(record["message"])
        self.record = record


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every object an append-only ledger holds, oldest first.

    A malformed line is skipped rather than fatal: these ledgers are the record
    a refusal is explained from, and losing the readable entries because one
    line was truncated would hide the decisions that were made.
    """
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def family_of(runner: str | None) -> str:
    return RUNNER_FAMILIES.get(str(runner or ""), "unknown")


def declared_review_policy(contract: dict[str, Any]) -> dict[str, Any]:
    """Read the structured review declaration, ignoring anything unstructured.

    Only two fields mean anything here, and both are exact values rather than
    free text: `work_class` selects the narrow exception, `reviewer_runner`
    names the family that must review. A `justification` string may travel with
    them for the record, but it never decides anything.
    """
    declaration = contract.get("review_policy")
    if not isinstance(declaration, dict):
        return {}
    policy: dict[str, Any] = {}
    work_class = str(declaration.get("work_class", "")).strip()
    if work_class in {MATERIAL, READ_ONLY_LOOKUP}:
        policy["work_class"] = work_class
    reviewer = str(declaration.get("reviewer_runner", "")).strip()
    if reviewer:
        policy["reviewer_runner"] = reviewer
    justification = str(declaration.get("justification", "")).strip()
    if justification:
        policy["justification"] = justification
    return policy


def observed_material_effects(
    task_dir: Path,
    *,
    workflow: str,
    access_grant: dict[str, Any] | None,
    contract: dict[str, Any],
) -> list[str]:
    """List the launch's own observable reasons to require a review.

    Each entry names something the runner can see for itself before the child
    exists: a grant it issued, a workflow it selected, a contract it loaded, a
    manifest already on disk. Nothing here reads prose.
    """
    effects: list[str] = []
    grant = access_grant if isinstance(access_grant, dict) else {}
    if grant.get("grants_write"):
        granted = ", ".join(str(item) for item in grant.get("granted_directories", []))
        effects.append(f"write access granted to {granted or 'a target repository'}")
    mode = grant.get("sandbox_mode")
    if mode and mode not in {"read-only"} and not grant.get("grants_write"):
        effects.append(f"child sandbox `{mode}` can change state outside a read-only run")
    if workflow != "standard":
        effects.append(f"`{workflow}` workflow delivers a product candidate")
    if str(contract.get("gate_status", "")) == "gated":
        effects.append("task contract gates the result on enforceable policy")
    if isinstance(contract.get("review_gates"), list) and contract["review_gates"]:
        effects.append("task declares review gates")
    if (task_dir / "deliverables" / "manifest.json").exists():
        effects.append("task registers user-facing deliverables")
    return effects


def classify_work(
    task_dir: Path,
    *,
    workflow: str,
    access_grant: dict[str, Any] | None,
    contract: dict[str, Any],
    review_launch: bool = False,
) -> dict[str, Any]:
    """Decide whether this launch is material work, a review, or the exception."""
    effects = observed_material_effects(
        task_dir, workflow=workflow, access_grant=access_grant, contract=contract
    )
    declaration = declared_review_policy(contract)
    declared_class = declaration.get("work_class")
    classification: dict[str, Any] = {
        "work_class": MATERIAL,
        "material_effects": effects,
        "declared": declaration,
    }
    if review_launch:
        # The launch was asked for a reviewer verdict, so it is the review, not
        # work that needs one. Saying so is what keeps the requirement from
        # regressing into a review of the review of the review.
        classification["work_class"] = REVIEW
        classification["classified_by"] = "declared_review_launch"
        return classification
    if declared_class == READ_ONLY_LOOKUP and not effects:
        classification["work_class"] = READ_ONLY_LOOKUP
        classification["classified_by"] = "declared_read_only_lookup_with_no_observed_effects"
        return classification
    if declared_class == READ_ONLY_LOOKUP and effects:
        classification["classified_by"] = "declared_read_only_lookup_contradicted_by_observation"
        return classification
    classification["classified_by"] = (
        "observed_material_effects" if effects else "undeclared_launch_defaults_to_material"
    )
    return classification


def reviewer_available(runner: str, which: Callable[[str], str | None] = shutil.which) -> bool:
    executable = RUNNER_EXECUTABLES.get(runner)
    return bool(executable) and which(executable) is not None


def resolve_pair(
    *,
    author_runner: str,
    declared_reviewer: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    """Bind an independent reviewer to this author, or say precisely why not."""
    author_family = family_of(author_runner)
    pair: dict[str, Any] = {
        "author_runner": author_runner,
        "author_family": author_family,
        "reviewer_runner": None,
        "reviewer_family": None,
        "reviewer_source": None,
        "bound": False,
    }
    if declared_reviewer:
        pair["reviewer_source"] = "declared"
        if declared_reviewer not in RUNNER_FAMILIES:
            pair["outcome"] = "unknown_reviewer_runner"
            pair["detail"] = (
                f"the declared reviewer `{declared_reviewer}` is not a runner this launcher knows"
            )
            return pair
        if family_of(declared_reviewer) == author_family:
            pair["outcome"] = "reviewer_is_author_family"
            pair["detail"] = (
                f"the declared reviewer `{declared_reviewer}` is the author's own "
                f"{author_family} family, so it would be the author reviewing itself"
            )
            return pair
        if not reviewer_available(declared_reviewer, which):
            pair["outcome"] = "reviewer_unavailable"
            pair["detail"] = (
                f"the declared reviewer `{declared_reviewer}` is not installed here "
                f"(`{RUNNER_EXECUTABLES[declared_reviewer]}` is not on PATH)"
            )
            return pair
        pair.update(
            {
                "reviewer_runner": declared_reviewer,
                "reviewer_family": family_of(declared_reviewer),
                "bound": True,
                "outcome": "bound",
                "detail": f"declared reviewer `{declared_reviewer}` is installed and independent",
            }
        )
        return pair

    candidates = [
        runner
        for runner in REVIEWER_PREFERENCE
        if family_of(runner) != author_family and family_of(runner) != "unknown"
    ]
    for candidate in candidates:
        if reviewer_available(candidate, which):
            pair.update(
                {
                    "reviewer_runner": candidate,
                    "reviewer_family": family_of(candidate),
                    "reviewer_source": "resolved_independent_family",
                    "bound": True,
                    "outcome": "bound",
                    "detail": (
                        f"`{candidate}` is installed here and is an independent "
                        f"family from the {author_family} author"
                    ),
                }
            )
            return pair
    pair["reviewer_source"] = "resolved_independent_family"
    if author_family == "unknown":
        pair["outcome"] = "unknown_author_runner"
        pair["detail"] = f"the author runner `{author_runner}` has no known provider family"
        return pair
    pair["outcome"] = "no_independent_runner_installed"
    pair["detail"] = (
        "no runner from a family other than "
        f"{author_family} is installed here (looked for "
        + ", ".join(f"`{RUNNER_EXECUTABLES[runner]}`" for runner in candidates)
        + ")"
    )
    return pair


def admissions(task_dir: Path) -> list[dict[str, Any]]:
    """Every admission decision this task number has recorded, oldest first."""
    return _read_jsonl(task_dir / ADMISSIONS_LEDGER)


def bound_author_admission(task_dir: Path) -> dict[str, Any] | None:
    """The admission that bound a reviewer to this number's material work.

    A number can hold several launches -- the author, the review, the rework,
    another review -- and only the author ones carry the binding acceptance has
    to honour. The ledger is consulted rather than the current-launch record,
    because by the time the reviewer is running, that record describes the
    reviewer.

    A task whose ledger predates this ledger falls back to its single current
    record, so an admission made before the ledger existed still binds.
    """
    for entry in reversed(admissions(task_dir)):
        classification = entry.get("classification")
        work_class = (
            classification.get("work_class") if isinstance(classification, dict) else None
        )
        if work_class == MATERIAL and entry.get("decision") == "admitted":
            return entry
    current = recorded_admission(task_dir)
    classification = current.get("classification")
    work_class = (
        classification.get("work_class") if isinstance(classification, dict) else None
    )
    if work_class == MATERIAL and current.get("decision") == "admitted":
        return current
    return None


def resolve_review_launch_pair(
    task_dir: Path, *, reviewer_runner: str
) -> dict[str, Any]:
    """Check that this review is the one the number was promised.

    The binding made before the author started named a family. A review by the
    author's own family is the substitution this project forbids outright, and a
    review by some third family is not the one that was bound -- both are
    refused here rather than discovered in the verdict.

    A number with no author binding of its own is not refused: a review task
    whose subject is another number is paired by the installation that owns
    cross-task pairing, and this launcher has nothing to contradict.
    """
    reviewer_family = family_of(reviewer_runner)
    pair: dict[str, Any] = {
        "reviewer_runner": reviewer_runner,
        "reviewer_family": reviewer_family,
        "author_runner": None,
        "author_family": None,
        "reviewer_source": "review_launch",
        "bound": False,
    }
    binding = bound_author_admission(task_dir)
    if binding is None:
        pair.update(
            {
                "bound": True,
                "outcome": "no_bound_author_in_this_task",
                "detail": (
                    "this task number recorded no material author launch, so the "
                    "review is paired by whoever owns its subject rather than here"
                ),
            }
        )
        return pair
    bound_pair = binding.get("pair") if isinstance(binding.get("pair"), dict) else {}
    author_runner = bound_pair.get("author_runner")
    author_family = bound_pair.get("author_family") or family_of(author_runner)
    bound_reviewer = bound_pair.get("reviewer_runner")
    bound_family = bound_pair.get("reviewer_family") or family_of(bound_reviewer)
    pair.update({"author_runner": author_runner, "author_family": author_family})
    if reviewer_family == author_family:
        pair.update(
            {
                "outcome": "review_by_author_family",
                "detail": (
                    f"`{reviewer_runner}` is the {author_family} family that authored "
                    "this task's work, and an author does not review itself"
                ),
            }
        )
        return pair
    if bound_family and reviewer_family != bound_family:
        pair.update(
            {
                "outcome": "review_by_unbound_family",
                "detail": (
                    f"{bound_family} was bound as this task's independent reviewer "
                    f"before the author started, and `{reviewer_runner}` is "
                    f"{reviewer_family}"
                ),
            }
        )
        return pair
    pair.update(
        {
            "bound": True,
            "outcome": "bound",
            "detail": (
                f"`{reviewer_runner}` is the {reviewer_family} reviewer bound to this "
                f"task before its {author_family} author started"
            ),
        }
    )
    return pair


def _review_refusal_reason(pair: dict[str, Any]) -> str:
    return (
        "task-runner refuses to start this review: it would not be the "
        f"independent review this task was admitted with -- {pair.get('detail')}."
    )


REVIEW_REFUSAL_ACTION = (
    "Run the review with the family bound to this task number, or -- if that "
    "family is unavailable -- leave the work waiting for it. The author's own "
    "family cannot stand in for it."
)


def _refusal_reason(classification: dict[str, Any], pair: dict[str, Any]) -> str:
    effects = classification.get("material_effects") or []
    because = effects[0] if effects else "it is not declared as an observably read-only lookup"
    return (
        "task-runner refuses to start the author: this launch needs an independent "
        f"reviewer because {because}, and none can be bound -- {pair.get('detail')}. "
        f"The {pair.get('author_family')} author will not review its own work."
    )


def _refusal_message(classification: dict[str, Any], pair: dict[str, Any]) -> str:
    return f"{_refusal_reason(classification, pair)} {REFUSAL_ACTION}"


def refusal_notification(record: dict[str, Any]) -> dict[str, str]:
    """Say a refusal in the two parts a notification needs.

    A refusal that exists only in the task's own files is invisible to whoever
    asked for the launch, and being heard before an author runs is this gate's
    whole value. The wording is the record's own, so the notification cannot
    describe a different decision from the one that was made.
    """
    message = str(record.get("message", "")).strip()
    return {
        "summary": str(record.get("refusal_reason") or message).strip(),
        "requested_action": str(record.get("refusal_action") or REFUSAL_ACTION).strip(),
    }


def evaluate(
    task_dir: Path,
    *,
    workflow: str,
    author_runner: str,
    access_grant: dict[str, Any] | None,
    contract: dict[str, Any],
    declared_reviewer: str | None = None,
    review_launch: bool = False,
    assurance: dict[str, Any] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    """Produce the full admission record without writing or refusing anything."""
    classification = classify_work(
        task_dir,
        workflow=workflow,
        access_grant=access_grant,
        contract=contract,
        review_launch=review_launch,
    )
    declared = declared_reviewer or classification["declared"].get("reviewer_runner")
    if classification["work_class"] == REVIEW:
        pair = resolve_review_launch_pair(task_dir, reviewer_runner=author_runner)
    else:
        pair = resolve_pair(
            author_runner=author_runner, declared_reviewer=declared, which=which
        )
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evaluated_at": utc_now(),
        "workflow": workflow,
        "task_dir": str(task_dir),
        "classification": classification,
        "pair": pair,
        "rework_rounds": "unlimited",
    }
    if classification["work_class"] == REVIEW:
        if pair["bound"]:
            record["decision"] = "admitted_review"
            record["message"] = (
                f"task-runner admitted this launch as the review of task "
                f"{task_dir.name}: {pair['detail']}. Its verdict decides whether the "
                "task is accepted, and a verdict of rework returns the same number to "
                "its author with no limit on further rounds."
            )
            return record
        record["decision"] = "refused"
        record["refusal_reason"] = _review_refusal_reason(pair)
        record["refusal_action"] = REVIEW_REFUSAL_ACTION
        record["message"] = f"{record['refusal_reason']} {REVIEW_REFUSAL_ACTION}"
        record["infrastructure_defect"] = False
        return record
    if classification["work_class"] == READ_ONLY_LOOKUP:
        record["decision"] = "exempt"
        record["message"] = (
            "task-runner admitted an observably read-only launch without an "
            "independent reviewer: it was declared `read_only_lookup` and the "
            "launcher observed no write grant, no delivery workflow, no gated "
            "contract and no registered deliverable."
        )
        return record
    if pair["bound"]:
        record["decision"] = "admitted"
        record["message"] = (
            f"task-runner bound {pair['reviewer_family']} as the independent reviewer "
            f"for this {pair['author_family']} author before starting it: {pair['detail']}. "
            "Review and rework stay phases of this task number, with no limit on rounds."
        )
        if workflow != "standard":
            # A dev-pipeline run is reviewed by the core, using the assurance the
            # installation hands it. If that assurance reviews with somebody else
            # -- or with nobody -- then the pair bound above is a note in a file
            # and the launch would run unreviewed.
            binding = assurance_binding(record, assurance)
            record["assurance_binding"] = binding
            if not binding["bound"]:
                refusal = assurance_refusal(binding)
                record["decision"] = "refused"
                record["refusal_reason"] = refusal["summary"]
                record["refusal_action"] = refusal["requested_action"]
                record["message"] = f"{refusal['summary']} {refusal['requested_action']}"
                record["infrastructure_defect"] = False
        return record
    record["decision"] = "refused"
    record["refusal_reason"] = _refusal_reason(classification, pair)
    record["refusal_action"] = REFUSAL_ACTION
    record["message"] = _refusal_message(classification, pair)
    record["infrastructure_defect"] = pair.get("outcome") in INFRASTRUCTURE_OUTCOMES
    return record


def infrastructure_obligation(
    *, source: str, reason: str, reference: str | None = None
) -> dict[str, Any]:
    """Name a review-machinery defect as work that belongs to another number.

    The task that ran into it keeps its own scope: its work is preserved as it
    stands and waits for a reviewer that can be bound, rather than growing a
    second subject or being accepted unreviewed. The number itself is allocated
    by `task-creator`, which owns task identity -- this record is the durable
    obligation to allocate it, not a second allocator hidden in the launcher.
    """
    return {
        "kind": "review_infrastructure_defect",
        "source": source,
        "reason": reason,
        "reference": reference,
        "subject_work": "preserved_and_waiting_for_a_bindable_reviewer",
        "subject_scope": "unchanged",
        "allocated_by": "skills/task-creator",
    }


def obligation_statement(entry: dict[str, Any]) -> str:
    """Say where the defect went, so the subject task is visibly not it."""
    filed = entry.get("recorded_as")
    where = (
        f"filed under its own task number as {filed}"
        if filed
        else (
            "its own task number could not be allocated here "
            f"({entry.get('allocation_error') or 'allocation unavailable'}), so file it "
            "through task-creator"
        )
    )
    return (
        "This is a defect in the review machinery, not in the work under review: "
        f"{where}. This task keeps its scope, keeps its work, and waits for a "
        "reviewer it can bind -- it is not accepted unreviewed and not reviewed "
        "by its own author."
    )


def _defect_key(reason: str, reference: str | None) -> str:
    """One key per distinct outage, so a retry does not allocate a second number."""
    normalized = " ".join(f"{reference or ''} {reason}".lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _tasks_index_command() -> tuple[list[str], Path]:
    """Resolve the canonical task-number owner, source tree or installed."""
    try:  # package install
        from .task_completion import TASKS_INDEX_PATH
    except ImportError:  # direct repository script
        from task_completion import TASKS_INDEX_PATH
    if TASKS_INDEX_PATH.suffix == ".py":
        return [sys.executable, str(TASKS_INDEX_PATH)], TASKS_INDEX_PATH
    return [str(TASKS_INDEX_PATH)], TASKS_INDEX_PATH


def allocate_infrastructure_task(
    task_dir: Path, *, reason: str, reference: str | None
) -> dict[str, Any]:
    """Give the review-machinery defect its own task number, through its owner.

    `task-creator` allocates numbers; this asks it to, rather than growing a
    second allocator here. The number belongs to the defect, so the subject task
    can stay exactly what it was.
    """
    resolved = task_dir.resolve()
    if resolved.parent.name != "tasks":
        # Numbers are allocated inside a workspace `tasks/` root. A task that is
        # not in one has no index to file against, and saying so is better than
        # creating a `tasks/` directory somewhere nobody looks.
        return {
            "recorded_as": None,
            "allocation_error": (
                f"{resolved} is not inside a workspace `tasks/` root, so no task "
                "number can be allocated for the defect here"
            ),
        }
    command, index_path = _tasks_index_command()
    title = f"Review infrastructure defect: {reason or reference or 'reviewer unavailable'}"
    summary = (
        f"Raised while task {task_dir.name} tried to obtain an independent review "
        f"({reference or 'review unavailable'}). {reason} The subject task keeps its "
        "own scope and waits for a reviewer it can bind."
    )
    env = dict(os.environ)
    env["TASKS_INDEX_ROOT"] = str(resolved.parents[1])
    result = subprocess.run(
        [*command, "add", title[:180], summary],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return {
            "recorded_as": None,
            "allocation_error": detail or f"exit {result.returncode}",
            "allocated_through": str(index_path),
        }
    return {
        "recorded_as": result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None,
        "allocated_through": str(index_path),
    }


def record_infrastructure_obligation(
    task_dir: Path,
    *,
    event_id: str,
    source: str,
    reason: str,
    reference: str | None = None,
    recorded_at: str | None = None,
    allocate: bool = True,
) -> dict[str, Any]:
    """File the defect under its own number and append the durable obligation.

    Once per event that raised it, and once per distinct defect: a retry of the
    same outage reuses the number already allocated for it instead of filling
    the index with copies of one problem.
    """
    path = task_dir / OBLIGATIONS_LEDGER
    key = _defect_key(reason, reference)
    allocation: dict[str, Any] | None = None
    for value in _read_jsonl(path):
        if value.get("event_id") == event_id:
            return value
        if value.get("defect_key") == key and value.get("recorded_as"):
            allocation = {
                "recorded_as": value["recorded_as"],
                "allocated_through": value.get("allocated_through"),
                "reused_existing_number": True,
            }
    if allocation is None and allocate:
        try:
            allocation = allocate_infrastructure_task(
                task_dir, reason=reason, reference=reference
            )
        except (OSError, subprocess.SubprocessError, ImportError, IndexError) as exc:
            # The refusal itself must survive a failure to file. An unfiled
            # defect is recorded as unfiled rather than silently dropped.
            allocation = {"recorded_as": None, "allocation_error": str(exc)}
    entry = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "recorded_at": recorded_at or utc_now(),
        "defect_key": key,
        **(allocation or {"recorded_as": None, "allocation_skipped": True}),
        **infrastructure_obligation(source=source, reason=reason, reference=reference),
    }
    entry["separate_task_number"] = entry.get("recorded_as") or "unfiled"
    entry["statement"] = obligation_statement(entry)
    _append_jsonl(path, entry)
    return entry


def infrastructure_obligations(task_dir: Path) -> list[dict[str, Any]]:
    """Every review-machinery defect this task ran into and must not absorb."""
    return _read_jsonl(task_dir / OBLIGATIONS_LEDGER)


def admit_launch(
    task_dir: Path,
    *,
    workflow: str,
    author_runner: str,
    access_grant: dict[str, Any] | None,
    contract: dict[str, Any],
    declared_reviewer: str | None = None,
    review_launch: bool = False,
    assurance: dict[str, Any] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    """Record the decision and refuse a material launch with no reviewer.

    The record is written for every outcome, including the refusal, because the
    task's own state is where a caller looks to find out why nothing started. It
    is also appended to the number's admission ledger, because the binding made
    here has to survive the launches that come after it -- the review that has to
    be the family that was bound, and the acceptance that has to find its
    approval.
    """
    record = evaluate(
        task_dir,
        workflow=workflow,
        author_runner=author_runner,
        access_grant=access_grant,
        contract=contract,
        declared_reviewer=declared_reviewer,
        review_launch=review_launch,
        assurance=assurance,
        which=which,
    )
    if record.pop("infrastructure_defect", False):
        # The outage is another number's work, and it is filed as one before the
        # refusal is reported, so the report can say where it went.
        obligation = record_infrastructure_obligation(
            task_dir,
            event_id=f"launch-admission:{record['evaluated_at']}",
            source="launch_admission",
            reason=str(record["pair"].get("detail", "")),
            reference=record["pair"].get("outcome"),
            recorded_at=record["evaluated_at"],
        )
        record["infrastructure_obligation"] = obligation
        record["message"] += " " + obligation["statement"]
        # The notification carries where the outage went for the same reason the
        # message does: the caller has to see that this task is not the defect.
        record["refusal_reason"] = (
            record.get("refusal_reason", "") + " " + obligation["statement"]
        ).strip()
    _write_json(task_dir / ADMISSION_RECORD, record)
    _append_jsonl(task_dir / ADMISSIONS_LEDGER, record)
    if record["decision"] == "refused":
        raise ReviewAdmissionError(record)
    return record


def recorded_admission(task_dir: Path) -> dict[str, Any]:
    return _read_json(task_dir / ADMISSION_RECORD)


def launch_is_review(task_dir: Path) -> bool:
    """Whether the launch now running was admitted as this number's review.

    Asked of the admission record rather than of the task contract, because
    `require_review_verdict_contract` leaves its requirement in the contract
    permanently: after one review, every later author run would look like a
    review to anything reading that flag, and the rework phase would disappear
    from the history the acceptance gate measures approvals against.
    """
    classification = recorded_admission(task_dir).get("classification")
    if not isinstance(classification, dict):
        return False
    return classification.get("work_class") == REVIEW


def finding_identity(finding: Any) -> str:
    """Name a finding the way a later round can recognise it again.

    A reviewer's own `id` is the identity when it publishes one, because that is
    what it will reuse when the same defect survives a fix. Free-text findings
    fall back to a digest of their normalized text, which recognises a literally
    repeated finding and honestly fails to recognise a reworded one.
    """
    if isinstance(finding, dict):
        identifier = str(finding.get("id", "")).strip()
        if identifier:
            return identifier
        text = " ".join(
            str(finding.get(key, "")).strip()
            for key in ("title", "summary", "description", "detail")
        ).strip()
    else:
        text = str(finding).strip()
    normalized = " ".join(text.lower().split())
    if not normalized:
        return "unnamed"
    return "text:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _rounds(task_dir: Path) -> list[dict[str, Any]]:
    return _read_jsonl(task_dir / ROUNDS_LEDGER)


def review_rounds(task_dir: Path) -> list[dict[str, Any]]:
    """Every review round this task number has already had. Never a budget."""
    return _rounds(task_dir)


def repeat_warning(repeated: Iterable[str], round_number: int) -> str:
    names = ", ".join(sorted(repeated))
    return (
        f"execution quality warning: review round {round_number} returned "
        f"finding(s) {names}, already demonstrated in an earlier round of this "
        "task and fixable then. Rework continues; this is reported, not enforced."
    )


def record_review_round(
    task_dir: Path,
    *,
    event_id: str,
    decision: dict[str, Any] | None,
    review_provider: str | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Append one review round and report findings this task has already seen.

    Rounds are appended without any ceiling. A repeated finding produces a
    warning for the user and changes nothing about whether rework proceeds: the
    honest deeper finding of a later round and the same finding coming back both
    keep the task moving, and only the second one is a quality signal.

    Replaying the same lifecycle event returns the round it already recorded, so
    a projector that re-reads its ledger does not invent a repeat.
    """
    existing = _rounds(task_dir)
    for entry in existing:
        if entry.get("event_id") == event_id:
            return entry
    decision = decision if isinstance(decision, dict) else {}
    findings = decision.get("findings")
    identities: list[str] = []
    if isinstance(findings, list):
        for finding in findings:
            identity = finding_identity(finding)
            if identity not in identities:
                identities.append(identity)
    seen: set[str] = set()
    for entry in existing:
        seen.update(str(value) for value in entry.get("finding_ids", []))
    repeated = [identity for identity in identities if identity in seen]
    entry = {
        "schema_version": SCHEMA_VERSION,
        "round": len(existing) + 1,
        "recorded_at": recorded_at or utc_now(),
        "event_id": event_id,
        "review_provider": review_provider,
        "reviewer_family": family_of(review_provider),
        "decision": str(decision.get("decision", "")) or None,
        "finding_ids": identities,
        "repeated_finding_ids": repeated,
    }
    entry["warning"] = repeat_warning(repeated, entry["round"]) if repeated else None
    _append_jsonl(task_dir / ROUNDS_LEDGER, entry)
    return entry


def round_decision(entry: dict[str, Any]) -> str:
    """`approved`, `rework`, or `unreadable` for one recorded round."""
    decision = str(entry.get("decision", "")).strip().lower()
    if decision in APPROVED_DECISIONS:
        return "approved"
    if decision in REWORK_DECISIONS:
        return "rework"
    return "unreadable"


def review_launch_hint(task_dir: Path, reviewer_runner: str | None) -> str:
    """How to obtain the review this task is waiting for, in one command.

    A gate that refuses acceptance without saying what would satisfy it reads as
    a dead end. The review is a phase of this same number, so the command names
    this task directory rather than a new one.
    """
    runner = reviewer_runner or "<independent-runner>"
    return (
        "Run the bound review as a phase of this same task number: "
        f"`task_runner.py start {task_dir} --runner {runner} "
        "--require-review-verdict`."
    )


def independent_review_status(
    task_dir: Path, *, author_phases: Iterable[dict[str, Any]] = ()
) -> dict[str, Any]:
    """Whether the work as it now stands carries the approval it was admitted with.

    Three things have to hold, and each is read from an append-only record this
    module or `task_phases` already keeps rather than from anything a run says
    about itself:

    - the number bound an independent reviewer to material work at all;
    - its most recent review round approved;
    - no author work has been recorded since that approval, so the approval is
      of what is there now rather than of something a later rework replaced.

    `author_phases` are the phase-history entries that mean the author was
    working -- the caller passes them because `task_phases` owns that vocabulary.
    Nothing here counts rounds: an unapproved round says "not yet", never "no
    more".
    """
    binding = bound_author_admission(task_dir)
    status: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "required": binding is not None,
        "satisfied": True,
        "rounds": len(_rounds(task_dir)),
    }
    if binding is None:
        status["reason"] = (
            "this task number bound no independent reviewer to material work"
        )
        return status
    pair = binding.get("pair") if isinstance(binding.get("pair"), dict) else {}
    reviewer_family = pair.get("reviewer_family")
    author_family = pair.get("author_family")
    status.update(
        {
            "reviewer_runner": pair.get("reviewer_runner"),
            "reviewer_family": reviewer_family,
            "author_family": author_family,
            "admitted_at": binding.get("evaluated_at"),
        }
    )
    status["action"] = review_launch_hint(task_dir, pair.get("reviewer_runner"))
    rounds = _rounds(task_dir)
    if not rounds:
        status["satisfied"] = False
        status["reason"] = (
            f"{reviewer_family} was bound as this task's independent reviewer "
            "before the author started, and it has recorded no review round yet"
        )
        return status
    last = rounds[-1]
    status["last_round"] = {
        "round": last.get("round"),
        "decision": last.get("decision"),
        "reviewer_family": last.get("reviewer_family") or family_of(last.get("review_provider")),
        "recorded_at": last.get("recorded_at"),
    }
    outcome = round_decision(last)
    last_family = status["last_round"]["reviewer_family"]
    if outcome != "approved":
        status["satisfied"] = False
        status["reason"] = (
            f"review round {last.get('round')} by {last_family} did not approve "
            f"(decision {last.get('decision')!r}); the task returns to its author "
            "for rework and another round, of which there is no limit"
        )
        return status
    if author_family and last_family == author_family:
        status["satisfied"] = False
        status["reason"] = (
            f"the approval on record was recorded for the {last_family} family, "
            "which authored this work; an author's approval of itself is not the "
            "independent review this task was admitted with"
        )
        return status
    approved_at = str(last.get("recorded_at", ""))
    later = [
        entry
        for entry in author_phases
        if str(entry.get("entered_at", "")) > approved_at
    ]
    if later:
        status["satisfied"] = False
        status["author_work_after_approval"] = later[-1]
        status["reason"] = (
            f"the approval was recorded at {approved_at}, and the author worked "
            f"again at {later[-1].get('entered_at')}; what is here now has not been "
            "reviewed"
        )
        return status
    status["reason"] = (
        f"review round {last.get('round')} by {last_family} approved this work and "
        "no author work was recorded after it"
    )
    return status


def assurance_binding(
    record: dict[str, Any], assurance: dict[str, Any] | None
) -> dict[str, Any]:
    """Check that the assurance a dev-pipeline run will use is the bound pair.

    A dev-pipeline launch does not review anything itself: the core does, using
    the assurance configuration the installation supplies. So the binding made
    before the author starts is only real if that configuration names the same
    reviewer. An assurance that names someone else, reviews with the author's own
    family, or is missing entirely from a material launch is refused here --
    after the author has run, the same mismatch costs the attempt.
    """
    pair = record.get("pair") if isinstance(record.get("pair"), dict) else {}
    classification = record.get("classification")
    work_class = (
        classification.get("work_class") if isinstance(classification, dict) else None
    )
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "bound_reviewer_runner": pair.get("reviewer_runner"),
        "bound_reviewer_family": pair.get("reviewer_family"),
        "assurance_review_provider": None,
        "assurance_strategy": None,
        "bound": True,
    }
    if work_class != MATERIAL or record.get("decision") != "admitted":
        result["outcome"] = "not_material_work"
        result["detail"] = "no reviewer was bound to this launch, so none can be contradicted"
        return result
    if not isinstance(assurance, dict) or not assurance:
        result.update(
            {
                "bound": False,
                "outcome": "assurance_missing",
                "detail": (
                    "this dev-pipeline launch carries no assurance configuration, so "
                    f"the {pair.get('reviewer_family')} reviewer bound to it would "
                    "never be asked to review anything"
                ),
            }
        )
        return result
    provider = assurance.get("review_provider")
    strategy = assurance.get("strategy")
    result["assurance_review_provider"] = provider
    result["assurance_strategy"] = strategy
    if not isinstance(provider, str) or not provider.strip():
        result.update(
            {
                "bound": False,
                "outcome": "assurance_reviews_nobody",
                "detail": (
                    f"the assurance configuration names no review provider (strategy "
                    f"`{strategy}`), so the {pair.get('reviewer_family')} reviewer "
                    "bound to this launch would never review it"
                ),
            }
        )
        return result
    if family_of(provider) != pair.get("reviewer_family"):
        result.update(
            {
                "bound": False,
                "outcome": "assurance_reviewer_mismatch",
                "detail": (
                    f"{pair.get('reviewer_family')} was bound as this launch's "
                    f"independent reviewer, and the assurance configuration hands the "
                    f"review to `{provider}` ({family_of(provider)})"
                ),
            }
        )
        return result
    result["outcome"] = "bound"
    result["detail"] = (
        f"the assurance configuration reviews with `{provider}`, the "
        f"{pair.get('reviewer_family')} family bound before the author starts"
    )
    return result


ASSURANCE_REFUSAL_ACTION = (
    "Supply the dev-pipeline launch with an assurance configuration whose "
    "`review_provider` is the family bound to this task, or name that family in "
    "the task contract's `review_policy.reviewer_runner` so the two agree."
)


def assurance_refusal(binding: dict[str, Any]) -> dict[str, str]:
    """The refusal text for an assurance that would not run the bound review."""
    reason = (
        "task-runner refuses to start the author: this dev-pipeline launch would "
        f"not be reviewed by the pair it was admitted with -- {binding.get('detail')}."
    )
    return {"summary": reason, "requested_action": ASSURANCE_REFUSAL_ACTION}
