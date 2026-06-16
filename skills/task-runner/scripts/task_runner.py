#!/usr/bin/env python3
import argparse
import json
import os
import re
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
    return f"""You are the child execution agent for task directory: {task_dir}

Before doing substantial work:
1. Read `{task_md}`
2. Read `{plan_md}`
3. Read `{task_contract_json}` if it exists and treat it as a structured execution contract.
4. If `{task_md}` is missing execution-critical inputs from the original request, add them before continuing.
5. Update `{status_json}` to reflect active work.
6. Append a short note to `{trace_md}` describing what you are doing.

While working:
- Keep `{trace_md}` updated with concise chronological notes.
- Keep `{status_json}` updated with `state`, `current_step`, and `updated_at`.
- Store all task-specific outputs inside `{task_dir}`.
- Do not store task outputs inside `{task_dir / '.runner'}`.
- If `{task_contract_json}` contains non-negotiable constraints, forbidden substitutions, or required live evidence, do not weaken or ignore them.
- Preserve original user-provided inputs that materially affect execution, such as dimensions, constraints, acceptance criteria, requested materials, or excluded options, in task artifacts instead of relying on the chat transcript.
- If you use external sources, write the concrete researched results into `{task_dir / 'findings.md'}` and the source list into `{task_dir / 'sources.md'}` before finishing.
- When you find concrete details such as addresses, contacts, dates, prices, or named options, record them in the task artifacts instead of leaving them only in your final reply.
- If the task directory name uses a generic placeholder such as `NNN-remote-request`, rename it early through `skills/task-creator/scripts/rename_task.sh` after you understand the request. Choose a short deliberate ASCII slug yourself; do not copy the full title and do not rely on transliteration.
- Avoid clearly destructive actions such as formatting disks, wiping broad directories, or damaging unrelated projects.
- If the task explicitly touches another project under `/opt/projects`, keep the change scoped to the requested files and avoid unrelated damage.
- If you change task lifecycle, task artifact structure, skill discovery or execution, agent orchestration, restore behavior, or resume behavior, update the relevant project docs in the same source change.
- If you change git-tracked source in a repository with a configured remote, commit and push after verification unless the task explicitly requires local-only work or publication is blocked.
- If verification or publication is blocked, record the reason and current repository state in task artifacts before finishing.

Before finishing:
- Ensure `{status_json}` has `state` set to `completed` or `blocked`.
- Append a final trace entry summarizing what was done.
- In your final response, summarize the result briefly and reference the task artifacts you updated.
"""


def codex_workdir(sandbox_mode: str | None) -> Path:
    if sandbox_mode == 'danger-full-access':
        return Path('/opt/projects')
    return repo_root()


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
    if runner == "codex":
        return "danger-full-access"
    if runner == "agent":
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

    append_trace(task_dir, f"Parent agent prepared child run with runner `{args.runner}` and workflow `{args.workflow}`.")
    write_status(
        task_dir,
        "running",
        f"Starting child agent via {args.runner} ({args.workflow})",
        {"runner": args.runner, "workflow": args.workflow},
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
    update_runner_meta(
        task_dir,
        {
            "runner": args.runner,
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


def cmd_status(args: argparse.Namespace) -> None:
    task_dir = resolve_task_dir(args.task_dir)
    ensure_task_contract(task_dir)

    payload = {
        "task_dir": str(task_dir),
        "status": read_json(status_path(task_dir)),
        "runner": read_json(runner_meta_path(task_dir)),
    }

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
    start_parser.add_argument("--runner", choices=["codex", "agent"], default="codex")
    start_parser.add_argument("--workflow", choices=["standard", "multi-agent-dev"], default="standard")
    start_parser.add_argument("--model", help="Optional model override.")
    start_parser.add_argument(
        "--sandbox-mode",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Sandbox mode for Codex child runs.",
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
    run_child_parser.add_argument("--runner", choices=["codex", "agent"], default="codex")
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
