"""Terminal cleanup for a task runner's own process scope and Git workspace.

This module deliberately has no scheduler or registry.  It consumes only the
current runner record, the current systemd scope, and Git's own reachability
data.  Every refusal is a normal retained outcome with one stable reason.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


def _run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _git_value(repository: Path, *arguments: str) -> str | None:
    result = _run_git(repository, *arguments)
    if result.returncode:
        return None
    return result.stdout.strip()


def _task_number(task_dir: Path) -> str | None:
    match = re.match(r"^(\d+)-", task_dir.name)
    return match.group(1) if match else None


def _path_names_task(path: Path, task_number: str) -> bool:
    escaped = re.escape(task_number)
    return bool(
        re.search(rf"(?:^|[-_])(?:task)?{escaped}(?:[-_]|$)", path.name)
    )


def _path_task_numbers(path: Path) -> set[str]:
    """Return task-like numeric components carried by a workspace basename."""
    return {
        match.group(1)
        for match in re.finditer(r"(?:^|[-_])(?:task)?(\d+)(?=[-_]|$)", path.name)
    }


def _protected_ignored_paths(repository: Path) -> list[str] | None:
    """Find ignored durable task/data state that cleanup must not erase."""
    ignored = _run_git(
        repository,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--",
        "tasks/",
        "data/",
        ".state/",
    )
    if ignored.returncode:
        return None
    return sorted(line for line in ignored.stdout.splitlines() if line)


def _absolute_git_path(repository: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = repository / path
    return path.resolve()


def _is_mountpoint(path: Path) -> bool:
    """Detect bind mounts too; os.path.ismount cannot identify all of them."""
    resolved = str(path.resolve())
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return os.path.ismount(path)
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        mountpoint = re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            fields[4],
        )
        if os.path.normpath(mountpoint) == resolved:
            return True
    return os.path.ismount(path)


def _containing_refs(repository: Path, head: str, *prefixes: str) -> list[str]:
    command = ["for-each-ref", "--format=%(refname)", f"--contains={head}", *prefixes]
    result = _run_git(repository, *command)
    if result.returncode:
        return []
    return sorted(line for line in result.stdout.splitlines() if line)


def _local_origin(repository: Path) -> Path | None:
    raw = _git_value(repository, "remote", "get-url", "origin")
    if not raw:
        return None
    if raw.startswith("file://"):
        raw = raw[7:]
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return None
    resolved = candidate.resolve()
    return resolved if resolved.exists() else None


def _reachability(
    repository: Path, head: str
) -> tuple[list[str], Path | None, str, str | None]:
    git_dir_raw = _git_value(repository, "rev-parse", "--git-dir")
    common_raw = _git_value(repository, "rev-parse", "--git-common-dir")
    if not git_dir_raw or not common_raw:
        return [], None, "not_git", "git_metadata_unreadable"
    git_dir = _absolute_git_path(repository, git_dir_raw)
    common_dir = _absolute_git_path(repository, common_raw)
    if git_dir != common_dir:
        refs = _containing_refs(repository, head, "refs/heads", "refs/remotes")
        return refs, common_dir, "worktree", None

    origin = _local_origin(repository)
    if origin is not None:
        refs = _containing_refs(origin, head, "refs/heads", "refs/remotes")
        return refs, origin, "clone", None
    refreshed = _run_git(repository, "fetch", "--quiet", "--prune", "origin")
    if refreshed.returncode:
        return [], None, "clone", "canonical_fetch_failed"
    refs = _containing_refs(repository, head, "refs/remotes/origin")
    return refs, None, "clone", None


def processes_referencing(path: Path, *, exclude: set[int] | None = None) -> list[int]:
    """Return processes whose executable, cwd, root, or open fd is below path."""
    excluded = exclude or set()
    prefix = f"{path.resolve()}{os.sep}"
    owners: set[int] = set()
    for entry in Path("/proc").glob("[0-9]*"):
        pid = int(entry.name)
        if pid in excluded:
            continue
        links = [entry / "cwd", entry / "root", entry / "exe"]
        fd_dir = entry / "fd"
        try:
            links.extend(fd_dir.iterdir())
        except OSError:
            pass
        for link in links:
            try:
                target = os.readlink(link)
            except OSError:
                continue
            if target == str(path) or target.startswith(prefix):
                owners.add(pid)
                break
    return sorted(owners)


def inspect_workspace(task_dir: Path, runner_meta: dict[str, Any]) -> dict[str, Any]:
    grants = runner_meta.get("access_grant", {}).get("granted_directories", [])
    if len(grants) != 1:
        return {"outcome": "retained", "reason": "target_not_unique"}
    repository = Path(grants[0]).expanduser().resolve()
    result: dict[str, Any] = {
        "outcome": "retained",
        "path": str(repository),
    }
    if not repository.is_dir():
        return {**result, "reason": "path_missing"}
    root = _git_value(repository, "rev-parse", "--show-toplevel")
    if root is None or Path(root).resolve() != repository:
        return {**result, "reason": "not_git_root"}
    number = _task_number(task_dir)
    git_dir_raw = _git_value(repository, "rev-parse", "--git-dir")
    common_raw = _git_value(repository, "rev-parse", "--git-common-dir")
    if not git_dir_raw or not common_raw:
        return {**result, "reason": "git_metadata_unreadable"}
    git_dir = _absolute_git_path(repository, git_dir_raw)
    common_dir = _absolute_git_path(repository, common_raw)
    disposable_by_git = git_dir != common_dir or _local_origin(repository) is not None
    path_numbers = _path_task_numbers(repository)
    if not number or (
        not _path_names_task(repository, number)
        and (path_numbers or not disposable_by_git)
    ):
        return {**result, "reason": "path_not_task_owned"}
    dirty = _run_git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty.returncode:
        return {**result, "reason": "git_status_failed"}
    dirty_paths = len(dirty.stdout.splitlines())
    if dirty_paths:
        return {**result, "reason": "dirty", "dirty_paths": dirty_paths}
    protected_ignored = _protected_ignored_paths(repository)
    if protected_ignored is None:
        return {**result, "reason": "ignored_paths_unreadable"}
    if protected_ignored:
        return {
            **result,
            "reason": "protected_ignored_paths",
            "protected_ignored_paths": protected_ignored,
        }
    head = _git_value(repository, "rev-parse", "HEAD")
    if head is None:
        return {**result, "reason": "head_unreadable"}
    refs, canonical, kind, reachability_error = _reachability(repository, head)
    result.update({"kind": kind, "head": head, "reachable_refs": refs})
    if canonical is not None:
        result["canonical_git"] = str(canonical)
    if reachability_error is not None:
        return {**result, "reason": reachability_error}
    if not refs:
        return {**result, "reason": "head_unreachable"}
    live = processes_referencing(repository, exclude={os.getpid()})
    if live:
        return {**result, "reason": "live_processes", "live_pids": live}
    return {**result, "outcome": "eligible", "reason": "safe"}


def cleanup_workspace(task_dir: Path, runner_meta: dict[str, Any]) -> dict[str, Any]:
    """Remove one proven-safe task target, or retain it with an exact reason."""
    result = inspect_workspace(task_dir, runner_meta)
    if result.get("outcome") != "eligible":
        return result
    repository = Path(result["path"])

    # Recheck the mutable safety predicates immediately before removal.
    dirty = _run_git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty.returncode or dirty.stdout:
        return {**result, "outcome": "retained", "reason": "changed_before_removal"}
    protected_ignored = _protected_ignored_paths(repository)
    if protected_ignored is None:
        return {
            **result,
            "outcome": "retained",
            "reason": "ignored_paths_unreadable",
        }
    if protected_ignored:
        return {
            **result,
            "outcome": "retained",
            "reason": "protected_ignored_paths",
            "protected_ignored_paths": protected_ignored,
        }
    live = processes_referencing(repository, exclude={os.getpid()})
    if live:
        return {
            **result,
            "outcome": "retained",
            "reason": "live_processes",
            "live_pids": live,
        }

    if result["kind"] == "worktree":
        common = Path(result["canonical_git"])
        removal = subprocess.run(
            ["git", f"--git-dir={common}", "worktree", "remove", str(repository)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if removal.returncode:
            return {
                **result,
                "outcome": "retained",
                "reason": "worktree_remove_failed",
                "detail": removal.stderr.strip(),
            }
    else:
        # An exact sandbox grant can expose the repository root as a bind mount.
        # rmtree would then delete children before failing with EBUSY at the
        # mounted root or one of its injected metadata views. Refuse before the
        # first deletion so a retained outcome still means an intact checkout.
        if _is_mountpoint(repository):
            return {
                **result,
                "outcome": "retained",
                "reason": "workspace_is_mountpoint",
            }
        try:
            shutil.rmtree(repository)
        except OSError as exc:
            return {
                **result,
                "outcome": "retained",
                "reason": "workspace_remove_failed",
                "detail": str(exc),
            }
    return {**result, "outcome": "removed", "reason": "safe"}


def record_completed_workspace_cleanup(
    task_dir: Path,
    *,
    require_finished_run: bool = False,
    label: str = "Terminal",
) -> dict[str, Any] | None:
    """Run and record the existing cleanup owner after accepted completion.

    The watcher calls this after a child exits.  The metadata command calls it
    only for an already-finished run, which covers a task closed later by an
    installation or publication step without racing a still-running child.
    """
    runner_dir = task_dir / ".runner"
    runner_meta_path = runner_dir / "runner.json"
    if not runner_meta_path.is_file():
        return None

    with (runner_dir / "ownership.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        runner_meta = json.loads(runner_meta_path.read_text(encoding="utf-8"))
        if require_finished_run and not runner_meta.get("finished_at"):
            return None

        prior = runner_meta.get("workspace_cleanup")
        if isinstance(prior, dict) and prior.get("outcome") == "removed":
            prior_path = prior.get("path")
            if isinstance(prior_path, str) and not Path(prior_path).exists():
                return prior

        result = cleanup_workspace(task_dir, runner_meta)
        runner_meta["workspace_cleanup"] = result
        temporary = runner_meta_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(runner_meta, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, runner_meta_path)

        trace_path = task_dir / "trace.md"
        if not trace_path.exists():
            trace_path.write_text("# Trace\n\n", encoding="utf-8")
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        with trace_path.open("a", encoding="utf-8") as trace:
            trace.write(
                f"- {timestamp} {label} workspace cleanup: "
                f"{result.get('outcome')} ({result.get('reason')})"
                + (f" for {result['path']}." if result.get("path") else ".")
                + "\n"
            )
        return result


def _task_cgroup(unit: str) -> Path | None:
    shown = subprocess.run(
        ["systemctl", "show", "--property=ControlGroup", "--value", unit],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if shown.returncode == 0 and shown.stdout.strip():
        relative = shown.stdout.strip().lstrip("/")
        root = Path("/sys/fs/cgroup").resolve()
        candidate = (root / relative).resolve()
        if candidate != root and root in candidate.parents and candidate.name == unit:
            return candidate

    # The original watcher can still prove its exact scope without systemctl.
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) != 3 or fields[0] != "0" or fields[1] != "":
            continue
        relative = fields[2].lstrip("/")
        if Path(relative).name != unit:
            return None
        root = Path("/sys/fs/cgroup").resolve()
        candidate = (root / relative).resolve()
        if candidate != root and root in candidate.parents:
            return candidate
    return None


def _cgroup_pids(cgroup: Path) -> list[int]:
    values = (cgroup / "cgroup.procs").read_text(encoding="utf-8").split()
    return sorted({int(value) for value in values})


def _scope_is_collected(unit: str) -> bool:
    shown = subprocess.run(
        [
            "systemctl",
            "show",
            "--property=LoadState",
            "--property=ActiveState",
            "--value",
            unit,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    values = {line.strip() for line in shown.stdout.splitlines() if line.strip()}
    return shown.returncode == 0 and values == {"inactive", "not-found"}


def drain_task_scope(
    runner_meta: dict[str, Any], *, grace_seconds: float = 2.0
) -> dict[str, Any]:
    """Terminate every process in this task scope except the terminal watcher."""
    boundary = runner_meta.get("supervision_boundary", {})
    if boundary.get("mode") != "systemd_scope":
        return {"outcome": "not_applicable", "reason": "no_task_cgroup"}
    unit = boundary.get("unit")
    if not isinstance(unit, str) or not unit.endswith(".scope"):
        return {"outcome": "unverified", "reason": "scope_unit_missing"}
    cgroup = _task_cgroup(unit)
    if cgroup is None:
        if _scope_is_collected(unit):
            return {"outcome": "cleared", "reason": "scope_collected", "terminated_pids": []}
        return {"outcome": "unverified", "reason": "scope_identity_mismatch"}

    own_pid = os.getpid()
    try:
        peers = [pid for pid in _cgroup_pids(cgroup) if pid != own_pid]
        initial = peers.copy()
        for pid in peers:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while peers and time.monotonic() < deadline:
            time.sleep(0.05)
            peers = [pid for pid in _cgroup_pids(cgroup) if pid != own_pid]
        for pid in peers:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if peers:
            time.sleep(0.05)
        remaining = [pid for pid in _cgroup_pids(cgroup) if pid != own_pid]
    except (OSError, ValueError) as exc:
        return {"outcome": "unverified", "reason": "cgroup_read_failed", "detail": str(exc)}
    if remaining:
        return {
            "outcome": "not_empty",
            "reason": "processes_survived",
            "initial_pids": initial,
            "remaining_pids": remaining,
        }
    return {
        "outcome": "cleared",
        "reason": "empty_except_watcher",
        "terminated_pids": initial,
    }
