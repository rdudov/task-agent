#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sys
import subprocess
from pathlib import Path
from typing import Any


DEFAULT_REVIEW_VERDICT = {
    "path": "findings.md",
    "allowed": ["approved", "rework"],
}


def default_task_contract() -> dict[str, Any]:
    return {
        "version": 1,
        "non_negotiable_constraints": [],
        "forbidden_substitutions": [],
        "required_live_evidence": [],
        "acceptance_criteria": [],
        "review_gates": [],
        "review_verdict": {},
        "completion_policy": {
            "require_all_required_live_evidence_passed": True,
            "require_forbidden_substitutions_absent": True,
            "require_mandatory_constraints_reported": True,
            "require_review_verdict": False,
        },
    }


def enforced_review_verdict(contract: dict[str, Any]) -> dict[str, Any] | None:
    """Return the explicit review-output requirement, never an inferred one."""
    policy = contract.get("completion_policy")
    if not isinstance(policy, dict) or not policy.get("require_review_verdict", False):
        return None
    requirement = contract.get("review_verdict")
    if not isinstance(requirement, dict):
        return None
    path = str(requirement.get("path", "")).strip()
    allowed = requirement.get("allowed")
    if path != "findings.md" or not isinstance(allowed, list):
        return None
    normalized = [str(value).strip().lower() for value in allowed if str(value).strip()]
    if not normalized:
        return None
    return {"path": path, "allowed": normalized}


def unsatisfied_review_verdict(contract: dict[str, Any], task_dir: Path) -> list[str]:
    """Require the reviewer's own findings file and one canonical verdict line."""
    requirement = enforced_review_verdict(contract)
    policy = contract.get("completion_policy")
    enabled = isinstance(policy, dict) and policy.get("require_review_verdict", False)
    if not enabled:
        return []
    if requirement is None:
        return ["review verdict policy is enabled but its contract is invalid"]
    path = task_dir / requirement["path"]
    if not path.is_file():
        return [f"{requirement['path']} is absent"]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"{requirement['path']} is unreadable: {exc}"]
    matches = re.findall(
        r"^Verdict:[ \t]*(?:\*\*)?([a-z]+)(?:\*\*)?[ \t]*\r?$",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if len(matches) != 1:
        return [
            f"{requirement['path']} must contain exactly one `Verdict: approved|rework` line"
        ]
    verdict = matches[0].lower()
    if verdict not in requirement["allowed"]:
        allowed = "|".join(requirement["allowed"])
        return [f"{requirement['path']} verdict is {verdict!r}, not one of {allowed}"]
    return []


def enforced_live_evidence(contract: dict[str, Any]) -> list[dict[str, Any]]:
    """The live-evidence items that can actually reject a completion.

    One definition, used both by `contract_gate_status` to label a contract and by
    `validate_review_against_contract` to enforce it. They used to answer this
    question differently -- the label consulted the policy switch and the
    validator did not -- so a contract could be reported as gating on nothing
    while still refusing a review, and vice versa.

    An item needs an id: the validator matches review entries by id, so an item
    without one cannot be enforced and must not be counted as a gate.
    """
    raw_policy = contract.get("completion_policy")
    policy = raw_policy if isinstance(raw_policy, dict) else {}
    if not bool(policy.get("require_all_required_live_evidence_passed", True)):
        return []
    items = contract.get("required_live_evidence")
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("required", True)
        and str(item.get("id", "")).strip()
    ]


# The vocabulary `skills/task-artifacts/SKILL.md` documents for a gate outcome
# (`- Result: **OK** | **FAIL** | **GAP**`), plus the two spellings that already
# occur in the tree. Measured over 1445 recorded sections: OK 992, PASS 11,
# PASSED 8 against GAP 75, FAIL 50, STOP 3, BLOCKED 1.
PASSING_EVIDENCE_RESULTS = frozenset({"OK", "PASS", "PASSED"})


def _verification_result(body: str) -> str:
    result = re.search(r"^[-*][ \t]*Result:[ \t]*(.+)$", body, re.MULTILINE)
    if result is None:
        return ""
    cleaned = re.sub(r"[*_`]", "", result.group(1)).strip()
    return cleaned.split()[0].rstrip(".,;:").upper() if cleaned else ""


def verification_section_records(verification: str) -> list[dict[str, str]]:
    """Parse every verification section with the canonical result semantics."""
    headings = list(re.finditer(r"^##[ \t]+([^\n]+)$", verification, re.MULTILINE))
    records: list[dict[str, str]] = []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(verification)
        body = verification[match.end():end]
        evidence = re.search(r"^[-*][ \t]*Evidence:[ \t]*(.+)$", body, re.MULTILINE)
        records.append({
            "heading": match.group(1).strip(),
            "result": _verification_result(body),
            "evidence": evidence.group(1).strip() if evidence else "",
        })
    return records


def verification_gate_record(verification: str, gate_id: str) -> dict[str, str] | None:
    """Return the last canonical record for one gate, including its evidence."""
    heading = re.compile(rf"^##[ \t]+{re.escape(gate_id)}(?:[ \t].*)?$", re.MULTILINE)
    matches = list(heading.finditer(verification))
    if not matches:
        return None
    match = matches[-1]
    rest = verification[match.end():]
    next_section = re.search(r"^##[ \t]", rest, re.MULTILINE)
    body = rest[: next_section.start()] if next_section else rest
    evidence = re.search(r"^[-*][ \t]*Evidence:[ \t]*(.+)$", body, re.MULTILINE)
    return {
        "heading": match.group(0).removeprefix("##").strip(),
        "result": _verification_result(body),
        "evidence": evidence.group(1).strip() if evidence else "",
    }


def verification_gate_result(verification: str, gate_id: str) -> str | None:
    """The recorded outcome of one live-evidence gate, or None if it has no section.

    Returns the empty string when the section exists but records no result, which
    is not the same as a passing one. `record_verification.sh` always writes a
    `- Result:` line, so an owner using the documented helper always produces
    something this can read.

    The heading must be the gate id, optionally followed by more text after
    whitespace or punctuation. A bare substring test let required gate
    `live_probe` be satisfied by a section titled `## live_probe_extra`.

    **The last section for a gate wins.** `verification.md` is append-only by
    construction -- `record_verification.sh` appends and never rewrites, and the
    documented workflow records a gate after every smoke check -- so repeated
    headings are the normal shape of a retried gate, not a malformed file. This
    used to take the first match and stop, which failed in both directions at
    once: `OK` then `FAIL` parsed as `OK`, so a regression recorded after a pass
    closed the task anyway, and `FAIL` then `OK` parsed as `FAIL`, so a correctly
    repaired task could not be closed through the documented path. Last-wins is
    the only positional rule that satisfies both, and it is what an append-only
    log means: the newest record describes the system as it stands now.
    """
    record = verification_gate_record(verification, gate_id)
    return None if record is None else record["result"]


def unsatisfied_live_evidence(
    contract: dict[str, Any],
    verification: str,
    deferred_gate_ids: frozenset[str] = frozenset(),
) -> list[str]:
    """Which enforced gates this `verification.md` fails to establish, and why.

    The dev-pipeline gate used to accept the mere presence of `## <id>`, so a
    section reading `- Result: **FAIL**` closed the task it documents as failed.
    Presence of a heading is not evidence of an outcome.
    """
    problems: list[str] = []
    for item in enforced_live_evidence(contract):
        gate_id = str(item["id"]).strip()
        if gate_id in deferred_gate_ids:
            continue
        result = verification_gate_result(verification, gate_id)
        if result is None:
            problems.append(f"{gate_id} has no `## {gate_id}` section")
        elif result == "":
            problems.append(f"{gate_id} records no `- Result:` line")
        elif result not in PASSING_EVIDENCE_RESULTS:
            problems.append(f"{gate_id} is {result}, not passed")
    return problems


# The two prose policy families, and the `completion_policy` switch that enforces
# each. They are kept apart from `required_live_evidence` because they are a
# different kind of requirement with a different surface: a live-evidence item has
# an id and a documented place to answer, while a constraint is a line of prose
# with no question the filesystem can be asked.
POLICY_FAMILIES = (
    ("non_negotiable_constraints", "require_mandatory_constraints_reported"),
    ("forbidden_substitutions", "require_forbidden_substitutions_absent"),
)

# Where the digest-bound review that establishes the prose families lives,
# relative to the task directory. The review subject binds the effective
# contract and the complete delivered candidate.
CONTRACT_REVIEW_DIRECTORY = "dev-pipeline/contract-review"
COMPLETION_REVIEW_SUBJECT = f"{CONTRACT_REVIEW_DIRECTORY}/completion-review-subject.json"
COMPLETION_REVIEW_CONTEXT = f"{CONTRACT_REVIEW_DIRECTORY}/context.json"
COMPLETION_REVIEW_RUN = f"{CONTRACT_REVIEW_DIRECTORY}/reviewer-run.json"
COMPLETION_REVIEW_DIAGNOSTICS = (
    f"{CONTRACT_REVIEW_DIRECTORY}/reviewer-diagnostics.stdout.jsonl"
)
COMPLETION_REVIEW_SOURCE_DIRECTORY = f"{CONTRACT_REVIEW_DIRECTORY}/candidate-source"
COMPLETION_REVIEW_PURPOSE = (
    "Independently verify every enforced task policy against the exact delivered candidate."
)
COMPLETION_REVIEW_QUESTION = (
    "Does the delivered candidate satisfy every non_negotiable_constraint and avoid "
    "every forbidden_substitution in the effective_contract embedded in the review "
    "subject? Review implementation behavior; approval is forbidden for a contract-only "
    "or readability-only review. Judge the two prose policy families only. Required live "
    "evidence is enforced separately by the completion predicate: do not require a future "
    "terminal delivery receipt or completion transition from this pre-terminal reviewer, "
    "and do not treat their honest pending state as a policy-family violation."
)
COMPLETION_REVIEW_EXCLUSIONS = [
    "Behavior outside the effective contract and exact delivered candidate."
]
PREEXISTING_DIRTY_BASELINE_FIELD = "preexisting_tracked_dirty_baseline"


def _repository_path(repository: Path, raw: str) -> Path:
    path = Path(raw)
    return (path if path.is_absolute() else repository / path).resolve()


def git_repository_identity(repository: Path) -> dict[str, str]:
    """Bind a baseline to one exact Git worktree, not merely a path-shaped input."""
    repository = repository.resolve()
    root = Path(subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "--show-toplevel"], text=True
    ).strip()).resolve()
    git_dir = _repository_path(root, subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--git-dir"], text=True
    ).strip())
    common_dir = _repository_path(root, subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"], text=True
    ).strip())
    return {
        "worktree": str(root),
        "git_dir": str(git_dir),
        "common_dir": str(common_dir),
    }


def _worktree_entry(repository: Path, relative: str) -> dict[str, Any]:
    path = repository / relative
    if path.is_symlink():
        content = os.readlink(path).encode("utf-8", errors="surrogateescape")
        state = "symlink"
        executable = False
    elif path.is_file():
        content = path.read_bytes()
        state = "present"
        executable = bool(path.stat().st_mode & 0o111)
    else:
        content = b""
        state = "deleted"
        executable = False
    return {
        "path": relative,
        "state": state,
        "digest": "deleted" if state == "deleted" else "sha256:" + hashlib.sha256(content).hexdigest(),
        "executable": executable,
        **({"target": os.readlink(path)} if state == "symlink" else {}),
    }


def capture_preexisting_tracked_dirty_baseline(repository: Path) -> dict[str, Any]:
    """Record visible, unstaged tracked dirt before a task owner can change it."""
    repository = repository.resolve()
    identity = git_repository_identity(repository)
    root = Path(identity["worktree"])
    staged = subprocess.check_output(
        ["git", "-C", str(root), "diff", "--cached", "--name-only", "-z", "--"]
    )
    dirty = subprocess.check_output(
        ["git", "-C", str(root), "diff", "--name-only", "-z", "--"]
    )
    tagged = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-v", "-z"]
    )
    visibility_flags = []
    for raw in (item for item in tagged.split(b"\0") if item):
        decoded = raw.decode("utf-8", errors="surrogateescape")
        tag, _, relative = decoded.partition(" ")
        if tag == "S" or tag.islower():
            visibility_flags.append({"path": relative, "tag": tag})
    payload = {
        "schema_version": 1,
        "repository_identity": identity,
        "entries": [
            _worktree_entry(root, item.decode("utf-8", errors="surrogateescape"))
            for item in sorted(set(part for part in dirty.split(b"\0") if part))
        ],
        "staged_paths": sorted(
            item.decode("utf-8", errors="surrogateescape")
            for item in set(part for part in staged.split(b"\0") if part)
        ),
        "visibility_flags": sorted(visibility_flags, key=lambda item: item["path"]),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["digest"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return payload


def recorded_preexisting_dirty_baseline(task_dir: Path) -> dict[str, Any] | None:
    runner = read_json(task_dir / ".runner" / "runner.json")
    baseline = runner.get(PREEXISTING_DIRTY_BASELINE_FIELD) if isinstance(runner, dict) else None
    return baseline if isinstance(baseline, dict) else None


def validate_preexisting_dirty_baseline(task_dir: Path, repository: Path) -> dict[str, Any] | None:
    """Accept only the exact visible dirt captured before the task run started."""
    current = capture_preexisting_tracked_dirty_baseline(repository)
    if current["visibility_flags"]:
        raise ValueError("tracked paths use assume-unchanged or skip-worktree visibility flags")
    if current["staged_paths"]:
        raise ValueError("tracked paths are staged outside the committed delivery")
    baseline = recorded_preexisting_dirty_baseline(task_dir)
    if baseline is None:
        if current["entries"]:
            raise ValueError("tracked worktree is dirty without a recorded pre-existing baseline")
        return None
    recorded_digest = baseline.get("digest")
    unsigned = {key: value for key, value in baseline.items() if key != "digest"}
    expected_digest = "sha256:" + hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if recorded_digest != expected_digest:
        raise ValueError("recorded pre-existing dirty baseline digest is invalid")
    if baseline.get("schema_version") != 1:
        raise ValueError("recorded pre-existing dirty baseline schema is unsupported")
    if baseline.get("staged_paths"):
        raise ValueError("a pre-existing dirty baseline cannot authorize staged paths")
    if baseline.get("visibility_flags"):
        raise ValueError("a pre-existing dirty baseline cannot authorize Git visibility flags")
    if baseline.get("repository_identity") != current["repository_identity"]:
        raise ValueError("recorded pre-existing dirty baseline repository identity differs")
    if baseline.get("entries") != current["entries"]:
        raise ValueError("tracked worktree differs from the recorded pre-existing dirty baseline")
    return baseline


def require_clean_tracked_worktree(repository: Path) -> None:
    """Reject mutable or masked dependency source that has no launch baseline."""
    current = capture_preexisting_tracked_dirty_baseline(repository)
    if current["visibility_flags"]:
        raise ValueError(
            f"dependency repository {repository} uses assume-unchanged or skip-worktree flags"
        )
    if current["staged_paths"] or current["entries"]:
        raise ValueError(f"dependency repository {repository} has tracked worktree changes")


def delivered_candidate(repository: Path) -> dict[str, Any]:
    """Describe the exact committed Git tree at HEAD, independent of local dirt."""
    repository = repository.resolve()
    head = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    raw = subprocess.check_output(
        ["git", "-C", str(repository), "ls-tree", "-r", "-z", "--full-tree", "HEAD"]
    )
    files: list[dict[str, Any]] = []
    for encoded in sorted(item for item in raw.split(b"\0") if item):
        metadata, encoded_path = encoded.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        relative = encoded_path.decode("utf-8", errors="surrogateescape")
        if mode == "120000":
            content = subprocess.check_output(
                ["git", "-C", str(repository), "cat-file", "blob", object_id]
            )
            target = content.decode("utf-8", errors="surrogateescape")
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            state = "symlink"
            executable = False
        elif object_type == "blob":
            content = subprocess.check_output(
                ["git", "-C", str(repository), "cat-file", "blob", object_id]
            )
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            state = "present"
            executable = mode == "100755"
        elif mode == "160000":
            digest = f"git:{object_id}"
            state = "gitlink"
            executable = False
        else:
            raise ValueError(f"unsupported Git tree entry {mode} {object_type} for {relative}")
        files.append({
            "path": relative,
            "state": state,
            "digest": digest,
            "executable": executable,
            **({"target": target} if state == "symlink" else {}),
        })
    canonical = json.dumps(
        {"head": head, "files": files}, sort_keys=True, separators=(",", ":")
    ).encode()
    return {
        "repository": str(repository),
        "head": head,
        "files": files,
        "digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
    }


def dev_pipeline_source_repository(executable: Path) -> Path:
    """Resolve the checkout imported by the exact selected console script."""
    executable = executable.resolve()
    interpreter = executable.parent / "python"
    if not interpreter.is_file():
        interpreter = Path(sys.executable).resolve()
    source = subprocess.check_output(
        [
            str(interpreter),
            "-c",
            "import pathlib, dev_pipeline; print(pathlib.Path(dev_pipeline.__file__).resolve())",
        ],
        text=True,
    ).strip()
    return Path(subprocess.check_output(
        ["git", "-C", str(Path(source).parent), "rev-parse", "--show-toplevel"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()).resolve()


def completion_review_subject(
    task_dir: Path,
    repository: Path,
    dev_pipeline_bin: Path,
) -> dict[str, Any]:
    dev_pipeline_bin = dev_pipeline_bin.resolve()
    dependency_paths = completion_review_repositories(repository, dev_pipeline_bin)[1:]
    for path in dependency_paths:
        require_clean_tracked_worktree(path)
    dependencies = [
        {"name": "dev-pipeline", "candidate": delivered_candidate(path)}
        for path in dependency_paths
    ]
    return {
        "schema_version": 1,
        "effective_contract": load_task_contract(task_dir),
        "delivered_candidate": delivered_candidate(repository),
        "preexisting_tracked_dirty_baseline": validate_preexisting_dirty_baseline(
            task_dir, repository
        ),
        "runtime_dependencies": dependencies,
        "review_runtime": {
            "dev_pipeline_bin": str(dev_pipeline_bin),
            "digest": "sha256:" + hashlib.sha256(dev_pipeline_bin.read_bytes()).hexdigest(),
        },
    }


def completion_review_repositories(
    repository: Path,
    dev_pipeline_bin: Path,
) -> list[Path]:
    """Git worktrees whose source defines the delivered completion behavior."""
    repositories = [repository.resolve()]
    root = dev_pipeline_source_repository(dev_pipeline_bin)
    if root not in repositories:
        repositories.append(root)
    return repositories


def completion_review_materials(
    repository: Path,
    *,
    include_head_commit: bool = False,
) -> list[Path]:
    """Existing delivery files the bounded reviewer must receive directly.

    The primary repository is always reviewed from the final HEAD commit's
    delta. A separately validated pre-existing dirty baseline is not delivery.
    That makes review-before-push publishable: pushing does not change HEAD or
    the reviewed files, while any later commit still stales the candidate.

    Dependency repositories stay worktree-only. Their complete tracked state is
    already bound in ``runtime_dependencies``; including their unrelated latest
    commit merely because they are clean would misstate the delivery.
    """
    repository = repository.resolve()
    if include_head_commit:
        tracked = subprocess.check_output(
            [
                "git",
                "-C",
                str(repository),
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                "HEAD",
            ]
        )
    else:
        tracked = subprocess.check_output(
            ["git", "-C", str(repository), "diff", "--name-only", "-z", "HEAD"]
        )
    names = sorted(set(item for item in tracked.split(b"\0") if item))
    return [
        repository / item.decode("utf-8", errors="surrogateescape")
        for item in names
        if (repository / item.decode("utf-8", errors="surrogateescape")).is_file()
    ]


def completion_review_all_materials(
    repository: Path,
    dev_pipeline_bin: Path,
) -> list[Path]:
    repositories = completion_review_repositories(repository, dev_pipeline_bin)
    return [
        path
        for index, candidate_repository in enumerate(repositories)
        for path in completion_review_materials(
            candidate_repository,
            include_head_commit=index == 0,
        )
    ]


def completion_review_bound_materials(
    task_dir: Path,
    repository: Path,
    dev_pipeline_bin: Path,
    *,
    materialize: bool = False,
) -> list[Path]:
    """Bind reviewers to committed bytes when visible baseline dirt overlaps a file.

    Ordinary clean paths stay directly reviewable in their repository. If the
    worktree copy differs from HEAD, a deterministic task-owned snapshot carries
    the committed bytes so the reviewer never receives the unrelated baseline as
    if it were delivery.
    """
    repositories = completion_review_repositories(repository, dev_pipeline_bin)
    source_root = task_dir / COMPLETION_REVIEW_SOURCE_DIRECTORY
    if materialize and source_root.exists():
        shutil.rmtree(source_root)
    bound: list[Path] = []
    for index, candidate_repository in enumerate(repositories):
        materials = completion_review_materials(
            candidate_repository, include_head_commit=index == 0
        )
        for path in materials:
            relative = path.resolve().relative_to(candidate_repository.resolve())
            committed = subprocess.check_output([
                "git", "-C", str(candidate_repository), "show",
                f"HEAD:{relative.as_posix()}",
            ])
            directly_bound = (
                path.is_file()
                and not path.is_symlink()
                and path.read_bytes() == committed
            )
            if directly_bound:
                bound.append(path)
                continue
            snapshot = source_root / f"{index}-{candidate_repository.name}" / relative
            if materialize:
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                snapshot.write_bytes(committed)
            if not snapshot.is_file() or snapshot.read_bytes() != committed:
                raise ValueError(
                    f"committed candidate snapshot is absent or stale for {relative.as_posix()}"
                )
            bound.append(snapshot)
    return bound


def completion_review_evidence(task_dir: Path) -> list[Path]:
    """Task-owned executable evidence made reachable to the bounded reviewer."""
    evidence_dir = task_dir / "evidence"
    live_evidence_dir = task_dir / "live-evidence"
    opus_dir = task_dir / "opus"
    candidates = [
        *(
            (
                path
                for path in evidence_dir.rglob("*")
                if path.suffix in {".json", ".xml"}
            )
            if evidence_dir.is_dir()
            else []
        ),
        *(
            (
                path
                for path in live_evidence_dir.rglob("*")
                if path.suffix in {".json", ".xml"}
            )
            if live_evidence_dir.is_dir()
            else []
        ),
        *(path for path in (
            task_dir / "implementation-evidence.json",
            task_dir / "single-owner-inventory.json",
        ) if path.is_file()),
        *((task_dir / "reviews").glob("*/run*.json") if (task_dir / "reviews").is_dir() else []),
        *((task_dir / "reviews").glob("*/diagnostics.stdout.jsonl") if (task_dir / "reviews").is_dir() else []),
        *(opus_dir.glob("scoring.json") if opus_dir.is_dir() else []),
        *(opus_dir.glob("executions.jsonl") if opus_dir.is_dir() else []),
        *(opus_dir.glob("runs/*/claude.json") if opus_dir.is_dir() else []),
        *(
            opus_dir.glob("worktrees/*/fixture-output/*.json")
            if opus_dir.is_dir()
            else []
        ),
    ]
    return sorted(
        {path.resolve() for path in candidates if path.is_file()},
        key=str,
    )


def validate_reviewer_diagnostics(path: Path, run: dict[str, Any]) -> None:
    """Validate the existing Codex stream as procedural execution evidence.

    This is deliberately not an anti-tamper authority. Under the cooperative
    participant model it proves that the supported bounded workflow retained the
    native session and completed turn that produced the decision.
    """
    events = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not events or events[0].get("type") != "thread.started":
        raise ValueError("reviewer diagnostics has no thread start")
    if events[0].get("thread_id") != run.get("native_session_id"):
        raise ValueError("reviewer diagnostics does not bind the native session")
    if not any(event.get("type") == "turn.started" for event in events):
        raise ValueError("reviewer diagnostics has no turn start")
    completed = [event for event in events if event.get("type") == "turn.completed"]
    if not completed:
        raise ValueError("reviewer diagnostics has no completed turn")
    messages = [
        event["item"].get("text")
        for event in events
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "agent_message"
    ]
    if not messages:
        raise ValueError("reviewer diagnostics has no agent decision message")
    try:
        raw_decision = json.loads(messages[-1])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("reviewer diagnostics final message is not a decision") from exc
    if raw_decision != run.get("decision"):
        raise ValueError("reviewer diagnostics decision differs from reviewer output")


def configured_repository(task_dir: Path, subject: dict[str, Any]) -> Path:
    candidate = subject.get("delivered_candidate")
    raw = candidate.get("repository") if isinstance(candidate, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("completion review subject does not name its repository")
    repository = Path(raw).resolve()
    runner = read_json(task_dir / ".runner" / "runner.json")
    grant = runner.get("access_grant") if isinstance(runner, dict) else None
    directories = grant.get("granted_directories") if isinstance(grant, dict) else None
    if isinstance(directories, list) and directories:
        configured = Path(str(directories[0])).resolve()
        if repository != configured:
            raise ValueError("completion review subject repository differs from the configured run")
    return repository


def enforced_policy_families(contract: dict[str, Any]) -> list[str]:
    """The prose requirement sets that can actually refuse this completion.

    One definition, used by `contract_gate_status` to label a contract, by the
    owner instruction to state the requirement, and by
    `unsatisfied_policy_families` to enforce it. They must not answer it
    separately: a label that counts a family the gate ignores is the exact defect
    task 566 recorded and this replaces -- `gate_status` said `gated` while
    `_contract_completion_ready` returned `(True, "")`.

    A family counts only when its `completion_policy` switch is on and its set is
    non-empty, for the same reason live evidence counts only when `required`: a
    requirement nobody would reject on is not a gate.
    """
    raw_policy = contract.get("completion_policy")
    policy = raw_policy if isinstance(raw_policy, dict) else {}
    families: list[str] = []
    for key, switch in POLICY_FAMILIES:
        value = contract.get(key)
        if bool(policy.get(switch, True)) and isinstance(value, list) and value:
            families.append(key)
    return families


def unsatisfied_policy_families(contract: dict[str, Any], task_dir: Path) -> list[str]:
    """Validate the public-core reviewer run over the current delivery subject."""
    families = enforced_policy_families(contract)
    if not families:
        return []
    named = " and ".join(families)
    subject_path = task_dir / COMPLETION_REVIEW_SUBJECT
    context_path = task_dir / COMPLETION_REVIEW_CONTEXT
    run_path = task_dir / COMPLETION_REVIEW_RUN
    diagnostics_path = task_dir / COMPLETION_REVIEW_DIAGNOSTICS
    missing = [
        str(path.relative_to(task_dir))
        for path in (subject_path, context_path, run_path, diagnostics_path)
        if not path.is_file()
    ]
    if missing:
        return [
            f"{named} require an approved delivery-bound reviewer run, and "
            f"{', '.join(missing)} is missing"
        ]

    from dev_pipeline.checkpoints import validate_decision
    from dev_pipeline.conventions import validate_context_packet

    try:
        subject = json.loads(subject_path.read_text(encoding="utf-8"))
        repository = configured_repository(task_dir, subject)
        runtime = subject.get("review_runtime")
        raw_bin = runtime.get("dev_pipeline_bin") if isinstance(runtime, dict) else None
        if not isinstance(raw_bin, str) or not raw_bin.strip():
            raise ValueError("completion review does not bind its public-core executable")
        dev_pipeline_bin = Path(raw_bin).resolve()
        if subject != completion_review_subject(task_dir, repository, dev_pipeline_bin):
            raise ValueError("completion review subject is stale")
        context = validate_context_packet(json.loads(context_path.read_text(encoding="utf-8")))
        run = json.loads(run_path.read_text(encoding="utf-8"))
        if context["role"] != "diff_review":
            raise ValueError("completion review context is not a diff_review")
        if context["purpose"] != COMPLETION_REVIEW_PURPOSE:
            raise ValueError("completion review purpose is not canonical")
        if context["question"] != COMPLETION_REVIEW_QUESTION:
            raise ValueError("completion review question is not canonical")
        if context["exclusions"] != COMPLETION_REVIEW_EXCLUSIONS:
            raise ValueError("completion review exclusions are not canonical")
        first = context["artifacts"][0]
        expected_digest = "sha256:" + hashlib.sha256(subject_path.read_bytes()).hexdigest()
        if first["path"] != str(subject_path.resolve()) or first["digest"] != expected_digest:
            raise ValueError("completion review does not bind the generated delivery subject")
        materials = completion_review_bound_materials(
            task_dir, repository, dev_pipeline_bin
        )
        expected_materials = [
            {
                "path": str(path.resolve()),
                "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in materials
        ]
        if context["artifacts"][1:] != expected_materials:
            raise ValueError("completion review does not directly bind every changed candidate file")
        expected_evidence = [
            {
                "path": str(path),
                "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in completion_review_evidence(task_dir)
        ]
        if context["evidence"] != expected_evidence:
            raise ValueError("completion review does not bind the current task evidence")
        if run.get("packet_digest") != context["packet_digest"]:
            raise ValueError("reviewer run does not bind its context packet")
        if run.get("runtime") != "codex" or run.get("role") != "diff_review":
            raise ValueError("reviewer run is not a bounded public-core diff review")
        if run.get("exit_code") != 0:
            raise ValueError("reviewer run did not exit successfully")
        reviewer = str(run.get("native_session_id", "")).strip()
        if not reviewer or reviewer in owner_lifecycle_identities(task_dir):
            raise ValueError("reviewer run does not establish a separate native session")
        packet = {
            "schema_version": "1.0",
            "review_type": context["decision_review_type"],
            "question": context["question"],
            "artifact": {
                "path": first["path"],
                "version": context["artifact_version"],
                "digest": first["digest"],
            },
            "original_constraints": [
                rule for pack in context["convention_packs"] for rule in pack["rules"]
            ],
            "target_instructions": [context["purpose"]],
            "evidence": [
                f"{item['path']} ({item['digest']})" for item in context["evidence"]
            ],
            "exclusions": context["exclusions"],
            "decision_schema_version": "1.0",
        }
        decision = validate_decision(run.get("decision"), packet)
        validate_reviewer_diagnostics(diagnostics_path, run)
        checked = decision.get("evidence_checked", [])
        repositories = completion_review_repositories(repository, dev_pipeline_bin)
        material_references = [
            reference
            for path in materials
            for reference in delivered_source_references(path, repositories)
        ]
        if materials and not any(
            delivered_source_named(str(item), reference)
            for item in checked
            for reference in material_references
        ):
            raise ValueError("approved review names no delivered source file as checked evidence")
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return [f"{named} completion review is invalid: {exc}"]
    if decision["decision"] != "approved":
        return [f"{named} completion review is {decision['decision']}, not approved"]
    return []


def delivered_source_references(path: Path, repositories: list[Path]) -> list[str]:
    """Names a reviewer can truthfully use for one directly bound source file.

    Public-core packets bind absolute paths, but reviewers commonly report the
    repository-relative name they inspected. Both names identify the same
    digest-bound artifact.
    """
    resolved = path.resolve()
    references = [str(resolved)]
    for repository in repositories:
        try:
            relative = resolved.relative_to(repository.resolve())
        except ValueError:
            continue
        relative_name = relative.as_posix()
        if relative_name and relative_name not in references:
            references.append(relative_name)
    parts = resolved.parts
    if "candidate-source" in parts:
        source_index = parts.index("candidate-source")
        if len(parts) > source_index + 2:
            original = Path(*parts[source_index + 2:]).as_posix()
            if original not in references:
                references.append(original)
    return references


def delivered_source_named(checked: str, reference: str) -> bool:
    """Match a path as a path, not as a substring of another filename."""
    path_character = r"A-Za-z0-9_./-"
    return re.search(
        rf"(?<![{path_character}]){re.escape(reference)}(?![{path_character}])",
        checked,
    ) is not None


def owner_lifecycle_identities(task_dir: Path) -> set[str]:
    """Identities the dev-pipeline owner runs under, so a self-review is nameable.

    Read from the public core's derived projection, which the owner does not
    write. Returns an empty set when there is no lifecycle state, which is the
    honest answer rather than a reason to refuse: a task can carry a contract
    without ever having run under the pipeline.
    """
    identities: set[str] = set()
    for state in (task_dir / "dev-pipeline").glob("*/state.json"):
        try:
            attempt = json.loads(state.read_text(encoding="utf-8")).get("attempt", {})
        except (json.JSONDecodeError, OSError):
            continue
        for key in ("attempt_id", "native_session_id"):
            value = attempt.get(key)
            if isinstance(value, str) and value.strip():
                identities.add(value.strip())
    return identities


def contract_gate_status(contract: dict[str, Any]) -> str:
    """Say whether this contract can actually refuse anything.

    A policy that requires all of an empty set passes unconditionally while
    reading as a gate. Naming the emptiness is what keeps an unauthored contract
    distinguishable from a satisfied one.

    Content alone is not a gate. A requirement counts only when the policy switch
    that enforces it is on, and only when it can actually be rejected on. The two
    prose families come from `enforced_policy_families` and live evidence from
    `enforced_live_evidence`, so the label cannot count a requirement the gate
    ignores -- which it did for both prose families until task 572 gave them a
    refusal path.
    """
    if enforced_policy_families(contract):
        return "gated"
    if enforced_live_evidence(contract):
        return "gated"
    if enforced_review_verdict(contract):
        return "gated"
    return "ungated"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _normalize_bullet_text(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    match = re.match(r"^[-*]\s+(.*)$", stripped)
    if match:
        value = match.group(1).strip()
        return value or None
    checkbox = re.match(r"^- \[(?: |x|X)\]\s+(.*)$", stripped)
    if checkbox:
        value = checkbox.group(1).strip()
        return value or None
    return None


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower())
    normalized = normalized.strip("_")
    return normalized or "evidence"


def parse_task_markdown_contract(task_md: Path) -> dict[str, Any]:
    contract = default_task_contract()
    if not task_md.exists():
        return contract

    lines = task_md.read_text(encoding="utf-8").splitlines()
    current_section: str | None = None
    current_evidence_item: dict[str, Any] | None = None
    pending_bullet: list[str] = []

    def flush_evidence_item() -> None:
        nonlocal current_evidence_item
        if current_evidence_item is None:
            return
        description = str(current_evidence_item.get("description", "")).strip()
        if description:
            contract["required_live_evidence"].append(current_evidence_item)
        current_evidence_item = None

    def record_bullet(bullet_text: str) -> None:
        nonlocal current_evidence_item
        if current_section == "hard constraints":
            contract["non_negotiable_constraints"].append(bullet_text)
            reject_match = re.match(r"^Reject if (.*)$", bullet_text, re.IGNORECASE)
            if reject_match:
                contract["forbidden_substitutions"].append(reject_match.group(1).strip())
        elif current_section == "review gates":
            contract["review_gates"].append(bullet_text)
            reject_match = re.match(r"^Reject if (.*)$", bullet_text, re.IGNORECASE)
            if reject_match:
                contract["forbidden_substitutions"].append(reject_match.group(1).strip())
        elif current_section == "acceptance criteria":
            contract["acceptance_criteria"].append(bullet_text)
        elif current_section == "required verification":
            if current_evidence_item is None:
                current_evidence_item = {
                    "id": _slugify(bullet_text),
                    "description": bullet_text,
                    "required": True,
                }
            else:
                description = str(current_evidence_item.get("description", "")).strip()
                current_evidence_item["description"] = f"{description}; {bullet_text}"

    def flush_bullet() -> None:
        nonlocal pending_bullet
        parts, pending_bullet = pending_bullet, []
        text = " ".join(part.strip() for part in parts if part.strip()).strip()
        if text:
            record_bullet(text)

    for raw_line in lines:
        line = raw_line.rstrip()
        heading = re.match(r"^(#{2,3})\s+(.*)$", line)
        if heading:
            flush_bullet()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            lowered = title.lower()
            if level == 2:
                flush_evidence_item()
                current_section = lowered
            elif current_section == "required verification" and level == 3:
                flush_evidence_item()
                current_evidence_item = {
                    "id": _slugify(title),
                    "description": title,
                    "required": True,
                }
            continue

        bullet_text = _normalize_bullet_text(line)
        if bullet_text:
            flush_bullet()
            pending_bullet = [bullet_text]
            continue

        # An indented non-empty line that starts no bullet of its own continues
        # the one above it. Without this a multi-line criterion was kept only up
        # to its first line break, which is how task 497's contract ended up
        # holding fragments such as "... canonical set, with the".
        if pending_bullet and line.strip() and raw_line[:1].isspace():
            pending_bullet.append(line)
            continue

        flush_bullet()

    flush_bullet()
    flush_evidence_item()
    return contract


def merge_task_contract(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = default_task_contract()
    for key in ("non_negotiable_constraints", "forbidden_substitutions", "acceptance_criteria", "review_gates"):
        merged: list[str] = []
        for source in (base.get(key, []), override.get(key, [])):
            if isinstance(source, list):
                for item in source:
                    value = str(item).strip()
                    if value and value not in merged:
                        merged.append(value)
        result[key] = merged

    evidence_by_id: dict[str, dict[str, Any]] = {}
    for source in (base.get("required_live_evidence", []), override.get("required_live_evidence", [])):
        if not isinstance(source, list):
            continue
        for raw_item in source:
            if not isinstance(raw_item, dict):
                continue
            description = str(raw_item.get("description", "")).strip()
            item_id = str(raw_item.get("id", "")).strip() or _slugify(description)
            if not description:
                description = item_id
            merged_item = {
                "id": item_id,
                "description": description,
                "required": bool(raw_item.get("required", True)),
            }
            evidence_by_id[item_id] = merged_item
    result["required_live_evidence"] = list(evidence_by_id.values())

    for source in (base, override):
        requirement = source.get("review_verdict")
        if isinstance(requirement, dict) and requirement:
            result["review_verdict"] = dict(requirement)

    result["completion_policy"] = dict(default_task_contract()["completion_policy"])
    for source in (base.get("completion_policy", {}), override.get("completion_policy", {})):
        if isinstance(source, dict):
            result["completion_policy"].update(source)

    version = override.get("version", base.get("version", 1))
    result["version"] = int(version) if str(version).isdigit() else 1
    result["gate_status"] = contract_gate_status(result)
    return result


def load_task_contract(task_dir: Path) -> dict[str, Any]:
    task_dir = task_dir.resolve()
    task_md = task_dir / "task.md"
    file_contract = read_json(task_dir / "task_contract.json")
    markdown_contract = parse_task_markdown_contract(task_md)
    return merge_task_contract(markdown_contract, file_contract)


def require_review_verdict_contract(task_dir: Path) -> Path:
    """Persist the explicit contract selected by a review launch."""
    path = ensure_task_contract_file(task_dir)
    contract = read_json(path)
    existing = contract.get("review_verdict")
    if existing not in (None, {}, DEFAULT_REVIEW_VERDICT):
        raise ValueError("task contract already declares a different review_verdict")
    contract["review_verdict"] = dict(DEFAULT_REVIEW_VERDICT)
    policy = contract.get("completion_policy")
    if not isinstance(policy, dict):
        policy = {}
    policy["require_review_verdict"] = True
    contract["completion_policy"] = policy
    contract["gate_status"] = "gated"
    contract.pop("gate_note", None)
    write_json(path, contract)
    return path


UNGATED_CONTRACT_NOTE = (
    "Generated from task.md, which declares no hard constraints, forbidden "
    "substitutions, required verification, or explicit review verdict. This contract gates on nothing: it "
    "cannot refuse a completion. Add a `## Hard Constraints`, `## Review Gates`, "
    "or `## Required Verification` section to task.md, launch an explicit review, "
    "or author task_contract.json "
    "by hand, to give it something to enforce."
)


def ensure_task_contract_file(task_dir: Path) -> Path:
    task_dir = task_dir.resolve()
    path = task_dir / "task_contract.json"
    if path.exists():
        return path
    contract = parse_task_markdown_contract(task_dir / "task.md")
    contract["source"] = "generated_from_task_md"
    contract["gate_status"] = contract_gate_status(contract)
    if contract["gate_status"] == "ungated":
        # An empty policy rather than three `false` flags. A stored `false` would
        # survive `merge_task_contract` and disable a gate genuinely added to
        # task.md later; an absent policy leaves the strict defaults in place for
        # whatever real content appears. The vacuity is stated in `gate_status`,
        # where it cannot be read as a decision about a real requirement.
        contract["completion_policy"] = {}
        contract["gate_note"] = UNGATED_CONTRACT_NOTE
    write_json(path, contract)
    return path


def render_task_contract_overlay(contract: dict[str, Any]) -> str:
    lines: list[str] = ["=== TASK EXECUTION CONTRACT ==="]

    non_negotiable = contract.get("non_negotiable_constraints", [])
    if isinstance(non_negotiable, list) and non_negotiable:
        lines.append("Non-negotiable constraints:")
        lines.extend(f"- {item}" for item in non_negotiable if str(item).strip())

    forbidden = contract.get("forbidden_substitutions", [])
    if isinstance(forbidden, list) and forbidden:
        lines.append("Forbidden substitutions:")
        lines.extend(f"- {item}" for item in forbidden if str(item).strip())

    required_evidence = contract.get("required_live_evidence", [])
    if isinstance(required_evidence, list) and required_evidence:
        lines.append("Required live evidence:")
        for item in required_evidence:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "")).strip()
            description = str(item.get("description", "")).strip()
            required = bool(item.get("required", True))
            requirement_label = "required" if required else "optional"
            rendered = description or item_id
            lines.append(f"- [{requirement_label}] {item_id}: {rendered}")

    acceptance = contract.get("acceptance_criteria", [])
    if isinstance(acceptance, list) and acceptance:
        lines.append("Acceptance criteria:")
        lines.extend(f"- {item}" for item in acceptance if str(item).strip())

    review_gates = contract.get("review_gates", [])
    if isinstance(review_gates, list) and review_gates:
        lines.append("Review gates:")
        lines.extend(f"- {item}" for item in review_gates if str(item).strip())

    review_verdict = enforced_review_verdict(contract)
    if review_verdict:
        allowed = "|".join(review_verdict["allowed"])
        lines.extend(
            [
                "",
                "Required review verdict artifact:",
                f"- Write `{review_verdict['path']}` yourself.",
                f"- It must contain exactly one line `Verdict: {allowed}`.",
                "- Completion is refused when that file or verdict line is absent or invalid.",
            ]
        )

    if contract_gate_status(contract) == "ungated":
        lines.extend(
            [
                "Gate status: UNGATED.",
                "- Nobody authored an execution contract for this task, and task.md declares no"
                " constraints, forbidden substitutions, required verification, or review verdict.",
                "- Nothing here can be satisfied or violated. Do not read this contract as"
                " evidence that completion was checked against anything.",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "Completion rule:",
            "- Do not mark the task approved or completed if any required live evidence is missing, blocked, or failed.",
            "- Do not mark the task approved or completed if a forbidden substitution is present.",
        ]
    )
    return "\n".join(lines)
