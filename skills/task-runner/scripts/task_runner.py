#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline_notify import try_send_pipeline_stop_message
from task_contract import ensure_task_contract_file


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def repo_root() -> Path:
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


def resolve_task_dir(task_path: str) -> Path:
    path = Path(task_path)
    if not path.is_absolute():
        path = repo_root() / path
    if path.exists():
        return path.resolve()

    match = re.match(r"^(\d{3})-.*$", path.name)
    if not match:
        return path.resolve(strict=False)

    tasks_dir = repo_root() / "tasks"
    candidates = sorted(tasks_dir.glob(f"{match.group(1)}-*"))
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


def append_trace(task_dir: Path, message: str) -> None:
    path = trace_path(task_dir)
    if not path.exists():
        path.write_text("# Trace\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"- {utc_now()} {message}\n")


def write_status(task_dir: Path, state: str, current_step: str, extra: dict | None = None) -> None:
    payload = read_json(status_path(task_dir))
    payload.update(
        {
            "state": state,
            "current_step": current_step,
            "updated_at": utc_now(),
        }
    )
    if extra:
        payload.update(extra)
    write_json(status_path(task_dir), payload)


def ensure_task_contract(task_dir: Path) -> None:
    if not task_dir.exists():
        raise SystemExit(f"Task directory does not exist: {task_dir}")
    for name in ("task.md", "plan.md"):
        if not (task_dir / name).exists():
            raise SystemExit(f"Missing required task artifact: {task_dir / name}")
    ensure_task_contract_file(task_dir)
    runner_dir(task_dir).mkdir(parents=True, exist_ok=True)


def build_child_prompt(task_dir: Path) -> str:
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
    return f"""You are the child execution agent for task directory: {task_dir}

Before doing substantial work:
1. Read `{task_md}`
2. Read `{plan_md}`
3. Read `{task_contract_json}` if it exists and treat it as a structured execution contract.
4. Read `{preferences_md}` if it exists, before choosing any unspecified output
   representation. The current request and later continuations override it.
5. If `{task_md}` is missing execution-critical inputs from the original request, add them before continuing.
6. Update `{status_json}` to reflect active work.
7. Append a short note to `{trace_md}` describing what you are doing.

While working:
- Keep `{trace_md}` updated with concise chronological notes.
- Keep `{status_json}` updated with `state`, `current_step`, and `updated_at`.
- For a long run, publish substantive live progress in `{progress_json}`: a version 1
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


def codex_workdir(sandbox_mode: str | None) -> Path:
    if sandbox_mode == 'danger-full-access':
        return workspace_root()
    return repo_root()


CLI_RUNNERS = ("codex", "claude", "agent")
DEFAULT_RUNNER = "codex"
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
) -> list[str]:
    """Map a runner-neutral access mode to Claude's real permission boundary."""
    if sandbox_mode == "danger-full-access":
        # --add-dir is variadic, so it must be followed by another option or it
        # swallows the trailing prompt argument.
        return [
            "--add-dir",
            str(codex_workdir(sandbox_mode)),
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
        # Claude's Bash sandbox always grants cwd writes, so it cannot express a
        # true read-only Bash boundary. Expose only non-writing built-ins.
        permission_mode = "dontAsk"
        tool_arguments = ["--tools", "Read,WebFetch,WebSearch"]
    elif sandbox_mode == "workspace-write":
        # Claude's sandbox defaults to writes in cwd and its session temp dir.
        # acceptEdits authorizes native file tools only within Claude's granted
        # project roots; no --add-dir is supplied.
        permission_mode = "acceptEdits"
        tool_arguments = ["--tools", "Read,Bash,Edit,Write,WebFetch,WebSearch"]
    else:
        raise SystemExit(f"Unsupported Claude sandbox mode: {sandbox_mode}")

    return [
        "--setting-sources",
        "project",
        *tool_arguments,
        "--permission-mode",
        permission_mode,
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


def resolve_sandbox_mode(
    runner: str,
    workflow: str,
    sandbox_mode: str | None,
) -> str | None:
    """Resolve the effective sandbox mode for a child run."""
    if sandbox_mode:
        return sandbox_mode
    if workflow != "multi-agent-dev":
        return None
    if runner in {"codex", "claude", "agent"}:
        return "danger-full-access"
    return None


def build_command(
    runner: str,
    prompt_path: Path,
    root: Path,
    model: str | None,
    sandbox_mode: str | None,
) -> list[str]:
    prompt = prompt_path.read_text(encoding="utf-8")
    if runner == "codex":
        resolved_sandbox_mode = sandbox_mode or "workspace-write"
        workdir = codex_workdir(resolved_sandbox_mode)
        command = [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            resolved_sandbox_mode,
            "-C",
            str(workdir),
        ]
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command

    if runner == "claude":
        resolved_sandbox_mode = sandbox_mode or "workspace-write"
        # The child keeps the repository as its working directory so CLAUDE.md,
        # and through it AGENTS.md and the always-on rules, load automatically.
        command = ["claude", "--print", *claude_access_arguments(resolved_sandbox_mode)]
        resolved_model = model or os.environ.get("CLAUDE_CHILD_DEFAULT_MODEL")
        if resolved_model:
            command.extend(["--model", resolved_model])
        command.append(prompt)
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
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command

    raise SystemExit(f"Unsupported runner: {runner}")


def build_workflow_command(
    workflow: str,
    runner: str,
    task_dir: Path,
    agents_dir: str | None,
    agents_repo_url: str | None,
    artifacts_subdir: str | None,
    sandbox_mode: str | None,
    resume: bool,
    model: str | None = None,
) -> list[str] | None:
    if workflow == "standard":
        return None
    if workflow != "multi-agent-dev":
        raise SystemExit(f"Unsupported workflow: {workflow}")
    if runner not in {"codex", "agent"}:
        raise SystemExit(
            "The multi-agent development workflow supports only the Codex (`codex`) or Cursor Agent (`agent`) runners."
        )

    command = [
        sys.executable,
        str(Path(__file__).with_name("codex_multi_agent.py")),
        str(task_dir),
        "--runner",
        runner,
    ]
    if agents_dir:
        command.extend(["--agents-dir", agents_dir])
    if agents_repo_url:
        command.extend(["--agents-repo-url", agents_repo_url])
    if artifacts_subdir:
        command.extend(["--artifacts-subdir", artifacts_subdir])
    if sandbox_mode:
        command.extend(["--sandbox-mode", sandbox_mode])
    if model:
        command.extend(["--model", model])
    if resume:
        command.append("--resume")
    return command


def structured_progress(task_dir: Path) -> dict | None:
    """Return validated version 1 progress, or None when it says nothing useful.

    The point of the schema is that a reader can trust it. `activity` must be
    real text, and the count triple is accepted only when it is complete and
    coherent, so nobody downstream has to infer a missing total.
    """
    payload = read_json(progress_path(task_dir))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None

    activity = payload.get("activity")
    if not isinstance(activity, str) or not activity.strip():
        return None

    progress: dict = {
        "version": 1,
        "activity": activity.strip(),
        "updated_at": payload.get("updated_at"),
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
            isinstance(completed, int)
            and isinstance(total, int)
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


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cmd_start(args: argparse.Namespace) -> None:
    root = repo_root()
    task_dir = resolve_task_dir(args.task_dir)
    ensure_task_contract(task_dir)
    # Resolve once, here, while this process is still a direct descendant of the
    # parent CLI. The detached watcher receives the decision and never re-detects.
    args.runner, runner_resolution = resolve_runner(args.runner)
    resolved_sandbox_mode = resolve_sandbox_mode(
        args.runner,
        args.workflow,
        getattr(args, "sandbox_mode", None),
    )

    workflow_command = build_workflow_command(
        args.workflow,
        args.runner,
        task_dir,
        getattr(args, "agents_dir", None),
        getattr(args, "agents_repo_url", None),
        getattr(args, "artifacts_subdir", None),
        resolved_sandbox_mode,
        getattr(args, "resume", False),
        getattr(args, "model", None),
    )
    if args.workflow == "standard":
        prompt = build_child_prompt(task_dir)
        runner_prompt_path(task_dir).write_text(prompt, encoding="utf-8")
    else:
        runner_prompt_path(task_dir).write_text(
            f"Workflow `{args.workflow}` is executed by a dedicated runner script.\n",
            encoding="utf-8",
        )
        workflow_meta = {
            "workflow": args.workflow,
            "agents_dir": getattr(args, "agents_dir", None),
            "agents_repo_url": getattr(args, "agents_repo_url", None),
            "artifacts_subdir": getattr(args, "artifacts_subdir", None),
            "sandbox_mode": resolved_sandbox_mode,
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
        },
    )

    command = workflow_command or build_command(
        args.runner,
        runner_prompt_path(task_dir),
        root,
        args.model,
        resolved_sandbox_mode,
    )
    meta = {
        "runner": args.runner,
        "runner_resolution": runner_resolution,
        "workflow": args.workflow,
        "started_at": utc_now(),
        "task_dir": str(task_dir),
        "prompt_path": str(runner_prompt_path(task_dir)),
        "log_path": str(runner_log_path(task_dir)),
        "command": command,
    }
    if resolved_sandbox_mode:
        meta["sandbox_mode"] = resolved_sandbox_mode

    if args.dry_run:
        meta["dry_run"] = True
        write_json(runner_meta_path(task_dir), meta)
        append_trace(task_dir, "Dry run prepared prompt and runner metadata without launching a child process.")
        write_status(task_dir, "ready", f"Prepared child run via {args.runner}", {"runner": args.runner})
        print(json.dumps(meta, indent=2))
        return

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
    ]
    if args.model:
        watcher_command.extend(["--model", args.model])
    if resolved_sandbox_mode:
        watcher_command.extend(["--sandbox-mode", resolved_sandbox_mode])
    if getattr(args, "agents_dir", None):
        watcher_command.extend(["--agents-dir", args.agents_dir])
    if getattr(args, "agents_repo_url", None):
        watcher_command.extend(["--agents-repo-url", args.agents_repo_url])
    if getattr(args, "artifacts_subdir", None):
        watcher_command.extend(["--artifacts-subdir", args.artifacts_subdir])
    if getattr(args, "resume", False):
        watcher_command.append("--resume")

    process = subprocess.Popen(
        watcher_command,
        cwd=root,
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
        raise SystemExit("Child runner failed to report startup metadata.")

    startup_meta = json.loads(startup_line)
    if not startup_meta.get("ok"):
        raise SystemExit(startup_meta.get("error", "Child runner failed before launch."))

    meta.update(
        {
            "watcher_pid": startup_meta["watcher_pid"],
            "pid": startup_meta["pid"],
            "child_started_at": startup_meta["child_started_at"],
        }
    )
    write_json(runner_meta_path(task_dir), meta)
    append_trace(task_dir, f"Child process started with pid {startup_meta['pid']}.")

    print(json.dumps(meta, indent=2))


def finalize_child_lifecycle(
    task_dir: Path,
    workflow: str,
    runner: str,
    return_code: int,
) -> None:
    """Make a child that died without finishing say so in the task artifacts.

    A child that exits non-zero before writing a terminal state would otherwise
    leave `running` behind, which reads as work still in progress.
    """
    task_state = read_json(status_path(task_dir)).get("state")
    if task_state in {"completed", "failed", "blocked"}:
        return
    if return_code == 0:
        return
    write_status(
        task_dir,
        "failed",
        f"Child process exited unsuccessfully with code {return_code}",
        {"runner": runner, "workflow": workflow, "exit_code": return_code},
    )
    append_trace(
        task_dir,
        f"Recorded terminal failed status after the {workflow} child exited with code "
        f"{return_code} before finalizing task artifacts.",
    )


def cmd_run_child(args: argparse.Namespace) -> None:
    root = repo_root()
    task_dir = resolve_task_dir(args.task_dir)
    ensure_task_contract(task_dir)
    resolved_sandbox_mode = resolve_sandbox_mode(
        args.runner,
        args.workflow,
        getattr(args, "sandbox_mode", None),
    )

    workflow_command = build_workflow_command(
        args.workflow,
        args.runner,
        task_dir,
        getattr(args, "agents_dir", None),
        getattr(args, "agents_repo_url", None),
        getattr(args, "artifacts_subdir", None),
        resolved_sandbox_mode,
        getattr(args, "resume", False),
        getattr(args, "model", None),
    )
    command = workflow_command or build_command(
        args.runner,
        runner_prompt_path(task_dir),
        root,
        args.model,
        resolved_sandbox_mode,
    )
    if args.runner == "claude" and args.workflow == "standard":
        require_claude_sandbox_dependencies(resolved_sandbox_mode or "workspace-write")
        require_safe_claude_project_settings(
            root, resolved_sandbox_mode or "workspace-write"
        )
    update_runner_meta(
        task_dir,
        {
            "runner": args.runner,
            "runner_resolution": getattr(args, "runner_resolution", None) or "inherited",
            "workflow": args.workflow,
            "task_dir": str(task_dir),
            "prompt_path": str(runner_prompt_path(task_dir)),
            "log_path": str(runner_log_path(task_dir)),
            "command": command,
            "watcher_pid": os.getpid(),
            "watcher_started_at": utc_now(),
        },
    )

    try:
        log_handle = runner_log_path(task_dir).open("ab")
        process = subprocess.Popen(
            command,
            cwd=root,
            env=child_environment(args.runner),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        update_runner_meta(
            task_dir,
            {
                "launch_error": str(exc),
                "finished_at": utc_now(),
                "outcome": "failed_to_launch",
            },
        )
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        raise

    child_started_at = utc_now()
    update_runner_meta(
        task_dir,
        {
            "pid": process.pid,
            "child_started_at": child_started_at,
        },
    )
    print(
        json.dumps(
            {
                "ok": True,
                "pid": process.pid,
                "watcher_pid": os.getpid(),
                "child_started_at": child_started_at,
            }
        ),
        flush=True,
    )

    return_code = process.wait()
    outcome = "succeeded" if return_code == 0 else "failed"
    update_runner_meta(
        task_dir,
        {
            "exit_code": return_code,
            "finished_at": utc_now(),
            "outcome": outcome,
        },
    )
    finalize_child_lifecycle(task_dir, args.workflow, args.runner, return_code)


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
    if isinstance(pid, int):
        payload["runner"]["process_alive"] = pid_is_running(pid)

    print(json.dumps(payload, indent=2))


def cmd_trace(args: argparse.Namespace) -> None:
    task_dir = resolve_task_dir(args.task_dir)
    ensure_task_contract(task_dir)
    path = trace_path(task_dir)
    if not path.exists():
        raise SystemExit(f"Trace file does not exist yet: {path}")
    print(path.read_text(encoding="utf-8"), end="")


def cmd_stop(args: argparse.Namespace) -> None:
    task_dir = resolve_task_dir(args.task_dir)
    ensure_task_contract(task_dir)
    runner_meta = read_json(runner_meta_path(task_dir))
    pid = runner_meta.get("pid")
    if not isinstance(pid, int):
        raise SystemExit("No child process metadata found.")
    if not pid_is_running(pid):
        raise SystemExit(f"Process is not running: {pid}")

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
    start_parser.add_argument("--workflow", choices=["standard", "multi-agent-dev"], default="standard")
    start_parser.add_argument("--model", help="Optional model override for the resolved runner.")
    start_parser.add_argument(
        "--sandbox-mode",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Access level for the child run, mapped per runner.",
    )
    start_parser.add_argument(
        "--agents-dir",
        help="Prompt library directory for the multi-agent workflow.",
    )
    start_parser.add_argument(
        "--agents-repo-url",
        help="Git repository to clone if the multi-agent prompt library directory is missing.",
    )
    start_parser.add_argument(
        "--artifacts-subdir",
        help="Task-local artifacts subdirectory for the multi-agent workflow.",
    )
    start_parser.add_argument("--dry-run", action="store_true", help="Prepare artifacts without launching the child process.")
    start_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing multi-agent workflow from its latest unfinished stage.",
    )
    start_parser.set_defaults(func=cmd_start)

    run_child_parser = subparsers.add_parser("_run-child", help=argparse.SUPPRESS)
    run_child_parser.add_argument("task_dir", help="Task directory path.")
    run_child_parser.add_argument("--runner", choices=list(CLI_RUNNERS), default=DEFAULT_RUNNER)
    run_child_parser.add_argument("--runner-resolution", help=argparse.SUPPRESS)
    run_child_parser.add_argument("--workflow", choices=["standard", "multi-agent-dev"], default="standard")
    run_child_parser.add_argument("--model", help="Optional model override.")
    run_child_parser.add_argument(
        "--sandbox-mode",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help=argparse.SUPPRESS,
    )
    run_child_parser.add_argument("--agents-dir", help=argparse.SUPPRESS)
    run_child_parser.add_argument("--agents-repo-url", help=argparse.SUPPRESS)
    run_child_parser.add_argument("--artifacts-subdir", help=argparse.SUPPRESS)
    run_child_parser.add_argument("--resume", action="store_true", help=argparse.SUPPRESS)
    run_child_parser.set_defaults(func=cmd_run_child)

    status_parser = subparsers.add_parser("status", help="Show current task runner status.")
    status_parser.add_argument("task_dir", help="Task directory path.")
    status_parser.set_defaults(func=cmd_status)

    trace_parser = subparsers.add_parser("trace", help="Print the task trace.")
    trace_parser.add_argument("task_dir", help="Task directory path.")
    trace_parser.set_defaults(func=cmd_trace)

    stop_parser = subparsers.add_parser("stop", help="Stop a running child agent.")
    stop_parser.add_argument("task_dir", help="Task directory path.")
    stop_parser.set_defaults(func=cmd_stop)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
