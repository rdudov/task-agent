#!/usr/bin/env python3
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import resource
from datetime import datetime, timezone
from pathlib import Path

try:  # package install
    from .application_adapter import (
        APPLICATION_API_VERSION,
        ApplicationAdapterError,
        LaunchRequestV1,
        StandardSessionRequestV1,
        StandardRunResultV1,
        json_session_state,
        load_application,
        parse_memory_limit,
    )
    from .pipeline_notify import (
        INTERRUPTED_COMPLETION_KIND,
        try_send_pipeline_stop_message,
    )
    from .task_completion import (
        application_completion_ready,
        block_task_metadata,
        complete_task_metadata,
        completion_ready,
    )
    from .task_contract import (
        COMPLETION_REVIEW_CONTEXT,
        COMPLETION_REVIEW_EXCLUSIONS,
        COMPLETION_REVIEW_PURPOSE,
        COMPLETION_REVIEW_QUESTION,
        COMPLETION_REVIEW_RUN,
        COMPLETION_REVIEW_SUBJECT,
        completion_review_bound_materials,
        completion_review_evidence,
        completion_review_subject,
        clear_published_review_verdicts,
        enforced_review_verdict,
        ensure_task_contract_file,
        git_repository_identity,
        load_task_contract,
        published_review_verdict,
        require_review_verdict_contract,
    )
    from . import review_admission, task_phases, task_workspace, write_admission
except ImportError:  # direct repository script
    from application_adapter import (
        APPLICATION_API_VERSION,
        ApplicationAdapterError,
        LaunchRequestV1,
        StandardSessionRequestV1,
        StandardRunResultV1,
        json_session_state,
        load_application,
        parse_memory_limit,
    )
    from pipeline_notify import (
        INTERRUPTED_COMPLETION_KIND,
        try_send_pipeline_stop_message,
    )
    from task_completion import (
        application_completion_ready,
        block_task_metadata,
        complete_task_metadata,
        completion_ready,
    )
    from task_contract import (
        COMPLETION_REVIEW_CONTEXT,
        COMPLETION_REVIEW_EXCLUSIONS,
        COMPLETION_REVIEW_PURPOSE,
        COMPLETION_REVIEW_QUESTION,
        COMPLETION_REVIEW_RUN,
        COMPLETION_REVIEW_SUBJECT,
        completion_review_bound_materials,
        completion_review_evidence,
        completion_review_subject,
        clear_published_review_verdicts,
        enforced_review_verdict,
        ensure_task_contract_file,
        git_repository_identity,
        load_task_contract,
        published_review_verdict,
        require_review_verdict_contract,
    )
    import review_admission
    import task_phases
    import task_workspace
    import write_admission

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


TASK_AGENT_ROOT_ENV = "TASK_AGENT_ROOT"


def repo_root() -> Path:
    configured = os.environ.get(TASK_AGENT_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    if __package__:
        return Path.cwd().resolve()
    return Path(__file__).resolve().parents[3]


WORKSPACE_ROOT_ENV = "TASK_AGENT_WORKSPACE_ROOT"


def workspace_root() -> Path:
    """Return the directory a full-access child may treat as its workspace.

    This is the one place that decides how wide `danger-full-access` reaches.
    It is deliberately configurable: a fork that keeps sibling checkouts next to
    this repository gets a useful default, and anything else sets the
    environment variable instead of patching the runner.
    """
    configured = os.environ.get(WORKSPACE_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return repo_root().parent


# A task number is however many digits `tasks_index.py` allocated, not three.
# A pattern pinned to three digits does not fail loudly on `1000-slug` -- it
# simply never matches, so the number stops being a way to name a task at all.
#
# What the three digits also did, by accident, was refuse an unpacked archive
# named after its date: `2026-05-26-openclaw-...` would otherwise hand back
# `2026` and glob every other date-named directory. `tasks_index.py` refuses a
# date-shaped name explicitly for the same reason, and so does this.
TASK_NUMBER_RE = re.compile(r"^(\d+)-")
DATE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-|$)")


def task_number_prefix(name: str) -> str | None:
    """The task number a directory name carries, or None if it carries none."""
    if DATE_NAME_RE.match(name):
        return None
    match = TASK_NUMBER_RE.match(name)
    return match.group(1) if match else None


def resolve_task_dir(task_path: str) -> Path:
    path = Path(task_path)
    if not path.is_absolute():
        path = repo_root() / path
    if path.exists():
        return path.resolve()

    number = task_number_prefix(path.name)
    if not number:
        return path.resolve(strict=False)

    tasks_dir = repo_root() / "tasks"
    candidates = sorted(tasks_dir.glob(f"{number}-*"))
    if len(candidates) == 1:
        return candidates[0].resolve()

    return path.resolve(strict=False)


def runner_dir(task_dir: Path) -> Path:
    return task_dir / ".runner"


def status_path(task_dir: Path) -> Path:
    return task_dir / "status.json"


def trace_path(task_dir: Path) -> Path:
    return task_dir / "trace.md"


def runner_meta_path(task_dir: Path) -> Path:
    return runner_dir(task_dir) / "runner.json"


def runner_log_path(task_dir: Path) -> Path:
    return runner_dir(task_dir) / "runner.log"


def runner_prompt_path(task_dir: Path) -> Path:
    return runner_dir(task_dir) / "prompt.txt"


def runner_workflow_path(task_dir: Path) -> Path:
    return runner_dir(task_dir) / "workflow.json"


def progress_path(task_dir: Path) -> Path:
    return task_dir / "progress.json"


def observe_progress_state(task_dir: Path) -> dict:
    """Capture the filesystem state used to bind progress to one run."""
    try:
        stat = progress_path(task_dir).stat()
    except OSError:
        return {"exists": False}
    return {
        "exists": True,
        "st_mtime_ns": stat.st_mtime_ns,
        "st_ino": stat.st_ino,
        "st_size": stat.st_size,
    }


def user_preferences_path(task_dir: Path) -> Path:
    """Durable user preferences live beside the task index, not inside a task."""
    return task_dir.parent / "USER_PREFERENCES.md"


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def update_runner_meta(task_dir: Path, extra: dict) -> dict:
    payload = read_json(runner_meta_path(task_dir))
    payload.update(extra)
    write_json(runner_meta_path(task_dir), payload)
    return payload


def finish_runner_meta(task_dir: Path, extra: dict) -> dict:
    """Record a terminal launcher outcome and release its pending claim."""
    payload = read_json(runner_meta_path(task_dir))
    payload.update(extra)
    payload.pop("launch_pending", None)
    write_json(runner_meta_path(task_dir), payload)
    return payload


def append_trace(task_dir: Path, message: str) -> None:
    path = trace_path(task_dir)
    if not path.exists():
        path.write_text("# Trace\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"- {utc_now()} {message}\n")


def write_status(task_dir: Path, state: str, current_step: str, extra: dict | None = None) -> None:
    payload = read_json(status_path(task_dir))
    if state in {"ready", "running"}:
        for terminal_field in (
            "exit_code",
            "finished_at",
            "outcome",
            "completion_refusal",
        ):
            payload.pop(terminal_field, None)
    payload.update(
        {
            "state": state,
            "current_step": current_step,
            "updated_at": utc_now(),
        }
    )
    if extra:
        payload.update(extra)
    # A terminal state is also the end of a phase, and the phase record is what
    # shows this one number carried the whole goal. Keeping the two in one place
    # stops them from disagreeing about how the task ended.
    phase = task_phases.phase_for_state(state)
    if phase:
        task_phases.record_phase(
            task_dir, phase, cause={"source": "task-runner", "state": state}
        )
    payload["phase"] = task_phases.current_phase(task_dir)
    write_json(status_path(task_dir), payload)


def ensure_task_contract(task_dir: Path) -> None:
    if not task_dir.exists():
        raise SystemExit(f"Task directory does not exist: {task_dir}")
    for name in ("task.md", "plan.md"):
        if not (task_dir / name).exists():
            raise SystemExit(f"Missing required task artifact: {task_dir / name}")
    ensure_task_contract_file(task_dir)
    runner_dir(task_dir).mkdir(parents=True, exist_ok=True)


def build_child_prompt(
    task_dir: Path,
    *,
    repository: Path | list[Path] | None = None,
    review_subject: str | None = None,
    review_subject_author: str | None = None,
    require_review_verdict: bool = False,
    product_review_packet: Path | None = None,
) -> str:
    task_dir = task_dir.resolve()
    task_md = task_dir / 'task.md'
    plan_md = task_dir / 'plan.md'
    task_contract_json = task_dir / 'task_contract.json'
    status_json = task_dir / 'status.json'
    trace_md = task_dir / 'trace.md'
    progress_json = progress_path(task_dir)
    preferences_md = user_preferences_path(task_dir)
    deliverables_dir = task_dir / 'deliverables'
    manifest_json = deliverables_dir / 'manifest.json'
    product_review_html = deliverables_dir / 'product-review.html'
    role = ""
    opening_steps = f"""1. Read `{task_md}`
2. Read `{plan_md}`
3. Read `{task_contract_json}` if it exists and treat it as a structured execution contract.
4. Read `{preferences_md}` if it exists, before choosing any unspecified output
   representation. The current request and later continuations override it.
5. If `{task_md}` is missing execution-critical inputs from the original request, add them before continuing.
6. Update `{status_json}` to reflect active work.
7. Append a short note to `{trace_md}` describing what you are doing."""
    if require_review_verdict and product_review_packet is not None:
        product_review_packet = product_review_packet.resolve()
        product_review_packet_sha256 = hashlib.sha256(
            product_review_packet.read_bytes()
        ).hexdigest()
        role = f"""
Role: fresh independent product and technical reviewer.
- Review the exact candidate for subject `{review_subject or task_dir}` written by
  `{review_subject_author or 'the recorded author'}`; do not repair it.
- The configured target repository is `{repository}` and is read-only.
- `{product_review_packet}` is the immutable product-review packet. It must contain
  the complete user contract, exact candidate identity, inputs, black-box commands,
  source manifest, and any explicit exclusions. Domain cases belong in that packet,
  never in this canonical instruction. Before using it, verify its SHA-256 is
  `{product_review_packet_sha256}`; a mismatch makes the product verdict
  `not established`.
- Evidence order is part of the result. Before reading `{task_md}`, `{plan_md}`,
  existing findings, implementation files, source code, diffs, tests, or technical
  explanations, read only the packet and begin `{product_review_html}` with one
  visible opening section containing exactly these four labelled lines:
  `User job:`, `Required actor:`, `Observable result:`, and `Strongest false proxy:`.
  Do not revise that opening section after implementation evidence is visible.
- Execute the packet's happy path and false-positive path as black-box scenarios.
  A matching aggregate, green suite, or execution of the named component cannot
  replace evidence that the required actor made each key decision. Record the exact
  input, observation, candidate identity, limitations, and one standalone
  `Product verdict: satisfied`, `Product verdict: not satisfied`, or
  `Product verdict: not established` line in the HTML report. `not established`
  is mandatory when the bounded fresh session cannot finish the scenarios.
- Only after recording that product verdict may you pull implementation detail from
  the packet's source manifest, and only to explain an observed result or gap. Do
  not continue this product verdict from a resumed or compacted context; if the
  fresh session cannot contain it, record `not established`, set the technical
  verdict to rework, and stop.
- After the product verdict is fixed, perform the ordinary technical review against
  the original request and contract. Keep the decisions distinct: the product
  verdict belongs in `{product_review_html}`; technical findings belong in
  `{task_dir / 'findings.md'}` and end with exactly one `Verdict: approved` or
  `Verdict: rework` line. Neither verdict substitutes for the other.
- Make the HTML useful to the person who requested the work: include the exact
  candidate, input, observations, verdict, limitations, and a concrete next-step
  plan. Refuse with `not established` if the packet omits the complete contract or
  enough source identity to tell what was actually reviewed.
- Register `product-review.html` in `{manifest_json}` without removing other
  registered deliverables. Treat all subject and repository files as read-only;
  writes are limited to this task's review artifacts.
"""
        opening_steps = f"""1. Read only `{product_review_packet}`.
2. Before opening any implementation or author evidence, write the four-line opening
   section required above to `{product_review_html}`.
3. Execute both black-box paths and record the product verdict in that same report.
4. Only then read `{task_md}`, `{plan_md}`, and `{task_contract_json}` and pull the
   minimum technical context needed for the separate technical review.
5. Read `{preferences_md}` if it exists before choosing any remaining unspecified
   representation; current intent overrides it.
6. Update `{status_json}` to reflect active work and append a concise note to
   `{trace_md}` without changing the already-recorded product framing or verdict."""
    elif require_review_verdict:
        role = f"""
Role: independent reviewer.
- Review the existing work for subject `{review_subject or task_dir}` written by
  `{review_subject_author or 'the recorded author'}`; do not re-execute or repair it.
- The configured target repository is `{repository}`.
- Review against what the user asked for, not against the derived statement. Read
  the user's own substantive words as preserved in `{task_md}` and, in
  `{task_dir / 'findings.md'}`, map each substantive requirement to the path that
  actually produced the claimed result and to the observation that shows it. If the
  user's words are absent from the task artifacts, that is itself a finding: say so
  instead of reviewing the derived statement alone.
- Then name the strongest false proxy for this task: the most convincing result that
  would pass the author's own checks while a different path, component, or actor made
  the key decision. State whether the evidence rules it out. A matching number, a
  green suite, or an executed named component does not rule it out by itself.
- An unaccepted substitution is a blocking finding. Only the user or the task
  contract can accept one; the author's confidence, the derived statement, and a
  similar surface effect cannot.
- How the code is organized is part of this review. Judge whether the work applies
  the practices a competent engineer would use for this language, domain, and
  repository - cohesive responsibilities, boundaries a reader can follow, no
  dumping ground or unjustified layer, entry documentation that still matches the
  code - and name a material deviation as a finding. Judge the result with
  professional judgement, not against a pattern catalogue, a required directory
  tree, or a line count, and weigh it by user risk: where the product's
  reliability depends on a person being able to follow the code, organization no
  reader can follow blocks the review even when the tests are green.
- Record concrete findings in `{task_dir / 'findings.md'}` and end that file with
  exactly one line: `Verdict: approved` or `Verdict: rework`.
- Treat the subject and target repository as read-only. Writes are limited to this
  review task's own artifacts.
"""
    elif repository is not None:
        role = f"\nConfigured target repository: `{repository}`.\n"
    return f"""You are the child execution agent for task directory: {task_dir}
{role}

Before doing substantial work:
{opening_steps}

While working:
- Before writing code, try in order: do nothing; remove or disable; configure or
  reuse; simplify; only then add the smallest necessary code. Briefly record why
  the observed gap could not be closed by an earlier option.
- Organize the code you do write the way a competent engineer would in this
  language, domain, and exact repository: cohesive responsibilities, boundaries a
  reader can follow, the least structure the job needs, no dumping ground or
  unjustified layer, and entry documentation that still matches the code. Apply
  current practice and judgement; no pattern catalogue, required directory tree,
  or line budget is prescribed here.
- Keep `{trace_md}` updated with concise chronological notes.
- Keep `{status_json}` updated with `state`, `current_step`, and `updated_at`.
- For a long run, publish substantive live progress in `{progress_json}`: a `schema_version: 1`
  object with a concrete `activity`, `updated_at`, and optionally `recent_outcome`.
  Publish `completed`, `total`, and `unit` only together and only when you actually
  know the bounds; never invent a total. Startup bookkeeping is not an outcome.
- Store all task-specific outputs inside `{task_dir}`.
- Do not store task outputs inside `{task_dir / '.runner'}`.
- If `{task_contract_json}` contains non-negotiable constraints, forbidden substitutions, or required live evidence, do not weaken or ignore them.
- Preserve original user-provided inputs that materially affect execution, such as dimensions, constraints, acceptance criteria, requested materials, or excluded options, in task artifacts instead of relying on the chat transcript.
- If you use external sources, write the concrete researched results into `{task_dir / 'findings.md'}` and the source list into `{task_dir / 'sources.md'}` before finishing.
- When you find concrete details such as addresses, contacts, dates, prices, or named options, record them in the task artifacts instead of leaving them only in your final reply.
- Treat files the user explicitly requested as user-facing deliverables, not as
  diagnostic records. Put each requested output file in `{deliverables_dir}` and list
  its basename in stable order in `{manifest_json}` under a `deliverables` array. Keep
  `findings.md`, `verification.md`, `sources.md`, and `trace.md` out of that manifest
  unless the user explicitly requested one of them; none of them substitutes for a
  different requested file.
- If a deliverable's correctness materially depends on visual rendering, render or open
  it in a real viewer or browser and inspect the rendered images before completion.
  Inspect every slide of a short presentation; for longer documents inspect the first,
  last, and representative intermediate pages, widening coverage on layout risk or after
  finding a defect. Check clipping, overflow, overlap, missing or broken images, font
  substitution, unreadable sizing or contrast, and broken responsive or print layout
  where relevant. Structural parsing, archive integrity, or text extraction alone are
  not sufficient. Keep the renders as internal evidence, not as registered deliverables.
- Verify artifact completeness against the source of truth before reporting success.
  Compare size, line count, and start/end boundaries, and look for truncation markers.
  A successful write or send proves delivery, not completeness.
- If the task directory name uses a generic placeholder such as `NNN-remote-request`, rename it early through `skills/task-creator/scripts/rename_task.sh` after you understand the request. Choose a short deliberate ASCII slug yourself; do not copy the full title and do not rely on transliteration.
- Avoid clearly destructive actions such as formatting disks, wiping broad directories, or damaging unrelated projects.
- If the task explicitly touches another project under `{workspace_root()}`, keep the change scoped to the requested files and avoid unrelated damage.
- Read the target repository's own operating context before raising language, syntax,
  build, or convention findings: its root `AGENTS.md`, hidden agent instructions, tool
  configuration, CI images, and declared runtime versions. Judge syntax against the
  declared target runtime, not against remembered defaults.
- When changed source is loaded by an active local service, daemon, worker, or unit,
  apply the change to those running units before reporting done: restart or reload them,
  confirm they are running with fresh start timestamps, check recent logs for startup
  errors, and record that evidence. If it is deferred or blocked, say so explicitly.
- If you change task lifecycle, task artifact structure, skill discovery or execution, agent orchestration, restore behavior, or resume behavior, update the relevant project docs in the same source change.
- If you change git-tracked source in a repository with a configured remote, commit and push after verification unless the task explicitly requires local-only work or publication is blocked.
- If verification or publication is blocked, record the reason and current repository state in task artifacts before finishing.

Before finishing:
- Ensure `{status_json}` has `state` set to `completed` or `blocked`.
- Re-read the original request and every continuation, then check that your response and
  the registered deliverables satisfy the latest complete intent. A later clarification
  may replace an earlier requested representation. If they do not match, keep the task
  blocked instead of claiming completion.
- If the user requested output files, verify that each one exists in `{deliverables_dir}`,
  is non-empty, and is listed in `{manifest_json}`.
- Append a final trace entry summarizing what was done.
- In your final response, summarize the result briefly and reference the task artifacts you updated.
"""


def review_prompt_identity(
    task_dir: Path, review_record: dict, required: bool
) -> tuple[str | None, str | None]:
    """Name the same-number subject and its admitted author in review prompts."""
    pair = review_record.get("pair")
    if not isinstance(pair, dict):
        pair = {}
    return (str(task_dir) if required else None, pair.get("author_runner"))


def codex_workdir(sandbox_mode: str | None, notebook: Path | None = None) -> Path:
    if sandbox_mode == 'danger-full-access':
        return workspace_root()
    if sandbox_mode == "read-only" and notebook is not None:
        return notebook
    return repo_root()


CLI_RUNNERS = ("codex", "claude", "agent")

# The dev-pipeline core names owner runtimes after the product; this launcher
# names runners after the executable it launches. `agent` is the Cursor CLI.
DEV_PIPELINE_OWNER_RUNTIMES = {"codex": "codex", "claude": "claude", "agent": "cursor"}
DEFAULT_RUNNER = "codex"
CODEX_APPROVAL_MODE = "never"
RUNNER_OVERRIDE_ENV = "TASK_AGENT_CHILD_RUNNER"

# Process names of the parent CLIs we can recognize by ancestry. Ancestry beats
# environment markers because both CLIs pass the other vendor's session
# variables to their children untouched, so a nested chain shows both.
RUNNER_PROCESS_NAMES = {"codex": "codex", "claude": "claude"}

# Session markers a CLI exports into every subprocess. Used as a fallback signal
# when no CLI ancestor is visible, and scrubbed from child environments so a
# child never inherits a foreign vendor's session identity.
RUNNER_SESSION_ENV = {
    "codex": (
        "CODEX_THREAD_ID",
        "CODEX_SANDBOX_NETWORK_DISABLED",
        "CODEX_CI",
    ),
    "claude": (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_PID",
        "CLAUDE_CODE_CHILD_SESSION",
        "AI_AGENT",
        "IS_SANDBOX",
    ),
}

CLAUDE_SANDBOX_DEPENDENCIES = ("bwrap", "socat")


def claude_sandbox_capabilities() -> dict[str, object]:
    """Describe what a restricted Claude child can actually rely on here.

    Claude's OS-level sandbox is Linux-specific and needs `bubblewrap` plus
    `socat`. Reporting that as data keeps the platform question in one place
    instead of scattering `sys.platform` checks through the launch path.
    """
    missing = [name for name in CLAUDE_SANDBOX_DEPENDENCIES if shutil.which(name) is None]
    return {
        "platform": sys.platform,
        "native_sandbox": sys.platform.startswith("linux") and not missing,
        "missing_dependencies": missing,
        # Claude refuses its secure nested mode as root: dropping every
        # capability before a second user namespace leaves the bundled seccomp
        # helper unable to map that namespace. The documented weaker nested mode
        # keeps bwrap filesystem confinement but bind-mounts host /proc.
        "needs_weaker_nested_sandbox": hasattr(os, "geteuid") and os.geteuid() == 0,
    }


def claude_access_arguments(
    sandbox_mode: str,
    capabilities: dict[str, object] | None = None,
    notebook: Path | None = None,
    access_directories: tuple[Path, ...] | list[Path] = (),
) -> list[str]:
    """Map a runner-neutral access mode to Claude's real permission boundary."""
    granted = [str(directory) for directory in access_directories]
    if sandbox_mode == "danger-full-access":
        # --add-dir is variadic, so it must be followed by another option or it
        # swallows the trailing prompt argument.
        return [
            "--add-dir",
            str(codex_workdir(sandbox_mode)),
            *granted,
            "--dangerously-skip-permissions",
        ]

    capabilities = capabilities or claude_sandbox_capabilities()
    sandbox_settings: dict[str, object] = {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "failIfUnavailable": True,
    }
    if capabilities.get("needs_weaker_nested_sandbox"):
        sandbox_settings["enableWeakerNestedSandbox"] = True

    if sandbox_mode == "read-only":
        read_only_tools = "Read,Grep,Glob,WebFetch,WebSearch"
        if notebook is None:
            permission_mode = "dontAsk"
            tool_arguments = ["--tools", read_only_tools]
        else:
            # build_command wraps this form in an outer mount namespace. That
            # namespace, not Claude's --add-dir mount, is the write boundary.
            # Native writes and Bash are therefore safe and necessary for the
            # reviewer to maintain its own task artifacts and run tests.
            tool_arguments = ["--tools", f"{read_only_tools},Bash,Write,Edit"]
            sandbox_settings = {"enabled": False}
            permission_mode = None
    elif sandbox_mode == "workspace-write":
        # Claude's sandbox defaults to writes in cwd and its session temp dir.
        # acceptEdits authorizes native file tools only within Claude's granted
        # project roots; no --add-dir is supplied.
        permission_mode = "acceptEdits"
        tool_arguments = ["--tools", "Read,Bash,Edit,Write,WebFetch,WebSearch"]
        if granted:
            writable = list(granted)
            if notebook is not None:
                writable.extend(
                    str(path)
                    for path in (notebook, repo_root() / ".state")
                    if str(path) not in writable
                )
            sandbox_settings["filesystem"] = {"allowWrite": writable}
    else:
        raise SystemExit(f"Unsupported Claude sandbox mode: {sandbox_mode}")

    return [
        *(["--add-dir", *granted] if granted else []),
        "--setting-sources",
        "project",
        *tool_arguments,
        *(["--permission-mode", permission_mode] if permission_mode else ["--dangerously-skip-permissions"]),
        "--settings",
        json.dumps({"sandbox": sandbox_settings}, separators=(",", ":")),
    ]


def require_claude_sandbox_dependencies(
    sandbox_mode: str,
    capabilities: dict[str, object] | None = None,
) -> None:
    """Fail before launch when a requested restricted sandbox cannot exist.

    Restricted modes fail closed. Downgrading a requested boundary to whatever
    the host happens to support would make task artifacts claim a confinement
    that never applied.
    """
    if sandbox_mode == "danger-full-access":
        return
    capabilities = capabilities or claude_sandbox_capabilities()
    if capabilities.get("native_sandbox"):
        return
    missing = capabilities.get("missing_dependencies") or []
    if not str(capabilities.get("platform", "")).startswith("linux"):
        raise RuntimeError(
            "Claude restricted sandbox modes require Linux. On "
            f"{capabilities.get('platform')} use --sandbox-mode danger-full-access "
            "deliberately, or run the child through a Linux host or container."
        )
    raise RuntimeError(
        "Claude restricted mode requires native sandbox dependencies: "
        + ", ".join(missing)
    )


def require_safe_claude_project_settings(repository: Path, sandbox_mode: str) -> None:
    """Reject project settings that can widen a restricted unattended run."""
    if sandbox_mode == "danger-full-access":
        return
    settings_path = repository / ".claude" / "settings.json"
    if not settings_path.exists():
        return
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot validate restricted Claude settings: {settings_path}") from exc
    if not isinstance(settings, dict):
        raise RuntimeError(f"Restricted Claude settings must be an object: {settings_path}")

    sandbox = settings.get("sandbox")
    filesystem = sandbox.get("filesystem") if isinstance(sandbox, dict) else None
    network = sandbox.get("network") if isinstance(sandbox, dict) else None
    permissions = settings.get("permissions")
    permission_allows = permissions.get("allow", []) if isinstance(permissions, dict) else []
    unsafe = {
        "hooks": bool(settings.get("hooks")),
        "enabledPlugins": bool(settings.get("enabledPlugins")),
        "additionalDirectories": bool(settings.get("additionalDirectories")),
        "env": bool(settings.get("env")),
        "fileSuggestion": bool(settings.get("fileSuggestion")),
        "statusLine": bool(settings.get("statusLine")),
        "sandbox.excludedCommands": bool(
            sandbox.get("excludedCommands") if isinstance(sandbox, dict) else None
        ),
        "sandbox.filesystem.allowWrite": bool(
            filesystem.get("allowWrite") if isinstance(filesystem, dict) else None
        ),
        "sandbox.filesystem.disabled": bool(
            filesystem.get("disabled") if isinstance(filesystem, dict) else None
        ),
        "sandbox.network.allowedDomains": bool(
            network.get("allowedDomains") if isinstance(network, dict) else None
        ),
        "sandbox.network.allowUnixSockets": bool(
            network.get("allowUnixSockets") if isinstance(network, dict) else None
        ),
        "sandbox.network.allowAllUnixSockets": bool(
            network.get("allowAllUnixSockets") if isinstance(network, dict) else None
        ),
        "sandbox.network.allowLocalBinding": bool(
            network.get("allowLocalBinding") if isinstance(network, dict) else None
        ),
        "sandbox.network.httpProxyPort": (
            isinstance(network, dict) and network.get("httpProxyPort") is not None
        ),
        "sandbox.network.socksProxyPort": (
            isinstance(network, dict) and network.get("socksProxyPort") is not None
        ),
        "permissions.additionalDirectories": bool(
            permissions.get("additionalDirectories")
            if isinstance(permissions, dict) else None
        ),
        "permissions.allow non-read": any(
            not (
                isinstance(rule, str)
                and (rule == "Read" or rule.startswith("Read("))
            )
            for rule in permission_allows
        ),
    }
    widened = sorted(name for name, present in unsafe.items() if present)
    if widened:
        raise RuntimeError(
            "Restricted Claude mode refuses project settings that can widen execution: "
            + ", ".join(widened)
        )


def process_ancestry(pid: int | None = None, limit: int = 24) -> tuple[list[str], str]:
    """Return process names from the given pid upward, nearest ancestor first.

    Returns the names and the source that produced them. `/proc` is the cheap
    path; `ps` covers hosts without it, such as macOS. Neither being available
    is reported rather than hidden, because the caller then has to fall back to
    weaker signals and should be able to say so.
    """
    current = os.getpid() if pid is None else pid
    names: list[str] = []
    for _ in range(limit):
        try:
            stat = Path(f"/proc/{current}/stat").read_text(encoding="utf-8")
        except OSError:
            break
        try:
            name = stat[stat.index("(") + 1:stat.rindex(")")]
            parent = int(stat[stat.rindex(")") + 2:].split()[1])
        except (ValueError, IndexError):
            break
        names.append(name)
        if current == 1 or parent == 0:
            break
        current = parent
    if names:
        return names, "proc"

    names = process_ancestry_via_ps(os.getpid() if pid is None else pid, limit)
    if names:
        return names, "ps"
    return [], "unavailable"


def process_ancestry_via_ps(pid: int, limit: int = 24) -> list[str]:
    """Walk the process tree with `ps` for hosts without a usable /proc."""
    if shutil.which("ps") is None:
        return []
    try:
        completed = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []

    table: dict[int, tuple[int, str]] = {}
    for line in completed.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) < 3:
            continue
        try:
            entry_pid = int(fields[0])
            entry_ppid = int(fields[1])
        except ValueError:
            continue
        table[entry_pid] = (entry_ppid, Path(fields[2].strip()).name)

    names: list[str] = []
    current = pid
    for _ in range(limit):
        entry = table.get(current)
        if entry is None:
            break
        parent, name = entry
        names.append(name)
        if current == 1 or parent == 0:
            break
        current = parent
    return names


def detect_parent_runner() -> tuple[str | None, str]:
    """Identify the CLI agent that launched this process."""
    ancestry, ancestry_source = process_ancestry()
    by_process = {value: key for key, value in RUNNER_PROCESS_NAMES.items()}
    for name in ancestry:
        if name in by_process:
            return by_process[name], f"parent_process:{name}"

    marked = sorted(
        runner
        for runner, names in RUNNER_SESSION_ENV.items()
        if any(os.environ.get(name) for name in names)
    )
    if len(marked) == 1:
        return marked[0], f"parent_env:{marked[0]}"
    if marked:
        return None, "ambiguous_parent_env:" + "+".join(marked)
    if ancestry_source == "unavailable":
        return None, "no_parent_signal:no_process_ancestry"
    return None, "no_parent_signal"


def resolve_runner(explicit: str | None) -> tuple[str, str]:
    """Resolve the child runner and record why it was chosen.

    Explicit selection always wins, so non-agent callers keep the runner they
    ask for. Detection only decides when nobody said anything.
    """
    if explicit:
        return explicit, "explicit_flag"
    override = os.environ.get(RUNNER_OVERRIDE_ENV)
    if override:
        if override not in CLI_RUNNERS:
            raise SystemExit(
                f"{RUNNER_OVERRIDE_ENV} must be one of {', '.join(CLI_RUNNERS)}: {override}"
            )
        return override, "env_override"
    detected, reason = detect_parent_runner()
    if detected:
        return detected, reason
    return DEFAULT_RUNNER, f"fallback_default:{reason}"


def child_environment(runner: str) -> dict[str, str]:
    """Build the child environment for a runner.

    Every vendor session marker is dropped so the child cannot inherit a foreign
    or stale session identity; each CLI repopulates its own markers. Claude
    additionally needs IS_SANDBOX because it refuses to bypass permissions while
    running as root.
    """
    env = dict(os.environ)
    for names in RUNNER_SESSION_ENV.values():
        for name in names:
            env.pop(name, None)
    if runner == "claude":
        env["IS_SANDBOX"] = "1"
    return env


WRITE_ACCESS_MODES = {"workspace-write", "danger-full-access"}


def resolve_access_directories(
    runner: str,
    repo: str | Path | list[str] | tuple[str, ...] | None,
) -> list[Path]:
    """Turn repeatable `--repo` values into exact directories in input order."""
    if not repo:
        return []
    if runner not in CLI_RUNNERS:
        raise SystemExit(f"Unsupported runner: {runner}")
    values = repo if isinstance(repo, (list, tuple)) else [repo]
    directories: list[Path] = []
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = repo_root() / path
        try:
            path = path.resolve(strict=True)
        except OSError as exc:
            raise SystemExit(
                f"--repo cannot be granted because it does not exist: {value} ({exc})"
            ) from exc
        if not path.is_dir():
            raise SystemExit(f"--repo must be a directory, not a file: {path}")
        if path in directories:
            raise SystemExit(f"--repo names the same repository twice: {path}")
        directories.append(path)
    return directories


def verify_write_access(directories: list[Path]) -> list[dict]:
    """Prove before spawning that a requested writable target is writable."""
    records = []
    for directory in directories:
        record: dict[str, object] = {"path": str(directory), "checked_at": utc_now()}
        probe: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".task-runner-access-check-",
                dir=directory,
                delete=False,
            ) as handle:
                probe = Path(handle.name)
                handle.write("task-runner write check\n")
            record["writable"] = True
        except OSError as exc:
            record["writable"] = False
            record["error"] = str(exc)
        finally:
            if probe is not None:
                probe.unlink(missing_ok=True)
        records.append(record)
    return records


def repository_write_directories(directories: list[Path]) -> list[Path]:
    """Expand exact worktrees to the Git metadata an author must also write."""
    writable: list[Path] = []
    for directory in directories:
        try:
            identity = git_repository_identity(directory)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            raise SystemExit(
                f"--repo must name an exact Git worktree: {directory} ({exc})"
            ) from None
        worktree = Path(identity["worktree"]).resolve()
        if worktree != directory:
            raise SystemExit(
                "--repo must name the exact Git worktree root, not a containing "
                f"directory or subdirectory: {directory} resolves to {worktree}."
            )
        for value in (
            worktree,
            Path(identity["git_dir"]),
            Path(identity["common_dir"]),
        ):
            resolved = value.resolve()
            if resolved not in writable:
                writable.append(resolved)
    return writable


def command_access_directories(
    directories: list[Path], grant: dict[str, object]
) -> list[Path]:
    """Use the verified Git-aware write set when constructing a child command."""
    writable = grant.get("writable_directories")
    if isinstance(writable, list) and writable:
        return [Path(value) for value in writable if isinstance(value, str)]
    return directories


def prepare_access_grant(
    runner: str,
    sandbox_mode: str | None,
    repo: str | Path | list[str] | tuple[str, ...] | None,
    *,
    require_git_worktree: bool = False,
) -> tuple[list[Path], dict]:
    """Resolve a runner-neutral target grant and fail closed when it cannot hold."""
    directories = resolve_access_directories(runner, repo)
    effective_mode = sandbox_mode or (
        "workspace-write" if runner in {"codex", "claude", "agent"} else None
    )
    grant: dict[str, object] = {
        "sandbox_mode": effective_mode,
        "granted_directories": [str(directory) for directory in directories],
        "writable_directories": [],
        "grants_write": bool(directories) and effective_mode in WRITE_ACCESS_MODES,
        "write_check": [],
    }
    if not directories or effective_mode not in WRITE_ACCESS_MODES:
        return directories, grant
    writable_directories = (
        repository_write_directories(directories)
        if require_git_worktree
        else directories
    )
    grant["writable_directories"] = [str(directory) for directory in writable_directories]
    checks = verify_write_access(writable_directories)
    grant["write_check"] = checks
    unwritable = [check for check in checks if not check["writable"]]
    if unwritable:
        details = "; ".join(
            f"{check['path']}: {check.get('error')}" for check in unwritable
        )
        raise SystemExit(
            "Refusing to start a child that cannot write where it was pointed. "
            f"--repo is not writable for this process: {details}."
        )
    return directories, grant


def resolve_sandbox_mode(
    runner: str,
    workflow: str,
    sandbox_mode: str | None,
) -> str | None:
    """Resolve the effective sandbox mode for a child run.

    The dev-pipeline workflow defaults to full access because its owner has to
    maintain the task directory alongside the target repository, and those are
    rarely the same tree. The standard workflow leaves the runner's own default
    alone.
    """
    if sandbox_mode:
        return sandbox_mode
    if workflow == "dev-pipeline" and runner in DEV_PIPELINE_OWNER_RUNTIMES:
        return "danger-full-access"
    return None


def build_command(
    runner: str,
    prompt_path: Path,
    root: Path,
    model: str | None,
    sandbox_mode: str | None,
    notebook: Path | None = None,
    access_directories: tuple[Path, ...] | list[Path] = (),
    application_arguments: tuple[str, ...] | list[str] = (),
) -> list[str]:
    prompt = prompt_path.read_text(encoding="utf-8")
    if runner == "codex":
        resolved_sandbox_mode = sandbox_mode or "workspace-write"
        workdir = codex_workdir(resolved_sandbox_mode, notebook)
        effective_sandbox = (
            "workspace-write"
            if resolved_sandbox_mode == "read-only" and notebook is not None
            else resolved_sandbox_mode
        )
        command = [
            "codex",
            "--ask-for-approval",
            CODEX_APPROVAL_MODE,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            effective_sandbox,
            "-C",
            str(workdir),
        ]
        if resolved_sandbox_mode == "read-only" and notebook is not None:
            command.extend(
                [
                    "-c",
                    "sandbox_workspace_write.writable_roots="
                    + json.dumps([str(repo_root() / ".state")]),
                    "-c",
                    "sandbox_workspace_write.exclude_slash_tmp=true",
                ]
            )
        elif resolved_sandbox_mode in WRITE_ACCESS_MODES and access_directories:
            for directory in access_directories:
                command.extend(["--add-dir", str(directory)])
        if model:
            command.extend(["--model", model])
        command.extend(application_arguments)
        command.append(prompt)
        return command

    if runner == "claude":
        resolved_sandbox_mode = sandbox_mode or "workspace-write"
        # The child keeps the repository as its working directory so CLAUDE.md,
        # and through it AGENTS.md and the always-on rules, load automatically.
        command = [
            "claude",
            "--print",
            *claude_access_arguments(
                resolved_sandbox_mode,
                notebook=notebook,
                access_directories=access_directories,
            ),
        ]
        resolved_model = model or os.environ.get("CLAUDE_CHILD_DEFAULT_MODEL")
        if resolved_model:
            command.extend(["--model", resolved_model])
        command.extend(application_arguments)
        command.append(prompt)
        if resolved_sandbox_mode == "read-only" and notebook is not None:
            # Claude's own sandbox currently makes cwd and --add-dir writable
            # even when its permission policy says otherwise. An outer mount
            # namespace is the enforceable boundary: the host is read-only and
            # only the review notebook, task index and runtime-owned Claude
            # session storage are rebound writable.
            writable_runtime = [
                path
                for path in (
                    Path.home() / ".claude",
                    Path.home() / ".claude.json",
                    Path.home() / ".cache",
                )
                if path.exists()
            ]
            boundary = [
                "bwrap",
                "--ro-bind", "/", "/",
                "--dev-bind", "/dev", "/dev",
                "--proc", "/proc",
                "--tmpfs", "/tmp",
                "--bind", str(notebook), str(notebook),
            ]
            state = repo_root() / ".state"
            if state.exists():
                boundary.extend(["--bind", str(state), str(state)])
            for path in writable_runtime:
                boundary.extend(["--bind", str(path), str(path)])
            boundary.extend(["--chdir", str(notebook), "--"])
            command = [*boundary, *command]
        return command

    if runner == "agent":
        command = [
            "agent",
            "--print",
            "--trust",
            "--force",
            "--workspace",
            str(root),
        ]
        for directory in access_directories:
            command.extend(["--add-dir", str(directory)])
        if model:
            command.extend(["--model", model])
        command.extend(application_arguments)
        command.append(prompt)
        return command

    raise SystemExit(f"Unsupported runner: {runner}")


def build_workflow_command(
    workflow: str,
    runner: str,
    task_dir: Path,
    sandbox_mode: str | None,
    model: str | None = None,
    *,
    repo: str | None = None,
    dev_pipeline_bin: str | None = None,
    operation: str = "start",
    state_dir: str | None = None,
    previous_state_dir: str | None = None,
    retry_reason: str | None = None,
    application: str | None = None,
    destination: str | None = None,
    assurance_config: str | None = None,
    review_packet: str | None = None,
) -> list[str] | None:
    """Return the command for a workflow that runs through a dedicated script.

    The standard workflow has none: it launches the CLI runner directly.
    """
    if workflow == "standard":
        return None
    if workflow == "dev-pipeline":
        return build_dev_pipeline_command(
            runner,
            task_dir,
            sandbox_mode,
            model,
            repo,
            dev_pipeline_bin,
            operation,
            state_dir,
            previous_state_dir,
            retry_reason,
            application,
            destination,
            assurance_config,
            review_packet,
        )
    raise SystemExit(f"Unsupported workflow: {workflow}")


DEV_PIPELINE_OPTIONS = (
    "repo",
    "dev_pipeline_bin",
    "operation",
    "state_dir",
    "previous_state_dir",
    "retry_reason",
    "application",
    "destination",
    "assurance_config",
    "review_packet",
)

DEV_PIPELINE_BIN_ENV = "TASK_AGENT_DEV_PIPELINE_BIN"


def resolve_dev_pipeline_bin(explicit: str | None = None) -> str:
    """Resolve the CLI installed by this repository's own README."""
    configured = explicit or os.environ.get(DEV_PIPELINE_BIN_ENV)
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.parent != Path(".") or candidate.is_absolute():
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise SystemExit(f"Dev-pipeline executable is not runnable: {candidate}")
            return str(candidate.resolve())
        found = shutil.which(configured)
        if found:
            return found
        raise SystemExit(f"Dev-pipeline executable is not on PATH: {configured}")

    local = repo_root() / ".venv" / "bin" / "dev-pipeline"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    found = shutil.which("dev-pipeline")
    if found:
        return found
    raise SystemExit(
        "dev-pipeline is not installed. Run `.venv/bin/pip install -r "
        "requirements.lock` from the task-agent checkout, or set "
        f"{DEV_PIPELINE_BIN_ENV}."
    )


def dev_pipeline_options(args: argparse.Namespace) -> dict:
    """Collect the dev-pipeline options a namespace actually carries.

    `start` and `_run-child` must agree on these without each restating the
    list, so the parent hands the watcher exactly what it resolved itself.
    """
    if getattr(args, "workflow", None) != "dev-pipeline":
        return {}
    options = {name: getattr(args, name, None) for name in DEV_PIPELINE_OPTIONS}
    return {name: value for name, value in options.items() if value is not None}


def watcher_options(args: argparse.Namespace) -> dict:
    """Return lifecycle inputs that must survive the detached watcher boundary."""
    options = {
        name: getattr(args, name, None)
        for name in ("operation", "application", "destination")
    }
    if getattr(args, "workflow", None) == "dev-pipeline":
        options.update(dev_pipeline_options(args))
    return {name: value for name, value in options.items() if value is not None}


def build_dev_pipeline_command(
    runner: str,
    task_dir: Path,
    sandbox_mode: str | None,
    model: str | None,
    repo: str | None,
    dev_pipeline_bin: str | None,
    operation: str,
    state_dir: str | None,
    previous_state_dir: str | None,
    retry_reason: str | None,
    application: str | None = None,
    destination: str | None = None,
    assurance_config: str | None = None,
    review_packet: str | None = None,
) -> list[str]:
    """Build the adapter invocation for the dev-pipeline workflow.

    The runner decides nothing about the pipeline itself. It hands the task
    directory to the adapter, which owns the public CLI contract and the
    projection of its events.
    """
    if runner not in DEV_PIPELINE_OWNER_RUNTIMES:
        raise SystemExit(
            "The dev-pipeline workflow supports the "
            + ", ".join(sorted(DEV_PIPELINE_OWNER_RUNTIMES))
            + " runners, because those are the owner runtimes the dev-pipeline "
            "core drives."
        )
    if not repo:
        raise SystemExit("The dev-pipeline workflow requires --repo.")
    command = [
        sys.executable,
        str(Path(__file__).with_name("dev_pipeline_adapter.py")),
        str(task_dir),
        "--repo",
        repo,
        "--dev-pipeline-bin",
        resolve_dev_pipeline_bin(dev_pipeline_bin),
        "--operation",
        operation,
        "--owner-runtime",
        DEV_PIPELINE_OWNER_RUNTIMES[runner],
    ]
    if state_dir:
        command.extend(["--state-dir", state_dir])
    if previous_state_dir:
        command.extend(["--previous-state-dir", previous_state_dir])
    if retry_reason:
        command.extend(["--retry-reason", retry_reason])
    if sandbox_mode:
        command.extend(["--sandbox", sandbox_mode])
    if model:
        command.extend(["--model", model])
    if application:
        command.extend(["--application", application])
    if destination:
        command.extend(["--destination", destination])
    if assurance_config:
        command.extend(["--assurance-config", assurance_config])
    if review_packet:
        command.extend(["--review-packet", review_packet])
    return command


def redact_sensitive_arguments(command: list[str]) -> list[str]:
    redacted = list(command)
    for option in ("--destination",):
        while option in redacted:
            index = redacted.index(option)
            if index + 1 < len(redacted):
                redacted[index + 1] = "<application-destination>"
            break
    return redacted


def prepared_application_launch(args: argparse.Namespace, task_dir: Path) -> dict:
    """Resolve v1 policy once and reuse its exact standard-session arguments."""
    spec = getattr(args, "application", None)
    operation = getattr(args, "operation", "start")
    destination = getattr(args, "destination", None)
    if args.workflow == "standard" and getattr(args, "launch_token", None) is not None:
        runner_meta = read_json(runner_meta_path(task_dir))
        existing = runner_meta.get("application")
        expected_binding = runner_meta.get("destination_binding")
        received_binding = (
            hashlib.sha256(destination.encode()).hexdigest()[:12] if destination else None
        )
        problems = []
        if not isinstance(existing, dict):
            problems.append("the parent application record is absent")
        else:
            if existing.get("api_version") != APPLICATION_API_VERSION:
                problems.append("the application API version changed")
            if existing.get("spec") != spec:
                problems.append("the application registration changed")
            if existing.get("operation") != operation:
                problems.append("the lifecycle operation changed")
            if not isinstance(existing.get("standard_session"), dict):
                problems.append("the prepared native session is absent")
        if expected_binding != received_binding:
            problems.append("the destination binding changed")
        if problems:
            raise ApplicationAdapterError(
                "Detached watcher refused inconsistent application context: "
                + "; ".join(problems)
            )
        return dict(existing)

    adapter = load_application(spec)
    requested = parse_memory_limit(getattr(args, "memory_limit", None))
    policy = adapter.launch_policy(
        LaunchRequestV1(
            task_dir=task_dir,
            runner=args.runner,
            workflow=args.workflow,
            operation=operation,
            destination=destination,
            requested_memory_limit_bytes=requested,
        )
    )
    if not hasattr(policy, "memory_limit_bytes"):
        raise ApplicationAdapterError("launch_policy must return LaunchPolicyV1")
    memory_limit = parse_memory_limit(policy.memory_limit_bytes)
    record: dict = {
        "api_version": APPLICATION_API_VERSION,
        "spec": spec,
        "operation": operation,
        "memory_limit_bytes": memory_limit,
    }
    if args.workflow == "standard":
        runner_meta = read_json(runner_meta_path(task_dir))
        existing = runner_meta.get("application", {})
        previous = (
            existing.get("standard_session", {}).get("state", {})
            if isinstance(existing, dict)
            else {}
        )
        session = adapter.standard_session(
            StandardSessionRequestV1(
                task_dir=task_dir,
                runner=args.runner,
                operation=record["operation"],
                destination=destination,
                previous_state=previous if isinstance(previous, dict) else {},
            )
        )
        if not hasattr(session, "command_arguments") or not hasattr(session, "state"):
            raise ApplicationAdapterError(
                "standard_session must return StandardSessionV1"
            )
        arguments = tuple(str(value) for value in session.command_arguments)
        if any("\x00" in value for value in arguments):
            raise ApplicationAdapterError("standard session arguments contain NUL")
        if destination and any(destination in value for value in arguments):
            raise ApplicationAdapterError(
                "standard session arguments must not contain the raw destination"
            )
        state = json_session_state(session.state)
        if destination and destination in json.dumps(state, sort_keys=True):
            raise ApplicationAdapterError(
                "standard session state must not contain the raw destination"
            )
        record["standard_session"] = {
            "command_arguments": list(arguments),
            "state": state,
        }
    return record


def child_resource_limiter(memory_limit_bytes: int | None):
    if memory_limit_bytes is None:
        return None

    def apply_limit() -> None:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        effective = memory_limit_bytes if hard == resource.RLIM_INFINITY else min(memory_limit_bytes, hard)
        resource.setrlimit(resource.RLIMIT_AS, (effective, hard))

    return apply_limit


def valid_progress_number(value: object) -> bool:
    """Accept a real number only. `True` is an int in Python; a count is not."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def structured_progress(task_dir: Path) -> dict | None:
    """Return validated `schema_version: 1` progress, or None when unusable.

    The point of the schema is that a reader can trust it. `activity` and
    `updated_at` must be real text, and the count triple is accepted only when
    it is complete and coherent, so nobody downstream has to infer a missing
    total. A malformed file yields None rather than taking down the caller.
    """
    try:
        payload = read_json(progress_path(task_dir))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None

    activity = payload.get("activity")
    updated_at = payload.get("updated_at")
    if (
        not isinstance(activity, str)
        or not activity.strip()
        or not isinstance(updated_at, str)
        or not updated_at.strip()
    ):
        return None

    progress: dict = {
        "schema_version": 1,
        "activity": activity.strip(),
        "updated_at": updated_at.strip(),
    }
    recent_outcome = payload.get("recent_outcome")
    if isinstance(recent_outcome, str) and recent_outcome.strip():
        progress["recent_outcome"] = recent_outcome.strip()

    completed = payload.get("completed")
    total = payload.get("total")
    unit = payload.get("unit")
    counts_declared = [value is not None for value in (completed, total, unit)]
    if all(counts_declared):
        if (
            valid_progress_number(completed)
            and valid_progress_number(total)
            and isinstance(unit, str)
            and unit.strip()
            and total > 0
            and 0 <= completed <= total
        ):
            progress.update(
                {
                    "completed": completed,
                    "total": total,
                    "unit": unit.strip(),
                    "percent": round(completed * 100 / total),
                }
            )
        else:
            progress["counts_rejected"] = "incoherent completed/total/unit"
    elif any(counts_declared):
        # A partial triple is the exact case that tempts a reader to invent the
        # rest. Report it as unusable instead of showing half a measurement.
        progress["counts_rejected"] = "incomplete completed/total/unit"

    return progress


def progress_belongs_to_current_run(task_dir: Path) -> bool:
    """Whether the current progress file changed after this run's baseline."""
    meta = read_json(runner_meta_path(task_dir))
    baseline = meta.get("progress_baseline")
    if not isinstance(baseline, dict) or "exists" not in baseline:
        return True
    current = observe_progress_state(task_dir)
    if not baseline["exists"]:
        return bool(current.get("exists"))
    if not current.get("exists"):
        return False
    return (
        current.get("st_ino") != baseline.get("st_ino")
        or current.get("st_mtime_ns", 0) > baseline.get("st_mtime_ns", 0)
    )


def incomplete_published_progress(task_dir: Path) -> dict | None:
    """Return an incomplete bound observed from the run that just ended."""
    if not progress_belongs_to_current_run(task_dir):
        return None
    progress = structured_progress(task_dir)
    if not progress or "completed" not in progress:
        return None
    if progress["completed"] >= progress["total"]:
        return None
    return {
        key: progress[key]
        for key in ("completed", "total", "unit", "activity")
    }


COMPLETION_REFUSAL_PREMATURE = "premature_completion"
COMPLETION_REFUSAL_INTERRUPTED = INTERRUPTED_COMPLETION_KIND


def completion_refusal(task_dir: Path, reason: str) -> dict:
    """Describe only what durable state establishes about a refused close."""
    published = incomplete_published_progress(task_dir)
    if published is None:
        return {
            "kind": COMPLETION_REFUSAL_PREMATURE,
            "reason": reason,
            "summary": f"Rejected premature completion: {reason}",
        }
    counts = f"{published['completed']:g} of {published['total']:g} {published['unit']}"
    return {
        "kind": COMPLETION_REFUSAL_INTERRUPTED,
        "reason": reason,
        "last_published_progress": published,
        "summary": (
            "Owner run ended before its closing step; last published progress "
            f"was {counts}, and where the owner actually got to is not observed. "
            "Work it started may have continued outside its process. Completion "
            f"refused because {reason}"
        ),
    }


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_identity_available() -> bool:
    """Report whether this host can produce a stable process identity at all.

    Separating "this host cannot answer" from "that process is gone" is the
    whole point. Both look like `process_identity() is None`, and a supervisor
    that conflates them either declares every live child dead or accepts a
    recycled PID as the original.
    """
    return process_identity(os.getpid()) is not None


def process_identity(pid: int) -> str | None:
    """Return a stable identity for a live pid, or None when unavailable.

    Identity is the kernel start-time tick of the process, hashed. A PID alone
    is not an identity: the kernel recycles it, so a stale recorded PID can name
    an unrelated process. The start-time tick pins the specific incarnation.

    Command text deliberately takes no part in this. A process may rewrite its
    own argv after launch — the Node-based Codex CLI does — so mutable command
    text cannot be a liveness signal.

    Degradation: this reads `/proc/<pid>/stat`, which is Linux-specific. Where
    `/proc` is absent or unreadable, every call returns None; use
    `process_identity_available()` to tell that host apart from a dead process,
    and fall back to `pid_is_running()` for the weaker PID-only check.
    """
    try:
        raw_stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    closing_paren = raw_stat.rfind(")")
    if closing_paren < 0:
        return None
    # The comm field is parenthesized and may itself contain spaces, so parsing
    # starts after its closing paren. `stat` field 22 is the start-time tick;
    # field 3 is the first token after comm, which puts start time at index 19.
    fields_after_command = raw_stat[closing_paren + 2:].split()
    if len(fields_after_command) <= 19:
        return None
    return hashlib.sha256(fields_after_command[19].encode()).hexdigest()


def pid_namespace_identity() -> str | None:
    """Return the kernel identity of the PID namespace this observer can see."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def observed_pid_namespace_identities() -> tuple[set[str], bool]:
    """Return PID namespaces visible through /proc and whether the scan was complete."""
    identities: set[str] = set()
    complete = True
    try:
        processes = list(Path("/proc").iterdir())
    except OSError:
        return identities, False
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            identities.add(os.readlink(process / "ns" / "pid"))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            complete = False
    return identities, complete


def runner_pid_namespace_state(meta: dict) -> str:
    """Classify whether this observer can decide the recorded run's liveness."""
    recorded = meta.get("pid_namespace")
    current = pid_namespace_identity()
    if not recorded or (current is not None and recorded == current):
        return "local"
    observed, complete = observed_pid_namespace_identities()
    if recorded in observed:
        return "foreign_live"
    if complete and host_systemd_scope_available():
        return "recorded_namespace_absent"
    return "different_pid_namespace"


def runner_pid_namespace_visible(meta: dict) -> bool:
    """Whether negative PID lookups are evidence for this runner record."""
    recorded = meta.get("pid_namespace")
    current = pid_namespace_identity()
    return not recorded or (current is not None and recorded == current)


def process_is_recorded_instance(pid: object, expected_identity: object) -> tuple[bool, str]:
    """Decide whether a recorded pid still names the process that was recorded.

    Returns the verdict and how it was reached, because the caller's options
    differ per source: an identity match is proof, while a PID-only match is a
    guess that a supervisor may accept for reporting but must not act on.
    """
    if not isinstance(pid, int):
        return False, "no_recorded_pid"
    if isinstance(expected_identity, str) and expected_identity:
        if process_identity(pid) == expected_identity:
            return True, "identity_match"
        return False, "identity_mismatch"
    if not process_identity_available():
        return pid_is_running(pid), "pid_only_no_process_identity"
    # The host can produce identities but none was recorded, so this metadata
    # predates identity tracking. PID liveness is all that is left.
    return pid_is_running(pid), "pid_only_unrecorded_identity"


def process_is_live(pid: object, expected_identity: object) -> bool:
    """Compatibility boolean over the canonical identity-aware liveness result."""
    return process_is_recorded_instance(pid, expected_identity)[0]


def live_run_processes(task_dir: Path) -> list[dict]:
    """Return the child/watcher processes of one task that are still running.

    An identity match is proof. A PID-only match is weaker, and on a host
    without `/proc` — macOS is the one that matters here — it is the only
    evidence that exists. Requiring proof there would report every live run as
    dead, and the caller that acts on this is the one refusing a second run for
    the same task, so it would admit a concurrent run on exactly the hosts that
    cannot detect one. Weaker evidence still counts as alive; each entry says
    which evidence it rests on so a caller that needs proof can insist on it.
    """
    meta = read_json(runner_meta_path(task_dir))
    if not meta or meta.get("dry_run"):
        return []
    alive: list[dict] = []
    for role, pid_key, identity_key in (
        ("child", "pid", "process_identity"),
        ("watcher", "watcher_pid", "watcher_process_identity"),
    ):
        pid = meta.get(pid_key)
        running, source = process_is_recorded_instance(pid, meta.get(identity_key))
        if running:
            alive.append({"role": role, "pid": pid, "evidence": source})
    return alive


def acquire_run_ownership(task_dir: Path):
    directory = runner_dir(task_dir)
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / "ownership.lock").open("a+")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def require_no_live_run(task_dir: Path, launch_token: str | None = None) -> None:
    """Refuse metadata replacement while an identity-bound run is live."""
    meta = read_json(runner_meta_path(task_dir))
    pending = meta.get("launch_pending")
    if isinstance(pending, dict) and pending.get("token") != launch_token:
        raise SystemExit(
            f"Refusing to start a second run for {task_dir.name}: a watcher launch "
            "is still pending. Wait for startup or stop it before retrying."
        )
    if (
        not runner_pid_namespace_visible(meta)
        and not meta.get("finished_at")
        and any(isinstance(meta.get(key), int) for key in ("pid", "watcher_pid"))
    ):
        raise SystemExit(
            f"Refusing to start a second run for {task_dir.name}: this process is in "
            "a different PID namespace and cannot establish that the recorded host "
            "child and watcher are dead. Retry from the host supervision context."
        )
    alive = live_run_processes(task_dir)
    if not alive:
        return
    rendered = ", ".join(
        f"{item['role']} pid {item['pid']} ({item['evidence']})" for item in alive
    )
    raise SystemExit(
        f"Refusing to start a second run for {task_dir.name}: {rendered} is still "
        "running under the recorded kernel identity. Stop or reattach it first."
    )


def host_systemd_scope_available() -> bool:
    """Whether this PID namespace can reach a host systemd manager."""
    try:
        init_name = Path("/proc/1/comm").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return False
    return (
        init_name == "systemd"
        and Path("/run/systemd/system").is_dir()
        and shutil.which("systemd-run") is not None
    )


def watcher_supervision_boundary(task_dir: Path, launch_token: str) -> tuple[list[str], dict]:
    """Give the detached watcher an independent cgroup when the host can."""
    if not host_systemd_scope_available():
        return [], {
            "mode": "process_session",
            "durability": "caller_cgroup",
            "reason": "host systemd manager is unavailable in this PID namespace",
        }
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_dir.name)[:80].strip("-.")
    unit = f"task-agent-{safe_task}-{launch_token[:12]}.scope"
    return [
        str(shutil.which("systemd-run")),
        "--quiet",
        "--scope",
        "--collect",
        "--description",
        f"Task Agent runner for {task_dir.name}",
        "--unit",
        unit,
    ], {
        "mode": "systemd_scope",
        "durability": "independent_cgroup",
        "unit": unit,
    }


def admission_liveness(candidate: Path) -> bool | None:
    """Tri-state supervision answer shared by claim and public observation."""
    meta = read_json(runner_meta_path(candidate))
    if (
        not runner_pid_namespace_visible(meta)
        and not meta.get("finished_at")
        and any(isinstance(meta.get(key), int) for key in ("pid", "watcher_pid"))
    ):
        return None
    return bool(live_run_processes(candidate))


def claim_write_admission(task_dir: Path, repository: Path, run_id: str) -> dict:
    """Atomically claim a write-mode launch in a Git repository.

    Liveness is asked of the same supervision this project already uses, so a
    host that can only offer PID-only evidence still blocks a concurrent write
    rather than admitting one.
    """

    claim, blockers = write_admission.claim_write_scope(
        tasks_root=task_dir.parent,
        task_dir=task_dir,
        repository=repository,
        run_id=run_id,
        is_live=admission_liveness,
    )
    if not blockers and claim is not None:
        return claim
    rendered = "; ".join(
        f"{Path(item['task']).name}: {item['reason']} ({item['detail']})"
        for item in blockers
    )
    raise SystemExit(
        f"Refusing to write {repository} from {task_dir.name}: {rendered}."
    )


def claim_write_admissions(
    task_dir: Path, repositories: list[Path], run_id: str
) -> list[dict]:
    claims, blockers = write_admission.claim_write_scopes(
        tasks_root=task_dir.parent,
        task_dir=task_dir,
        repositories=repositories,
        run_id=run_id,
        is_live=admission_liveness,
    )
    if not blockers:
        return claims
    rendered = "; ".join(
        f"{Path(item['task']).name}: {item['reason']} ({item['detail']})"
        for item in blockers
    )
    raise SystemExit(
        "Refusing the exact repository set from "
        f"{task_dir.name}: {rendered}. No write scope was opened."
    )


def report_review_admission_refusal(
    task_dir: Path, record: dict, args: argparse.Namespace
) -> dict:
    """Tell whoever asked for this launch that it was refused.

    The refusal already exists in the task's own state, but state is something
    a caller has to go and look at. This gate only pays for itself if the
    person who asked for a material launch hears, before any author work, that
    nobody could review it. Only the message travels: the decision was made
    before this call, and neither an absent transport nor a broken one can turn
    the refusal back into a launch.
    """
    parts = review_admission.refusal_notification(record)
    try:
        delivered, detail = try_send_pipeline_stop_message(
            task_dir=task_dir,
            summary=parts["summary"],
            requested_action=parts["requested_action"],
            artifact_paths=[status_path(task_dir), trace_path(task_dir)],
            destination=getattr(args, "destination", None),
            application=getattr(args, "application", None),
            workflow=args.workflow,
        )
    except Exception as exc:  # a failed transport must not hide the refusal
        delivered, detail = False, f"delivery raised {type(exc).__name__}: {exc}"
    return {
        "kind": "pipeline_stopped",
        "delivered": bool(delivered),
        "detail": detail,
        "trace": (
            f"Delivered the review-admission refusal to the caller: {detail}"
            if delivered
            else f"Could not deliver the review-admission refusal: {detail}"
        ),
    }


def configured_assurance(args: argparse.Namespace) -> dict | None:
    """The assurance configuration this launch would hand to the dev-pipeline core.

    Read here, at the launcher, because this is where the reviewer was bound and
    where a disagreement between the two is still free to fix. An unreadable file
    is reported as no assurance at all: a material launch is then refused for the
    same reason a missing one is, which is the honest reading of "the review this
    was admitted with will not happen".
    """
    configured = getattr(args, "assurance_config", None)
    if not configured:
        return None
    value = read_json(Path(configured))
    return value or None


def cmd_start(args: argparse.Namespace) -> None:
    root = repo_root()
    task_dir = resolve_task_dir(args.task_dir)
    ownership_lock = acquire_run_ownership(task_dir)
    require_no_live_run(task_dir)
    ensure_task_contract(task_dir)
    if getattr(args, "require_review_verdict", False):
        try:
            require_review_verdict_contract(task_dir)
        except ValueError as exc:
            raise SystemExit(f"Cannot prepare review verdict contract: {exc}") from None
    # Resolve once, here, while this process is still a direct descendant of the
    # parent CLI. The detached watcher receives the decision and never re-detects.
    args.runner, runner_resolution = resolve_runner(args.runner)
    resolved_sandbox_mode = resolve_sandbox_mode(
        args.runner,
        args.workflow,
        getattr(args, "sandbox_mode", None),
    )
    access_directories, access_grant = prepare_access_grant(
        args.runner,
        resolved_sandbox_mode,
        getattr(args, "repo", None),
        require_git_worktree=bool(getattr(args, "require_git_worktree", False)),
    )
    # Decide the reviewer before the author exists. A material launch that nobody
    # independent can check is cheapest to stop here: after the author runs, the
    # same missing reviewer costs the whole attempt. The same call admits the
    # other half of the pair: a launch asked for a verdict is the review, and it
    # has to be the family this number was promised.
    #
    # Deciding is not binding. The admission says which family authored this
    # number's work, so it is committed further down, where the author actually
    # starts -- everything between here and there can still refuse, and a launch
    # that ran nothing must not be found later as this number's latest author.
    # `committing` is about this launch's own records, not about the binding: a
    # dry run is evaluated and refused identically and writes neither its refusal
    # nor the outage number another number owes.
    committing = not args.dry_run
    try:
        review_record = review_admission.admit_launch(
            task_dir,
            workflow=args.workflow,
            author_runner=args.runner,
            access_grant=access_grant,
            contract=load_task_contract(task_dir),
            declared_reviewer=getattr(args, "reviewer_runner", None),
            review_launch=bool(getattr(args, "require_review_verdict", False)),
            assurance=configured_assurance(args),
            persist=committing,
        )
    except review_admission.ReviewAdmissionError as exc:
        # A refused preparation still reports: being told before an author runs
        # is what preparing a launch is for, and a refusal binds nobody. Only the
        # binding and the outage number are withheld from a run that never began.
        notification = report_review_admission_refusal(task_dir, exc.record, args)
        write_status(
            task_dir,
            "blocked",
            exc.record["message"],
            {
                "runner": args.runner,
                "workflow": args.workflow,
                "review_admission": {**exc.record, "notification": notification},
            },
        )
        append_trace(task_dir, exc.record["message"])
        append_trace(task_dir, notification["trace"])
        ownership_lock.close()
        raise SystemExit(exc.record["message"]) from None
    append_trace(
        task_dir,
        review_record["message"]
        if committing
        else (
            "Dry run evaluated this launch without committing its admission: "
            + review_record["message"]
        ),
    )
    admission_receipt: dict | None = None

    try:
        application_launch = prepared_application_launch(args, task_dir)
    except ApplicationAdapterError as exc:
        raise SystemExit(f"Application launch policy refused the run: {exc}") from None

    workflow_command = build_workflow_command(
        args.workflow,
        args.runner,
        task_dir,
        resolved_sandbox_mode,
        getattr(args, "model", None),
        **dev_pipeline_options(args),
    )
    if args.workflow == "standard":
        repository = access_directories if access_directories else None
        review_subject, review_author = review_prompt_identity(
            task_dir, review_record, bool(getattr(args, "require_review_verdict", False))
        )
        prompt = build_child_prompt(
            task_dir,
            repository=repository,
            review_subject=review_subject,
            review_subject_author=review_author,
            require_review_verdict=bool(getattr(args, "require_review_verdict", False)),
            product_review_packet=(
                Path(args.product_review_packet)
                if getattr(args, "product_review_packet", None)
                else None
            ),
        )
        runner_prompt_path(task_dir).write_text(prompt, encoding="utf-8")
    else:
        runner_prompt_path(task_dir).write_text(
            f"Workflow `{args.workflow}` is executed by a dedicated runner script.\n",
            encoding="utf-8",
        )
        workflow_meta = {
            "workflow": args.workflow,
            "sandbox_mode": resolved_sandbox_mode,
            **dev_pipeline_options(args),
        }
        write_json(runner_workflow_path(task_dir), workflow_meta)

    append_trace(
        task_dir,
        f"Parent agent prepared child run with runner `{args.runner}` "
        f"(resolved by {runner_resolution}) and workflow `{args.workflow}`.",
    )
    write_status(
        task_dir,
        "running",
        f"Starting child agent via {args.runner} ({args.workflow})",
        {
            "runner": args.runner,
            "runner_resolution": runner_resolution,
            "workflow": args.workflow,
            "review_admission": review_record,
        },
    )

    command = workflow_command or build_command(
        args.runner,
        runner_prompt_path(task_dir),
        root,
        args.model,
        resolved_sandbox_mode,
        task_dir,
        command_access_directories(access_directories, access_grant),
        application_launch.get("standard_session", {}).get("command_arguments", []),
    )
    meta = {
        "runner": args.runner,
        "runner_resolution": runner_resolution,
        "workflow": args.workflow,
        "started_at": utc_now(),
        "task_dir": str(task_dir),
        "prompt_path": str(runner_prompt_path(task_dir)),
        "log_path": str(runner_log_path(task_dir)),
        "command": redact_sensitive_arguments(command),
        "application": application_launch,
        "access_grant": access_grant,
        "review_admission": review_record,
        "progress_baseline": observe_progress_state(task_dir),
        "pid_namespace": pid_namespace_identity(),
    }
    if resolved_sandbox_mode:
        meta["sandbox_mode"] = resolved_sandbox_mode
    destination = getattr(args, "destination", None)
    if destination:
        meta["destination_binding"] = hashlib.sha256(destination.encode()).hexdigest()[:12]

    # Preserve the exact previous run's terminal write-scope evidence before
    # either a dry run or a real start replaces the single-current-run metadata
    # file. The admission ledger is append-only, so a later real watcher can
    # consume this evidence instead of the owner destroying its own recovery.
    write_admission.preserve_terminal_scope_evidence(
        task_dir, read_json(runner_meta_path(task_dir))
    )

    if args.dry_run:
        meta["dry_run"] = True
        write_json(runner_meta_path(task_dir), meta)
        append_trace(task_dir, "Dry run prepared prompt and runner metadata without launching a child process.")
        write_status(task_dir, "ready", f"Prepared child run via {args.runner}", {"runner": args.runner})
        ownership_lock.close()
        print(json.dumps(meta, indent=2))
        return

    if getattr(args, "foreground", False):
        # Application-managed workers already have a durable outer supervisor.
        # Keep the ordinary admission, prompt, child supervision, review-round,
        # and completion owners, but do not detach a watcher whose container
        # would end as soon as this command returned.
        launch_token = uuid.uuid4().hex
        meta["supervision_boundary"] = {
            "mode": "foreground_process",
            "durability": "caller_owned",
        }
        meta["launch_pending"] = {"token": launch_token, "started_at": utc_now()}
        write_json(runner_meta_path(task_dir), meta)
        review_admission.commit_admission(
            task_dir, review_record, launch_token=launch_token
        )
        foreground_args = argparse.Namespace(
            **{
                **vars(args),
                "launch_token": launch_token,
                "runner_resolution": runner_resolution,
                "repo": [str(path) for path in access_directories]
                if access_directories and args.workflow == "standard"
                else getattr(args, "repo", None),
            }
        )
        append_trace(
            task_dir,
            "Running the admitted child in the foreground under the caller-owned "
            "application supervision boundary.",
        )
        try:
            cmd_run_child(foreground_args)
        finally:
            current_meta = read_json(runner_meta_path(task_dir))
            current_meta.pop("launch_pending", None)
            write_json(runner_meta_path(task_dir), current_meta)
            ownership_lock.close()
        return

    launch_token = uuid.uuid4().hex
    scope_prefix, supervision_boundary = watcher_supervision_boundary(
        task_dir, launch_token
    )
    meta["supervision_boundary"] = supervision_boundary
    meta["launch_pending"] = {
        "token": launch_token,
        "started_at": utc_now(),
        **({"unit": supervision_boundary["unit"]} if "unit" in supervision_boundary else {}),
    }
    write_json(runner_meta_path(task_dir), meta)

    watcher_command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_run-child",
        str(task_dir),
        "--runner",
        args.runner,
        "--runner-resolution",
        runner_resolution,
        "--workflow",
        args.workflow,
        "--launch-token",
        launch_token,
    ]
    if args.model:
        watcher_command.extend(["--model", args.model])
    if resolved_sandbox_mode:
        watcher_command.extend(["--sandbox-mode", resolved_sandbox_mode])
    if getattr(args, "require_git_worktree", False):
        watcher_command.append("--require-git-worktree")
    if access_directories and args.workflow == "standard":
        for repository in access_directories:
            watcher_command.extend(["--repo", str(repository)])
    for name, value in watcher_options(args).items():
        watcher_command.extend([f"--{name.replace('_', '-')}", str(value)])
    if getattr(args, "memory_limit", None) is not None:
        watcher_command.extend(["--memory-limit", str(args.memory_limit)])

    def abort_start(detail: str, meta_extra: dict | None = None) -> None:
        """Fail the start without leaving the task claiming to be running.

        Every process boundary here can refuse: the parent may fail to spawn the
        watcher, the watcher may fail to spawn the child, and the watcher may
        emit something that is not a startup record. A refusal at any of them is
        a failure, not ongoing work, so none of them may leave `running` behind.
        The watcher records its own terminal state when it can; this is the
        parent's backstop for when it could not.

        None of these started an author, so none of them may leave this number's
        review binding behind either: while the commitment made just above is
        still outstanding it is withdrawn here, and the pair the number had
        before this launch stands. The commitment is read from the task by launch
        token rather than carried in this closure, because this process is not
        the only one that can reach this outcome and may not be alive when it is
        reached -- and because a child that did start has already ended the
        commitment, so a refusal to read its startup record does not take the
        binding away from work that exists.
        """
        withdrawn = review_admission.annul_admission(
            task_dir, reason=detail, launch_token=launch_token
        )
        if withdrawn:
            append_trace(task_dir, withdrawn["statement"])
        terminal_extra = dict(meta_extra or {})
        current_meta = read_json(runner_meta_path(task_dir))
        terminal_extra.setdefault("launch_error", detail)
        terminal_extra.setdefault("finished_at", current_meta.get("finished_at") or utc_now())
        terminal_extra.setdefault(
            "outcome", current_meta.get("outcome") or "watcher_failed_before_start"
        )
        finish_runner_meta(task_dir, terminal_extra)
        if read_json(status_path(task_dir)).get("state") not in {"completed", "failed", "blocked"}:
            write_status(
                task_dir,
                "failed",
                detail,
                {"runner": args.runner, "workflow": args.workflow},
            )
            append_trace(task_dir, detail)
        ownership_lock.close()
        raise SystemExit(detail)

    # The last act before the launch becomes real, and the only thing that makes
    # this launch the author of this number. Everything above it -- the
    # application launch policy, the workflow command, the prompt, the runner
    # metadata -- can still refuse, and every one of those refusals used to leave
    # a launch that started nothing recorded as this number's latest author:
    # enough to lock the bound reviewer out as "the author's own family" and let
    # the family that actually wrote the work review and close it.
    #
    # It is committed before the spawn rather than after the startup handshake
    # because the watcher reads this record to decide which phase it enters, so
    # the commitment is outstanding until an author is actually started: while it
    # is, the binding it made is not this number's binding, and no process has to
    # be alive for that to be the answer. The refusals that live past this line
    # -- a watcher that cannot be spawned, a watcher that refuses before its
    # child, a startup record that never arrives -- reach `abort_start` here or
    # `report_launch_failure` in the watcher, and either withdraws it explicitly.
    review_admission.commit_admission(
        task_dir, review_record, launch_token=launch_token
    )

    try:
        process = subprocess.Popen(
            [*scope_prefix, *watcher_command],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
    except OSError as exc:
        abort_start(
            f"Parent agent could not start the task watcher: {exc}",
            {
                "launch_error": str(exc),
                "finished_at": utc_now(),
                "outcome": "watcher_failed_to_launch",
            },
        )

    startup_line = ""
    if process.stdout is not None:
        startup_line = process.stdout.readline().strip()
        process.stdout.close()

    if not startup_line:
        process.wait(timeout=5)
        abort_start("Child runner failed to report startup metadata.")

    try:
        startup_meta = json.loads(startup_line)
    except json.JSONDecodeError:
        process.wait(timeout=5)
        abort_start(f"Child runner emitted unparsable startup output: {startup_line[:200]}")
    if not isinstance(startup_meta, dict) or not startup_meta.get("ok"):
        detail = "Child runner failed before launch."
        if isinstance(startup_meta, dict):
            detail = startup_meta.get("error") or detail
        abort_start(detail)
    required_startup_fields = (
        "watcher_pid",
        "pid",
        "child_started_at",
        "process_identity",
        "watcher_process_identity",
    )
    if not all(key in startup_meta for key in required_startup_fields):
        abort_start("Child runner startup record is missing process identity fields.")

    # The identity values may legitimately be null on a host without `/proc`;
    # the fields must still be present so the record says which host it was.
    meta.update({key: startup_meta[key] for key in required_startup_fields})
    meta.pop("launch_pending", None)
    # The watcher has been writing to this same file since it started. Replacing
    # it wholesale from the parent's older copy would silently drop whatever the
    # watcher recorded in between — the open Git write scope, among others.
    persisted = read_json(runner_meta_path(task_dir))
    persisted.pop("launch_pending", None)
    persisted.update(meta)
    meta = persisted
    write_json(runner_meta_path(task_dir), meta)
    append_trace(task_dir, f"Child process started with pid {startup_meta['pid']}.")
    ownership_lock.close()

    print(json.dumps(meta, indent=2))


def cmd_review(args: argparse.Namespace) -> None:
    """Run the reviewer already bound to this task's material author."""
    task_dir = resolve_task_dir(args.task_dir)
    admission = review_admission.bound_author_admission(task_dir)
    pair = admission.get("pair") if isinstance(admission, dict) else None
    reviewer = pair.get("reviewer_runner") if isinstance(pair, dict) else None
    if reviewer not in review_admission.REVIEW_RUNNERS:
        raise SystemExit(
            "This task has no bound reviewer from a started material author launch."
        )
    access_profile = admission.get("access_profile")
    targets = (
        access_profile.get("target_repositories")
        if isinstance(access_profile, dict)
        else None
    )
    valid_targets = isinstance(targets, list) and not any(
        not isinstance(value, str) or not value.strip() for value in targets
    )
    grants_write = (
        access_profile.get("grants_write")
        if isinstance(access_profile, dict)
        else None
    )
    if (
        not isinstance(access_profile, dict)
        or access_profile.get("role") != "author"
        or not valid_targets
        or not isinstance(grants_write, bool)
        or grants_write != bool(targets)
    ):
        raise SystemExit(
            "The bound author admission has an invalid target profile. Refusing "
            "to guess a repository for the reviewer."
        )
    args.runner = reviewer
    args.workflow = "standard"
    args.require_review_verdict = True
    args.reviewer_runner = None
    args.sandbox_mode = "read-only"
    args.repo = list(targets)
    args.operation = "start"
    cmd_start(args)


def cmd_author(args: argparse.Namespace) -> None:
    """Run the standard author with its fixed, verified write profile."""
    if not getattr(args, "repo", None):
        raise SystemExit(
            "The author role requires at least one exact --repo target; refusing "
            "to launch with an undefined work result."
        )
    args.workflow = "standard"
    args.require_review_verdict = False
    args.sandbox_mode = "workspace-write"
    args.require_git_worktree = True
    args.operation = "start"
    cmd_start(args)


def cmd_product_review(args: argparse.Namespace) -> None:
    """Run a staged product acceptance through the existing review owner."""
    task_dir = resolve_task_dir(args.task_dir)
    packet = Path(args.packet).expanduser().resolve()
    try:
        packet.relative_to(task_dir.resolve())
    except ValueError:
        raise SystemExit("The product-review packet must be inside the task directory.") from None
    if not packet.is_file() or packet.stat().st_size == 0:
        raise SystemExit(f"Product-review packet is missing or empty: {packet}")
    args.product_review_packet = str(packet)
    cmd_review(args)


def record_terminal_phase(task_dir: Path, state: str) -> None:
    """Close the phase for a terminal state, whoever wrote that state.

    `write_status` handles the states this runner writes itself. This covers the
    other source: a standard child that maintains its own `status.json` and has
    no notion of phases at all.
    """
    phase = task_phases.phase_for_state(state)
    if not phase:
        return
    task_phases.record_phase(task_dir, phase, cause={"source": "child", "state": state})
    status = read_json(status_path(task_dir))
    if status:
        status["phase"] = task_phases.current_phase(task_dir)
        write_json(status_path(task_dir), status)


def apply_terminal_scope_cleanup(
    task_dir: Path,
    state: str | None,
    *,
    runner: str | None,
    workflow: str | None,
    recovered: bool = False,
) -> str | None:
    """Drain this run's scope before a child-written completion is accepted."""
    label = "Recovered terminal" if recovered else "Terminal"
    scope_cleanup = task_workspace.drain_task_scope(
        read_json(runner_meta_path(task_dir))
    )
    update_runner_meta(task_dir, {"scope_cleanup": scope_cleanup})
    append_trace(
        task_dir,
        f"{label} scope cleanup: "
        f"{scope_cleanup.get('outcome')} ({scope_cleanup.get('reason')}).",
    )
    if scope_cleanup.get("outcome") not in {
        "cleared",
        "not_applicable",
    }:
        detail = (
            "Refused completion because the task cgroup could not be proven empty: "
            f"{scope_cleanup.get('reason')}."
        )
        try:
            block_task_metadata(task_dir)
        except RuntimeError as exc:
            detail = f"{detail} Metadata reconciliation also failed: {exc}"
        write_status(
            task_dir,
            "blocked",
            detail,
            {"runner": runner, "workflow": workflow, "scope_cleanup": scope_cleanup},
        )
        append_trace(task_dir, detail)
        return "blocked"
    return state


def apply_terminal_workspace_cleanup(
    task_dir: Path, state: str | None, *, recovered: bool = False
) -> str | None:
    """Clean the recorded target only after durable completion is accepted."""
    if state != "completed":
        return state
    label = "Recovered terminal" if recovered else "Terminal"
    workspace_cleanup = task_workspace.cleanup_workspace(
        task_dir, read_json(runner_meta_path(task_dir))
    )
    update_runner_meta(task_dir, {"workspace_cleanup": workspace_cleanup})
    append_trace(
        task_dir,
        f"{label} workspace cleanup: "
        f"{workspace_cleanup.get('outcome')} ({workspace_cleanup.get('reason')})"
        + (
            f" for {workspace_cleanup['path']}."
            if workspace_cleanup.get("path")
            else "."
        ),
    )
    return state


def record_standard_review_round(task_dir: Path, runner: str) -> dict | None:
    """Append the round a finished standard review just decided.

    A `dev-pipeline` review reaches the round ledger as a lifecycle event the
    adapter projects. A standard review publishes its decision as the one
    canonical `Verdict:` line the contract already requires of it, and without
    this the same decision would never reach the ledger the acceptance gate
    reads -- an approved review would leave the task blocked, and a rework
    verdict would close it.

    The round is keyed on this run, so finalizing twice records one round.
    """
    admission = review_admission.recorded_admission(task_dir)
    classification = admission.get("classification")
    work_class = (
        classification.get("work_class") if isinstance(classification, dict) else None
    )
    if work_class != review_admission.REVIEW:
        return None
    verdict = published_review_verdict(task_dir)
    if verdict is None:
        # The completion gate already refuses a review that published no single
        # verdict. Recording an unreadable round would put a decision nobody made
        # into the ledger the next round is measured against.
        return None
    meta = read_json(runner_meta_path(task_dir))
    run_key = (
        meta.get("write_scope_run_id")
        or meta.get("child_started_at")
        or admission.get("evaluated_at")
        or utc_now()
    )
    entry = review_admission.record_review_round(
        task_dir,
        event_id=f"standard-review:{run_key}",
        decision={"decision": verdict},
        review_provider=runner,
    )
    append_trace(
        task_dir,
        f"Recorded review round {entry['round']} from the {runner} reviewer's "
        f"published verdict `{verdict}`.",
    )
    if entry.get("warning"):
        append_trace(task_dir, entry["warning"])
    return entry


def finalize_child_lifecycle(
    task_dir: Path,
    workflow: str,
    runner: str,
    return_code: int,
    destination: str | None = None,
) -> None:
    """Make a child that died without finishing say so in the task artifacts.

    A child that exits non-zero before writing a terminal state would otherwise
    leave `running` behind, which reads as work still in progress.
    """
    review_round = None
    if workflow == "standard":
        metadata = read_json(runner_meta_path(task_dir))
        registration = metadata.get("application")
        application_record = registration if isinstance(registration, dict) else {}
        spec = application_record.get("spec")
        session = application_record.get("standard_session")
        session_record = session if isinstance(session, dict) else {}
        try:
            disposition = load_application(spec).standard_run_finished(
                StandardRunResultV1(
                    task_dir=task_dir,
                    runner=runner,
                    operation=application_record.get("operation", "start"),
                    return_code=return_code,
                    log_path=runner_log_path(task_dir),
                    session_state=session_record.get("state", {}),
                    destination=destination,
                )
            )
            if disposition is not None and not all(
                hasattr(disposition, name) for name in ("state", "current_step", "metadata")
            ):
                raise ApplicationAdapterError(
                    "standard_run_finished must return StandardRunDispositionV1 or None"
                )
            if disposition is not None:
                if disposition.state not in {"waiting_for_quota", "blocked", "failed"}:
                    raise ApplicationAdapterError(
                        "standard run disposition state must be waiting_for_quota, blocked, or failed"
                    )
                extra = json_session_state(disposition.metadata)
                if destination and destination in json.dumps(extra, sort_keys=True):
                    raise ApplicationAdapterError(
                        "standard run disposition must not contain the raw destination"
                    )
                write_status(
                    task_dir,
                    disposition.state,
                    disposition.current_step,
                    {"runner": runner, "workflow": workflow, "exit_code": return_code, **extra},
                )
                append_trace(
                    task_dir,
                    f"Application handled the standard run as `{disposition.state}`: "
                    f"{disposition.current_step}",
                )
                try:
                    block_task_metadata(task_dir)
                except RuntimeError as exc:
                    append_trace(
                        task_dir,
                        f"Could not reconcile unfinished task metadata: {exc}",
                    )
                return
        except (ApplicationAdapterError, OSError, TypeError, ValueError) as exc:
            write_status(
                task_dir,
                "failed",
                f"Application could not classify the completed standard run: {exc}",
                {"runner": runner, "workflow": workflow, "exit_code": return_code},
            )
            append_trace(task_dir, f"Application standard-run classification failed: {exc}")
            try:
                block_task_metadata(task_dir)
            except RuntimeError as metadata_exc:
                append_trace(
                    task_dir,
                    f"Could not reconcile failed task metadata: {metadata_exc}",
                )
            return
        # A quota pause returns above without depositing a review round. Once
        # the application confirms the run reached its own end, a clean
        # reviewer decision belongs in the ledger before completion reads it.
        if return_code == 0:
            review_round = record_standard_review_round(task_dir, runner)
        # An approved review owns the task's terminal bookkeeping. Check every
        # other gate while allowing the expected pre-terminal metadata state,
        # then use the canonical metadata owner and verify the full predicate.
        scope_cleanup = metadata.get("scope_cleanup")
        scope_cleanup_refused = isinstance(scope_cleanup, dict) and scope_cleanup.get(
            "outcome"
        ) not in {"cleared", "not_applicable"}
        if (
            review_round
            and review_round.get("decision") == "approved"
            and not scope_cleanup_refused
        ):
            ready, reason = completion_ready(
                task_dir, workflow=workflow, defer_task_status=True
            )
            if ready:
                try:
                    complete_task_metadata(task_dir)
                    ready, reason = completion_ready(task_dir, workflow=workflow)
                except RuntimeError as exc:
                    ready, reason = False, str(exc)
                if ready:
                    write_status(
                        task_dir,
                        "completed",
                        "Independent review approved and all completion gates passed",
                        {
                            "runner": runner,
                            "workflow": workflow,
                            "exit_code": return_code,
                            "review_round": review_round.get("round"),
                            "review_result": "approved",
                        },
                    )
                    append_trace(
                        task_dir,
                        "Approved independent review completed canonical task metadata; "
                        "all completion gates passed.",
                    )

    task_state = read_json(status_path(task_dir)).get("state")
    if task_state in {"completed", "failed", "blocked"}:
        # The child recorded its own runtime state, but canonical metadata and
        # the phase still have to agree with it. A child writes `status.json`
        # itself and knows nothing about phases, so a run that ended well would
        # otherwise stay in the phase it was working in, and a blocked run could
        # remain hidden behind premature `completed` frontmatter.
        if task_state == "completed":
            ready, reason = completion_ready(task_dir, workflow=workflow)
            if not ready:
                refusal = completion_refusal(task_dir, reason)
                try:
                    block_task_metadata(task_dir)
                except RuntimeError as exc:
                    refusal = completion_refusal(task_dir, f"{reason}; {exc}")
                write_status(
                    task_dir,
                    "blocked",
                    refusal["summary"],
                    {
                        "runner": runner,
                        "workflow": workflow,
                        "exit_code": return_code,
                        "completion_refusal": refusal,
                    },
                )
                append_trace(task_dir, refusal["summary"])
                return
            write_admission.record_completion_acceptance(task_dir)
        else:
            try:
                block_task_metadata(task_dir)
            except RuntimeError as exc:
                append_trace(
                    task_dir,
                    f"Could not reconcile {task_state} task metadata: {exc}",
                )
        record_terminal_phase(task_dir, task_state)
        return
    if workflow == "dev-pipeline" and task_state == "waiting":
        # A validated lifecycle event, not the adapter process exit, owns this
        # durable pause. The registered installation has already been offered
        # the event and may arm its scheduler; do not rewrite the pause as a
        # missing terminal event after the adapter exits.
        return
    if return_code == 0 and workflow != "dev-pipeline":
        ready, reason = completion_ready(task_dir, workflow=workflow)
        if ready:
            write_admission.record_completion_acceptance(task_dir)
            return
        refusal = completion_refusal(task_dir, reason)
        try:
            block_task_metadata(task_dir)
        except RuntimeError as exc:
            refusal = completion_refusal(task_dir, f"{reason}; {exc}")
        write_status(
            task_dir,
            "blocked",
            refusal["summary"],
            {
                "runner": runner,
                "workflow": workflow,
                "exit_code": return_code,
                "completion_refusal": refusal,
            },
        )
        append_trace(task_dir, refusal["summary"])
        return
    if workflow == "dev-pipeline":
        # The dev-pipeline workflow states its own outcome through lifecycle
        # events. A clean subprocess exit only means the adapter stopped
        # reading, so treating it as success would invent an outcome nobody
        # reported.
        detail = "Dev-pipeline process exited without a terminal lifecycle event"
        trace_detail = (
            "Rejected the dev-pipeline process exit as completion because no terminal "
            "lifecycle event was projected."
        )
    else:
        detail = f"Child process exited unsuccessfully with code {return_code}"
        trace_detail = (
            f"Recorded terminal failed status after the {workflow} child exited with code "
            f"{return_code} before finalizing task artifacts."
        )
    write_status(
        task_dir,
        "failed",
        detail,
        {"runner": runner, "workflow": workflow, "exit_code": return_code},
    )
    try:
        block_task_metadata(task_dir)
    except RuntimeError as exc:
        append_trace(task_dir, f"Could not reconcile failed task metadata: {exc}")
    append_trace(task_dir, trace_detail)


def report_launch_failure(task_dir: Path, args: argparse.Namespace, exc: Exception) -> None:
    """Turn any pre-launch failure into one truthful startup record.

    Everything before the child exists shares this path, because a refusal that
    leaves the task reading `running` is worse than the refusal it reports.

    This is the watcher's own end of the launch, and the watcher is supervised
    independently of the parent that committed the review binding. Reaching here
    means no author of this launch exists, so the binding is withdrawn before the
    pending claim is released -- the parent may be gone, and releasing the claim
    while a launch that started nobody is on record as this number's latest
    author is precisely how the bound reviewer loses the work to the family that
    wrote it. A launch token that does not match the outstanding commitment
    withdraws nothing: this failure is then not the one that ended it.
    """
    launch_token = getattr(args, "launch_token", None)
    if launch_token is not None:
        withdrawn = review_admission.annul_admission(
            task_dir,
            reason=f"the watcher failed before starting a child: {exc}",
            launch_token=launch_token,
        )
        if withdrawn:
            append_trace(task_dir, withdrawn["statement"])
    finish_runner_meta(
        task_dir,
        {
            "launch_error": str(exc),
            "finished_at": utc_now(),
            "outcome": "failed_to_launch",
        },
    )
    write_status(
        task_dir,
        "failed",
        f"Child failed to launch: {exc}",
        {
            "runner": getattr(args, "runner", None),
            "workflow": getattr(args, "workflow", None),
        },
    )
    append_trace(task_dir, f"Child failed to launch before starting: {exc}")
    print(json.dumps({"ok": False, "error": str(exc)}), flush=True)


def cmd_run_child(args: argparse.Namespace) -> None:
    root = repo_root()
    task_dir = resolve_task_dir(args.task_dir)
    launch_token = getattr(args, "launch_token", None)
    direct_lock = None
    if launch_token is None:
        direct_lock = acquire_run_ownership(task_dir)
    require_no_live_run(task_dir, launch_token)
    ensure_task_contract(task_dir)

    if launch_token is not None:
        meta = read_json(runner_meta_path(task_dir))
        pending = meta.get("launch_pending")
        if not isinstance(pending, dict) or pending.get("token") != launch_token:
            raise SystemExit("Watcher launch token does not match the pending launch claim.")

    # Preflight and command construction can both refuse to proceed. They run
    # inside the guarded block so a refusal reaches the parent as structured
    # startup output rather than as a traceback the parent cannot parse.
    try:
        resolved_sandbox_mode = resolve_sandbox_mode(
            args.runner,
            args.workflow,
            getattr(args, "sandbox_mode", None),
        )
        access_directories, access_grant = prepare_access_grant(
            args.runner,
            resolved_sandbox_mode,
            getattr(args, "repo", None),
            require_git_worktree=bool(getattr(args, "require_git_worktree", False)),
        )
        application_launch = prepared_application_launch(args, task_dir)
        workflow_command = build_workflow_command(
            args.workflow,
            args.runner,
            task_dir,
            resolved_sandbox_mode,
            getattr(args, "model", None),
            **dev_pipeline_options(args),
        )
        command = workflow_command or build_command(
            args.runner,
            runner_prompt_path(task_dir),
            root,
            args.model,
            resolved_sandbox_mode,
            task_dir,
            command_access_directories(access_directories, access_grant),
            application_launch.get("standard_session", {}).get("command_arguments", []),
        )
        if args.runner == "claude" and args.workflow == "standard":
            require_claude_sandbox_dependencies(resolved_sandbox_mode or "workspace-write")
            require_safe_claude_project_settings(
                root, resolved_sandbox_mode or "workspace-write"
            )
        write_targets = (
            access_directories
            if access_grant.get("grants_write") and access_directories
            else []
        )
    except (Exception, SystemExit) as exc:
        report_launch_failure(task_dir, args, exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
        raise SystemExit(1)

    update_runner_meta(
        task_dir,
        {
            "runner": args.runner,
            "runner_resolution": getattr(args, "runner_resolution", None) or "inherited",
            "workflow": args.workflow,
            "task_dir": str(task_dir),
            "prompt_path": str(runner_prompt_path(task_dir)),
            "log_path": str(runner_log_path(task_dir)),
            "command": redact_sensitive_arguments(command),
            "application": application_launch,
            "access_grant": access_grant,
            **(
                {"pid_namespace": pid_namespace_identity()}
                if not read_json(runner_meta_path(task_dir)).get("pid_namespace")
                else {}
            ),
            **(
                {"destination_binding": hashlib.sha256(args.destination.encode()).hexdigest()[:12]}
                if getattr(args, "destination", None)
                else {}
            ),
            "watcher_pid": os.getpid(),
            "watcher_process_identity": process_identity(os.getpid()),
            "watcher_started_at": utc_now(),
        },
    )

    # A `standard` run publishes no lifecycle events, so the phase it enters is
    # recorded here. A `dev-pipeline` run's phases come from the events its
    # owner emits, and inventing one now would contradict the first of them.
    if args.workflow == "standard":
        # What this launch was admitted as, not what the contract still asks of
        # the task: a review leaves `require_review_verdict` in the contract for
        # good, so reading the contract would turn every later author run into
        # another review and erase the rework the review asked for.
        entering = task_phases.phase_for_standard_start(
            task_dir,
            require_review_verdict=review_admission.launch_is_review(task_dir)
            or (
                not review_admission.recorded_admission(task_dir)
                and (
                    bool(getattr(args, "require_review_verdict", False))
                    or enforced_review_verdict(load_task_contract(task_dir)) is not None
                )
            ),
        )
        task_phases.record_phase(
            task_dir,
            entering,
            cause={"source": "task-runner", "workflow": args.workflow, "runner": args.runner},
        )
        append_trace(task_dir, f"Task entered the `{entering}` phase.")

    write_scope_run_id = None
    if write_targets:
        write_scope_run_id = launch_token or uuid.uuid4().hex
        try:
            claims = claim_write_admissions(task_dir, write_targets, write_scope_run_id)
            update_runner_meta(task_dir, {
                "write_scope_run_id": write_scope_run_id,
                "write_scope_repositories": [
                    claim["before"]["repository"] for claim in claims
                ],
                "preexisting_tracked_dirty_baselines": {
                    claim["before"]["repository"]:
                    claim["before"]["preexisting_tracked_dirty_baseline"]
                    for claim in claims
                },
            })
        except (Exception, SystemExit) as exc:
            report_launch_failure(
                task_dir,
                args,
                exc if isinstance(exc, Exception) else RuntimeError(str(exc)),
            )
            raise SystemExit(1)

    review_findings_snapshot = None
    removed_verdicts = 0
    try:
        log_handle = runner_log_path(task_dir).open("ab")
        if args.workflow == "standard" and review_admission.launch_is_review(task_dir):
            requirement = enforced_review_verdict(load_task_contract(task_dir)) or {}
            findings_path = task_dir / requirement.get("path", "findings.md")
            if findings_path.is_file():
                review_findings_snapshot = (findings_path, findings_path.read_bytes())
            removed_verdicts = clear_published_review_verdicts(
                task_dir, requirement.get("path", "findings.md")
            )
        process = subprocess.Popen(
            command,
            cwd=root,
            env=child_environment(args.runner),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=child_resource_limiter(application_launch.get("memory_limit_bytes")),
        )
    except Exception as exc:
        if review_findings_snapshot is not None:
            findings_path, prior_bytes = review_findings_snapshot
            try:
                findings_path.write_bytes(prior_bytes)
            except OSError as restore_exc:
                exc = RuntimeError(
                    f"{exc}; could not restore prior canonical review verdict: {restore_exc}"
                )
        report_launch_failure(task_dir, args, exc)
        raise SystemExit(1)

    if removed_verdicts:
        append_trace(
            task_dir,
            f"Cleared {removed_verdicts} prior canonical verdict line(s) before the new "
            "review; round history remains in reviews/rounds.jsonl.",
        )

    child_started_at = utc_now()
    # Read the child's identity now, while it is certainly the process just
    # spawned. Recording it later would risk pinning whatever inherited the PID.
    child_identity = process_identity(process.pid)
    update_runner_meta(
        task_dir,
        {
            "pid": process.pid,
            "process_identity": child_identity,
            "child_started_at": child_started_at,
        },
    )
    # The author of this launch now exists, which is the only event that makes
    # its committed pair this number's binding. It is recorded here, by the
    # process that started the author, rather than at the parent's handshake:
    # the parent may never read that handshake, and the work this child is
    # already free to write must keep the reviewer it was admitted with.
    if launch_token is not None:
        review_admission.confirm_admission(task_dir, launch_token=launch_token)
    if direct_lock is not None:
        direct_lock.close()
    print(
        json.dumps(
            {
                "ok": True,
                "pid": process.pid,
                "watcher_pid": os.getpid(),
                "child_started_at": child_started_at,
                "process_identity": child_identity,
                "watcher_process_identity": process_identity(os.getpid()),
            }
        ),
        flush=True,
    )

    return_code = process.wait()
    apply_terminal_scope_cleanup(
        task_dir,
        read_json(status_path(task_dir)).get("state"),
        runner=args.runner,
        workflow=args.workflow,
    )
    if write_scope_run_id is not None:
        # Close the scope even though the child may have failed: what the run
        # did to the repository is a fact about the repository, not about the
        # exit code. A scope left open here is still recoverable by measurement,
        # but only this process knows the run it belonged to.
        try:
            for repository in write_targets:
                write_admission.close_write_scope(
                    task_dir, write_scope_run_id, repository=repository
                )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            append_trace(task_dir, f"Could not close the Git write scope: {exc}")
    finalize_child_lifecycle(
        task_dir,
        args.workflow,
        args.runner,
        return_code,
        destination=getattr(args, "destination", None),
    )
    terminal_state = apply_terminal_workspace_cleanup(
        task_dir,
        read_json(status_path(task_dir)).get("state"),
    )
    if terminal_state in {"waiting", "waiting_for_quota"}:
        outcome = "waiting_for_quota"
    elif terminal_state == "completed" and return_code == 0:
        outcome = "succeeded"
    elif terminal_state == "blocked" and return_code == 0:
        outcome = "rejected_completion_contract"
    else:
        outcome = "failed"
    update_runner_meta(
        task_dir,
        {
            "exit_code": return_code,
            "finished_at": utc_now(),
            "outcome": outcome,
        },
    )


def cmd_monitor_existing(args: argparse.Namespace) -> None:
    """Watch a child this process did not spawn, until its identity disappears.

    A recovered watcher is not the child's parent, so it cannot wait for an exit
    code. It can only observe that the recorded identity stopped existing, and
    read the terminal state the child was supposed to leave behind.
    """
    task_dir = resolve_task_dir(args.task_dir)
    meta = read_json(runner_meta_path(task_dir))
    if not runner_pid_namespace_visible(meta):
        print(
            json.dumps({"ok": False, "error": "Recorded child is in a different PID namespace"}),
            flush=True,
        )
        return
    pid = meta.get("pid")
    expected = meta.get("process_identity")
    if not isinstance(expected, str) or not expected or not isinstance(pid, int):
        print(
            json.dumps({"ok": False, "error": "No identity-bound child is recorded"}),
            flush=True,
        )
        return
    if process_identity(pid) != expected:
        print(
            json.dumps({"ok": False, "error": "Recorded child identity is not running"}),
            flush=True,
        )
        return

    watcher_identity = process_identity(os.getpid())
    update_runner_meta(
        task_dir,
        {
            "watcher_pid": os.getpid(),
            "watcher_process_identity": watcher_identity,
            "watcher_started_at": utc_now(),
        },
    )
    print(
        json.dumps(
            {
                "ok": True,
                "pid": pid,
                "watcher_pid": os.getpid(),
                "watcher_process_identity": watcher_identity,
            }
        ),
        flush=True,
    )

    interval = max(1, args.poll_interval)
    while process_identity(pid) == expected:
        time.sleep(interval)

    state = apply_terminal_scope_cleanup(
        task_dir,
        read_json(status_path(task_dir)).get("state"),
        runner=meta.get("runner"),
        workflow=meta.get("workflow"),
        recovered=True,
    )
    state = apply_terminal_workspace_cleanup(task_dir, state, recovered=True)
    if state in {"completed", "failed", "blocked"}:
        outcome = f"recovered_{state}"
    else:
        outcome = "recovered_terminal_state_unknown"
        write_status(
            task_dir,
            "failed",
            "Recovered watcher observed the child disappear without a terminal state",
            {
                "runner": meta.get("runner"),
                "workflow": meta.get("workflow"),
            },
        )
        append_trace(
            task_dir,
            "Recovered watcher rejected the child's disappearance as completion "
            "because no terminal state was recorded.",
        )
    update_runner_meta(task_dir, {"finished_at": utc_now(), "outcome": outcome})


def cmd_reattach(args: argparse.Namespace) -> None:
    """Restore supervision of a live child after its watcher was lost."""
    task_dir = resolve_task_dir(args.task_dir)
    ensure_task_contract(task_dir)
    meta = read_json(runner_meta_path(task_dir))
    if not runner_pid_namespace_visible(meta):
        raise SystemExit(
            "Recorded run belongs to a different PID namespace; reattach from "
            "the host supervision context."
        )

    # Reattach fails closed. Its whole value is refusing a child that only looks
    # alive, and without kernel identity a recycled PID is indistinguishable
    # from the original process.
    if not process_identity_available():
        raise SystemExit(
            "Reattach requires kernel process identity, which needs a readable "
            "/proc on this host. Without it a recycled pid cannot be told apart "
            "from the recorded child."
        )
    pid = meta.get("pid")
    expected = meta.get("process_identity")
    if not isinstance(pid, int):
        raise SystemExit("No child process metadata found.")
    if not isinstance(expected, str) or not expected:
        raise SystemExit(
            "The recorded run has no process identity, so a live pid cannot be "
            "attributed to it."
        )
    if process_identity(pid) != expected:
        raise SystemExit(f"Recorded child process identity is not running: {pid}")

    watcher_pid = meta.get("watcher_pid")
    watcher_identity = meta.get("watcher_process_identity")
    if (
        isinstance(watcher_pid, int)
        and isinstance(watcher_identity, str)
        and watcher_identity
        and process_identity(watcher_pid) == watcher_identity
    ):
        raise SystemExit(f"A watcher is already monitoring this child: {watcher_pid}")

    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_monitor-existing",
            str(task_dir),
            "--poll-interval",
            str(args.poll_interval),
        ],
        cwd=repo_root(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )
    startup_line = ""
    if process.stdout is not None:
        startup_line = process.stdout.readline().strip()
        process.stdout.close()
    if not startup_line:
        process.wait(timeout=5)
        raise SystemExit("Recovered watcher failed to report startup metadata.")
    try:
        startup = json.loads(startup_line)
    except json.JSONDecodeError:
        process.wait(timeout=5)
        raise SystemExit(f"Recovered watcher emitted unparsable output: {startup_line[:200]}")
    if not isinstance(startup, dict) or not startup.get("ok"):
        detail = startup.get("error") if isinstance(startup, dict) else None
        raise SystemExit(detail or "Reattach failed.")

    update_runner_meta(
        task_dir,
        {
            "watcher_pid": startup["watcher_pid"],
            "watcher_process_identity": startup["watcher_process_identity"],
            "reattached_at": utc_now(),
        },
    )
    append_trace(task_dir, f"Reattached watcher {startup['watcher_pid']} to child pid {pid}.")
    print(json.dumps(startup, indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    task_dir = resolve_task_dir(args.task_dir)
    ensure_task_contract(task_dir)

    payload = {
        "task_dir": str(task_dir),
        "status": read_json(status_path(task_dir)),
        "runner": read_json(runner_meta_path(task_dir)),
    }

    progress = structured_progress(task_dir)
    if progress:
        payload["progress"] = progress

    pid = payload["runner"].get("pid")
    if isinstance(pid, int) and runner_pid_namespace_visible(payload["runner"]):
        alive, source = process_is_recorded_instance(
            pid, payload["runner"].get("process_identity")
        )
        payload["runner"]["process_alive"] = alive
        payload["runner"]["process_alive_source"] = source
    elif isinstance(pid, int):
        payload["runner"]["process_alive"] = None
        payload["runner"]["process_visibility"] = "different_pid_namespace"

    print(json.dumps(payload, indent=2))


def cmd_trace(args: argparse.Namespace) -> None:
    task_dir = resolve_task_dir(args.task_dir)
    ensure_task_contract(task_dir)
    path = trace_path(task_dir)
    if not path.exists():
        raise SystemExit(f"Trace file does not exist yet: {path}")
    print(path.read_text(encoding="utf-8"), end="")


def request_dev_pipeline_phase_stop(task_dir: Path, runner_meta: dict) -> None:
    """Bind a supported stop to the exact live public-pipeline phase first."""
    if runner_meta.get("workflow") != "dev-pipeline":
        return
    workflow = read_json(runner_workflow_path(task_dir))
    state_dir = workflow.get("state_dir")
    if not isinstance(state_dir, str) or not state_dir:
        raise SystemExit(
            "Cannot stop this dev-pipeline run safely: its lifecycle state directory "
            "is not recorded."
        )
    command = [
        resolve_dev_pipeline_bin(workflow.get("dev_pipeline_bin")),
        "handoff",
        "request-stop",
        "--task-ref",
        task_dir.name,
        "--state-dir",
        state_dir,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SystemExit(
            "Cannot stop this dev-pipeline run safely because the durable phase "
            f"marker was refused: {detail or f'exit {result.returncode}'}"
        )
    append_trace(task_dir, "Recorded the supported dev-pipeline stop before signalling it.")


def cmd_stop(args: argparse.Namespace) -> None:
    task_dir = resolve_task_dir(args.task_dir)
    ensure_task_contract(task_dir)
    runner_meta = read_json(runner_meta_path(task_dir))
    if not runner_pid_namespace_visible(runner_meta):
        raise SystemExit(
            "Recorded child belongs to a different PID namespace; run stop from "
            "the host supervision context."
        )
    pid = runner_meta.get("pid")
    if not isinstance(pid, int):
        raise SystemExit("No child process metadata found.")
    # Signalling a process group is destructive, so a recycled PID must never
    # reach `killpg`. Where identity was recorded, a mismatch is a refusal.
    alive, source = process_is_recorded_instance(pid, runner_meta.get("process_identity"))
    if not alive:
        if source == "identity_mismatch":
            raise SystemExit(
                f"Recorded child process identity is no longer running: {pid}"
            )
        raise SystemExit(f"Process is not running: {pid}")

    request_dev_pipeline_phase_stop(task_dir, runner_meta)
    os.killpg(pid, signal.SIGTERM)
    append_trace(task_dir, f"Parent agent requested stop for pid {pid}.")
    write_status(task_dir, "blocked", "Child agent stopped by parent request")
    sent, detail = try_send_pipeline_stop_message(
        task_dir=task_dir,
        summary="The task runner was stopped by a parent request.",
        requested_action="Inspect the task in CLI and decide whether to restart or resume it.",
        artifact_paths=[trace_path(task_dir), status_path(task_dir)],
    )
    if sent:
        append_trace(task_dir, "Sent pipeline notification about parent-requested stop.")
    else:
        append_trace(task_dir, f"Skipped pipeline notification: {detail}")
    print(json.dumps({"stopped_pid": pid}, indent=2))


def cmd_review_candidate(args: argparse.Namespace) -> None:
    """Materialize the exact completion subject and run a bounded reviewer."""
    from dev_pipeline.conventions import build_context_packet

    task_dir = resolve_task_dir(args.task_dir)
    repo_values = args.repo if isinstance(args.repo, list) else [args.repo]
    repositories = [Path(value).expanduser().resolve() for value in repo_values]
    for repository in repositories:
        if not repository.is_dir():
            raise SystemExit(f"Review repository is not a directory: {repository}")
    ensure_task_contract(task_dir)
    executable = resolve_dev_pipeline_bin(args.dev_pipeline_bin)
    review_dir = task_dir / "dev-pipeline" / "contract-review"
    review_dir.mkdir(parents=True, exist_ok=True)
    subject_path = task_dir / COMPLETION_REVIEW_SUBJECT
    context_path = task_dir / COMPLETION_REVIEW_CONTEXT
    run_path = task_dir / COMPLETION_REVIEW_RUN
    try:
        subject = completion_review_subject(task_dir, repositories, Path(executable))
        write_json(subject_path, subject)
        materials = completion_review_bound_materials(
            task_dir, repositories, Path(executable), materialize=True
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SystemExit(
            "The completion reviewer requires a committed candidate and stable "
            f"tracked baseline: {exc}"
        ) from None
    context = build_context_packet(
        role="diff_review",
        purpose=COMPLETION_REVIEW_PURPOSE,
        question=COMPLETION_REVIEW_QUESTION,
        artifacts=[subject_path, *materials],
        evidence=completion_review_evidence(task_dir),
        exclusions=COMPLETION_REVIEW_EXCLUSIONS,
        risks=["compatibility"],
        artifact_version="1.0.0",
    )
    write_json(context_path, context)
    command = [
        executable,
        "agent",
        "--packet",
        str(context_path),
        "--repo",
        str(repositories[0]),
        "--output",
        str(run_path),
        "--diagnostics-prefix",
        str(review_dir / "reviewer-diagnostics"),
        "--sandbox",
        "read-only",
    ]
    if args.model:
        command.extend(["--model", args.model])
    completed = subprocess.run(command, cwd=repo_root(), check=False)
    if completed.returncode:
        raise SystemExit(completed.returncode)
    print(json.dumps({"subject": str(subject_path), "context": str(context_path),
                      "reviewer_run": str(run_path)}, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or monitor child task agents.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Start a child agent for a task.")
    start_parser.add_argument("task_dir", help="Task directory path.")
    start_parser.add_argument(
        "--runner",
        choices=list(CLI_RUNNERS),
        default=None,
        help="Child CLI agent. Omit to follow the parent CLI agent.",
    )
    start_parser.add_argument(
        "--workflow",
        choices=["standard", "dev-pipeline"],
        default="standard",
    )
    start_parser.add_argument("--model", help="Optional model override for the resolved runner.")
    start_parser.add_argument(
        "--reviewer-runner",
        choices=list(review_admission.REVIEW_RUNNERS),
        default=None,
        help=(
            "Provider family that must review this launch. Omit to bind the first "
            "installed family independent from the author."
        ),
    )
    start_parser.add_argument(
        "--require-review-verdict",
        action="store_true",
        help="Require the review owner's canonical Verdict line before completion.",
    )
    start_parser.add_argument(
        "--sandbox-mode",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Access level for the child run, mapped per runner.",
    )
    start_parser.add_argument(
        "--repo",
        action="append",
        help="Target repository: standard child access root or dev-pipeline owner workspace.",
    )
    start_parser.add_argument(
        "--dev-pipeline-bin",
        help="Executable for the dev-pipeline CLI. Defaults to this checkout's .venv, then PATH.",
    )
    start_parser.add_argument(
        "--operation",
        choices=["start", "resume", "retry"],
        default="start",
        help="Lifecycle operation; standard resume/retry semantics come from the registered application.",
    )
    start_parser.add_argument(
        "--application",
        help="Versioned installation adapter as Python module:attribute (API v1).",
    )
    start_parser.add_argument(
        "--destination",
        help="Opaque installation-owned delivery destination; never stored in clear text.",
    )
    start_parser.add_argument(
        "--memory-limit",
        help="Child address-space limit in bytes or with K/M/G/T suffix; application policy may adjust it.",
    )
    start_parser.add_argument(
        "--state-dir",
        help="Task-local dev-pipeline core state directory.",
    )
    start_parser.add_argument(
        "--previous-state-dir",
        help="Prior dev-pipeline core state directory, for `--operation retry`.",
    )
    start_parser.add_argument(
        "--retry-reason",
        choices=["native_unavailable", "intentional_replacement"],
        help="Why a dev-pipeline retry replaces the previous attempt.",
    )
    start_parser.add_argument(
        "--assurance-config",
        help="Installation assurance configuration used for admission and dev-pipeline.",
    )
    start_parser.add_argument(
        "--review-packet",
        help="Digest-bound review packet for automatic assurance handoff.",
    )
    start_parser.add_argument("--dry-run", action="store_true", help="Prepare artifacts without launching the child process.")
    start_parser.add_argument(
        "--foreground",
        action="store_true",
        help=(
            "Run and supervise the child in this process for an application-owned "
            "outer lifecycle instead of detaching a watcher."
        ),
    )
    start_parser.set_defaults(func=cmd_start)

    author_parser = subparsers.add_parser(
        "author",
        help="Run a standard author with the launcher-owned write profile.",
    )
    author_parser.add_argument("task_dir", help="Task directory path.")
    author_parser.add_argument(
        "--repo",
        action="append",
        required=True,
        help="Exact writable target repository; repeat for one multi-repository candidate.",
    )
    author_parser.add_argument(
        "--runner",
        choices=list(CLI_RUNNERS),
        default=None,
        help="Author CLI agent. Omit to follow the parent CLI agent.",
    )
    author_parser.add_argument("--model", help="Optional model override for the resolved runner.")
    author_parser.add_argument(
        "--reviewer-runner",
        choices=list(review_admission.REVIEW_RUNNERS),
        default=None,
        help="Provider family that must review this author.",
    )
    author_parser.add_argument("--application", help="Versioned installation adapter.")
    author_parser.add_argument("--destination", help="Opaque delivery destination.")
    author_parser.add_argument("--memory-limit")
    author_parser.add_argument("--assurance-config")
    author_parser.add_argument("--dry-run", action="store_true")
    author_parser.add_argument("--foreground", action="store_true")
    author_parser.set_defaults(func=cmd_author)

    review_parser = subparsers.add_parser(
        "review",
        help="Run the independent reviewer bound to this task's author.",
    )
    review_parser.add_argument("task_dir", help="Task directory path.")
    review_parser.add_argument("--model", help="Optional reviewer model override.")
    review_parser.add_argument("--application", help="Versioned installation adapter.")
    review_parser.add_argument("--destination", help="Opaque delivery destination.")
    review_parser.add_argument("--memory-limit")
    review_parser.add_argument("--dry-run", action="store_true")
    review_parser.add_argument("--foreground", action="store_true")
    review_parser.set_defaults(func=cmd_review)

    product_review_parser = subparsers.add_parser(
        "product-review",
        help=(
            "Run a fresh staged product acceptance and separate technical review "
            "through the reviewer bound to this task."
        ),
    )
    product_review_parser.add_argument("task_dir", help="Task directory path.")
    product_review_parser.add_argument(
        "--packet",
        required=True,
        help=(
            "Task-local immutable packet with the user contract, candidate identity, "
            "inputs, black-box commands, source manifest, and exclusions."
        ),
    )
    product_review_parser.add_argument("--model", help="Optional reviewer model override.")
    product_review_parser.add_argument("--application", help="Versioned installation adapter.")
    product_review_parser.add_argument("--destination", help="Opaque delivery destination.")
    product_review_parser.add_argument("--memory-limit")
    product_review_parser.add_argument("--dry-run", action="store_true")
    product_review_parser.add_argument("--foreground", action="store_true")
    product_review_parser.set_defaults(func=cmd_product_review)

    run_child_parser = subparsers.add_parser("_run-child", help=argparse.SUPPRESS)
    run_child_parser.add_argument("task_dir", help="Task directory path.")
    run_child_parser.add_argument("--runner", choices=list(CLI_RUNNERS), default=DEFAULT_RUNNER)
    run_child_parser.add_argument("--runner-resolution", help=argparse.SUPPRESS)
    run_child_parser.add_argument("--launch-token", help=argparse.SUPPRESS)
    run_child_parser.add_argument(
        "--workflow",
        choices=["standard", "dev-pipeline"],
        default="standard",
    )
    run_child_parser.add_argument("--model", help="Optional model override.")
    run_child_parser.add_argument(
        "--sandbox-mode",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help=argparse.SUPPRESS,
    )
    run_child_parser.add_argument("--repo", action="append", help=argparse.SUPPRESS)
    run_child_parser.add_argument(
        "--require-git-worktree", action="store_true", help=argparse.SUPPRESS
    )
    run_child_parser.add_argument("--dev-pipeline-bin", help=argparse.SUPPRESS)
    run_child_parser.add_argument(
        "--operation", choices=["start", "resume", "retry"], default="start", help=argparse.SUPPRESS
    )
    run_child_parser.add_argument("--application", help=argparse.SUPPRESS)
    run_child_parser.add_argument("--destination", help=argparse.SUPPRESS)
    run_child_parser.add_argument("--memory-limit", help=argparse.SUPPRESS)
    run_child_parser.add_argument("--state-dir", help=argparse.SUPPRESS)
    run_child_parser.add_argument("--previous-state-dir", help=argparse.SUPPRESS)
    run_child_parser.add_argument(
        "--retry-reason",
        choices=["native_unavailable", "intentional_replacement"],
        help=argparse.SUPPRESS,
    )
    run_child_parser.add_argument("--assurance-config", help=argparse.SUPPRESS)
    run_child_parser.add_argument("--review-packet", help=argparse.SUPPRESS)
    run_child_parser.set_defaults(func=cmd_run_child)

    monitor_parser = subparsers.add_parser("_monitor-existing", help=argparse.SUPPRESS)
    monitor_parser.add_argument("task_dir", help="Task directory path.")
    monitor_parser.add_argument("--poll-interval", type=int, default=5, help=argparse.SUPPRESS)
    monitor_parser.set_defaults(func=cmd_monitor_existing)

    reattach_parser = subparsers.add_parser(
        "reattach", help="Restore watcher supervision for a running child."
    )
    reattach_parser.add_argument("task_dir", help="Task directory path.")
    reattach_parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="Seconds between child liveness checks.",
    )
    reattach_parser.set_defaults(func=cmd_reattach)

    status_parser = subparsers.add_parser("status", help="Show current task runner status.")
    status_parser.add_argument("task_dir", help="Task directory path.")
    status_parser.set_defaults(func=cmd_status)

    trace_parser = subparsers.add_parser("trace", help="Print the task trace.")
    trace_parser.add_argument("task_dir", help="Task directory path.")
    trace_parser.set_defaults(func=cmd_trace)

    stop_parser = subparsers.add_parser("stop", help="Stop a running child agent.")
    stop_parser.add_argument("task_dir", help="Task directory path.")
    stop_parser.set_defaults(func=cmd_stop)

    review_parser = subparsers.add_parser(
        "review-candidate",
        help="Run the bounded contract-policy review over a committed candidate.",
    )
    review_parser.add_argument("task_dir", help="Task directory path.")
    review_parser.add_argument("--repo", action="append", required=True, help="Committed target repository; repeat for one exact candidate spanning repositories.")
    review_parser.add_argument("--dev-pipeline-bin", help="Dev-pipeline CLI executable.")
    review_parser.add_argument("--model", help="Optional reviewer model override.")
    review_parser.set_defaults(func=cmd_review_candidate)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
