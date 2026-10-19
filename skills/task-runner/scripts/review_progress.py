"""Observable standard-review inputs, kept in the existing review phase cause.

The admission owner decides the next action. This module only captures and
compares bytes and the already-recorded user-input identity; clocks, author
prose and the reviewer's runtime status are not inputs to that comparison.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from . import task_phases, write_admission
    from .deliverables import check_deliverables
    from .task_completion import load_tasks_index
except ImportError:
    import task_phases
    import write_admission
    from deliverables import check_deliverables
    from task_completion import load_tasks_index


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def file_hash(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError(f"unsafe review input: {path.name}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"review input is not a regular file: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evidence_hash(task_dir: Path, relative: str) -> str | None:
    """Evidence is an explicit contained file, never a notebook/timestamp claim."""
    parts = Path(relative).parts
    if (not parts or Path(relative).is_absolute() or ".." in parts
            or parts[0] not in {"evidence", "live-evidence"}):
        raise ValueError("finding evidence must be inside evidence/ or live-evidence/")
    path = task_dir
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("finding evidence cannot follow symlinks")
    return file_hash(path)


def finding_evidence(task_dir: Path, findings: list) -> dict[str, dict[str, str | None]]:
    """Bind reviewer finding ids to the evidence paths selected for that finding.

    The reviewer or subsequent author names paths against that finding id.
    Baseline hashes ensure that adding a mapping to an unchanged old proof
    cannot qualify. New structured proof uses the existing evidence selector.
    """
    result = {}
    for finding in findings:
        if not isinstance(finding, dict) or not isinstance(finding.get("id"), str):
            continue
        paths = finding.get("evidence", [])
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError("finding evidence must be an array of task-relative paths")
        result[finding["id"].strip()] = {p: evidence_hash(task_dir, p) for p in paths}
    return result


def snapshot(task_dir: Path, repositories: list[str], *, include_evidence: bool = False) -> dict[str, Any]:
    checked = check_deliverables(task_dir, max_files=None, max_bytes=None)
    if not checked["ok"]:
        raise ValueError("invalid review candidate: " + "; ".join(checked["errors"]))
    files = {name: file_hash(task_dir / "deliverables" / name)
             for name in sorted(checked["registered"])}
    record = load_tasks_index().read_record(task_dir)
    lifecycle = read_object(task_dir / "task_lifecycle.json")
    # The lifecycle owner already assigns the current author generation from
    # accepted user envelopes. Pending inputs do not belong to this author yet.
    generation = lifecycle.get("run_generation")
    evidence = {}
    if include_evidence:
        try:
            from .task_contract import completion_review_evidence
        except ImportError:
            from task_contract import completion_review_evidence
        # Capture the canonical current proof set once at the review boundary,
        # not the accumulating history on every state observation.
        for path in completion_review_evidence(task_dir):
            relative = path.relative_to(task_dir.resolve()).as_posix()
            if Path(relative).parts[0] in {"evidence", "live-evidence"}:
                evidence[relative] = evidence_hash(task_dir, relative)
        # Preserve selected non-structured proofs too, before the launch owner
        # retires the publication. A failed attempt must not reuse its own basis.
        try:
            findings = read_object(task_dir / "review-findings.json").get("findings", [])
            if isinstance(findings, list):
                for paths in finding_evidence(task_dir, findings).values():
                    evidence.update(paths)
        except (OSError, ValueError):
            pass  # Publication protocol validation owns malformed output.
    return {
        "evidence": evidence,
        "files": files,
        "repositories": {p: write_admission.git_write_state(Path(p)) for p in repositories},
        "requirements": file_hash(task_dir / "requirements.md"),
        "contract": file_hash(task_dir / "task_contract.json"),
        "user_input": generation,
        "author": {"status": record.get("status"),
                   "status_detail": record.get("status_detail"),
                   "blocker": read_object(task_dir / "status.json").get("blocker")},
    }


def latest_snapshot(task_dir: Path, phases: dict | None = None) -> dict | None:
    record = phases if phases is not None else task_phases.read_phases(task_dir)
    index = record.get("review_candidate_index")
    history = record.get("history", [])
    if type(index) is not int or not 0 <= index < len(history):
        return None
    value = history[index].get("cause", {}).get("review_candidate")
    return value if isinstance(value, dict) else None


def changes(task_dir: Path, before: dict, current: dict, last_round: dict) -> list[str]:
    changed = [key for key in ("files", "repositories", "requirements", "contract", "user_input")
               if before.get(key) != current.get(key)]
    try:
        publication = read_object(task_dir / "review-findings.json")
        findings = publication.get("findings", [])
        selected = finding_evidence(task_dir, findings) if isinstance(findings, list) else {}
    except (OSError, ValueError):
        # A malformed reviewer publication is handled by protocol admission,
        # never as changed evidence or as an invalid delivered file.
        return changed
    for identifier, paths in selected.items():
        if identifier not in last_round.get("finding_ids", []):
            continue
        prior_paths = last_round.get("finding_evidence", {}).get(identifier, {})
        for path, digest in paths.items():
            # Structured proofs can be newly named by the author. Other file
            # types need the baseline captured when the reviewer named them.
            if Path(path).suffix not in {".json", ".xml"} and path not in prior_paths:
                continue
            if last_round.get("review_attempt") == before.get("attempt"):
                prior_hash = prior_paths.get(path, before.get("evidence", {}).get(path))
            else:
                # This attempt already consumed the change that admitted it;
                # stale findings from an older round cannot spend it twice.
                prior_hash = before.get("evidence", {}).get(path)
            if digest is not None and digest != prior_hash:
                changed.append(f"finding:{identifier}:{path}")
    return changed
